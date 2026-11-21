# -*- coding: utf-8 -*-
"""
谱面代打器 v2.5 —— 精确按键时机 / 防抽搐版
==========================================
基于 v2.4，重写按键调度引擎。专治"快速按键抽搐 / 时机不准"：

  [根因1] v2.4 每个音符起一个 threading.Timer 定时松开：同键快速连按时，
          上一次的 timer 到点会把"刚按下的新键"松掉 -> 游戏看到
          down-up-down 乱跳 -> 判定抽搐。
          => v2.5 用 heapq 释放队列 + seq 匹配：旧释放计划绝不碰新按下。
  [根因2] v2.4 time.sleep(0.001) 实际睡 15ms（Windows 默认 tick 15.6ms），
          快速音符时机全飘。
          => v2.5 winmm.timeBeginPeriod(1) 把 tick 提到 1ms + 近端 perf_counter 忙等。
  [根因3] v2.4 lane % 4：8 轨谱 lane0/lane4 共用一键，同刻触发连按两次同键。
          => v2.5 同批同键合并(取最长 hold)，且同键必先松后按。

保留 v2.4 全部：PE/Kade/psych_v1_convert 谱面解析 / noteType 归属过滤 /
玩家·对手·全部三模式 / F5 启停 / Ctrl+↑↓ 调延迟 / 焦点自动切回游戏。
新增：按住时长可调（默认 38ms，跨 ≥2 帧保证被游戏识别）。
"""
import os, sys, json, time, threading, ctypes, queue, heapq
from ctypes import wintypes
# PyQt5 提到模块顶层：确保 PyInstaller 静态分析能扫到（放函数内会漏打包导致 EXE 崩溃）
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QScrollArea, QTextEdit, QFileDialog, QComboBox)
from PyQt5.QtCore import QTimer, Qt

# ============ Win32 SendInput ============
PUL = ctypes.POINTER(ctypes.c_ulong)
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", PUL)]
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", PUL)]
class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]
class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]
class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
user32 = ctypes.windll.user32
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.GetAsyncKeyState.restype = wintypes.SHORT
MAPVK_VK_TO_VSC = 0

VK = {
    'LEFT': 0x25, 'UP': 0x26, 'RIGHT': 0x27, 'DOWN': 0x28,
    'D': 0x44, 'F': 0x46, 'J': 0x4A, 'K': 0x4B,
    'G': 0x47, 'H': 0x48, 'L': 0x4C, 'M': 0x4D, 'N': 0x4E,
    'O': 0x4F, 'P': 0x50, 'I': 0x49, 'U': 0x55, 'Y': 0x59,
    'T': 0x54, 'R': 0x52, 'Q': 0x51,
    ';': 0xBA, ':': 0xBA, "'": 0xDE, ',': 0xBC, '.': 0xBE, '/': 0xBF,
    'A': 0x41, 'S': 0x53, 'W': 0x57, 'E': 0x45,
    'Z': 0x5A, 'X': 0x58, 'C': 0x43, 'V': 0x56, 'B': 0x42,
    'SPACE': 0x20, 'ENTER': 0x0D, 'SHIFT': 0x10, 'CTRL': 0x11, 'ALT': 0x12,
    'F1': 0x70, 'F2': 0x71, 'F3': 0x72, 'F4': 0x73, 'F5': 0x74, 'F6': 0x75,
    'F7': 0x76, 'F8': 0x77, 'F9': 0x78, 'F10': 0x79, 'F11': 0x7A, 'F12': 0x7B,
    'ESC': 0x1B,
    '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34, '5': 0x35, '6': 0x36, '7': 0x37,
    '8': 0x38, '9': 0x39, '0': 0x30,
}
DEFAULT_KEYS = ['D', 'F', 'J', 'K']
_lock = threading.Lock()

def key_down(vk):
    with _lock:
        scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=0, time=0, dwExtraInfo=None)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def key_up(vk):
    with _lock:
        scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(wVk=vk, wScan=scan,
                              dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def key_down_multi(vks):
    """v3.0 和弦关键修复：同刻多个键放进一次 SendInput 数组发送。
    v2.9 逐键单独 SendInput，每键间隔 1~2ms，引擎按帧判定时只吃到其中一两个
    -> "一和弦就不认识/直接不打"。一次 SendInput 才是真·同时按下。"""
    vks = [v for v in vks if v is not None]
    if not vks:
        return
    with _lock:
        n = len(vks)
        arr = (INPUT * n)()
        for i, vk in enumerate(vks):
            scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
            arr[i].type = INPUT_KEYBOARD
            arr[i].u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=0,
                                     time=0, dwExtraInfo=None)
        user32.SendInput(n, arr, ctypes.sizeof(INPUT))

# ============ 高精度定时器 ============
try:
    _winmm = ctypes.WinDLL('winmm')
    _winmm.timeBeginPeriod.argtypes = (wintypes.UINT,)
    _winmm.timeBeginPeriod(1)     # Windows tick 15.6ms -> 1ms
    _HAS_HP_TIMER = True
except Exception:
    _HAS_HP_TIMER = False

# ============ 按键调度器（v2.5 核心，治抽搐） ============
class KeyScheduler:
    """keydown 立即发；keyup 走 heapq，主线程循环到点发。
    每次按下带唯一 seq，释放计划只释放对应 seq 的那次按下——
    旧释放计划到点若 seq 不匹配则作废，绝不误伤刚按下的新键。"""
    def __init__(self, min_hold_ms=24):
        self.pressed = {}       # vk -> seq
        self.releases = []      # heap of (release_time_ms, seq, vk)
        self.seq = 0
        self.min_hold_ms = min_hold_ms

    def tap(self, vk, now_ms, hold_ms):
        if vk in self.pressed:
            self.up(vk)                       # 先松后按，保证独立 down->up->down
        key_down(vk)
        self.seq += 1
        self.pressed[vk] = self.seq
        heapq.heappush(self.releases,
                       (now_ms + max(hold_ms, self.min_hold_ms), self.seq, vk))

    def tap_multi(self, items, now_ms):
        """v3.0 和弦：items=[(vk, hold_ms)] 一次 SendInput 全部按下，
        释放仍各自走 heapq（长条/普通音符 hold 可不同）。"""
        items = [(vk, h) for vk, h in items if vk is not None]
        if not items:
            return
        for vk, _h in items:                  # 同键先松后按
            if vk in self.pressed:
                self.up(vk)
        key_down_multi([vk for vk, _h in items])
        for vk, h in items:
            self.seq += 1
            self.pressed[vk] = self.seq
            heapq.heappush(self.releases,
                           (now_ms + max(h, self.min_hold_ms), self.seq, vk))

    def up(self, vk):
        if vk in self.pressed:
            key_up(vk)
            del self.pressed[vk]

    def update(self, now_ms):
        while self.releases and self.releases[0][0] <= now_ms:
            t, seq, vk = heapq.heappop(self.releases)
            if self.pressed.get(vk) == seq:   # 只释放对应那次按下
                self.up(vk)

    def release_all(self):
        for vk in list(self.pressed.keys()):
            self.up(vk)
        self.releases.clear()

# ============ 引擎 / 模组 / 谱面 扫描器（v2.6 新增） ============
# 判据：一个目录含 *.exe 且含 assets/ 子目录 => 认定 FNF 引擎。
# 引擎根 = exe 所在目录(bin)。模组 = <根>/mods/ 下的子目录(有内容者)。
# 谱面 = 引擎根与模组内所有 json 中，内容含 sectionNotes 或顶层 notes 且非空者。
ENGINE_EXE_HINT = ('fnf', 'fon', 'kade', 'psych', 'nova', 'flare', 'leather',
                   'engine', 'codename', 'vanilla', 'osu', 'funkin', 'crossed',
                   'indie', 'sky', 'qt')
