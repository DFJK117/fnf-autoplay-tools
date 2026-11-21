# -*- coding: utf-8 -*-
"""
全自动内存代打引擎 v4.0 —— 直接读游戏内存里的 Note 对象, 引擎算好判定窗口, 零时钟同步
=====================================================================================
核心突破:
  * NF 实际编译 codename 引擎分支: Note = codename.funkin.game.Note
  * hxcpp 非压缩 GC, Note 对象地址在曲目生命周期内稳定
  * 每个 Note 对象头 8 字节 = 同一个 vtable 指针 -> 一首歌几百上千个 = 强特征
  * 引擎每帧自己算 canBeHit (基于 songPosition + 判定窗口), 我们只读结果:
      canBeHit==true && wasGoodHit==false && 玩家侧 -> 立即发键
    => 不需要自己数时钟, 不需要导谱面, 零漂移, 天然多 K

多 K:
  * noteData 是绝对 lane; strumID = noteData % keyCount
  * keyCount = strumLine.members.length (从内存读)
  * 键位来自 ClientPrefs.keyBinds: <N>_key_<i>, 见 KEYBINDS 表 (1K~9K)
  * FOJC 等 >9K 走 setOnLuas keyCount + 自定义 hscript, 仍可用绝对 lane % K 映射

字段偏移全部运行时自适应推断 (find_layout), 不硬编码 -> 换版本也能用。
"""
import ctypes, os, json, time, threading, struct
import numpy as np
from ctypes import wintypes

k32 = ctypes.windll.kernel32
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x400000

class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD), ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]

k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t)
k32.ReadProcessMemory.restype = wintypes.BOOL
k32.ReadProcessMemory.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))

# ---- 键位表: 索引 = 实际 lane 数 (strumLine.members.length), 值 = 每个 lane 的物理键 ----
# 引擎 ClientPrefs.keyBinds 的编号 N 对应 N+1 个 lane:
#   1_key → 2 lane (D,K), 2_key → 3 lane (D,SPACE,K), 4_key → 5 lane (D,F,SPACE,J,K),
#   5_key → 6 lane, 6_key → 7 lane, 7_key → 8 lane, 8_key → 9 lane, 9_key → 10 lane
# 这里统一按【实际 lane 数】索引, 与内存读到的 keyCount 严格对齐。
# 键名用 Windows VK 名; SendInput 通过 autoplay.VK 映射。
KEYBINDS_ENGINE = {
    2: ['D', 'K'],
    3: ['D', 'SPACE', 'K'],
    4: ['D', 'F', 'J', 'K'],              # 经典 4K (用户最常用 DFJK)
    5: ['D', 'F', 'SPACE', 'J', 'K'],     # 引擎 4_key
    6: ['S', 'D', 'F', 'J', 'K', 'L'],    # 引擎 5_key
    7: ['S', 'D', 'F', 'SPACE', 'J', 'K', 'L'],  # 6_key
    8: ['A', 'S', 'D', 'F', 'J', 'K', 'L', 'SEMICOLON'],  # 7_key
    9: ['A', 'S', 'D', 'F', 'SPACE', 'J', 'K', 'L', 'SEMICOLON'],  # 8_key
    10: ['A', 'S', 'D', 'F', 'V', 'N', 'J', 'K', 'L', 'SEMICOLON'],  # 9_key
}
# 经典模式: 4K 强制 DFJK, 其余同引擎
KEYBINDS = dict(KEYBINDS_ENGINE)
KEYBINDS[4] = ['D', 'F', 'J', 'K']

