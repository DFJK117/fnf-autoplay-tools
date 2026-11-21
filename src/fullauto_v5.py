# -*- coding: utf-8 -*-
"""
全自动外挂 v5 (external EXE) —— 真键盘 SendInput + 内存 songPosition 时钟闭环
================================================================================
跟 Lua 完全是两回事：这是独立 EXE，读游戏内存时钟，发真实键盘事件，
判定走游戏正常流程（能被排行榜接受），不是 Bot Play。

原理:
  1. hxcpp 把 Conductor.songPosition 编译成 exe 数据段的全局 double
  2. 本程序启动时自动扫描：找「随墙钟 1:1 增长、在 exe 主模块、排除显卡驱动区」
     的 double = songPosition，锁定绝对地址，写缓存
  3. 每 ~1ms 读 songPosition，当它 >= 音符 strumTime 就 SendInput 发真键 -> 闭环零漂移
  4. 支持任意 K（4K/7K/13K...），键位可配

用法:
  1. 选谱面 JSON（FNF 标准 / psych_v1_convert 自动识别）
  2. 设键数 + 键位（默认 4K = D F J K）
  3. 进游戏，让一首歌「正在播放」（不能在暂停/菜单）
  4. 点 [自动锁定并开打]，程序扫描锁定时钟后自动开打，曲终自动停
  5. F5 急停 / Ctrl+↑↓ 微调偏移

  锁定后的模块+偏移写入 songpos_lock.json；下次同版本游戏启动直接读缓存，
  校验数值合理（0<v<3600000 且持续增长）后秒开，不用重扫。
"""
import os, sys, json, time, threading, ctypes, queue
from ctypes import wintypes
import numpy as np

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
k32 = ctypes.windll.kernel32
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.GetAsyncKeyState.restype = wintypes.SHORT
MAPVK_VK_TO_VSC = 0

# 全键盘 VK 表（支持多 K 任意键位）
VK = {
    'A':0x41,'B':0x42,'C':0x43,'D':0x44,'E':0x45,'F':0x46,'G':0x47,'H':0x48,
    'I':0x49,'J':0x4A,'K':0x4B,'L':0x4C,'M':0x4D,'N':0x4E,'O':0x4F,'P':0x50,
    'Q':0x51,'R':0x52,'S':0x53,'T':0x54,'U':0x55,'V':0x56,'W':0x57,'X':0x58,
    'Y':0x59,'Z':0x5A,
    '1':0x31,'2':0x32,'3':0x33,'4':0x34,'5':0x35,'6':0x36,'7':0x37,'8':0x38,'9':0x39,'0':0x30,
    'LEFT':0x25,'UP':0x26,'RIGHT':0x27,'DOWN':0x28,
    'SPACE':0x20,'ENTER':0x0D,'SHIFT':0x10,'CTRL':0x11,'ALT':0x12,
    'F1':0x70,'F2':0x71,'F3':0x72,'F4':0x73,'F5':0x74,'F6':0x75,'F7':0x76,'F8':0x77,'F9':0x78,
    'ESC':0x1B,'TAB':0x09,
}
DEFAULT_KEYS = ['D','F','J','K']
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

# ============ 进程/模块/内存 ============
PROCESS_VM_READ=0x0010; PROCESS_QUERY_INFORMATION=0x0400
MEM_COMMIT=0x1000; MEM_PRIVATE=0x20000; MEM_MAPPED=0x40000; MEM_IMAGE=0x400000
EXCLUDE_MOD=('nvoglv64','nvwgf2umx','nvidia','amd','radeon','intel','dxgi','d3d','opengl','vulkan','audiodg')

class MBI(ctypes.Structure):
    _fields_=[("BaseAddress",ctypes.c_void_p),("AllocationBase",ctypes.c_void_p),
              ("AllocationProtect",wintypes.DWORD),("RegionSize",ctypes.c_size_t),
              ("State",wintypes.DWORD),("Protect",wintypes.DWORD),("Type",wintypes.DWORD)]