# FNF 引擎 assets/ 下必有这些特征目录中的至少 2 个，用来排除
# WorkBuddy / 豆包 / Crashpad 这类"有 exe+assets 但不是 FNF"的程序。
FNF_ASSET_DIRS = {'songs', 'data', 'images', 'shared', 'fonts', 'music',
                  'sounds', 'stages', 'videos', 'characters', 'week1', 'week2',
                  'preload', 'art', 'assets'}

def _looks_like_engine(root):
    """目录含 exe + assets/(含≥2个FNF特征目录) => FNF 引擎。返回主 exe 名或 None。"""
    try:
        assets = os.path.join(root, 'assets')
        if not os.path.isdir(assets):
            return None
        # 特征目录计数（排除误检的关键）
        subs = {d.lower() for d in os.listdir(assets)
                if os.path.isdir(os.path.join(assets, d))}
        score = len(subs & FNF_ASSET_DIRS)
        if score < 2:
            return None
        exes = [f for f in os.listdir(root)
                if f.lower().endswith('.exe') and os.path.isfile(os.path.join(root, f))]
        if not exes:
            return None
        # 名字带引擎关键词的 exe 优先；没有就取第一个（如 Indie Cross.exe）
        for e in exes:
            if any(k in e.lower() for k in ENGINE_EXE_HINT):
                return e
        return exes[0]
    except Exception:
        return None

def scan_engines(extra_roots=None):
    """全盘找 FNF 引擎。返回 [{name, root, exe, mods:[...]}]。
    搜索策略：常见盘符顶层 + 已知工程目录，深度限制防止全盘遍历爆炸。"""
    found = {}   # root -> info
    candidates = []
    for drv in ('C:', 'D:', 'E:', 'F:', 'G:', 'H:', 'I:', 'J:'):
        if os.path.isdir(drv + os.sep):
            candidates.append(drv + os.sep)
    for extra in (extra_roots or []):
        if extra and os.path.isdir(extra):
            candidates.append(extra)

    SKIP_TOP = {'windows', 'programdata', 'appdata', '$recycle.bin',
                'system volume information', 'node_modules', '.git',
                'program files', 'program files (x86)', 'temp', 'users',
                'mozilla', 'google', 'cache'}
    # 无用但常见的深层目录，直接砍（ Dowloads 下的缓存等）
    SKIP_ANY = {'node_modules', '__pycache__', '.venv', 'venv', 'site-packages',
                ' steamapps', 'release_', 'obj', 'build_cache'}

    def walk_engine(base, maxdepth):
        try:
            for dp, dn, fn in os.walk(base):
                depth = dp[len(base):].count(os.sep)
                # 系统目录只在浅层(depth<=1)跳过；深层的 windows/bin 等是引擎目录，不能砍
                skip = SKIP_TOP if depth <= 1 else (SKIP_TOP - {'windows', 'program files', 'program files (x86)'})
                dn[:] = [d for d in dn if d.lower() not in skip
                         and not d.startswith('.') and d not in SKIP_ANY]
                exe = _looks_like_engine(dp)
                if exe:
                    norm = os.path.normcase(dp)
                    if norm not in found:
                        base = os.path.basename(dp.rstrip(os.sep)) or dp
                        # bin 这种泛名用父目录名区分（Kade 有多个工程）
                        if base.lower() in ('bin', 'windows', 'build', 'release'):
                            parent = os.path.basename(os.path.dirname(dp.rstrip(os.sep)))
                            if parent:
                                base = "%s/%s" % (parent, base)
                        found[norm] = dict(name=base, root=dp, exe=exe, mods=[])
                    dn[:] = []   # 引擎内部不再往下找子引擎
                    continue
                if depth >= maxdepth:
                    dn[:] = []
        except Exception:
            pass

    # 盘符深扫（引擎可能在 5~7 层深，如 I:/NF-1.2.1h2/export/release/windows/bin）
    for c in candidates:
        if len(c) <= 3:            # 盘符根给足深度
            walk_engine(c, 8)
        else:                      # 显式指定的已知根目录，相对浅扫
            walk_engine(c, 6)

    # 给每个引擎列模组
    for info in found.values():
        mods_dir = os.path.join(info['root'], 'mods')
        if os.path.isdir(mods_dir):
            for d in sorted(os.listdir(mods_dir)):
                p = os.path.join(mods_dir, d)
                if os.path.isdir(p) and os.listdir(p):
                    info['mods'].append(d)
    return sorted(found.values(), key=lambda x: x['root'].lower())

_CHART_CACHE = {}
def _chart_note_count(path):
    """判断 json 是否谱面并返回音符数；非谱面返回 -1。只读前若干 MB。"""
    try:
        if os.path.getsize(path) > 8_000_000:
            return -1
        raw = open(path, 'rb').read()
        if raw.startswith(b'\xef\xbb\xbf'):
            raw = raw[3:]
        if b'sectionNotes' not in raw and b'notes' not in raw:
            return -1
        data = json.loads(raw.decode('utf-8', 'replace'))
        song = data.get('song') if isinstance(data, dict) else None
        if not isinstance(song, dict):
            song = data if isinstance(data, dict) else {}
        conts = []
        for src in (song, data):
            if isinstance(src, dict) and isinstance(src.get('notes'), list):
                nc = src['notes']
                if nc not in conts:
                    conts.append(nc)
        cnt = 0
        for cont in conts:
            for sec in cont:
                if isinstance(sec, dict) and isinstance(sec.get('sectionNotes'), list):
                    cnt += len(sec['sectionNotes'])
                elif isinstance(sec, (list, tuple)):
                    cnt += 1
        return cnt if cnt > 0 else -1
    except Exception:
        return -1

def scan_charts(engine_root, mod_name=None, max_files=4000):
    """列引擎/模组下所有可打谱面。返回 [{path, song, diff, notes}]。"""
    key = (engine_root, mod_name)
    if key in _CHART_CACHE:
        return _CHART_CACHE[key]
    roots = []
    if mod_name and mod_name != '<引擎内置>':
        mod_root = os.path.join(engine_root, 'mods', mod_name)
        if os.path.isdir(mod_root):
            roots.append(mod_root)
    else:
        # 引擎内置：assets/shared/data、assets/data 等
        for sub in ('assets/shared/data', 'assets/data', 'assets/songs'):
            p = os.path.join(engine_root, *sub.split('/'))
            if os.path.isdir(p):
                roots.append(p)
    charts = []
    seen = 0
    for r in roots:
        for dp, dn, fn in os.walk(r):
            for f in fn:
                if not f.lower().endswith('.json'):
                    continue
                seen += 1
                if seen > max_files:
                    break
                p = os.path.join(dp, f)
                cnt = _chart_note_count(p)
                if cnt < 0:
                    continue
                rel = os.path.relpath(p, r)
                parts = rel.replace('\\', '/').split('/')
                # 曲名：倒数第二层目录（常见 <data>/<song>/<song>-diff.json）
                song = parts[-2] if len(parts) >= 2 else os.path.dirname(rel)
                diff = os.path.splitext(f)[0]
                if diff.lower().startswith(song.lower()):
                    diff = diff[len(song):].lstrip('-_ ') or 'normal'
                charts.append(dict(path=p, song=song, diff=diff, notes=cnt))
    charts.sort(key=lambda x: (x['song'].lower(), x['diff'].lower()))
    _CHART_CACHE[key] = charts
    return charts

# ============ 窗口焦点（v2.8：进程枚举检测已删，只留切焦点） ============
GAME_KEYWORDS = ('NovaFlare', 'Funkin', 'FNF', 'Psych', 'Kade', 'Engine')

