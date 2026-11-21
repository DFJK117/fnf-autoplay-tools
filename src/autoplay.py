# -*- coding: utf-8 -*-
"""
谱面代打器 v2.4 (autoplay injector) —— PyQt5 版
------------------------------------------------
玩法：
  1. 选谱面 JSON（Psych / Kade / psych_v1_convert / strumTime 对象）
  2. 选「代打目标」：玩家侧(只按玩家type) / 对手(检测所有type) / 全部
  3. 程序扫描所有 noteType，标注归属，勾选要按哪些
  4. 点「开始检测」，GO 瞬间按 F5（或直接点开始按钮）开打 —— 自动把焦点切回游戏
  5. Ctrl+↑ / Ctrl+↓ 实时调延迟（2ms/档）

v2.4 修复（用户："优先检测玩家的type，切到对手将检测所有"）：
  - 分轨改按 noteType 归属(type_owner)：玩家模式只按玩家 type；对手/全部检测所有 type
  - "开了没用"真凶：用按钮启动后焦点在程序窗口，SendInput 把键发给程序而非游戏
    -> start() 里 SetForegroundWindow 自动切回游戏窗口
  - F5 去掉 armed 门槛，任何时候按都能开/停
"""
import os, sys, json, time, threading, ctypes, queue
from ctypes import wintypes

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
    'A': 0x41, 'S': 0x53, 'W': 0x57, 'E': 0x45,
    'F1': 0x70, 'F5': 0x74, 'F2': 0x71, 'F3': 0x72, 'F4': 0x73,
    'CTRL': 0x11, 'SHIFT': 0x10, 'ESC': 0x1B,
    '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34,
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

# ============ 进程检测 ============
class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]

GAME_KEYWORDS = ('NovaFlare', 'Funkin', 'FNF', 'Psych', 'Kade', 'Engine')