k32.OpenProcess.restype=wintypes.HANDLE
k32.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
k32.VirtualQueryEx.restype=ctypes.c_size_t
k32.VirtualQueryEx.argtypes=(wintypes.HANDLE,ctypes.c_void_p,ctypes.POINTER(MBI),ctypes.c_size_t)
k32.ReadProcessMemory.restype=wintypes.BOOL
k32.ReadProcessMemory.argtypes=(wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p,
                                ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t))

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

GAME_KEYWORDS=('novaflare','funkin','fnf','psych','kade','engine')
def find_game_pid():
    k32.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    snap=k32.CreateToolhelp32Snapshot(0x2,0)
    k32.Process32FirstW.restype=wintypes.BOOL; k32.Process32NextW.restype=wintypes.BOOL
    k32.Process32FirstW.argtypes=(wintypes.HANDLE,ctypes.POINTER(PROCESSENTRY32))
    k32.Process32NextW.argtypes=(wintypes.HANDLE,ctypes.POINTER(PROCESSENTRY32))
    pe=PROCESSENTRY32(); pe.dwSize=ctypes.sizeof(PROCESSENTRY32)
    ok=k32.Process32FirstW(snap,ctypes.byref(pe))
    while ok:
        nm=pe.szExeFile
        if any(t in nm.lower() for t in GAME_KEYWORDS):
            k32.CloseHandle(snap); return pe.th32ProcessID,nm
        ok=k32.Process32NextW(snap,ctypes.byref(pe))
    k32.CloseHandle(snap); return None,None

def focus_game():
    hits=[]
    WND=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    def cb(h,l):
        n=user32.GetWindowTextLengthW(h)
        if n:
            b=ctypes.create_unicode_buffer(n+1); user32.GetWindowTextW(h,b,n+1)
            if any(k in b.value.lower() for k in GAME_KEYWORDS): hits.append(h)
        return True
    try: user32.EnumWindows(WND(cb),0)
    except Exception: pass
    for h in hits:
        try:
            user32.ShowWindow(h,9); user32.SetForegroundWindow(h)
            return h
        except Exception: continue
    return None

def modules(h):
    psapi=ctypes.windll.psapi; HMOD=wintypes.HMODULE
    psapi.EnumProcessModulesEx.argtypes=(wintypes.HANDLE,ctypes.POINTER(HMOD),
        wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),wintypes.DWORD)
    psapi.GetModuleBaseNameW.argtypes=(wintypes.HANDLE,HMOD,ctypes.c_wchar_p,wintypes.DWORD)
    mods=(HMOD*512)(); need=wintypes.DWORD()
    psapi.EnumProcessModulesEx(h,mods,ctypes.sizeof(mods),ctypes.byref(need),0x03)
    out=[]
    for i in range(need.value//ctypes.sizeof(HMOD)):
        b=ctypes.create_unicode_buffer(260)
        psapi.GetModuleBaseNameW(h,mods[i],b,260)
        out.append((int(mods[i]),b.value))
    return out

def locate_mod(addr,mods):
    best=None
    for base,name in mods:
        if base<=addr<base+0x4000000 and (best is None or base>best[0]):
            best=(base,name)
    return best if best else (None,'?')

def regions(h,only_image=False):
    addr=0; mbi=MBI()
    while addr<0x7FFFFFFF0000:
        n=k32.VirtualQueryEx(h,ctypes.c_void_p(addr),ctypes.byref(mbi),ctypes.sizeof(mbi))
        if n==0: break
        base=mbi.BaseAddress or 0; size=mbi.RegionSize
        ok=(mbi.State==MEM_COMMIT and mbi.Protect not in (0x01,0)
            and 0<size<=64*1024*1024)
        if only_image: ok = ok and mbi.Type==MEM_IMAGE
        else: ok = ok and mbi.Type in (MEM_PRIVATE,MEM_MAPPED,MEM_IMAGE)
        if ok: yield base,size,mbi.Type
        nxt=base+size
        if nxt<=addr: break
        addr=nxt

def read_region(h,base,size):
    buf=ctypes.create_string_buffer(size); got=ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h,ctypes.c_void_p(base),buf,size,ctypes.byref(got)):
        return None
    n=got.value//8
    return np.frombuffer(buf.raw[:got.value],dtype='<f8',count=n) if n else None