# 完整 Windows 虚拟键码 (多K必需: SPACE/SEMICOLON/L/V/N/Z/X/C/数字键)
VK_FULL = {
    'A': 65, 'B': 66, 'C': 67, 'D': 68, 'E': 69, 'F': 70, 'G': 71, 'H': 72,
    'I': 73, 'J': 74, 'K': 75, 'L': 76, 'M': 77, 'N': 78, 'O': 79, 'P': 80,
    'Q': 81, 'R': 82, 'S': 83, 'T': 84, 'U': 85, 'V': 86, 'W': 87, 'X': 88,
    'Y': 89, 'Z': 90,
    'ZERO': 48, 'ONE': 49, 'TWO': 50, 'THREE': 51, 'FOUR': 52,
    'FIVE': 53, 'SIX': 54, 'SEVEN': 55, 'EIGHT': 56, 'NINE': 57,
    'SPACE': 32, 'SEMICOLON': 186, 'COMMA': 188, 'PERIOD': 190, 'SLASH': 191,
    'QUOTE': 222, 'LBRACKET': 219, 'RBRACKET': 221, 'BACKSLASH': 220, 'MINUS': 189, 'EQUAL': 187,
    'UP': 38, 'DOWN': 40, 'LEFT': 37, 'RIGHT': 39,
    'SHIFT': 16, 'CTRL': 17, 'ALT': 18, 'ENTER': 13, 'ESC': 27,
    'F1': 112, 'F2': 113, 'F3': 114, 'F4': 115, 'F5': 116, 'F6': 117,
}

EXCLUDE_MOD = ('nvoglv64', 'nvwgf2umx', 'nvidia', 'amd', 'radeon', 'intel', 'dxgi', 'd3d', 'opengl')


def find_pid(key='novaflare'):
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    k32.Process32FirstW.restype = wintypes.BOOL; k32.Process32NextW.restype = wintypes.BOOL
    k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PE))
    k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PE))
    pe = PE(); pe.dwSize = ctypes.sizeof(PE)
    ok = k32.Process32FirstW(snap, ctypes.byref(pe))
    while ok:
        if key in pe.szExeFile.lower():
            k32.CloseHandle(snap); return pe.th32ProcessID
        ok = k32.Process32NextW(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    return None


def modules(h):
    psapi = ctypes.windll.psapi; HMOD = wintypes.HMODULE
    psapi.EnumProcessModulesEx.argtypes = (wintypes.HANDLE, ctypes.POINTER(HMOD),
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
    psapi.GetModuleBaseNameW.argtypes = (wintypes.HANDLE, HMOD, ctypes.c_wchar_p, wintypes.DWORD)
    mods = (HMOD * 512)(); need = wintypes.DWORD()
    psapi.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods), ctypes.byref(need), 0x03)
    out = []
    for i in range(need.value // ctypes.sizeof(HMOD)):
        b = ctypes.create_unicode_buffer(260)
        psapi.GetModuleBaseNameW(h, mods[i], b, 260)
        out.append((int(mods[i]), b.value))
    return out


def all_regions(h, include_image=True):
    addr = 0; mbi = MBI()
    types = (MEM_PRIVATE, MEM_MAPPED) + ((MEM_IMAGE,) if include_image else ())
    while addr < 0x7FFFFFFF0000:
        n = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if n == 0: break
        base = mbi.BaseAddress or 0; size = mbi.RegionSize
        if (mbi.State == MEM_COMMIT and mbi.Protect not in (0x01, 0)
                and mbi.Type in types and 0 < size <= 256 * 1024 * 1024):
            yield base, size, mbi.Type
        nxt = base + size
        if nxt <= addr: break
        addr = nxt


def read_region(h, base, size):
    buf = ctypes.create_string_buffer(size); got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(got)):
        return None
    return buf.raw[:got.value]