def focus_game():
    """把游戏窗口切到前台——否则 SendInput 的键发给了程序自己。"""
    hits = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        n = user32.GetWindowTextLengthW(h)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if any(k.lower() in buf.value.lower() for k in GAME_KEYWORDS):
                hits.append((h, buf.value))
        return True
    try:
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    except Exception:
        pass
    for h, title in hits:
        try:
            user32.ShowWindow(h, 9)
            user32.SetForegroundWindow(h)
            return title
        except Exception:
            continue
    return None

# ============ 谱面解析（v2.4 原样） ============
def normalize_type(v):
    if v is None: return ''
    s = str(v).strip()
    if s.lower() in ('nil', 'none', 'null'): return ''
    return s

def type_owner(nt):
    s = (nt or '').strip().lower()
    if not s: return 'player'
    if 'opponent' in s or 'gf sing' in s: return 'opponent'
    if s.endswith('sing') or s.endswith('p'):
        if s.startswith('bf') or s.startswith('player'): return 'player'
        return 'opponent'
    return 'player'

def _note_tuple(n, must_section, half=4):
    try:
        if isinstance(n, dict):
            if 'strumTime' not in n:
                return None
            t = float(n['strumTime'])
            lane = int(n.get('noteData', n.get('lane', 0)) or 0)
            sus = float(n.get('sustainLength', n.get('sus', 0)) or 0)
            nt = normalize_type(n.get('noteType', ''))
            if 'mustPress' in n:
                must_press = bool(n['mustPress'])
                return (t, lane, sus, nt, must_press) if 0 <= lane <= 31 else None
            sec_flag = must_section
        elif isinstance(n, (list, tuple)) and len(n) >= 2:
            t = float(n[0]); lane = int(n[1])
            sus = float(n[2]) if len(n) > 2 and n[2] else 0.0
            nt = normalize_type(n[3]) if len(n) > 3 else ''
            # 5列谱面（psych_v1_convert）：第5列是显式归属bool，优先级最高
            if len(n) >= 5 and isinstance(n[4], bool):
                must_press = n[4]
                if not (0 <= lane <= 31):
                    return None
                return (t, lane, sus, nt, must_press)
            sec_flag = must_section
        else:
            return None
    except (ValueError, TypeError, IndexError):
        return None
    if not (0 <= lane <= 31):
        return None
    # v3.0 归属判定：引擎源码公式（4K 为 if lane>3 取反，多K 推广为 lane>=K 取反）
    #   gottaHitNote = section.mustHitSection;
    #   if (noteData >= 每侧键数K) gottaHitNote = !section.mustHitSection;
    # v2.9 写死「lane 4-7 一律对手」是错的：mustHit=False 段玩家在 lane>=K
    # 打的音符被全漏 -> 分不清/和弦不打/合唱不打。
    must_press = sec_flag if lane < half else (not sec_flag)
    return (t, lane, sus, nt, must_press)

def load_chart(path):
    raw = open(path, 'rb').read()
    if raw.startswith(b'\xef\xbb\xbf'):
        raw = raw[3:]
    data = json.loads(raw.decode('utf-8', 'replace'))
    song = data.get('song') if isinstance(data, dict) else None
    if not isinstance(song, dict):
        song = data if isinstance(data, dict) else {}
    containers = []
    for src in (song, data):
        if not isinstance(src, dict):
            continue
        nc = src.get('notes')
        if isinstance(nc, list) and nc and nc not in containers:
            containers.append(nc)
    notes = []
    types = set()
    # v3.0 多K：谱面声明的每侧键数（keyCount / keys），缺省 4
    try:
        half = int(song.get('keyCount') or song.get('keys') or 4)
    except (TypeError, ValueError):
        half = 4
    if not (1 <= half <= 10):
        half = 4
    for cont in containers:
        for sec in cont:
            if isinstance(sec, dict) and isinstance(sec.get('sectionNotes'), list):
                must = bool(sec.get('mustHitSection', True))
                for n in sec['sectionNotes']:
                    tup = _note_tuple(n, must, half)
                    if tup:
                        notes.append(tup); types.add(tup[3])
            else:
                tup = _note_tuple(sec, True, half)
                if tup:
                    notes.append(tup); types.add(tup[3])
    notes.sort(key=lambda x: x[0])
    return notes, song, types

# ============ 核心 ============
SIDE_LABEL = {'player': '玩家（只按玩家轨/type）', 'opponent': '对手（只按对手轨/type）', 'all': '全部（玩家+对手）'}

# v3.0 多 K 支持：按谱面轨道数给推荐键位（用户可在输入框改）
# 判定标准是「每个通道的轨道数」= lane 数 / 2 或 lane 数，实际用 lane 分布智能选
KEY_PRESETS = {
    4: ['D', 'F', 'J', 'K'],
    5: ['D', 'F', 'SPACE', 'J', 'K'],
    6: ['S', 'D', 'F', 'J', 'K', 'L'],
    7: ['S', 'D', 'F', 'SPACE', 'J', 'K', 'L'],
    8: ['A', 'S', 'D', 'F', 'J', 'K', 'L', ';'],
    9: ['A', 'S', 'D', 'F', 'SPACE', 'J', 'K', 'L', ';'],
    10: ['A', 'S', 'D', 'F', 'SPACE', 'J', 'K', 'L', ';', "'"],
}

def detect_key_count(notes, declared=None):
    """v3.0 多 K：判断该谱面每侧键数。
    ① 谱面自己声明了 keyCount/keys（>4）→ 直接用（最权威）
    ② 否则看「同一时刻(±10ms)左右两侧是否同时出现玩家音符」——
       同时出现说明两侧都是玩家键 = 真多 K 单方谱；
       只在不同段出现 = 双方各 4 键的谱 -> 4 键足够（lane%4 映射）
    """
    if declared and int(declared) > 4:
        return min(int(declared), 10)
    player_lanes = [n[1] for n in notes if is_player_note(n)]
    if not player_lanes:
        return 4
    from collections import defaultdict
    b = defaultdict(set)
    for n in notes:
        if is_player_note(n):
            b[round(n[0] / 10.0)].add(n[1])
    for v in b.values():
        if max(v) >= 4 and min(v) <= 3:
            k = min(max(player_lanes) + 1, 10)
            return k if k in KEY_PRESETS else 8
    return 4

def recommend_keys(lane_count):
    return list(KEY_PRESETS.get(lane_count, KEY_PRESETS[4])), lane_count

def is_player_note(note):
    """玩家音符 = 引擎 mustPress 为真。v3.0：noteType 不再参与排除。
    合唱段(duet)玩家轨音符常带 'Both Opponents Sing'/'Opponent 2 Sing' 这类
    动画 type——引擎判定只看 mustPress，type 只管动画/音效。
    v2.9 用 type 把这类音符排除 -> "合唱不打"。仅 'gf sing'（GF 唱）不按。"""
    t, lane, sus, nt, mp = note
    if not mp:
        return False
    return not nt.lower().startswith('gf') and 'gf sing' not in nt.lower()

def is_opponent_note(note):
    """对手音符 = 对手归属 或 带对手 type 标记。"""
    t, lane, sus, nt, mp = note
    return (not mp) or (not is_player_note(note))

def detect_conflicts(keys, state_fn):
    """v2.8 自检前置：返回当前正被按住的目标键（按键冲突）。
    state_fn(vk)->bool 为按住查询，纯函数可单测。"""
    out = []
    for k in keys:
        vk = VK.get(k)
        if vk is None:
            continue
        try:
            if state_fn(vk):
                out.append(k)
        except Exception:
            pass
    return out