# ============ 谱面解析（复用 v2.4，扩展到多 K） ============
def normalize_type(v):
    if v is None: return ''
    s=str(v).strip()
    if s.lower() in ('nil','none','null'): return ''
    return s

def type_owner(nt):
    s=(nt or '').strip().lower()
    if not s: return 'player'
    if 'opponent' in s or 'gf sing' in s: return 'opponent'
    if s.endswith('sing') or s.endswith('p'):
        if s.startswith('bf') or s.startswith('player'): return 'player'
        return 'opponent'
    return 'player'

def _note_tuple(n,must_section):
    try:
        if isinstance(n,dict):
            if 'strumTime' not in n: return None
            t=float(n['strumTime'])
            lane=int(n.get('noteData',n.get('lane',0)) or 0)
            sus=float(n.get('sustainLength',n.get('sus',0)) or 0)
            nt=normalize_type(n.get('noteType',''))
            if 'mustPress' in n:
                return (t,lane,sus,nt,bool(n['mustPress'])) if 0<=lane<=31 else None
            sec_flag=must_section
        elif isinstance(n,(list,tuple)) and len(n)>=2:
            t=float(n[0]); lane=int(n[1])
            sus=float(n[2]) if len(n)>2 and n[2] else 0.0
            nt=normalize_type(n[3]) if len(n)>3 else ''
            sec_flag=must_section
        else: return None
    except (ValueError,TypeError,IndexError): return None
    if not (0<=lane<=31): return None
    must_press=sec_flag if lane<4 else (not sec_flag)  # 4K 分界；多K按 noteType 归属兜底
    return (t,lane,sus,nt,must_press)

def load_chart(path):
    raw=open(path,'rb').read()
    if raw.startswith(b'\xef\xbb\xbf'): raw=raw[3:]
    data=json.loads(raw.decode('utf-8','replace'))
    song=data.get('song') if isinstance(data,dict) else None
    if not isinstance(song,dict): song=data if isinstance(data,dict) else {}
    containers=[]
    for src in (song,data):
        if not isinstance(src,dict): continue
        nc=src.get('notes')
        if isinstance(nc,list) and nc and nc not in containers: containers.append(nc)
    notes=[]; types=set()
    for cont in containers:
        for sec in cont:
            if isinstance(sec,dict) and isinstance(sec.get('sectionNotes'),list):
                must=bool(sec.get('mustHitSection',True))
                for n in sec['sectionNotes']:
                    tup=_note_tuple(n,must)
                    if tup: notes.append(tup); types.add(tup[3])
            else:
                tup=_note_tuple(sec,True)
                if tup: notes.append(tup); types.add(tup[3])
    notes.sort(key=lambda x:x[0])
    return notes,song,types