class NoteScanner:
    """运行时自适应扫描 Note 对象群 + 推断字段偏移"""

    def __init__(self, h, log):
        self.h = h
        self.log = log
        self.layout = None      # dict
        self.note_addrs = []    # Note 对象地址列表
        self.strum_off_rel = 8  # strumLine 指针相对 strumTime 的偏移 (Note.h: extra,strumTime,strumLine)
        self.cpu_strums = set() # 玩家 strumLine 地址 (cpu=false)
        self.cpu_known = False

    def _image_ranges(self, mods):
        ranges = []
        for base, name in mods:
            if name.lower().endswith('.exe') or name.lower().endswith('.dll') or 'ndll' in name.lower():
                ranges.append((base, base + 0x10000000))
        return ranges

    def find_layout(self):
        """扫描堆, 找 Note 群 vtable 并推断 strumTime/noteData/canBeHit 偏移"""
        from collections import Counter
        mods = modules(self.h)
        img = self._image_ranges(mods)
        regs = list(all_regions(self.h, include_image=False))  # Note 在 GC 堆 (PRIVATE/MAPPED)
        self.log("扫描 %d 个堆区, 定位 Note 对象群…" % len(regs))

        # 第1遍: 统计指向映像区的 8字节值 (= vtable 候选)
        vt_count = Counter()
        for base, size, ty in regs:
            data = read_region(self.h, base, size)
            if not data: continue
            n = len(data) // 8
            if n == 0: continue
            arr = np.frombuffer(data[:n*8], dtype='<u8', count=n)
            mask = np.zeros(n, dtype=bool)
            for lo, hi in img:
                mask |= (arr >= lo) & (arr < hi)
            vals = arr[mask]
            if vals.size:
                vt_count.update(vals.tolist())

        top = vt_count.most_common(24)
        self.log("vtable 候选 top: " + ", ".join("0x%X×%d" % (v, c) for v, c in top[:6]))

        # 第2遍: 对每个候选 vtable 收集对象地址, 推断布局
        best = None
        for vt, cnt in top:
            if cnt < 30: continue
            addrs = []
            for base, size, ty in regs:
                data = read_region(self.h, base, size)
                if not data: continue
                n = len(data) // 8
                if n == 0: continue
                arr = np.frombuffer(data[:n*8], dtype='<u8', count=n)
                idx = np.nonzero(arr == vt)[0]
                if idx.size:
                    addrs.extend([base + int(j) * 8 for j in idx])
            if len(addrs) < 30: continue

            sample = addrs[:500]
            mem = b''
            for ad in sample:
                b = read_region(self.h, ad, 96)
                mem += b if b and len(b) >= 96 else b'\x00' * 96
            blob = np.frombuffer(mem[:len(sample)*96], dtype=np.uint8).reshape(-1, 96)
            nobjs = blob.shape[0]

            # strumTime: 8字节偏移, double 值大量落在 (100, 3e6), 且多样
            strum = None
            for off in range(0, 80, 8):
                vals = np.array([struct.unpack('<d', blob[i, off:off+8].tobytes())[0]
                                 for i in range(nobjs)])
                ok = (vals > 100) & (vals < 3_000_000) & np.isfinite(vals)
                frac = ok.mean()
                distinct = len(set(vals[ok].astype(np.int64).tolist()))
                if frac > 0.65 and distinct > 15:
                    if strum is None or (frac, distinct) > (strum[1], strum[3]):
                        strum = (off, frac, float(np.median(vals[ok])), distinct)

            # noteData: 4字节偏移 int 在 0~80, 后跟连续 bool(0/1)
            nd = None
            for off in range(4, 80, 4):
                ints = np.array([struct.unpack('<i', blob[i, off:off+4].tobytes())[0]
                                 for i in range(nobjs)])
                okint = (ints >= 0) & (ints <= 80)
                after = blob[:, off+4:off+9]
                boolfrac = ((after == 0) | (after == 1)).mean()
                if okint.mean() > 0.75 and boolfrac > 0.85 and ints[okint].max() >= 1:
                    score = okint.mean() * boolfrac
                    if nd is None or score > nd[1]:
                        nd = (off, score, int(ints[okint].min()), int(ints[okint].max()))

            if strum and nd:
                # canBeHit / wasGoodHit 紧跟 noteData (Note.h 声明顺序):
                #   int noteData; bool canBeHit; bool tooLate; bool wasGoodHit; bool avoid;
                layout = {
                    'vtable': vt, 'count': len(addrs),
                    'strum_off': strum[0], 'noteData_off': nd[0],
                    'canHit_off': nd[0] + 4, 'tooLate_off': nd[0] + 5,
                    'wasGood_off': nd[0] + 6,
                    'noteData_max': nd[3], 'strum_median': strum[2],
                    'score': strum[1] * nd[1],
                }
                self.log("✓ 找到 Note 群: vtable 0x%X × %d 个对象" % (vt, len(addrs)))
                self.log("  偏移: strumTime=+%d noteData=+%d canBeHit=+%d wasGoodHit=+%d (noteData max=%d)" %
                         (strum[0], nd[0], layout['canHit_off'], layout['wasGood_off'], nd[3]))
                if best is None or layout['score'] > best['score']:
                    best = layout

        if best:
            self.layout = best
            self.note_addrs = addrs  # 最后一次循环的 addrs 对应 best? 需重收
            # 重新精确收集 best vtable 的所有对象地址
            self.note_addrs = []
            for base, size, ty in regs:
                data = read_region(self.h, base, size)
                if not data: continue
                n = len(data) // 8
                if n == 0: continue
                arr = np.frombuffer(data[:n*8], dtype='<u8', count=n)
                idx = np.nonzero(arr == best['vtable'])[0]
                if idx.size:
                    self.note_addrs.extend([base + int(j) * 8 for j in idx])
        return best

    def refresh_addrs(self):
        """重新扫描 Note 对象地址 (分代GC可能移动对象, 周期性刷新防地址失效)"""
        if not self.layout:
            return
        regs = list(all_regions(self.h, include_image=False))
        vt = self.layout['vtable']
        addrs = []
        for base, size, ty in regs:
            data = read_region(self.h, base, size)
            if not data: continue
            n = len(data) // 8
            if n == 0: continue
            arr = np.frombuffer(data[:n*8], dtype='<u8', count=n)
            idx = np.nonzero(arr == vt)[0]
            if idx.size:
                addrs.extend([base + int(j) * 8 for j in idx])
        old = len(self.note_addrs)
        self.note_addrs = addrs
        return old, len(addrs)

    def read_notes_fast(self):
        """高速读取所有 Note 的关键字段: (strumTime, noteData, canHit, wasGood, strumLinePtr, addr)"""
        if not self.layout or not self.note_addrs:
            return []
        L = self.layout
        out = []
        so = L['strum_off'] + self.strum_off_rel  # strumLine 指针偏移
        # 分块读, 每块 512 个对象地址
        for i in range(0, len(self.note_addrs), 512):
            chunk = self.note_addrs[i:i+512]
            for ad in chunk:
                buf = read_region(self.h, ad, 96)
                if not buf or len(buf) < 96: continue
                try:
                    st = struct.unpack_from('<d', buf, L['strum_off'])[0]
                    nd = struct.unpack_from('<i', buf, L['noteData_off'])[0]
                    ch = buf[L['canHit_off']]
                    wg = buf[L['wasGood_off']]
                    sl = struct.unpack_from('<Q', buf, so)[0] if so + 8 <= 96 else 0
                except Exception:
                    continue
                if 0 < st < 3_000_000 and 0 <= nd <= 200:
                    out.append((st, nd, bool(ch), bool(wg), sl, ad))
        return out

    def learn_player_strums(self, notes):
        """从 Note 的 strumLine 指针识别玩家侧 (cpu=false) strumLine。
        StrumLine 是 hxcpp 对象: 头8字节=vtable, 之后字段含 cpu(bool), opponentSide(bool)。
        判据: 在所有 strumLine 对象里找某个 bool 偏移, 把 strumLine 分成两组;
              玩家侧那组 = 其 note 的 strumTime 跨度覆盖全曲且数量最多(通常2条: 玩家+对手,
              也有 GF 第三条)。用 cpu 字段: StrumLine.h 构造函数 `this->cpu=false` 默认,
              对手 strumline 传 cpu=true。运行时用 note 归属推断: 同 strumLine 的 note 的
              noteData 范围是 [0,keyCount), 数量最多的通常是玩家侧。
        """
        if self.cpu_known: return
        from collections import Counter, defaultdict
        sls = set(n[4] for n in notes if n[4])
        if not sls: return
        # 采样每个 strumLine 对象前 96 字节, 找 cpu 字段偏移:
        #   读它的 vtable (应与 Note 不同), 并找第一个"不同 strumLine 取值不同"的 bool 偏移
        samples = {}
        for sl in list(sls)[:20]:
            b = read_region(self.h, sl, 96)
            if b and len(b) >= 96:
                samples[sl] = b
        if len(samples) < 2:
            # 只有一个 strumLine: 直接认定是玩家
            self.cpu_strums = set(samples.keys())
            self.cpu_known = True
            self.log("  只有 1 条 strumLine, 按玩家侧处理")
            return
        # 找区分各 strumLine 的 byte 偏移 (cpu/opponentSide 在头部 64 字节内)
        cand_offs = []
        for off in range(8, 64, 1):
            vals = set(s[off] for s in samples.values())
            if vals.issubset({0, 1}) and len(vals) >= 2:
                cand_offs.append(off)
        # cpu 字段: 玩家=false(0), 对手=true(1)。opponentSide 同理。
        # 用 note 数量判定哪条是玩家: 统计每个 strumLine 关联的 note 数
        note_per_sl = Counter(n[4] for n in notes)
        # 逐个 bool 偏移试: 该偏移==0 的组若包含最多 note 的 strumLine => 视为玩家组
        best = None
        for off in cand_offs:
            zeros = [sl for sl, b in samples.items() if b[off] == 0]
            ones = [sl for sl, b in samples.items() if b[off] == 1]
            nz = sum(note_per_sl[sl] for sl in zeros)
            no = sum(note_per_sl[sl] for sl in ones)
            # 玩家侧 note 通常更多(玩家谱面密度>对手); 取 0组占比更接近"玩家多"
            if zeros and ones and nz > 0:
                score = nz / (nz + no)
                if best is None or score > best[0]:
                    best = (score, off, set(zeros))
        if best:
            self.cpu_strums = best[2]
            self.log("  识别玩家 strumLine %d 条 (cpu=0 @ +%d, note占比%.0f%%)" %
                     (len(self.cpu_strums), best[1], best[0]*100))
        else:
            # 兜底: 所有 strumLine 都当玩家 (宁可多按不可漏按)
            self.cpu_strums = set(samples.keys())
            self.log("  未能区分玩家/对手, 兜底: 全部 strumLine 都打")
        self.cpu_known = True


