# -*- coding: utf-8 -*-
"""
谱面代打器 v4.0 —— 人味随机延迟版（模拟真人敲打）
==================================================
v4.0 新增（本次更新）：
  [人味抖动] 每个音符/和弦的按下时刻在可配置区间内随机偏移（正=晚、负=早），
             默认 -20ms ~ +60ms。不是逐音符独立白噪声：偏态三角分布(多数偏早)
             + 每 8 组一次的低频手速漂移，且和弦共用同一抖动（真人一个动作）。
  [严格区间] 抖动值被 clamp 在设定区间内，绝不越界；可选"补偿均值"扣掉区间中点，
             平均仍落在拍点上，个体随机 —— 看起来像人，分数不被整体拖偏。
  [真人失误] 可选偶发漏按概率(默认 0)，按住时长微抖(默认开，±15%)。
  [可观测]   GUI 实时显示实测抖动最小/最大/平均和漏按组数，便于调参。
  [时序安全] 抖动后的发键走 heapq 待发队列，按键顺序强制单调(最小 6ms 间隔)，
             不会因随机量把后面的音符发到前面；和弦仍是一次 SendInput。

以下保留 v2.5 原始说明：
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
import os, sys, json, time, threading, ctypes, queue, heapq, re, shutil, glob, random
from ctypes import wintypes
# PyQt5 提到模块顶层：确保 PyInstaller 静态分析能扫到（放函数内会漏打包导致 EXE 崩溃）
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QScrollArea, QTextEdit, QFileDialog, QComboBox,
    QMessageBox)
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

# ============ v4.0 人味抖动发生器 ============
class HumanJitter:
    """模拟真人按键时机：偏态三角分布 + 低频漂移 + 严格区间限制。
    纯逻辑、无系统副作用；三条独立随机流保证 jitter / 漏按 / hold 互不干扰。
    所有时间单位为毫秒，返回值 = 相对理论拍点的偏移（正=晚按）。"""
    def __init__(self, lo=-20.0, hi=60.0, compensate=True,
                 miss_pct=0.0, hold_var=True, seed=None, drift_every=8):
        self.enabled = True
        self.lo = float(lo); self.hi = float(hi)
        self.compensate = bool(compensate)
        self.miss_pct = float(miss_pct)
        self.hold_var = bool(hold_var)
        self.drift_every = max(1, int(drift_every))
        self.seed = seed
        self.reset()

    def reset(self, fresh=False):
        """fresh=True 用系统熵换种子（每首曲不同）；否则用固定 seed 便于复现。"""
        self.jr = random.Random(None if (fresh or self.seed is None) else self.seed)
        self.mr = random.Random(None if (fresh or self.seed is None) else self.seed + 101)
        self.hr = random.Random(None if (fresh or self.seed is None) else self.seed + 202)
        self._n = 0
        self._drift = 0.0

    def configure(self, lo, hi, compensate, miss_pct, hold_var):
        lo, hi = float(lo), float(hi)
        if hi < lo:
            lo, hi = hi, lo
        self.lo, self.hi = lo, hi
        self.compensate = bool(compensate)
        self.miss_pct = max(0.0, min(100.0, float(miss_pct)))
        self.hold_var = bool(hold_var)

    @property
    def center(self):
        # 偏态三角分布的数学期望 = (lo+hi+mode)/3，mode=lo+0.28*width。
        # 必须按期望补偿而不是区间中点，否则分布偏早会被补偿成整体偏早。
        return self.lo + (1.0 + 0.28) / 3.0 * (self.hi - self.lo)

    def next_jitter(self):
        """偏态三角(most likely 靠早端) + 每 drift_every 组一次低频漂移。
        clamp 保证原始抖动严格落在 [lo, hi]；补偿后落在 [lo-center, hi-center]。"""
        if not self.enabled:
            return 0.0
        width = self.hi - self.lo
        if width <= 0:
            v = self.lo
        else:
            mode = self.lo + 0.28 * width      # 真人多数略偏早，少数拖后
            v = self.jr.triangular(self.lo, self.hi, mode)
            if self._n % self.drift_every == 0:
                self._drift = self.jr.uniform(-1.0, 1.0) * width * 0.22
            v += self._drift
            v = max(self.lo, min(self.hi, v))  # 严格区间，绝不越界
        self._n += 1
        return v - self.center if self.compensate else v

    def should_miss(self):
        return self.enabled and self.miss_pct > 0 and self.mr.random() * 100.0 < self.miss_pct

    def hold_scale(self):
        return self.hr.uniform(0.85, 1.18) if (self.enabled and self.hold_var) else 1.0

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

def _win_title(h):
    n = user32.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, b, n + 1)
    return b.value

def _win_class(h):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, b, 256)
    return b.value

def list_game_windows():
    """枚举全部可见 SDL_app 窗口：[(hwnd, 标题, PID, 最小化)]"""
    rows = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        try:
            if user32.IsWindowVisible(h) and _win_class(h) == 'SDL_app':
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                rows.append((h, _win_title(h), pid.value, bool(user32.IsIconic(h))))
        except Exception:
            pass
        return True
    try:
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    except Exception:
        pass
    return rows

def focus_window(hwnd):
    """还原最小化 + 强制置前。SetForegroundWindow 从后台进程直接调用会被拒，
    必须 AttachThreadInput 到当前前台线程才肯放行。"""
    if not hwnd:
        return None
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE：最小化的游戏必须还原，否则收不到键
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if tid_fg and tid_fg != cur:
            user32.AttachThreadInput(cur, tid_fg, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        if tid_fg and tid_fg != cur:
            user32.AttachThreadInput(cur, tid_fg, False)
        return _win_title(hwnd) or ('hwnd %s' % hwnd)
    except Exception:
        return None

def focus_game(target_hwnd=None):
    """切到游戏窗口。给了 target_hwnd 就只认它；否则按标题猜一个。"""
    if target_hwnd:
        name = focus_window(target_hwnd)
        if name:
            return name
    rows = list_game_windows()
    if not rows:
        return None
    def score(r):
        h, t, pid, mini = r
        tl = (t or '').lower()
        bad = any(k in tl for k in ('osu', 'chrome', 'edge', 'notepad', 'code'))
        good = any(k in tl for k in ('novaflare', 'impostor', 'funkin', 'fnf', 'psych', 'kade'))
        return (0 if good else 1, 1 if bad else 0, 1 if mini else 0)
    rows.sort(key=score)
    for h, t, pid, mini in rows:
        name = focus_window(h)
        if name:
            return name
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
def diagnose_input(logq):
    """一键诊断（纯只读）：按键到底卡在哪一环 —— 前台窗口/权限/可见游戏窗口。"""
    import os
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    adv = ctypes.windll.advapi32

    def elev(pid):
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return '未知'
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):
            k32.CloseHandle(h)
            return '未知'
        val = wintypes.DWORD(); rl = wintypes.DWORD()
        adv.GetTokenInformation(tok, 20, ctypes.byref(val), ctypes.sizeof(val), ctypes.byref(rl))
        k32.CloseHandle(tok); k32.CloseHandle(h)
        return '管理员' if val.value else '普通'

    def title(h):
        n = user32.GetWindowTextLengthW(h)
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, b, n + 1)
        return b.value

    def cls(h):
        b = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, b, 256)
        return b.value

    logq.put('===== 输入诊断 =====')
    logq.put('本程序权限：%s（游戏若是管理员，本程序也必须是管理员，否则按键被 UIPI 丢弃）' % elev(os.getpid()))
    fg = user32.GetForegroundWindow()
    logq.put('当前前台窗口：%s [%s]' % (title(fg) or '(无标题)', cls(fg)))
    rows = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, l):
        try:
            if user32.IsWindowVisible(h) and cls(h) == 'SDL_app':
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                rows.append('  %s | PID %d | %s | 最小化=%s | 前台=%s' % (
                    title(h) or '(无标题)', pid.value, elev(pid.value),
                    bool(user32.IsIconic(h)), h == fg))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    except Exception:
        pass
    if rows:
        logq.put('SDL_app 游戏窗口 %d 个：' % len(rows))
        for r in rows:
            logq.put(r)
    else:
        logq.put('没找到 SDL_app 窗口 —— 游戏没开，或不是 SDL/Haxe 引擎')
    logq.put('判定顺序：① 前台=True 的那个才是能收键的窗口（不是就点一下游戏再按开始）'
             ' ② 权限必须同为管理员 ③ 键位框必须等于游戏内绑定 '
             '④ 游戏设置 Botplay 必须关（它会让引擎丢弃所有真实按键）')


SIDE_LABEL = {'player': '玩家（只按玩家轨/type）', 'opponent': '对手（自动开游戏 Play as Opponent）', 'all': '全部（玩家+对手）'}

# ============ v3.8 游戏存档 Play as Opponent 开关（按对手的真正前提） ============
# 引擎源码事实（NF PlayState.hx:4074 keyPressed 闸门 + 1782 归属公式）：
#   不开 playOpponent 时，真实按键永远只可能命中玩家侧音符（mustPress=True），
#   对手音符由引擎 CPU 自动打 —— 物理上按不出对手轨。代打器选「对手」时如果
#   游戏存档里 playOpponent=false，按再多键都是白按（表现为「不按/不认轨道」）。
# 存档：%APPDATA%/<公司>/<应用>/funkin.sol，Haxe 序列化，字段形如
#   y12:playOpponent 后紧跟 't'(真)/'f'(假)。只翻转这一个字节，其余原样保留。

_SOL_PO_RE = re.compile(rb'y12:playOpponent([tf])')

def find_play_opponent_sols():
    """全部含 playOpponent 字段的 funkin.sol，按修改时间新→旧排序。"""
    root = os.environ.get('APPDATA') or os.path.expanduser('~/AppData/Roaming')
    out = []
    for p in glob.glob(os.path.join(root, '*', '*', 'funkin.sol')):
        try:
            with open(p, 'rb') as f:
                if b'playOpponent' in f.read():
                    out.append((os.path.getmtime(p), p))
        except OSError:
            continue
    out.sort(reverse=True)
    return [p for _, p in out]

def read_play_opponent(path):
    """读单个存档的 playOpponent：True/False；解析不出返回 None。纯函数可单测。"""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return None
    return _read_po_bytes(data)

def _read_po_bytes(data):
    m = _SOL_PO_RE.search(data)
    if not m:
        return None
    return m.group(1) == b't'

def _default_backup_root():
    base = r'I:\_trash_ust_work\代打器sol备份'
    if os.path.isdir('I:\\'):
        return base
    return os.path.join(os.environ.get('TEMP', '.'), 'dadai_sol_backup')

def set_play_opponent(path, on=True, backup_dir=None):
    """翻转存档里的 playOpponent。返回 'flipped'/'already'/None(不认字段)。
    改前先把原文件完整备份到 backup_dir（按 I 盘归档铁律，默认 I:/_trash_ust_work）。"""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return None
    m = _SOL_PO_RE.search(data)
    if not m:
        return None
    want = b't' if on else b'f'
    if m.group(1) == want:
        return 'already'
    if backup_dir:
        os.makedirs(backup_dir, exist_ok=True)
        tag = os.path.basename(os.path.dirname(path))
        shutil.copy2(path, os.path.join(backup_dir, tag + '_funkin.sol'))
    new = _SOL_PO_RE.sub(b'y12:playOpponent' + want, data, count=1)
    tmp = path + '.tmp_dadai'
    with open(tmp, 'wb') as f:
        f.write(new)
    os.replace(tmp, path)
    return 'flipped'

def game_windows_open():
    """游戏是否正在运行（有可见 SDL_app 窗口）。"""
    try:
        return len(list_game_windows()) > 0
    except Exception:
        return False

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
        self.opp_hittable = 0
        self.opp_auto_only = 0
        self.last_f5 = self.last_f8 = self.last_up = self.last_down = False
        self.target_hwnd = None   # v3.7：GUI 选定的目标游戏窗口（None=自动挑）
        # v4.0 人味随机延迟
        self.human = HumanJitter(-20.0, 60.0, True, 0.0, True)
        self.human_devs = []       # 实测偏差统计（相对音符拍点，ms）
        self.human_missed = 0      # 主动漏按的组数

    def log(self, s):
        self.logq.put(s)

    def ensure_opponent_mode(self):
        """v3.8：对手/全部模式前置检查。存档 playOpponent=false 就自动打开。
        返回 (ok, 需要弹窗的提示行列表)。改存档前自动备份。"""
        msgs = []
        sols = find_play_opponent_sols()
        if not sols:
            return False, ['✗ 没找到 NF 系 funkin.sol（%APPDATA%/*/*/funkin.sol）。'
                           '请确认玩的是 NF 引擎，并到游戏 设置→游戏性 手动打开 Play as Opponent']
        target = sols[0]   # 最新写入的那份 = 正在玩的
        st = read_play_opponent(target)
        if st is True:
            msgs.append('✓ 游戏存档已是 Play as Opponent=开（%s）' % target)
            return True, msgs
        # v3.9：游戏运行时绝不写存档 —— 游戏退出时会把内存里的旧值 flush 回来，
        # 我们的改动被覆盖（用户实测：选过对手，下次进游戏还是关的）。
        if game_windows_open():
            msgs.append('✗ 游戏正在运行，现在写存档会被游戏退出时覆盖，而且本次也不会生效。')
            msgs.append('   正确顺序：完全退出 NF → 点这里选「对手」（自动写入并校验）→ 重新启动游戏。')
            msgs.append('   当前存档 Play as Opponent = 关：%s' % target)
            return False, msgs
        ts = time.strftime('%Y%m%d_%H%M%S')
        bk = os.path.join(_default_backup_root(), ts)
        try:
            r = set_play_opponent(target, True, bk)
        except OSError:
            r = set_play_opponent(target, True, None)   # I 盘不可用则不备份也改（保底）
            bk = None
        if r == 'flipped':
            # 回读校验：确认真写进去了
            if read_play_opponent(target) is not True:
                msgs.append('✗ 写入后回读校验失败，存档可能被占用：%s' % target)
                return False, msgs
            msgs.append('✓ 存档里 Play as Opponent 是关的——这就是按不到对手的根本原因。已写入并校验：')
            msgs.append('   %s' % target)
            msgs.append('   原件备份 → %s' % (bk if bk else target + '（同目录，I 盘不可用）'))
            msgs.append('→ 下一步：启动 NF → 选歌 → F5 即可按对手。')
            return True, msgs
        if r == 'already':
            msgs.append('✓ 存档已是 Play as Opponent=开（%s）' % target)
            return True, msgs
        msgs.append('✗ 存档 %s 不含 playOpponent 字段，无法自动开。请到游戏 设置→游戏性 手动打开 Play as Opponent' % target)
        return False, msgs

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
        # v3.3 修正：lane_count 必须是「每侧键数」，不是总轨道数。
        # duet 谱（0-3 一侧 / 4-7 另一侧）= 每侧 4 键，旧代码取 max(lanes)+1=8，
        # 会把对手轨 lane%8 映射成不存在的第 5-8 键 → 引擎「根本不认轨道」。
        try:
            declared = int(song.get('keyCount') or song.get('keys') or 4)
        except (TypeError, ValueError):
            declared = 4
        self.lane_count = detect_key_count(self.notes, declared)
        self.opp_hittable = self.opponent_cnt
        self.opp_auto_only = 0
        rec, kk = recommend_keys(self.lane_count)
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
        self.log("  分轨：" + ' '.join('轨%d(P%d/O%d)' % (l + 1, by_lane_p[l], by_lane_o[l]) for l in lanes))
        buckets = defaultdict(list)
        for n in self.notes:
            if is_player_note(n):
                buckets[round(n[0] / 2.0)].append(n)
        chords = [(k * 2, v) for k, v in buckets.items() if len(v) >= 2]
        max_sim = max((len(v) for _, v in chords), default=0)
        self.log("玩家侧和弦：%d 个，最大同时 %d 键" % (len(chords), max_sim))
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
            if self.target_side == 'opponent':
                # v3.8：按对手的硬前提 = 游戏 playOpponent 开。存档没开就自动开。
                ok, msgs = self.ensure_opponent_mode()
                for m in msgs:
                    self.log(m)
                if not ok:
                    self.log('✗ 已取消本次启动：先按上面提示把游戏设置好，再按 F5'); return
                if any(m.startswith('‼') for m in msgs):
                    self.log('✗ 已取消本次启动：请完全退出 NF 再重新进（让存档改动生效），再按 F5'); return
            fg = focus_game(self.target_hwnd)
            self.log("焦点 → " + (fg if fg else "切不到游戏窗口"))
            if not fg:
                self.log("✗ 没切到游戏窗口，本次不启动（否则按键会打进别的窗口，等于不按）。把游戏窗口调出来再按 F5。")
                return
            self.sched.release_all()
            self.running = True
            self.idx = 0
            self.pressed_count = 0
            self.dev_last = 0.0; self.dev_avg = 0.0; self.dev_cnt = 0
            self.human_devs = []; self.human_missed = 0
            self.human.reset(fresh=True)
            self.total_target = targets
            # v3.0：自检整段删除。F5 按下那一刻 = 时间零点，零等待直接开打。
            # （v2.8 的 1 秒自检把开头 1 秒白白浪费掉，开头音符必然错过）
            self.t0 = time.perf_counter()
            self.log("★ 立即开打：%s，%d 音符 (offset=%+dms, hold=%dms)，零自检" %
                     (SIDE_LABEL[self.target_side], targets, self.offset_ms, self.hold_ms))
            if self.target_side == 'opponent':
                self.log("对手轨按键 = %s（已与存档键位核对：D=左 F=下 J=上 K=右）。"
                         "游戏里的 Botplay 必须保持关（开着会丢弃真实按键）。" % ' '.join(self.keys))
            elif self.target_side == 'all':
                self.log("ℹ 全部模式：玩家轨 %d + 对手轨 %d，共 %d 键。" %
                         (self.player_cnt, self.opponent_cnt, targets))
            hm = self.human
            if hm.enabled:
                span = ('%.0f ~ %+.0f ms' % (hm.lo, hm.hi))
                self.log("人味延迟：开 · 区间 %s · 补偿均值=%s · 漏按=%.1f%% · 按住微抖=%s"
                         % (span, '是' if hm.compensate else '否', hm.miss_pct,
                            '是' if hm.hold_var else '否'))
            else:
                self.log("人味延迟：关（机器级精准打点）")
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
        """v4.0 人味版：按原拍点精确收集，抖动后的按键进待发队列按计划时刻发出。
        收集时机不变 → 音符不会丢；发键时刻随机 → 不再像机器。"""
        notes = self.notes
        n = len(notes)
        sched = self.sched
        human = self.human
        pending = []          # heap: (fire_at, seq, items, names, gt, jitter)
        pseq = 0
        last_fire = -1e9      # 单调约束：后一组绝不早于前一组 + 6ms
        while self.running and (self.idx < n or pending):
            now = (time.perf_counter() - self.t0) * 1000.0
            sched.update(now)                    # 到点释放（每次循环都查）

            # ---- v4.0：先发「抖动后到点」的按键 ----
            while pending and pending[0][0] <= now:
                fire_at, _, items, names, gt, jitter = heapq.heappop(pending)
                if not self.running:
                    break
                send_at = max(now, fire_at)
                sched.tap_multi(items, send_at)
                # 偏差还原到音符时间域：实际按下 + 全局提前量 - 音符时间
                self.dev_last = send_at + self.offset_ms - gt
                self.dev_cnt += 1
                self.dev_avg += (self.dev_last - self.dev_avg) / self.dev_cnt
                self.human_devs.append(self.dev_last)
                if len(self.human_devs) > 4000:
                    self.human_devs.pop(0)
                if self.pressed_count <= 6 or self.pressed_count % 25 == 0:
                    tag = ('[和弦 %d 键]' % len(items)) if len(items) > 1 else ''
                    self.log("  发键 %s @%.0fms 人味%+.0fms %s" %
                             (' '.join(names), gt, jitter, tag))
            if not self.running:
                break
            if self.idx >= n:
                if pending:
                    time.sleep(0.0005)
                    continue
                break

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
            groups = {}          # round(t,1) -> {key_idx: [lane, hold]}
            LATE_SKIP = 300.0    # 晚于判定窗口(300ms)的音符直接跳过，绝不补发乱按
            while self.idx < n and (now + self.offset_ms) >= notes[self.idx][0]:
                t, lane, sus, nt, mp = notes[self.idx]
                self.idx += 1
                if nt not in self.enabled_types:
                    continue
                if not self.should_press(notes[self.idx - 1]):
                    continue
                if now - t > LATE_SKIP:          # F5 按晚了/卡顿：跳过过期音符
                    continue
                # v3.5：引擎把 lane 归一化成 lane%键数，lane4-7 与 0-3 等价，不跳过。
                kper = self.lane_count if self.lane_count and self.lane_count <= len(self.keys) else len(self.keys)
                key_idx = lane % kper
                hold = sus if sus > 0 else self.hold_ms
                grp = groups.setdefault(round(t, 1), {})
                if key_idx in grp:
                    grp[key_idx][1] = max(grp[key_idx][1], hold)
                else:
                    grp[key_idx] = [lane, hold, nt]
            for gt in sorted(groups):
                if human.should_miss():          # v4.0 可选真人失误
                    self.human_missed += 1
                    continue
                items, names = [], []
                for key_idx, (lane, hold, nt) in groups[gt].items():
                    if human.hold_var:           # v4.0 按住时长微抖
                        hold = max(24.0, hold * human.hold_scale())
                    try:
                        items.append((VK[self.keys[key_idx]], hold))
                        names.append(self.keys[key_idx])
                        self.last_press = "轨%d→%s" % (lane + 1, self.keys[key_idx])
                    except Exception:
                        continue
                if not items:
                    continue
                # 和弦共用一次抖动（真人一个动作），不是每键各抖一次
                jitter = human.next_jitter()
                self.pressed_count += len(items)
                fire_at = now + jitter
                if fire_at < last_fire + 6.0:    # 单调：随机量不能打乱先后顺序
                    fire_at = last_fire + 6.0
                last_fire = fire_at
                pseq += 1
                heapq.heappush(pending, (fire_at, pseq, items, names, gt, jitter))
        if self.running:
            self.running = False
            self.sched.release_all()
            self.log("✓ 谱面打完（共按 %d 键，人味漏按 %d 组）" %
                     (self.pressed_count, self.human_missed))

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

    w = QWidget(); w.setWindowTitle("谱面代打器 v4.0 人味版 · F5 打 / F8 重来 / F5 停 / Esc 急停 / Ctrl+↑↓ 调延迟")
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
    side_cb.addItems(['玩家（只按玩家轨/type，默认）', '对手（只按对手轨；自动开游戏 Play as Opponent 开关）', '全部（玩家+对手）'])
    side_cb.setCurrentIndex(0)
    def apply_side():
        eng.target_side = ['player', 'opponent', 'all'][side_cb.currentIndex()]
        logq.put("代打目标 → " + SIDE_LABEL[eng.target_side]); refresh_target()
        if eng.target_side == 'opponent':
            # v3.8：选对手就立刻体检存档（这是之前按不到对手的根本原因）
            ok, msgs = eng.ensure_opponent_mode()
            for m in msgs:
                logq.put(m)
            if not ok or any(m.startswith('‼') for m in msgs):
                QMessageBox.warning(w, '对手模式前置检查',
                                    '按得到对手的前提 = 游戏「Play as Opponent」打开。\n\n'
                                    + '\n'.join(msgs)
                                    + '\n\n改完存档必须完全退出 NF 再重新进才会生效。')
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

    # ===== v4.0 人味随机延迟设置 =====
    rowhm = QHBoxLayout()
    hum_cb = QCheckBox("模拟真人随机延迟")
    hum_cb.setChecked(True)
    hlo = QLineEdit('%.0f' % eng.human.lo); hlo.setMaximumWidth(55)
    hhi = QLineEdit('%.0f' % eng.human.hi); hhi.setMaximumWidth(55)
    hcomp = QCheckBox("补偿均值"); hcomp.setChecked(True)
    hmiss = QLineEdit('%.1f' % eng.human.miss_pct); hmiss.setMaximumWidth(45)
    hhold = QCheckBox("按住微抖"); hhold.setChecked(True)
    def apply_human():
        try:
            lo = float(hlo.text().strip()); hi = float(hhi.text().strip())
            miss = float(hmiss.text().strip())
        except Exception:
            logq.put("人味参数需数字：下限/上限填毫秒，漏按填百分数")
            return
        if not (-200 <= lo <= 200 and -200 <= hi <= 200):
            logq.put("人味区间需在 -200 ~ +200 ms 内")
            return
        eng.human.enabled = hum_cb.isChecked()
        eng.human.configure(lo, hi, hcomp.isChecked(), miss, hhold.isChecked())
        logq.put("人味延迟 → %s · %+.0f ~ %+.0f ms · 补偿均值=%s · 漏按=%.1f%% · 按住微抖=%s" %
                 ('开' if eng.human.enabled else '关', eng.human.lo, eng.human.hi,
                  '是' if eng.human.compensate else '否', eng.human.miss_pct,
                  '是' if eng.human.hold_var else '否'))
    hum_cb.stateChanged.connect(lambda _s: apply_human())
    hcomp.stateChanged.connect(lambda _s: apply_human())
    hhold.stateChanged.connect(lambda _s: apply_human())
    bh = QPushButton("应用人味参数"); bh.clicked.connect(apply_human)
    rowhm.addWidget(hum_cb)
    rowhm.addWidget(QLabel("区间ms：")); rowhm.addWidget(hlo); rowhm.addWidget(QLabel("~")); rowhm.addWidget(hhi)
    rowhm.addWidget(hcomp)
    rowhm.addWidget(QLabel("漏按%：")); rowhm.addWidget(hmiss)
    rowhm.addWidget(hhold); rowhm.addWidget(bh)
    lay.addLayout(rowhm)
    lay.addWidget(QLabel("人味说明：默认 -20~+60ms，正=晚按、负=早按；补偿均值开启时平均仍落在拍点，"
                         "个体随机。和弦共用一次抖动。漏按 0 表示不故意漏。"))
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
    humanlabel = QLabel("人味抖动：—"); humanlabel.setStyleSheet("font-weight:bold;color:#a40;")
    lay.addWidget(offlabel); lay.addWidget(statlabel); lay.addWidget(tgtlabel); lay.addWidget(devlabel); lay.addWidget(humanlabel)

    def refresh_target():
        if eng.notes:
            tgtlabel.setText("当前将按：%d / %d 音符（%s）" %
                             (eng.targets_now(), len(eng.notes), SIDE_LABEL[eng.target_side]))
        else:
            tgtlabel.setText("当前将按：—")
    refresh_target()

    rowwin = QHBoxLayout()
    win_cb = QComboBox()

    def refresh_wins():
        win_cb.blockSignals(True)
        win_cb.clear()
        ws = list_game_windows()
        for h, t, pid, mini in ws:
            win_cb.addItem('%s [PID %d]%s' % (t or '无标题', pid, ' · 最小化' if mini else ''), h)
        if not ws:
            win_cb.addItem('（没找到游戏窗口，点刷新）', 0)
        win_cb.blockSignals(False)

    bw = QPushButton("刷新窗口"); bw.clicked.connect(refresh_wins)

    def apply_win():
        h = win_cb.currentData()
        eng.target_hwnd = h or None
        if h:
            logq.put("目标窗口 → %s" % win_cb.currentText())

    win_cb.currentIndexChanged.connect(lambda i: apply_win())
    rowwin.addWidget(QLabel("目标窗口：")); rowwin.addWidget(win_cb, 1); rowwin.addWidget(bw)
    lay.addLayout(rowwin)
    refresh_wins()

    rowrun = QHBoxLayout()
    bs = QPushButton("▶ 开始代打"); bs.clicked.connect(lambda: threading.Thread(target=eng.start, daemon=True).start())
    bst = QPushButton("■ 停止"); bst.clicked.connect(lambda: eng.stop("按钮停止"))
    bd = QPushButton("🔍 诊断输入")
    bd.clicked.connect(lambda: threading.Thread(target=diagnose_input, args=(logq,), daemon=True).start())
    rowrun.addWidget(bs); rowrun.addWidget(bst); rowrun.addWidget(bd)
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
        if eng.human_devs:
            ds = eng.human_devs
            humanlabel.setText("人味抖动：%s · 实测 %+.0f ~ %+.0f ms / 平均 %+.0f ms · %d 组 · 漏按 %d 组" %
                               ('开' if eng.human.enabled else '关', min(ds), max(ds),
                                sum(ds) / len(ds), len(ds), eng.human_missed))
        else:
            humanlabel.setText("人味抖动：%s · 区间 %+.0f ~ %+.0f ms（待开打实测）" %
                               ('开' if eng.human.enabled else '关',
                                eng.human.lo - (eng.human.center if eng.human.compensate else 0),
                                eng.human.hi - (eng.human.center if eng.human.compensate else 0)))
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

    # === v3.8 新增：存档 playOpponent 读取/翻转（临时文件，不碰真存档） ===
    fake = (b'oy6:mobileby12:playOpponentfy15:hideHitboxHintsfy8:flashi'
            b'ny6:ghostTappingt')
    assert _read_po_bytes(fake) is False, _read_po_bytes(fake)
    assert _read_po_bytes(b'oy12:playOpponentt') is True
    assert _read_po_bytes(b'oy8:nothinabcdefg') is None
    assert _read_po_bytes(b'') is None
    fd, solp = tempfile.mkstemp(suffix='.sol'); os.close(fd)
    bkd = tempfile.mkdtemp()
    try:
        with open(solp, 'wb') as f:
            f.write(fake)
        assert read_play_opponent(solp) is False
        assert set_play_opponent(solp, True, bkd) == 'flipped'
        assert read_play_opponent(solp) is True
        assert set_play_opponent(solp, True, bkd) == 'already'   # 幂等
        # 翻转只动那一个字节，其余内容原样
        with open(solp, 'rb') as f:
            after = f.read()
        assert after.startswith(b'oy6:mobileby12:playOpponentt'), after
        assert b'hideHitboxHintsfy8:flashiny6:ghostTappingt' in after, after
        # 备份 = 原始 false 版本
        bks = os.listdir(bkd)
        assert len(bks) == 1, bks
        with open(os.path.join(bkd, bks[0]), 'rb') as f:
            assert b'playOpponentf' in f.read()
        assert set_play_opponent(solp + '_not_exist.sol') is None
    finally:
        os.remove(solp)
        shutil.rmtree(bkd, ignore_errors=True)

    # === v2.6 新增：引擎/模组/谱面扫描器（造临时假引擎树验证） ===
    # tempfile/shutil：上方 v3.8 段已分别局部/模块级引入，勿重复 import（会触发 UnboundLocalError）
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
    globals()['focus_game'] = lambda *a, **k: '测试焦点'
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

    # === v4.0 新增：人味抖动确定性单测（纯函数，不依赖游戏） ===
    hj = HumanJitter(-20.0, 60.0, True, 0.0, True, seed=42)
    ctr = hj.center
    vals = [hj.next_jitter() for _ in range(3000)]
    assert min(vals) >= -20.0 - ctr - 1e-9 and max(vals) <= 60.0 - ctr + 1e-9, (min(vals), max(vals))
    assert abs(sum(vals) / len(vals)) < 4.0, sum(vals) / len(vals)   # 补偿后均值≈0
    hj2 = HumanJitter(-20.0, 60.0, False, 0.0, False, seed=42)
    vals2 = [hj2.next_jitter() for _ in range(3000)]
    assert min(vals2) >= -20.0 - 1e-9 and max(vals2) <= 60.0 + 1e-9, (min(vals2), max(vals2))
    assert sum(vals2) / len(vals2) < 20.0, sum(vals2) / len(vals2)   # 偏态：均值 < 区间中点
    hj2.reset(); assert [hj2.next_jitter() for _ in range(8)] == vals2[:8], '固定 seed 不可复现'
    hj3 = HumanJitter(0.0, 100.0, True, 25.0, True, seed=7)
    miss = sum(1 for _ in range(2000) if hj3.should_miss())
    assert 400 <= miss <= 600, miss                                    # 25% ±5%
    scales = [hj3.hold_scale() for _ in range(2000)]
    assert min(scales) >= 0.85 - 1e-9 and max(scales) <= 1.18 + 1e-9
    hj4 = HumanJitter(-20.0, 60.0, True, 0.0, False, seed=1)
    hj4.enabled = False
    assert hj4.next_jitter() == 0.0 and hj4.hold_scale() == 1.0
    egh = Engine(queue.Queue())
    assert hasattr(egh, 'human_devs') and hasattr(egh, 'human_missed')
    print("     - v4.0: 人味抖动 3000 组区间/均值/偏态/复现/漏按率/hold 微抖 全过")

    print("[OK] v3.0 自检：引擎公式分轨 + 和弦批量发送 + 和弦诊断 + 防抽搐 + 扫描")
    print("     - 分轨严格复刻引擎: mustPress = mustHitSection, lane>3 取反")
    print("       (v2.9 写死 lane4-7=对手，mustHit=False 段玩家 lane4-7 全漏 = 分不清根因)")
    print("     - 和弦: 同刻多键一次 SendInput 批量按下（v2.9 逐键间隔导致只吃到部分）")
    print("     - 载入谱面打印每轨玩家/对手归属 + 和弦数/最大同时键数/前5个和弦明细")
    print("     - 长条 hold 各自独立释放（同批和弦可不同 hold）")
    print("     - 三级联动：引擎 -> 模组 -> 谱面，异步扫描不卡界面")
    print("     - v4.0: 人味随机延迟（偏态+漂移+区间限制+和弦共享+单调发键+实测统计）")

if __name__ == '__main__':
    if '--selftest' in sys.argv: selftest()
    else: build_gui()