# ============ 时钟锁定器 ============
class Locator:
    def __init__(self,pid,logq):
        self.pid=pid; self.logq=logq
        self.h=k32.OpenProcess(PROCESS_VM_READ|PROCESS_QUERY_INFORMATION,False,pid)
        self.mods=modules(self.h) if self.h else []
        self.addr=None; self._stable={}
        self.cache=os.path.join(os.path.dirname(os.path.abspath(__file__)),'songpos_lock.json')
    def log(self,s): self.logq.put(s)
    def load_cache(self):
        """返回缓存地址（若文件存在），运行时校验其数值合理性。"""
        try:
            d=json.load(open(self.cache,encoding='utf-8'))
            off=int(d['offset'],16) if isinstance(d['offset'],str) else int(d['offset'])
            base=None
            for b,nm in self.mods:
                if nm==d['module']: base=b; break
            if base is None:
                # 模块基址变了（重开游戏），用 main exe 基址兜底
                for b,nm in self.mods:
                    if nm.lower().endswith('.exe'): base=b; break
            if base is None: return None
            ad=base+off
            v1=self.read_at(ad); time.sleep(0.15); v2=self.read_at(ad)
            if v1 is not None and v2 is not None and 0<v1<3600000 and 0.5*150<(v2-v1)*1<1.6*150:
                return ad
        except Exception: pass
        return None
    def read_at(self,ad):
        v=ctypes.c_double()
        ok=k32.ReadProcessMemory(self.h,ctypes.c_void_p(int(ad)),ctypes.byref(v),8,None)
        return v.value if ok else None
    def scan_once(self):
        if not self.h: return set()
        regs=[(b,s,t) for b,s,t in regions(self.h) if t==MEM_IMAGE]  # 只扫映像区: songPosition 在 exe .data
        snap1=[read_region(self.h,b,s) for b,s,t in regs]
        time.sleep(0.28); wall=280.0
        grow=set()
        for i,(b,s,t) in enumerate(regs):
            a=snap1[i]
            if a is None: continue
            cur=read_region(self.h,b,s)
            if cur is None or len(cur)!=len(a): continue
            d=cur-a
            with np.errstate(all='ignore'):
                m=(a>0)&(a<3600000)&(d>wall*0.7)&(d<wall*1.3)&np.isfinite(d)
            for j in np.nonzero(m)[0]:
                ad=b+int(j)*8
                base,mod=locate_mod(ad,self.mods)
                if any(x in mod.lower() for x in EXCLUDE_MOD): continue
                if base is not None and (ad-base)<0x10000: continue  # 排除 PE 头低 RVA
                grow.add(ad)
        return grow
    def lock(self,need=3,max_try=60):
        if not self.h:
            self.log("无法打开游戏进程"); return None
        # 先试缓存
        cached=self.load_cache()
        if cached:
            self.addr=cached; base,mod=locate_mod(cached,self.mods)
            self.log("★ 命中缓存锁定: 0x%X (%s +0x%X)"%(cached,mod,cached-(base or 0)))
            return cached
        self.log("扫描中：请确认游戏正在播放一首歌（不能暂停/在菜单）…")
        for t in range(max_try):
            g=self.scan_once()
            for a in g: self._stable[a]=self._stable.get(a,0)+1
            for a in list(self._stable):
                if a not in g: self._stable[a]=0
            ready=[a for a,c in self._stable.items() if c>=need]
            if ready:
                def score(a):
                    base,mod=locate_mod(a,self.mods)
                    off=a-base if base else (1<<60)
                    # 优先主 exe、偏移小的（真全局 double 在 .data）
                    isexe=0 if mod.lower().endswith('.exe') else 1
                    return (isexe,off if off<(1<<40) else (1<<60))
                ready.sort(key=score)
                self.addr=ready[0]
                base,mod=locate_mod(self.addr,self.mods)
                off=self.addr-(base or 0)
                self.log("★ 锁定 songPosition: 0x%X (%s +0x%X)"%(self.addr,mod,off))
                try:
                    json.dump({'module':mod,'offset':hex(off),'addr':hex(self.addr)},
                              open(self.cache,'w',encoding='utf-8'),indent=2)
                except Exception: pass
                return self.addr
            if t%10==0: self.log("  扫描 %d/%d，候选 %d（要游戏放歌才会增长）"%(t,max_try,len(g)))
            time.sleep(0.2)
        self.log("✗ 没锁定。请让一首歌正在播放后再点开打。")
        return None