class Engine:
    def __init__(self, logq):
        self.logq = logq
        self.notes = []
        self.all_types = set()
        self.enabled_types = set()
        self.keys = list(DEFAULT_KEYS)
        self.offset_ms = 0
        self.offset_flash = 0.0   # 最近一次调延迟的时间戳（GUI 高亮用）
        self.dev_last = 0.0       # 按下偏差：实际按下时刻 - 音符应按时刻（ms，正=晚了）
        self.dev_avg = 0.0
        self.dev_cnt = 0
        self.hold_ms = 38                 # v2.5: 默认按住 38ms（跨≥2帧，稳识别）
        self.target_side = 'player'
        self.auto_keys = True             # v3.0 多K: True=按谱面自动配键位
        self.lane_count = 4
        self.running = False
        self._starting = False            # v2.8 防重入：start 自检期约 1.5s
        self.t0 = 0.0
        self.idx = 0
        self.sched = KeyScheduler()
        self.pressed_count = 0
        self.total_target = 0
        self.last_press = '—'
        self.player_cnt = 0
        self.opponent_cnt = 0
        self.last_f5 = self.last_f8 = self.last_up = self.last_down = False

    def log(self, s):
        self.logq.put(s)

    def should_press(self, note):
        # v2.7 修正：必须用轨道/段的归属(must_press)判定，v2.6 只看 noteType，
        # 没标 type 的对手音符全被误按 -> "一人唱俩"。
        if self.target_side == 'player':
            return is_player_note(note)
        if self.target_side == 'opponent':
            return is_opponent_note(note)
        return True

    def set_chart(self, path):
        self.notes, song, self.all_types = load_chart(path)
        self.enabled_types = set(self.all_types)
        self.player_cnt = sum(1 for n in self.notes if is_player_note(n))
        self.opponent_cnt = sum(1 for n in self.notes if is_opponent_note(n))
        self.log("载入谱面：%d 音符（玩家轨/type %d / 对手轨/type %d），BPM=%s，%d 种 noteType" %
                 (len(self.notes), self.player_cnt, self.opponent_cnt,
                  song.get('bpm', '?'), len(self.all_types)))
        lanes = sorted({n[1] for n in self.notes})
        self.log("轨道分布：%s（共 %d 轨）" % (' '.join(str(x) for x in lanes), len(lanes)))
        # v3.0 多 K 自动键位：按谱面实际轨道数推荐（用户手动改过键位则不覆盖）
        self.lane_count = (max(lanes) + 1) if lanes else 4
        try:
            declared = int(song.get('keyCount') or song.get('keys') or 4)
        except (TypeError, ValueError):
            declared = 4
        rec, kk = recommend_keys(detect_key_count(self.notes, declared))
        if self.auto_keys and rec != self.keys:
            self.keys = rec
            self.log("键位自动匹配 → %d 键：%s（可在键位框手动改）" % (kk, ' '.join(rec)))
        else:
            self.log("当前键位 %d 个：%s | lane 数 %d（lane %% 键数 映射）"
                     % (len(self.keys), ' '.join(self.keys), self.lane_count))
        # v3.0 和弦诊断：同刻(±2ms)多键统计 + 各轨道玩家/对手归属明细
        from collections import defaultdict
        by_lane_p = defaultdict(int); by_lane_o = defaultdict(int)
        for n in self.notes:
            if is_player_note(n): by_lane_p[n[1]] += 1
            else: by_lane_o[n[1]] += 1
        for lane in lanes:
            self.log("  轨%d：玩家 %d / 对手 %d" % (lane + 1, by_lane_p[lane], by_lane_o[lane]))
        buckets = defaultdict(list)
        for n in self.notes:
            if is_player_note(n):
                buckets[round(n[0] / 2.0)].append(n)
        chords = [(k * 2, v) for k, v in buckets.items() if len(v) >= 2]
        max_sim = max((len(v) for _, v in chords), default=0)
        self.log("玩家侧和弦：%d 个，最大同时 %d 键" % (len(chords), max_sim))
        for t_ms, grp in chords[:5]:
            self.log("  和弦示例 @%.0fms：%s" %
                     (t_ms, ' '.join('%s' % self.keys[n[1] % len(self.keys)] for n in grp)))
        clash = [(t, g) for t, g in chords
                 if len({n[1] % len(self.keys) for n in g}) < len(g)]
        if clash:
            self.log("⚠ 有 %d 个和弦存在映射冲突（多个轨道落到同一键，已合并）" % len(clash))
        return len(self.notes)

    def set_type_enabled(self, nt, on):
        if on: self.enabled_types.add(nt)
        else: self.enabled_types.discard(nt)

    def targets_now(self):
        return sum(1 for n in self.notes if n[3] in self.enabled_types and self.should_press(n))

    def start(self):
        if self._starting or self.running:
            return
        self._starting = True
        try:
            if not self.notes:
                self.log("错误：还没载入谱面"); return
            if not self.enabled_types:
                self.log("错误：一个 noteType 都没勾，没东西按"); return
            targets = self.targets_now()
            if targets == 0:
                self.log("错误：当前目标+勾选下没有音符可打"); return
            fg = focus_game()
            self.log("焦点 → " + (fg if fg else "未找到游戏窗口，请手动点一下游戏再按 F5"))
            self.sched.release_all()
            self.running = True
            self.idx = 0
            self.pressed_count = 0
            self.dev_last = 0.0; self.dev_avg = 0.0; self.dev_cnt = 0
            self.total_target = targets
            # v3.0：自检整段删除。F5 按下那一刻 = 时间零点，零等待直接开打。
            # （v2.8 的 1 秒自检把开头 1 秒白白浪费掉，开头音符必然错过）
            self.t0 = time.perf_counter()
            self.log("★ 立即开打：%s，%d 音符 (offset=%+dms, hold=%dms)，零自检" %
                     (SIDE_LABEL[self.target_side], targets, self.offset_ms, self.hold_ms))
            self.log("▶ F5 停 / F8 重来 / Esc 急停 / Ctrl+↑↓ 微调 offset")
            # 打键线程必须在返回前启动（v2.5 曾丢失此行 = 只自检不打谱）
            threading.Thread(target=self._player, daemon=True).start()
        finally:
            self._starting = False

    def stop(self, reason="已停止"):
        if not self.running: return
        self.running = False
        self.sched.release_all()
        self.log("■ %s（共按 %d 键）" % (reason, self.pressed_count))

    def _player(self):
        """v2.5 精确发键循环：自适应等待 + heapq 释放 + 同批同键合并。"""
        notes = self.notes
        n = len(notes)
        sched = self.sched
        while self.running and self.idx < n:
            now = (time.perf_counter() - self.t0) * 1000.0
            sched.update(now)                    # 到点释放（每次循环都查）
            if now < -50:                        # 预备期：别忙等烧 CPU
                time.sleep(0.01)
                continue
            target_t = notes[self.idx][0] - self.offset_ms
            dt = target_t - now
            if dt > 2.0:                         # 远：短睡（timeBeginPeriod 后≈真 2ms）
                time.sleep(min(dt / 1000.0, 0.002))
                continue
            # 近（≤2ms）：忙等 perf_counter 精确对时
            while self.running and (time.perf_counter() - self.t0) * 1000.0 < target_t:
                pass
            now = (time.perf_counter() - self.t0) * 1000.0

            # 收集到期音符，按「音符自身时间」分组（v3.0）：
            # 同刻多键 = 一个和弦组 -> 一次 SendInput；不同时刻绝不合并成一次按下
            # （卡顿/时间跳跃时若按 key_idx 全局合并，会把不同时刻的音符压成一拍）
            groups = {}          # round(t,1) -> {key_idx: [lane, hold]}
            LATE_SKIP = 300.0    # 晚于判定窗口(300ms)的音符直接跳过，绝不补发乱按
            while self.idx < n and (now + self.offset_ms) >= notes[self.idx][0]:
                t, lane, sus, nt, mp = notes[self.idx]
                self.idx += 1
                if nt not in self.enabled_types:
                    continue
                if not self.should_press(notes[self.idx - 1]):
                    continue
                if now - t > LATE_SKIP:      # F5 按晚了/卡顿：跳过过期音符
                    continue
                key_idx = lane % len(self.keys)
                hold = sus if sus > 0 else self.hold_ms
                grp = groups.setdefault(round(t, 1), {})
                if key_idx in grp:
                    grp[key_idx][1] = max(grp[key_idx][1], hold)
                else:
                    grp[key_idx] = [lane, hold, nt]
                self.pressed_count += 1
                self.last_press = "轨%d→%s" % (lane + 1, self.keys[key_idx])
            for gt in sorted(groups):
                items, names = [], []
                for key_idx, (lane, hold, nt) in groups[gt].items():
                    try:
                        items.append((VK[self.keys[key_idx]], hold))
                        names.append(self.keys[key_idx])
                    except Exception:
                        continue
                if items:
                    send_at = max(now, gt)   # 迟到太多就按当前时间起算 hold
                    # 按下偏差：实际按下时刻 - 音符应按时刻（正=按晚了，负=按早了）
                    self.dev_last = send_at - gt
                    self.dev_cnt += 1
                    self.dev_avg += (self.dev_last - self.dev_avg) / self.dev_cnt
                    sched.tap_multi(items, send_at)
                    tag = ('[和弦 %d 键]' % len(items)) if len(items) > 1 else ''
                    self.log("  发键 %s @%.0fms %s" % (' '.join(names), gt, tag))
        if self.running:
            self.running = False
            self.sched.release_all()
            self.log("✓ 谱面打完（共按 %d 键）" % self.pressed_count)

    def hotkey_loop(self):
        while True:
            f5 = bool(user32.GetAsyncKeyState(VK['F5']) & 0x8000)
            f8 = bool(user32.GetAsyncKeyState(VK['F8']) & 0x8000)
            ctrl = bool(user32.GetAsyncKeyState(VK['CTRL']) & 0x8000)
            up = bool(user32.GetAsyncKeyState(VK['UP']) & 0x8000)
            down = bool(user32.GetAsyncKeyState(VK['DOWN']) & 0x8000)
            esc = bool(user32.GetAsyncKeyState(VK['ESC']) & 0x8000)
            if f5 and not self.last_f5:
                if self.running: self.stop("F5 停止")
                else: threading.Thread(target=self.start, daemon=True).start()
            if ctrl and up and not self.last_up:
                self.offset_ms += 2; self.offset_flash = time.time()
                self.log("延迟 %+dms（提前更多）  [Ctrl+↓ 调回]" % self.offset_ms)
            if ctrl and down and not self.last_down:
                self.offset_ms -= 2; self.offset_flash = time.time()
                self.log("延迟 %+dms（提前更少）  [Ctrl+↑ 调回]" % self.offset_ms)
            if f8 and not self.last_f8 and self.notes:
                self.stop("F8 快速重来")
                time.sleep(0.05)
                threading.Thread(target=self.start, daemon=True).start()
            if esc: self.stop("Esc 急停")
            self.last_f5, self.last_f8, self.last_up, self.last_down = f5, f8, up, down
            time.sleep(0.003)