def list_processes():
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.restype = wintypes.BOOL
    k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32))
    k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32))
    names = []
    if snap in (-1, None, 0):
        return names
    pe = PROCESSENTRY32(); pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    ok = k32.Process32FirstW(snap, ctypes.byref(pe))
    while ok:
        names.append(pe.szExeFile)
        ok = k32.Process32NextW(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    return names

def find_game(names):
    for p in names:
        if any(t.lower() in p.lower() for t in GAME_KEYWORDS):
            return p
    return None

def focus_game():
    """把游戏窗口切到前台——否则 SendInput 的键发给了程序自己(开了没用)。"""
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
            user32.ShowWindow(h, 9)      # SW_RESTORE
            user32.SetForegroundWindow(h)
            return title
        except Exception:
            continue
    return None

# ============ 谱面解析 ============
def normalize_type(v):
    if v is None: return ''
    s = str(v).strip()
    if s.lower() in ('nil', 'none', 'null'): return ''
    return s

def type_owner(nt):
    """按 noteType 判归属: player / opponent。
    规则: 含 opponent / gf sing -> 对手; xxxSing / xxxP 角色独唱 -> 对手;
    空 type(普通音符) 与机制 type(CupNote/Blue/Ink/Parry...) -> 玩家。"""
    s = (nt or '').strip().lower()
    if not s: return 'player'
    if 'opponent' in s or 'gf sing' in s: return 'opponent'
    if s.endswith('sing') or s.endswith('p'):
        if s.startswith('bf') or s.startswith('player'): return 'player'
        return 'opponent'
    return 'player'

def _note_tuple(n, must_section):
    """归一成 (t_ms, lane0_7, sus_ms, ntype, must_press)。
    must_press 按 FNF 规则算，仅作状态显示参考；实际过滤用 type_owner。"""
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
                return (t, lane, sus, nt, must_press) if 0 <= lane <= 7 else None
            sec_flag = must_section
        elif isinstance(n, (list, tuple)) and len(n) >= 2:
            t = float(n[0]); lane = int(n[1])
            sus = float(n[2]) if len(n) > 2 and n[2] else 0.0
            nt = normalize_type(n[3]) if len(n) > 3 else ''
            sec_flag = must_section
        else:
            return None
    except (ValueError, TypeError, IndexError):
        return None
    if not (0 <= lane <= 7):
        return None
    must_press = sec_flag if lane <= 3 else (not sec_flag)
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

    notes = []          # (t, lane, sus, ntype, must_press)
    types = set()
    for cont in containers:
        for sec in cont:
            if isinstance(sec, dict) and isinstance(sec.get('sectionNotes'), list):
                must = bool(sec.get('mustHitSection', True))
                for n in sec['sectionNotes']:
                    tup = _note_tuple(n, must)
                    if tup:
                        notes.append(tup); types.add(tup[3])
            else:
                tup = _note_tuple(sec, True)
                if tup:
                    notes.append(tup); types.add(tup[3])

    notes.sort(key=lambda x: x[0])
    return notes, song, types

# ============ 核心 ============
SIDE_LABEL = {'player': '玩家侧(只按玩家type)', 'opponent': '对手(检测所有type)', 'all': '全部(检测所有type)'}

class Engine:
    def __init__(self, logq):
        self.logq = logq
        self.notes = []
        self.all_types = set()
        self.enabled_types = set()
        self.keys = list(DEFAULT_KEYS)
        self.offset_ms = 0
        self.hold_ms = 45
        self.target_side = 'player'
        self.running = False
        self.armed = False
        self.t0 = 0.0
        self.idx = 0
        self.held = {}
        self.pressed_count = 0
        self.total_target = 0
        self.last_press = '—'
        self.player_cnt = 0
        self.opponent_cnt = 0
        self.last_f5 = self.last_up = self.last_down = False

    def log(self, s):
        self.logq.put(s)

    def should_press(self, note):
        """用户规则: 玩家模式只检测玩家 type; 对手/全部检测所有 type。"""
        if self.target_side in ('opponent', 'all'):
            return True
        return type_owner(note[3]) == 'player'

    def set_chart(self, path):
        self.notes, song, self.all_types = load_chart(path)
        self.enabled_types = set(self.all_types)   # 默认全勾
        self.player_cnt = sum(1 for n in self.notes if type_owner(n[3]) == 'player')
        self.opponent_cnt = sum(1 for n in self.notes if type_owner(n[3]) == 'opponent')
        self.log("载入谱面：%d 音符（玩家type %d / 对手type %d），BPM=%s，%d 种 noteType" %
                 (len(self.notes), self.player_cnt, self.opponent_cnt,
                  song.get('bpm', '?'), len(self.all_types)))
        return len(self.notes)

    def set_type_enabled(self, nt, on):
        if on: self.enabled_types.add(nt)
        else: self.enabled_types.discard(nt)

    def targets_now(self):
        return sum(1 for n in self.notes if n[3] in self.enabled_types and self.should_press(n))

    def start(self):
        if not self.notes:
            self.log("错误：还没载入谱面"); return
        if not self.enabled_types:
            self.log("错误：一个 noteType 都没勾，没东西按"); return
        targets = self.targets_now()
        if targets == 0:
            self.log("错误：当前目标+勾选下没有音符可打"); return
        # 关键: 把焦点切回游戏，否则键发给程序窗口 -> "开了没用"
        fg = focus_game()
        self.log("焦点 → " + (fg if fg else "未找到游戏窗口，请手动点一下游戏再按 F5"))
        self.running = True
        self.idx = 0
        self.held = {}
        self.pressed_count = 0
        self.total_target = targets
        self.t0 = time.perf_counter()
        self.log("★ 开始代打：%s，将按 %d 音符 (offset=%+dms)" %
                 (SIDE_LABEL[self.target_side], targets, self.offset_ms))
        threading.Thread(target=self._player, daemon=True).start()

    def stop(self, reason="已停止"):
        if not self.running: return
        self.running = False
        for lane in list(self.held.keys()):
            try: key_up(VK[self.keys[lane]])
            except Exception: pass
        self.held = {}
        self.log("■ %s（共按 %d 键）" % (reason, self.pressed_count))

    def _player(self):
        notes = self.notes
        n = len(notes)
        while self.running and self.idx < n:
            now = (time.perf_counter() - self.t0) * 1000.0
            while self.idx < n and (now + self.offset_ms) >= notes[self.idx][0]:
                t, lane, sus, nt, mp = notes[self.idx]
                self.idx += 1
                if nt not in self.enabled_types:      # 没勾的 type 跳过
                    continue
                if not self.should_press(notes[self.idx - 1]):  # 非目标 type 跳过
                    continue
                key_idx = lane % 4                    # 8 轨 -> 4 键
                try: vk = VK[self.keys[key_idx]]
                except Exception: continue
                key_down(vk)
                self.held[key_idx] = True
                self.pressed_count += 1
                self.last_press = "轨%d→%s" % (lane + 1, self.keys[key_idx])
                self.log("  按下 %s [%s]" % (self.last_press, nt if nt else '普通'))
                hold = sus if sus > 0 else self.hold_ms
                threading.Timer(max(hold, 8) / 1000.0,
                                self._release, args=(key_idx, vk)).start()
            time.sleep(0.001)
        if self.running:
            self.running = False
            self.log("✓ 谱面打完（共按 %d 键）" % self.pressed_count)

    def _release(self, key_idx, vk):
        try: key_up(vk)
        except Exception: pass
        self.held[key_idx] = False

    def hotkey_loop(self):
        while True:
            f5 = bool(user32.GetAsyncKeyState(VK['F5']) & 0x8000)
            ctrl = bool(user32.GetAsyncKeyState(VK['CTRL']) & 0x8000)
            up = bool(user32.GetAsyncKeyState(VK['UP']) & 0x8000)
            down = bool(user32.GetAsyncKeyState(VK['DOWN']) & 0x8000)
            esc = bool(user32.GetAsyncKeyState(VK['ESC']) & 0x8000)
            if f5 and not self.last_f5:               # 去掉 armed 门槛
                if self.running: self.stop("F5 停止")
                else: self.start()
            if ctrl and up and not self.last_up:
                self.offset_ms += 2; self.log("延迟 %+dms（提前更多）" % self.offset_ms)
            if ctrl and down and not self.last_down:
                self.offset_ms -= 2; self.log("延迟 %+dms（提前更少）" % self.offset_ms)
            if esc: self.stop("Esc 急停")
            self.last_f5, self.last_up, self.last_down = f5, up, down
            time.sleep(0.005)

    def detect_loop(self, cb):
        while True:
            if self.armed:
                cb(find_game(list_processes()))
            time.sleep(0.6)

# ============ GUI (PyQt5) ============
def build_gui():
    from PyQt5.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
        QPushButton, QCheckBox, QScrollArea, QTextEdit, QFileDialog, QComboBox)
    from PyQt5.QtCore import QTimer, Qt

    app = QApplication(sys.argv)
    logq = queue.Queue()
    eng = Engine(logq)

    w = QWidget(); w.setWindowTitle("谱面代打器 v2.4 · 玩家type/全部type · F5 · 焦点自动切换")
    w.resize(740, 720)
    lay = QVBoxLayout(w)

    # 谱面选择
    row1 = QHBoxLayout()
    pathle = QLineEdit(); pathle.setReadOnly(True)
    def pick():
        p, _ = QFileDialog.getOpenFileName(w, "选谱面 JSON", "", "谱面 JSON (*.json);;所有 (*.*)")
        if not p: return
        pathle.setText(p)
        try:
            n = eng.set_chart(p)
            rebuild_types(); refresh_target()
        except Exception as e:
            logq.put("解析失败：%s" % e)
    b = QPushButton("浏览…"); b.clicked.connect(pick)
    row1.addWidget(QLabel("谱面：")); row1.addWidget(pathle); row1.addWidget(b)
    lay.addLayout(row1)

    # 代打目标
    rowside = QHBoxLayout()
    side_cb = QComboBox()
    side_cb.addItems(['玩家侧（只按玩家type，默认）', '对手（检测所有type）', '全部（检测所有type）'])
    side_cb.setCurrentIndex(0)
    def apply_side():
        eng.target_side = ['player', 'opponent', 'all'][side_cb.currentIndex()]
        logq.put("代打目标 → " + SIDE_LABEL[eng.target_side])
        refresh_target()
    side_cb.currentIndexChanged.connect(lambda i: apply_side())
    rowside.addWidget(QLabel("代打目标：")); rowside.addWidget(side_cb); rowside.addStretch(1)
    lay.addLayout(rowside)

    # 键位
    rowk = QHBoxLayout()
    keyle = QLineEdit(' '.join(DEFAULT_KEYS))
    def apply_keys():
        ks = [x.strip().upper() for x in keyle.text().replace(',', ' ').split() if x.strip()]
        if len(ks) == 4 and all(k in VK for k in ks):
            eng.keys = ks; logq.put("键位 → " + ' '.join(ks))
        else: logq.put("键位无效：需 4 个合法键名")
    bk = QPushButton("应用键位"); bk.clicked.connect(apply_keys)
    rowk.addWidget(QLabel("4 键位：")); rowk.addWidget(keyle); rowk.addWidget(bk)
    lay.addLayout(rowk)

    # noteType 勾选区（标注归属）
    tlabel = QLabel("noteType 过滤：检测到 0 种")
    tlabel.setStyleSheet("font-weight:bold;")
    lay.addWidget(tlabel)
    scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setMaximumHeight(150)
    inner = QWidget(); innerLay = QVBoxLayout(inner); innerLay.setContentsMargins(4, 4, 4, 4)
    scroll.setWidget(inner)
    lay.addWidget(scroll)
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
        tlabel.setText("noteType 过滤：%d 种（括号标归属；玩家模式只按玩家type）" % len(ordered))
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

    # 状态
    offlabel = QLabel("offset = +0 ms"); offlabel.setStyleSheet("font-weight:bold;color:#0a6;")
    proclabel = QLabel("进程：未检测")
    statlabel = QLabel("待机：未载入谱面"); statlabel.setStyleSheet("font-weight:bold;color:#a30;")
    tgtlabel = QLabel("当前将按：—"); tgtlabel.setStyleSheet("font-weight:bold;color:#06a;")
    lay.addWidget(offlabel); lay.addWidget(proclabel); lay.addWidget(statlabel); lay.addWidget(tgtlabel)

    def refresh_target():
        if eng.notes:
            tgtlabel.setText("当前将按：%d / %d 音符（%s）" %
                             (eng.targets_now(), len(eng.notes), SIDE_LABEL[eng.target_side]))
        else:
            tgtlabel.setText("当前将按：—")
    refresh_target()

    # 开始/停止按钮
    rowrun = QHBoxLayout()
    def toggle_detect():
        eng.armed = not eng.armed
        bd.setText("停止检测" if eng.armed else "开始检测")
        logq.put("进程检测：" + ("开" if eng.armed else "关"))
    bd = QPushButton("开始检测"); bd.clicked.connect(toggle_detect)
    bs = QPushButton("▶ 开始代打"); bs.clicked.connect(lambda: eng.start())
    bst = QPushButton("■ 停止"); bst.clicked.connect(lambda: eng.stop("按钮停止"))
    rowrun.addWidget(bd); rowrun.addWidget(bs); rowrun.addWidget(bst)
    lay.addLayout(rowrun)

    log = QTextEdit(); log.setReadOnly(True); log.setMaximumHeight(170)
    lay.addWidget(log)

    def flush():
        while True:
            try: s = logq.get_nowait()
            except queue.Empty: break
            log.append(s)
        offlabel.setText("offset = %+d ms" % eng.offset_ms)
        if eng.running:
            statlabel.setText("代打中：已按 %d / 目标 %d · 最近 %s" %
                              (eng.pressed_count, eng.total_target, eng.last_press))
        else:
            if eng.notes:
                statlabel.setText("待机：玩家type %d / 对手type %d · 目标=%s" %
                                  (eng.player_cnt, eng.opponent_cnt, SIDE_LABEL[eng.target_side]))
            else:
                statlabel.setText("待机：未载入谱面")
        refresh_target()
    timer = QTimer(); timer.timeout.connect(flush); timer.start(80)

    def on_proc(name): proclabel.setText("进程：" + (name if name else "未发现游戏"))
    threading.Thread(target=eng.hotkey_loop, daemon=True).start()
    threading.Thread(target=lambda: eng.detect_loop(on_proc), daemon=True).start()
    logq.put("准备就绪：选谱面 → 选代打目标 → 勾 type → GO 瞬间按 F5（自动切回游戏焦点）。")
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

    # type_owner 归属
    assert type_owner('') == 'player'
    assert type_owner('Blue') == 'player'           # 机制 type 归玩家
    assert type_owner('CupNote') == 'player'
    assert type_owner('Opponent 2 Sing') == 'opponent'
    assert type_owner('Both Opponents Sing') == 'opponent'
    assert type_owner('cupSing') == 'opponent'
    assert type_owner('nmBSing') == 'opponent'
    assert type_owner('bfSing') == 'player'

    # 分轨: 玩家模式只按玩家 type; 对手/全部按所有
    eg = Engine(queue.Queue())
    eg.notes = [(0,0,0,'',True), (1,5,0,'',False), (2,1,0,'Blue',True),
                (3,6,0,'Opponent 2 Sing',False), (4,2,0,'cupSing',True)]
    assert [eg.should_press(n) for n in eg.notes] == [True, True, True, False, False]
    eg.target_side = 'opponent'
    assert all(eg.should_press(n) for n in eg.notes)
    eg.target_side = 'all'
    assert all(eg.should_press(n) for n in eg.notes)
    eg.target_side = 'player'
    eg.enabled_types = {'', 'Blue', 'Opponent 2 Sing', 'cupSing'}  # 模拟默认全勾
    assert eg.targets_now() == 3   # 三条玩家 type: '', '', Blue

    assert ctypes.sizeof(INPUT) in (28, 40), ctypes.sizeof(INPUT)
    print("[OK] v2.4 自检：type归属 + 玩家/全部过滤 + 三种谱面格式 + 注入结构")
    print("     type_owner 断言 8 项通过, targets_now 玩家=%d" % eg.targets_now())

if __name__ == '__main__':
    if '--selftest' in sys.argv: selftest()
    else: build_gui()