# ============ 引擎（时钟闭环 + SendInput） ============
class Engine:
    def __init__(self,logq):
        self.logq=logq
        self.notes=[]; self.all_types=set(); self.enabled_types=set()
        self.keys=list(DEFAULT_KEYS); self.key_count=4
        self.target_side='player'
        self.offset_ms=0; self.hold_ms=45
        self.running=False; self.locator=None
        self.idx=0; self.held={}; self.pressed_count=0; self.total_target=0
        self.last_press='—'; self.last_f5=self.last_up=self.last_down=False
    def log(self,s): self.logq.put(s)
    def should_press(self,note):
        if self.target_side in ('opponent','all'): return True
        return type_owner(note[3])=='player'
    def set_chart(self,path):
        self.notes,song,self.all_types=load_chart(path)
        self.enabled_types=set(self.all_types)
        # 推断键数：取最大 lane+1
        maxlane=max((n[1] for n in self.notes),default=3)
        inferred=maxlane+1
        self.log("载入 %d 音符（最大轨道 %d → 推断 %dK），%d 种 noteType，BPM=%s"%(
            len(self.notes),maxlane,inferred,len(self.all_types),song.get('bpm','?')))
        return inferred
    def targets_now(self):
        return sum(1 for n in self.notes if n[3] in self.enabled_types and self.should_press(n))
    def start(self,pid):
        if not self.notes: self.log("错误：还没载入谱面"); return
        targets=self.targets_now()
        if targets==0: self.log("错误：当前勾选下没有音符可打"); return
        fg=focus_game()
        self.log("焦点→"+("游戏" if fg else "未找到游戏窗口，请手动点一下游戏"))
        loc=Locator(pid,self.logq)
        addr=loc.lock()
        if not addr: return
        self.locator=loc
        self.running=True; self.idx=0; self.held={}; self.pressed_count=0
        self.total_target=targets
        self.log("★ 开始代打：将按 %d 音符，%dK 键位 %s，offset=%+dms"%(
            targets,self.key_count,' '.join(self.keys[:self.key_count]),self.offset_ms))
        threading.Thread(target=self._player,daemon=True).start()
    def stop(self,reason="已停止"):
        if not self.running: return
        self.running=False
        for k in list(self.held.keys()):
            try: key_up(VK[self.keys[k]])
            except Exception: pass
        self.held={}
        self.log("■ %s（共按 %d 键）"%(reason,self.pressed_count))
    def _player(self):
        notes=self.notes; n=len(notes); loc=self.locator
        # 跳过曲首前的负时间偏移
        while self.running and self.idx<n:
            pos=loc.read_at(loc.addr)
            if pos is None: time.sleep(0.002); continue
            now=pos+self.offset_ms
            while self.idx<n and now>=notes[self.idx][0]:
                t,lane,sus,nt,mp=notes[self.idx]; self.idx+=1
                if nt not in self.enabled_types: continue
                if not self.should_press(notes[self.idx-1]): continue
                # 多K: lane 直接映射到键位；超出键数则取模
                ki=lane if lane<self.key_count else lane%self.key_count
                try: vk=VK[self.keys[ki]]
                except Exception: continue
                key_down(vk); self.held[ki]=True; self.pressed_count+=1
                self.last_press="轨%d→%s"%(lane+1,self.keys[ki])
                self.log("  按下 %s [%s] @%.0fms"%(self.last_press,nt or '普通',pos))
                hold=sus if sus>0 else self.hold_ms
                threading.Timer(max(hold,8)/1000.0,self._release,args=(ki,vk)).start()
            time.sleep(0.001)
        if self.running:
            self.running=False; self.log("✓ 谱面打完（共按 %d 键）"%self.pressed_count)
    def _release(self,ki,vk):
        try: key_up(vk)
        except Exception: pass
        self.held[ki]=False
    def hotkey_loop(self):
        while True:
            f5=bool(user32.GetAsyncKeyState(VK['F5'])&0x8000)
            ctrl=bool(user32.GetAsyncKeyState(VK['CTRL'])&0x8000)
            up=bool(user32.GetAsyncKeyState(VK['UP'])&0x8000)
            down=bool(user32.GetAsyncKeyState(VK['DOWN'])&0x8000)
            if f5 and not self.last_f5:
                if self.running: self.stop("F5 急停")
            if ctrl and up and not self.last_up:
                self.offset_ms+=2; self.log("偏移 %+dms（提前）"%self.offset_ms)
            if ctrl and down and not self.last_down:
                self.offset_ms-=2; self.log("偏移 %+dms（推后）"%self.offset_ms)
            self.last_f5,self.last_up,self.last_down=f5,up,down
            time.sleep(0.005)