class MemoryBot:
    def __init__(self, logq, ap):
        self.logq = logq
        self.ap = ap
        self.running = False
        self.h = None
        self.pid = None
        self.scanner = None
        self.key_mode = 'engine'   # 'engine' | 'classic'
        self.key_override = None   # dict {keyCount:[key names]}
        self.hold_ms = 45
        self.pressed = 0
        self.last_press = '—'
        self.kcount = 4
        self._fired = set()        # 已发键的 note 地址, 去重

    def log(self, s): self.logq.put(s)

    def _keys_for(self, k):
        if self.key_override and k in self.key_override:
            return self.key_override[k]
        table = KEYBINDS_ENGINE if self.key_mode == 'engine' else KEYBINDS
        if k in table: return table[k]
        # >10K: 字母键盘扩展 (FOJC 13K/18K)
        base = ['A', 'S', 'D', 'F', 'V', 'N', 'J', 'K', 'L', 'SEMICOLON']
        extra = ['Z', 'X', 'C', 'ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE']
        return (base + extra)[:k] if k <= len(base) + len(extra) else base

    def start(self):
        if self.running: return
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        self.log("等待游戏进程…")
        # 等进程出现
        while not self._should_stop():
            self.pid = find_pid()
            if self.pid: break
            time.sleep(1.0)
        if self._should_stop(): return
        self.log("检测到游戏 PID %d" % self.pid)
        self.h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, self.pid)
        if not self.h:
            self.log("OpenProcess 失败"); return
        self.running = True
        self.scanner = NoteScanner(self.h, self.log)

        # 等进曲: 反复扫 Note 群直到找到
        deadline = time.time() + 180
        while not self._should_stop() and time.time() < deadline:
            layout = self.scanner.find_layout()
            if layout:
                break
            self.log("还没进曲/没扫到 Note 群, 3秒后重试…（请进一首歌）")
            time.sleep(3.0)
        if self._should_stop():
            self._cleanup(); return
        if not self.scanner.layout:
            self.log("✗ 超时未找到 Note 群。请确认已进入曲目播放。")
            self._cleanup(); return

        self.ap.focus_game()

        # 先学几帧, 确定玩家侧 strumLine (不按键)
        for _ in range(5):
            notes0 = self.scanner.read_notes_fast()
            if notes0:
                self.scanner.learn_player_strums(notes0)
                if self.scanner.cpu_known: break
            time.sleep(0.05)

        self.log("★ 全自动开打 (目标: 玩家侧 canBeHit 音符, 零时钟同步)")

        # 主循环: 高频读 canBeHit && !wasGoodHit && 玩家侧 strumLine 的 note
        last_refresh = time.time()
        last_note_count = 0
        while not self._should_stop():
            notes = self.scanner.read_notes_fast()
            now = time.time()
            # 分代GC可能移动对象: 每0.6s重扫地址, 或读到数量骤降时立即重扫
            if now - last_refresh > 0.6 or (last_note_count > 20 and len(notes) < last_note_count // 3):
                self.scanner.refresh_addrs()
                if self.scanner.cpu_known:
                    self.scanner.cpu_known = False  # strumLine地址也可能变, 重新学
                last_refresh = now
            last_note_count = len(notes)
            if notes:
                # GC刷新后重新识别玩家侧 strumLine
                if not self.scanner.cpu_known:
                    self.scanner.learn_player_strums(notes)
                mx = max(n[1] for n in notes)
                self.kcount = min(max(self.kcount, mx + 1), 18)
            for st, nd, can_hit, was_good, sl, ad in notes:
                if not (can_hit and not was_good):
                    continue
                # 玩家侧过滤: strumLine 必须在玩家组
                if self.scanner.cpu_known and sl not in self.scanner.cpu_strums:
                    continue
                # 用内容指纹去重 (GC 移动地址会变, 但 strumTime+noteData 唯一)
                fp = (round(st, 1), nd)
                if fp in self._fired:
                    continue
                sid = nd % self.kcount
                keys = self._keys_for(self.kcount)
                if sid >= len(keys): continue
                key = keys[sid]
                vk = VK_FULL.get(key) or self.ap.VK.get(key)
                if vk is None: continue
                self.ap.key_down(vk)
                self.pressed += 1
                self.last_press = "lane%d→%s" % (nd, key)
                self._fired.add(fp)
                threading.Timer(max(self.hold_ms, 8) / 1000.0,
                                self._release, args=(vk,)).start()
            if len(self._fired) > 30000:
                self._fired.clear()
            time.sleep(0.0015)

        self._cleanup()
        self.log("■ 已停止 (共按 %d 键)" % self.pressed)

    def _release(self, vk):
        try: self.ap.key_up(vk)
        except Exception: pass

    def _should_stop(self):
        return getattr(self, '_stop_flag', False)

    def _cleanup(self):
        self.running = False
        if self.h:
            k32.CloseHandle(self.h); self.h = None

    def stop(self):
        self._stop_flag = True
        self._cleanup()
        self.log("■ 停止指令已发")

    def reset(self):
        self._stop_flag = False
        self.pressed = 0
        self._fired.clear()