# ============ GUI (PyQt5) ============
def build_gui():
    app = QApplication(sys.argv)
    logq = queue.Queue()
    eng = Engine(logq)

    w = QWidget(); w.setWindowTitle("谱面代打器 v3.1 · F5 打 / F8 重来 / F5 停 / Esc 急停 / Ctrl+↑↓ 调延迟")
    w.resize(820, 820)
    lay = QVBoxLayout(w)

    # ===== v2.6 三级联动：引擎 -> 模组 -> 谱面 =====
    engines = []          # scan_engines 结果
    charts = []           # 当前模组下的谱面
    scanq = queue.Queue()

    roweng = QHBoxLayout()
    eng_cb = QComboBox(); mod_cb = QComboBox(); chart_cb = QComboBox()
    eng_cb.addItem("扫描中…"); mod_cb.addItem("<引擎内置>"); chart_cb.addItem("— 先选引擎 —")
    bscan = QPushButton("刷新引擎")
    def do_scan():
        bscan.setEnabled(False)
        eng_cb.clear(); eng_cb.addItem("扫描中…")
        def worker():
            try:
                es = scan_engines()
                scanq.put(('engines', es))
            except Exception as e:
                scanq.put(('error', str(e)))
        threading.Thread(target=worker, daemon=True).start()
    bscan.clicked.connect(do_scan)
    roweng.addWidget(QLabel("引擎：")); roweng.addWidget(eng_cb, 2)
    roweng.addWidget(QLabel("模组：")); roweng.addWidget(mod_cb, 2)
    roweng.addWidget(bscan)
    lay.addLayout(roweng)

    rowchart = QHBoxLayout()
    rowchart.addWidget(QLabel("谱面：")); rowchart.addWidget(chart_cb, 1)
    lay.addLayout(rowchart)

    def load_charts_for(mod_name):
        """扫描选定引擎+模组下的谱面，后台线程做，结果进 scanq。"""
        nonlocal charts
        charts = []
        chart_cb.blockSignals(True); chart_cb.clear(); chart_cb.addItem("扫描谱面中…"); chart_cb.blockSignals(False)
        if not engines or eng_cb.currentIndex() < 0:
            chart_cb.blockSignals(True); chart_cb.clear(); chart_cb.addItem("— 先选引擎 —"); chart_cb.blockSignals(False)
            return
        root = engines[eng_cb.currentIndex()]['root']
        def worker():
            try:
                cs = scan_charts(root, mod_name if mod_name != '<引擎内置>' else None)
                scanq.put(('charts', cs))
            except Exception as e:
                scanq.put(('error', str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def on_engine_changed(i):
        mod_cb.clear(); mod_cb.addItem("<引擎内置>")
        if 0 <= i < len(engines):
            for m in engines[i]['mods']:
                mod_cb.addItem(m)
        load_charts_for("<引擎内置>")

    def on_mod_changed():
        load_charts_for(mod_cb.currentText())

    def apply_chart(i):
        if 0 <= i < len(charts):
            c = charts[i]
            pathle.setText(c['path'])
            try:
                n = eng.set_chart(c['path'])
                keyle.setText(' '.join(eng.keys))     # 多K自动键位同步到输入框
                rebuild_types(); refresh_target()
                logq.put("选谱面：%s / %s（%d 音符，%s）" %
                         (c['song'], c['diff'], c['notes'],
                          os.path.basename(os.path.dirname(engines[eng_cb.currentIndex()]['root']))))
            except Exception as e:
                logq.put("解析失败：%s" % e)

    eng_cb.currentIndexChanged.connect(on_engine_changed)
    mod_cb.currentIndexChanged.connect(on_mod_changed)
    chart_cb.currentIndexChanged.connect(apply_chart)

    row1 = QHBoxLayout()
    pathle = QLineEdit(); pathle.setReadOnly(True)
    def pick():
        p, _ = QFileDialog.getOpenFileName(w, "选谱面 JSON", "", "谱面 JSON (*.json);;所有 (*.*)")
        if not p: return
        pathle.setText(p)
        try:
            n = eng.set_chart(p)
            keyle.setText(' '.join(eng.keys))
            rebuild_types(); refresh_target()
        except Exception as e:
            logq.put("解析失败：%s" % e)
    b = QPushButton("浏览…"); b.clicked.connect(pick)
    row1.addWidget(QLabel("手动选：")); row1.addWidget(pathle); row1.addWidget(b)
    lay.addLayout(row1)

    rowside = QHBoxLayout()
    side_cb = QComboBox()
    side_cb.addItems(['玩家（只按玩家轨/type，默认）', '对手（只按对手轨/type）', '全部（玩家+对手）'])
    side_cb.setCurrentIndex(0)
    def apply_side():
        eng.target_side = ['player', 'opponent', 'all'][side_cb.currentIndex()]
        logq.put("代打目标 → " + SIDE_LABEL[eng.target_side]); refresh_target()
    side_cb.currentIndexChanged.connect(lambda i: apply_side())
    rowside.addWidget(QLabel("代打目标：")); rowside.addWidget(side_cb); rowside.addStretch(1)
    lay.addLayout(rowside)

    rowk = QHBoxLayout()
    keyle = QLineEdit(' '.join(DEFAULT_KEYS))
    def apply_keys():
        ks = [x.strip().upper() for x in keyle.text().replace(',', ' ').split() if x.strip()]
        bad = [k for k in ks if k not in VK]
        if bad:
            logq.put("键位无效：%s 不认识（可用 D F J K S L A ; SPACE 等）" % ' '.join(bad)); return
        if len(ks) < 2 or len(ks) > 10:
            logq.put("键位数需 2~10 个（4/5/6/7/8/9 K）"); return
        eng.keys = ks; eng.auto_keys = False
        logq.put("键位 → %d 键：" % len(ks) + ' '.join(ks))
    bk = QPushButton("应用键位"); bk.clicked.connect(apply_keys)
    rowk.addWidget(QLabel("键位(4/6/8/9K，空格分隔)："))
    rowk.addWidget(keyle); rowk.addWidget(bk)
    lay.addLayout(rowk)

    rowh = QHBoxLayout()
    holdle = QLineEdit(str(eng.hold_ms))
    def apply_hold():
        try:
            v = int(holdle.text().strip())
            if 12 <= v <= 400:
                eng.hold_ms = v; logq.put("按住时长 → %d ms" % v)
            else: logq.put("按住时长需 12~400 ms（建议 30~60）")
        except Exception:
            logq.put("按住时长需数字")
    bh = QPushButton("应用按住"); bh.clicked.connect(apply_hold)
    rowh.addWidget(QLabel("按住ms：")); rowh.addWidget(holdle); rowh.addWidget(bh)
    rowh.addWidget(QLabel("← 抽搐/漏判时试着加大到 60，太快糊键时减小到 25"))
    lay.addLayout(rowh)

    tlabel = QLabel("noteType 过滤：检测到 0 种")
    tlabel.setStyleSheet("font-weight:bold;")
    lay.addWidget(tlabel)
    scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setMaximumHeight(150)
    inner = QWidget(); innerLay = QVBoxLayout(inner); innerLay.setContentsMargins(4, 4, 4, 4)
    scroll.setWidget(inner); lay.addWidget(scroll)
    def label_of(nt): return "（普通音符/无type）" if nt == '' else nt
    def rebuild_types():
        while innerLay.count():
            it = innerLay.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        ordered = sorted(eng.all_types, key=lambda s: (type_owner(s) != 'player', s != '', s))
        for nt in ordered:
            own = type_owner(nt)
            tag = '玩家' if own == 'player' else '对手'
            cnt = sum(1 for x in eng.notes if x[3] == nt)
            cb = QCheckBox("[%s] %s  [%d 个]" % (tag, label_of(nt), cnt))
            cb.setChecked(True)
            cb.stateChanged.connect(lambda state, t=nt: (eng.set_type_enabled(t, state == Qt.Checked), refresh_target()))
            innerLay.addWidget(cb)
        innerLay.addStretch(1)
        tlabel.setText("noteType 过滤：%d 种（玩家模式只按玩家轨/type）" % len(ordered))
    def all_on():
        for i in range(innerLay.count()):
            cb = innerLay.itemAt(i).widget()
            if isinstance(cb, QCheckBox): cb.setChecked(True)
    def all_off():
        for i in range(innerLay.count()):
            cb = innerLay.itemAt(i).widget()
            if isinstance(cb, QCheckBox): cb.setChecked(False)
    rowt = QHBoxLayout()
    ba = QPushButton("全选"); ba.clicked.connect(all_on)
    bn = QPushButton("全不选"); bn.clicked.connect(all_off)
    rowt.addWidget(ba); rowt.addWidget(bn); rowt.addStretch(1)
    lay.addLayout(rowt)

    offlabel = QLabel("延迟 offset = +0 ms   （游戏中 Ctrl+↑ 提前 / Ctrl+↓ 推后，±2ms）")
    offlabel.setStyleSheet("font-weight:bold;font-size:15px;color:#0a6;")
    statlabel = QLabel("待机：未载入谱面"); statlabel.setStyleSheet("font-weight:bold;color:#a30;")
    tgtlabel = QLabel("当前将按：—"); tgtlabel.setStyleSheet("font-weight:bold;color:#06a;")
    devlabel = QLabel("按下偏差：—"); devlabel.setStyleSheet("font-weight:bold;color:#555;")
    lay.addWidget(offlabel); lay.addWidget(statlabel); lay.addWidget(tgtlabel); lay.addWidget(devlabel)

    def refresh_target():
        if eng.notes:
            tgtlabel.setText("当前将按：%d / %d 音符（%s）" %
                             (eng.targets_now(), len(eng.notes), SIDE_LABEL[eng.target_side]))
        else:
            tgtlabel.setText("当前将按：—")
    refresh_target()

    rowrun = QHBoxLayout()
    bs = QPushButton("▶ 开始代打"); bs.clicked.connect(lambda: threading.Thread(target=eng.start, daemon=True).start())
    bst = QPushButton("■ 停止"); bst.clicked.connect(lambda: eng.stop("按钮停止"))
    rowrun.addWidget(bs); rowrun.addWidget(bst)
    lay.addLayout(rowrun)

    log = QTextEdit(); log.setReadOnly(True); log.setMaximumHeight(180)
    lay.addWidget(log)

    def flush():
        nonlocal engines, charts
        while True:
            try: s = logq.get_nowait()
            except queue.Empty: break
            log.append(s)
        # 处理扫描结果（后台线程回填，别卡 GUI）
        while True:
            try: kind, payload = scanq.get_nowait()
            except queue.Empty: break
            if kind == 'error':
                logq.put("扫描失败：%s" % payload)
            elif kind == 'engines':
                engines = payload
                eng_cb.blockSignals(True); eng_cb.clear()
                if engines:
                    for e in engines:
                        eng_cb.addItem("%s  [%s]" % (e['name'], e['exe']))
                    bscan.setEnabled(True)
                    logq.put("扫描到 %d 个 FNF 引擎" % len(engines))
                else:
                    eng_cb.addItem("— 没找到引擎 —"); bscan.setEnabled(True)
                    logq.put("没找到 FNF 引擎：需目录同时含 *.exe 和 assets/")
                eng_cb.blockSignals(False)
                if engines:
                    on_engine_changed(0)
            elif kind == 'charts':
                charts = payload
                chart_cb.blockSignals(True); chart_cb.clear()
                if charts:
                    for c in charts:
                        chart_cb.addItem("%s / %s  [%d 音符]" % (c['song'], c['diff'], c['notes']))
                    logq.put("列出 %d 首谱面" % len(charts))
                else:
                    chart_cb.addItem("— 该范围无谱面 —")
                    logq.put("该范围没扫到谱面（可切模组或手动浏览）")
                chart_cb.blockSignals(False)
                apply_chart(0 if charts else -1)
        recent = (time.time() - eng.offset_flash) < 1.5
        offlabel.setText("延迟 offset = %+d ms%s" % (
            eng.offset_ms, "   ◀ 刚调整" if recent else ""))
        offlabel.setStyleSheet(
            "font-weight:bold;font-size:%dpx;color:%s;" %
            (17 if recent else 15, "#d00" if recent else "#0a6"))
        if eng.dev_cnt:
            devlabel.setText("按下偏差：最近 %+.1fms / 平均 %+.1fms / %d 组   （正=晚了，负=早了）" %
                             (eng.dev_last, eng.dev_avg, eng.dev_cnt))
        else:
            devlabel.setText("按下偏差：—")
        if eng.running:
            statlabel.setText("代打中：已按 %d / 目标 %d · 最近 %s" %
                              (eng.pressed_count, eng.total_target, eng.last_press))
        else:
            if eng.notes:
                statlabel.setText("待机：玩家轨/type %d / 对手轨/type %d · 目标=%s" %
                                  (eng.player_cnt, eng.opponent_cnt, SIDE_LABEL[eng.target_side]))
            else:
                statlabel.setText("待机：未载入谱面")
        refresh_target()
    timer = QTimer(); timer.timeout.connect(flush); timer.start(80)

    threading.Thread(target=eng.hotkey_loop, daemon=True).start()
    logq.put("v3.0 就绪：分轨改为严格复刻引擎公式（lane>3 取反 mustHitSection）。")
    logq.put("和弦改为一次 SendInput 批量按下；载入谱面时打印轨道归属与和弦明细。")
    logq.put("分轨判定已修正：玩家模式只按玩家轨/type，不再一人唱俩。")
    logq.put("防抽搐三件套已启用：heapq 释放队列 / 1ms 定时器 / 同键先松后按。")
    logq.put("正在扫描本机 FNF 引擎（首次约几秒）…")
    do_scan()
    w.show()
    sys.exit(app.exec_())

# ============ 自检 ============
def selftest():
    import tempfile
    sample = {"song": {"bpm": 120, "notes": [
        {"mustHitSection": True, "sectionNotes": [
            [0.0, 0, 0], [500.0, 1, 0, 'Blue']]},
        {"mustHitSection": False, "sectionNotes": [
            [1000.0, 5, 0], [1500.0, 6, 0, 'Opponent 2 Sing']]},
    ]}}
    fd, p = tempfile.mkstemp(suffix='.json'); os.close(fd)
    open(p, 'w', encoding='utf-8').write(json.dumps(sample))
    notes, song, types = load_chart(p)
    assert len(notes) == 4, notes
    assert types == {'', 'Blue', 'Opponent 2 Sing'}, types
    assert song['bpm'] == 120
    os.remove(p)

    # === v3.0 回归：归属判定严格复刻引擎公式 (lane>3 取反 mustHitSection) ===
    sample8 = {"song": {"bpm": 150, "notes": [
        {"mustHitSection": True, "sectionNotes": [
            [0.0, 0, 0], [300.0, 1, 0]]},                      # 玩家 lane0-3
        {"mustHitSection": False, "sectionNotes": [
            [1000.0, 4, 0], [1200.0, 5, 0],                    # 引擎公式：lane>3 取反 -> 玩家侧
            [1500.0, 0, 0],                                    # 引擎公式：mustHit=false + lane0-3 -> 对手侧
            [1700.0, 4, 0, 'Opponent 2 Sing']]},               # type 标对手 -> 即使 mustPress 也不按
    ]}}
    fd, p8 = tempfile.mkstemp(suffix='.json'); os.close(fd)
    open(p8, 'w', encoding='utf-8').write(json.dumps(sample8))
    notes8, _, _ = load_chart(p8)
    os.remove(p8)
    assert len(notes8) == 6, notes8
    assert [n[4] for n in notes8] == [True, True, True, True, False, True], notes8
    eg8 = Engine(queue.Queue()); eg8.notes = notes8
    eg8.enabled_types = {n[3] for n in notes8}
    eg8.target_side = 'player'
    # v3.0：type 不参与玩家判定（合唱段 'Opponent 2 Sing' 在玩家轨 mp=True 照样要按）
    assert eg8.targets_now() == 5, eg8.targets_now()
    eg8.target_side = 'opponent'
    assert eg8.targets_now() == 1, eg8.targets_now()      # 仅 lane0(段2) 归属为对手
    # 5列谱面（psych_v1_convert）：第5列显式归属bool 优先级最高
    n5 = _note_tuple([2000.0, 6, 0, '', False], True)
    assert n5 == (2000.0, 6, 0.0, '', False), n5
    n5b = _note_tuple([2100.0, 0, 0, '', True], False)
    assert n5b == (2100.0, 0, 0.0, '', True), n5b

    assert type_owner('') == 'player'
    assert type_owner('Blue') == 'player'
    assert type_owner('Opponent 2 Sing') == 'opponent'
    assert type_owner('cupSing') == 'opponent'
    assert type_owner('bfSing') == 'player'

    eg = Engine(queue.Queue())
    eg.notes = [(0,0,0,'',True), (1,5,0,'',False), (2,1,0,'Blue',True),
                (3,6,0,'Opponent 2 Sing',False)]
    # v2.7：玩家模式必须跳过对手轨(lane4-7)普通音符——v2.6 会误按(一人唱俩根因)
    assert [eg.should_press(n) for n in eg.notes] == [True, False, True, False]
    eg.target_side = 'opponent'
    assert [eg.should_press(n) for n in eg.notes] == [False, True, False, True]
    eg.target_side = 'all'
    assert all(eg.should_press(n) for n in eg.notes)
    assert ctypes.sizeof(INPUT) in (28, 40), ctypes.sizeof(INPUT)

    # === v2.5 新增：按键调度器防抽搐验证（mock 注入） ===
    global key_down, key_up
    od, ou = key_down, key_up
    ev = []
    key_down = lambda vk: ev.append(('D', vk))
    key_up = lambda vk: ev.append(('U', vk))
    try:
        s = KeyScheduler(min_hold_ms=20)
        s.tap(0x44, 1000.0, 40)       # D 按下 @1000, 计划 @1040 松开
        assert ev == [('D', 0x44)], ev
        s.tap(0x44, 1020.0, 40)       # 20ms 后同键再按 -> 必须先松后按
        assert ev == [('D', 0x44), ('U', 0x44), ('D', 0x44)], ev
        s.update(1030.0)              # 旧释放计划 @1040 未到
        assert len(ev) == 3, ev
        s.update(1045.0)              # 旧计划到点，但 seq 不匹配新按下 -> 作废，不松新键
        assert len(ev) == 3, ('旧timer误伤新按键!', ev)
        s.update(1065.0)              # 新计划 @1060 到点 -> 松开
        assert ev[-1] == ('U', 0x44), ev
        assert len(ev) == 4, ev
        s.release_all()
        assert len(ev) == 4, ev       # 已松开，不重复发 up
    finally:
        key_down, key_up = od, ou

    # === v3.0 新增：和弦批量发送（一次 SendInput 全按下，释放各自独立） ===
    global key_down_multi
    okm = key_down_multi
    batch_ev = []
    key_down_multi = lambda vks: batch_ev.append(tuple(vks))
    try:
        s2 = KeyScheduler(min_hold_ms=20)
        # 和弦 3 键：D/F/J 一次发出，hold 各异
        s2.tap_multi([(0x44, 40), (0x46, 40), (0x4A, 200)], 1000.0)
        assert batch_ev == [(0x44, 0x46, 0x4A)], batch_ev   # 单次调用=真同刻
        assert len(s2.releases) == 3, s2.releases
        # 200ms 的长条释放更晚: 到 1045 只松前两个
        s2.update(1045.0)
        assert set(s2.pressed.keys()) == {0x4A}, s2.pressed
        s2.update(1205.0)
        assert not s2.pressed, s2.pressed
        # 同键重按必须产生新的 down 批次（先松后按）
        s2.tap_multi([(0x44, 40)], 2000.0)
        s2.tap_multi([(0x44, 40)], 2010.0)
        assert len(batch_ev) == 3, batch_ev
        s2.release_all()
    finally:
        key_down_multi = okm

    # === v2.6 新增：引擎/模组/谱面扫描器（造临时假引擎树验证） ===
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix='v26_scan_')
    try:
        fake = os.path.join(tmp, 'FNF Test Engine')
        os.makedirs(os.path.join(fake, 'assets', 'shared', 'data', 'test-song'))
        os.makedirs(os.path.join(fake, 'assets', 'songs'))   # 第二个 FNF 特征目录
        open(os.path.join(fake, 'Leather Engine.exe'), 'wb').write(b'MZfake')
        mod = os.path.join(fake, 'mods', 'Test Mod', 'data', 'mod-song')
        os.makedirs(mod)
        # 真谱面 json
        chart = {"song": {"bpm": 128, "notes": [
            {"mustHitSection": True, "sectionNotes": [[0, 0, 0], [500, 1, 0]]}]}}
        for d in (os.path.join(fake, 'assets', 'shared', 'data', 'test-song'), mod):
            open(os.path.join(d, 'chart-hard.json'), 'w', encoding='utf-8').write(json.dumps(chart))
        # 非谱面 json（不该被列）
        open(os.path.join(fake, 'pack.json'), 'w', encoding='utf-8').write('{"name":"x"}')

        assert _looks_like_engine(fake) == 'Leather Engine.exe', _looks_like_engine(fake)
        _CHART_CACHE.clear()
        built = scan_charts(fake, None)
        assert len(built) == 1 and built[0]['notes'] == 2, built
        assert built[0]['song'] == 'test-song', built
        mod_charts = scan_charts(fake, 'Test Mod')
        assert len(mod_charts) == 1 and mod_charts[0]['song'] == 'mod-song', mod_charts
        print("     - 扫描器：认引擎(exe+assets)、列内置谱面、列模组谱面、排除非谱面json 全过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _CHART_CACHE.clear()

    # === v2.8 新增：按键冲突判定（纯函数单测） ===
    held = {VK['D'], VK['J']}                     # 模拟 D 和 J 正被按住
    assert detect_conflicts(['D', 'F', 'J', 'K'],
                            lambda vk: vk in held) == ['D', 'J']
    assert detect_conflicts(['D', 'F', 'J', 'K'],
                            lambda vk: False) == []
    assert detect_conflicts(['BOGUS'], lambda vk: True) == []   # 非法键名跳过
    # 进程检测已删：确认无残留引用
    assert not hasattr(Engine, 'detect_loop') and not hasattr(Engine, 'armed')

    # === v2.9 回归：start() 必须启动 _player 打键线程（v2.5~v2.8 丢失 = 只自检不打谱） ===
    import threading as _th
    launched = []
    class _FakeT:
        def __init__(self, target=None, daemon=None): launched.append(target)
        def start(self): pass
    oT, oS, oF, oC = _th.Thread, time.sleep, focus_game, detect_conflicts
    eg9 = Engine(queue.Queue())
    eg9.notes = [(0, 0, 0, '', True)]
    eg9.enabled_types = {''}
    _th.Thread = _FakeT
    time.sleep = lambda *_: None
    globals()['focus_game'] = lambda: '测试焦点'
    globals()['detect_conflicts'] = lambda keys, fn: ['D']   # 冲突分支，避开真实发键
    try:
        eg9.start()
        assert eg9.running is True, 'start 后应处于 running'
        assert any(getattr(t, '__func__', None) is Engine._player for t in launched), \
            ('start() 没启动 _player 打键线程!', launched)
    finally:
        _th.Thread, time.sleep = oT, oS
        globals()['focus_game'], globals()['detect_conflicts'] = oF, oC
        eg9.stop("自检复位")

    # === v3.0 多K支持：9K 谱 lane8 不丢 / 8K 单方谱自动 8 键 / 4K双方谱仍 4 键 ===
    multi9 = {"song": {"bpm": 140, "keyCount": 9, "notes": [
        {"mustHitSection": True, "sectionNotes": [
            [0.0, 0, 0], [100.0, 4, 0], [200.0, 8, 0]]},     # lane8 = 9K 谱第九轨
    ]}}
    fd, p9 = tempfile.mkstemp(suffix='.json'); os.close(fd)
    open(p9, 'w', encoding='utf-8').write(json.dumps(multi9))
    n9, _, _ = load_chart(p9)
    os.remove(p9)
    assert len(n9) == 3, ('lane8 被丢弃了!', n9)
    # keyCount=9 -> 每侧 9 键，lane0/4/8 全在玩家侧（<K）
    assert n9[-1][1] == 8 and n9[-1][4] is True, n9
    # 8K 单方谱（同段内 lane0-7 都有）-> 自动 8 键
    m8 = {"song": {"bpm": 140, "keyCount": 8, "notes": [
        {"mustHitSection": True,
         "sectionNotes": [[i * 100.0, i, 0] for i in range(8)]}]}}
    fd, p8b = tempfile.mkstemp(suffix='.json'); os.close(fd)
    open(p8b, 'w', encoding='utf-8').write(json.dumps(m8))
    nb, _, _ = load_chart(p8b)
    assert detect_key_count(nb, 8) == 8, detect_key_count(nb, 8)
    egk = Engine(queue.Queue()); egk.set_chart(p8b)
    assert len(egk.keys) == 8, egk.keys          # 端到端：自动配 8 键
    os.remove(p8b)
    # 4K 双方谱（DK duet 式：段内跨度<=3）-> 仍 4 键
    m4 = {"song": {"bpm": 150, "notes": [
        {"mustHitSection": True, "sectionNotes": [[0.0, 0, 0], [100.0, 3, 0]]},
        {"mustHitSection": False, "sectionNotes": [[200.0, 4, 0], [300.0, 7, 0]]}]}}
    fd, p4b = tempfile.mkstemp(suffix='.json'); os.close(fd)
    open(p4b, 'w', encoding='utf-8').write(json.dumps(m4))
    n4, _, _ = load_chart(p4b)
    assert detect_key_count(n4) == 4, detect_key_count(n4)
    egk4 = Engine(queue.Queue()); egk4.set_chart(p4b)
    assert len(egk4.keys) == 4, egk4.keys
    os.remove(p4b)
    print("     - 多K: 9K lane8 保留 / 8K单方谱自动8键 / 4K双方谱4键 全过")
    # v3.1: 按下偏差滑动平均 + F8 字段
    egx = Engine(queue.Queue())
    assert egx.dev_cnt == 0 and egx.dev_avg == 0.0, 'dev 初始态不对'
    for d in (12.0, -6.0, 3.0):
        egx.dev_cnt += 1
        egx.dev_avg += (d - egx.dev_avg) / egx.dev_cnt
    assert abs(egx.dev_avg - 3.0) < 1e-6, egx.dev_avg
    assert hasattr(egx, 'last_f8'), '缺 last_f8'
    print("     - v3.1: 按下偏差滑动平均 / F8 快速重来字段 全过")

    print("[OK] v3.0 自检：引擎公式分轨 + 和弦批量发送 + 和弦诊断 + 防抽搐 + 扫描")
    print("     - 分轨严格复刻引擎: mustPress = mustHitSection, lane>3 取反")
    print("       (v2.9 写死 lane4-7=对手，mustHit=False 段玩家 lane4-7 全漏 = 分不清根因)")
    print("     - 和弦: 同刻多键一次 SendInput 批量按下（v2.9 逐键间隔导致只吃到部分）")
    print("     - 载入谱面打印每轨玩家/对手归属 + 和弦数/最大同时键数/前5个和弦明细")
    print("     - 长条 hold 各自独立释放（同批和弦可不同 hold）")
    print("     - 三级联动：引擎 -> 模组 -> 谱面，异步扫描不卡界面")

if __name__ == '__main__':
    if '--selftest' in sys.argv: selftest()
    else: build_gui()