# ============ GUI ============
def build_gui():
    from PyQt5.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QLabel,
        QLineEdit,QPushButton,QCheckBox,QScrollArea,QTextEdit,QFileDialog,QComboBox,QSpinBox)
    from PyQt5.QtCore import QTimer,Qt
    app=QApplication(sys.argv)
    logq=queue.Queue(); eng=Engine(logq)
    w=QWidget(); w.setWindowTitle("全自动外挂 v5 · 真键盘 · 内存时钟闭环 · 多K")
    w.resize(760,760); lay=QVBoxLayout(w)

    row1=QHBoxLayout(); pathle=QLineEdit(); pathle.setReadOnly(True)
    def pick():
        p,_=QFileDialog.getOpenFileName(w,"选谱面 JSON","","谱面 JSON (*.json);;所有 (*.*)")
        if not p: return
        pathle.setText(p)
        try:
            k=eng.set_chart(p); kb.setValue(k); rebuild_types(); refresh_target()
        except Exception as e: logq.put("解析失败：%s"%e)
    b=QPushButton("浏览…"); b.clicked.connect(pick)
    row1.addWidget(QLabel("谱面：")); row1.addWidget(pathle); row1.addWidget(b); lay.addLayout(row1)

    # 键数 + 键位
    rowk=QHBoxLayout()
    kb=QSpinBox(); kb.setRange(1,18); kb.setValue(4)
    keyle=QLineEdit(' '.join(DEFAULT_KEYS))
    def apply_keys():
        ks=[x.strip().upper() for x in keyle.text().replace(',',' ').split() if x.strip()]
        n=kb.value()
        if len(ks)>=n and all(k in VK for k in ks[:n]):
            eng.keys=ks; eng.key_count=n
            logq.put("键位→ %dK: %s"%(n,' '.join(ks[:n])))
        else: logq.put("键位无效：至少 %d 个合法键名"%n)
    bk=QPushButton("应用"); bk.clicked.connect(apply_keys)
    rowk.addWidget(QLabel("键数")); rowk.addWidget(kb)
    rowk.addWidget(QLabel("键位")); rowk.addWidget(keyle); rowk.addWidget(bk); lay.addLayout(rowk)

    rowside=QHBoxLayout(); side_cb=QComboBox()
    side_cb.addItems(['玩家侧（只按玩家type）','对手（检测所有type）','全部（检测所有type）'])
    def apply_side():
        eng.target_side=['player','opponent','all'][side_cb.currentIndex()]
        logq.put("代打目标→"+eng.target_side); refresh_target()
    side_cb.currentIndexChanged.connect(lambda i:apply_side())
    rowside.addWidget(QLabel("目标：")); rowside.addWidget(side_cb); rowside.addStretch(1); lay.addLayout(rowside)

    tlabel=QLabel("noteType 过滤：0 种"); tlabel.setStyleSheet("font-weight:bold;"); lay.addWidget(tlabel)
    scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setMaximumHeight(130)
    inner=QWidget(); innerLay=QVBoxLayout(inner); innerLay.setContentsMargins(4,4,4,4); scroll.setWidget(inner)
    lay.addWidget(scroll)
    def rebuild_types():
        while innerLay.count():
            it=innerLay.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        for nt in sorted(eng.all_types,key=lambda s:(type_owner(s)!='player',s!='',s)):
            own='玩家' if type_owner(nt)=='player' else '对手'
            cnt=sum(1 for x in eng.notes if x[3]==nt)
            cb=QCheckBox("[%s] %s [%d]"%(own,nt or '（普通）',cnt)); cb.setChecked(True)
            cb.stateChanged.connect(lambda st,t=nt:(eng.set_type_enabled(t,st==Qt.Checked),refresh_target()))
            innerLay.addWidget(cb)
        innerLay.addStretch(1)
        tlabel.setText("noteType 过滤：%d 种"%len(eng.all_types))

    statlabel=QLabel("待机：未载入谱面"); statlabel.setStyleSheet("font-weight:bold;color:#a30;")
    tgtlabel=QLabel("当前将按：—"); tgtlabel.setStyleSheet("font-weight:bold;color:#06a;")
    offlabel=QLabel("偏移 = +0 ms"); offlabel.setStyleSheet("color:#0a6;")
    lay.addWidget(statlabel); lay.addWidget(tgtlabel); lay.addWidget(offlabel)
    def refresh_target():
        if eng.notes: tgtlabel.setText("当前将按：%d / %d 音符"%(eng.targets_now(),len(eng.notes)))
        else: tgtlabel.setText("当前将按：—")
    refresh_target()

    rowrun=QHBoxLayout()
    def do_start():
        pid,nm=find_game_pid()
        if not pid: logq.put("✗ 没找到游戏进程，请先开游戏"); return
        logq.put("游戏进程：%s (PID %d)"%(nm,pid))
        threading.Thread(target=lambda:eng.start(pid),daemon=True).start()
    bs=QPushButton("▶ 自动锁定并开打"); bs.clicked.connect(do_start)
    bst=QPushButton("■ 停止(F5)"); bst.clicked.connect(lambda:eng.stop("按钮停止"))
    rowrun.addWidget(bs); rowrun.addWidget(bst); lay.addLayout(rowrun)

    log=QTextEdit(); log.setReadOnly(True); log.setMaximumHeight(180); lay.addWidget(log)
    def flush():
        while True:
            try: s=logq.get_nowait()
            except queue.Empty: break
            log.append(s)
        offlabel.setText("偏移 = %+d ms"%eng.offset_ms)
        if eng.running:
            statlabel.setText("代打中：已按 %d / %d · 最近 %s"%(eng.pressed_count,eng.total_target,eng.last_press))
        elif eng.notes: statlabel.setText("待机：%d 音符 / %d 种 type"%(len(eng.notes),len(eng.all_types)))
    timer=QTimer(); timer.timeout.connect(flush); timer.start(80)

    threading.Thread(target=eng.hotkey_loop,daemon=True).start()
    logq.put("准备就绪：选谱面 → 设键数/键位 → 进游戏放歌 → 点[自动锁定并开打]。")
    logq.put("说明：锁定内存时钟后全自动，无需 F5；F5 急停，Ctrl+↑↓ 微调偏移。")
    w.show(); sys.exit(app.exec_())

# ============ 自检 ============
def selftest():
    import tempfile
    sample={"song":{"bpm":130,"notes":[
        {"mustHitSection":True,"sectionNotes":[[0,0,0],[500,1,0,'Blue']]},
        {"mustHitSection":False,"sectionNotes":[[1000,5,0],[1500,6,0,'Opponent 2 Sing']]},
    ]}}
    fd,p=tempfile.mkstemp(suffix='.json'); os.close(fd)
    open(p,'w',encoding='utf-8').write(json.dumps(sample))
    notes,song,types=load_chart(p)
    assert len(notes)==4 and song['bpm']==130
    assert max(n[1] for n in notes)==6
    os.remove(p)
    # 多K键位
    eg=Engine(queue.Queue()); eg.keys=['D','F','J','K','SPACE','L','S']; eg.key_count=7
    assert all(k in VK for k in eg.keys)
    assert ctypes.sizeof(INPUT) in (28,40)
    print("[OK] v5 自检：谱面解析 + 多K键位 + SendInput 注入结构 + 全键盘VK表")
    print("     VK 键数=%d, INPUT 大小=%d"%(len(VK),ctypes.sizeof(INPUT)))

if __name__=='__main__':
    if '--selftest' in sys.argv: selftest()
    else: build_gui()
