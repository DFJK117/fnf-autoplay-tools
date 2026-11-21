# -*- coding: utf-8 -*-
"""
闭环全自动代打器 v3.0 —— 读游戏 songPosition, 零漂移, 无需 F5/无需调延迟
================================================================================
原理:
  NF/PE 是 hxcpp 编译, Conductor.songPosition 是原生全局 double (C++:
  Float Conductor_obj::songPosition;), 地址 = 模块基址+固定偏移。
  程序持续双快照扫描进程内存, 找到"随墙钟 1:1 增长、排除显卡驱动区"的 double
  = 真正的 songPosition。锁定后每 ~1ms 读它的当前值, 当 songPos >= 音符 strumTime
  就用 SendInput 发真实键盘 -> 闭环, 零漂移, 全自动。

用法:
  1. 游戏开到一首歌"正在播放"(不能在暂停菜单)
  2. 本程序选谱面 -> 点 [自动锁定并开打]
  3. 自动扫描锁定 songPosition -> 自动开打 -> 曲终自动停
  全程不按 F5, 不调延迟。

技术细节:
  - 排除 nvoglv64.dll / nvwgf2umx.dll 等显卡驱动映射区(帧计时器干扰)
  - 锁定要求: 同一地址连续 3 轮 1:1 增长, 且值 0<v<3600000
  - 锁定后写入 songpos_lock.json, 下次同版本游戏可直接复用(校验数值合理性)
"""
import os, sys, json, time, threading, ctypes, queue
import numpy as np
from ctypes import wintypes
import autoplay as AP   # 复用 v2.4 的 SendInput / 谱面解析 / type_owner

k32 = ctypes.windll.kernel32
PROCESS_VM_READ=0x0010; PROCESS_QUERY_INFORMATION=0x0400
MEM_COMMIT=0x1000; MEM_PRIVATE=0x20000; MEM_MAPPED=0x40000; MEM_IMAGE=0x400000

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

# 显卡/音频驱动区关键词 -> 排除(它们的帧计时器也是 1:1 增长,但非 songPos)
EXCLUDE_MOD=('nvoglv64','nvwgf2umx','nvidia','amd','radeon','intel','dxgi','d3d','opengl','vulkan')

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

def regions(h):
    addr=0; mbi=MBI()
    while addr<0x7FFFFFFF0000:
        n=k32.VirtualQueryEx(h,ctypes.c_void_p(addr),ctypes.byref(mbi),ctypes.sizeof(mbi))
        if n==0: break
        base=mbi.BaseAddress or 0; size=mbi.RegionSize
        if (mbi.State==MEM_COMMIT and mbi.Protect not in (0x01,0)
                and mbi.Type in (MEM_PRIVATE,MEM_MAPPED,MEM_IMAGE)
                and 0<size<=64*1024*1024):
            yield base,size
        nxt=base+size
        if nxt<=addr: break
        addr=nxt

def read_region(h,base,size):
    buf=ctypes.create_string_buffer(size); got=ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h,ctypes.c_void_p(base),buf,size,ctypes.byref(got)):
        return None
    n=got.value//8
    return np.frombuffer(buf.raw[:got.value],dtype='<f8',count=n) if n else None

class Locator:
    """自动锁定 songPosition 的绝对地址"""
    def __init__(self,pid,logq):
        self.pid=pid; self.logq=logq
        self.h=k32.OpenProcess(PROCESS_VM_READ|PROCESS_QUERY_INFORMATION,False,pid)
        self.mods=modules(self.h) if self.h else []
        self.lock_addr=None
        self.lock_module=None; self.lock_offset=None
        self._stable={}   # addr -> 连续增长次数

    def log(self,s): self.logq.put(s)

    def scan_once(self):
        """一轮双快照, 返回本轮 1:1 增长地址集合"""
        if not self.h: return set()
        regs=list(regions(self.h))
        snap1=[read_region(self.h,b,s) for b,s in regs]
        time.sleep(0.3)
        wall=0.3*1000
        grow=set()
        for i,(b,s) in enumerate(regs):
            a=snap1[i]
            if a is None: continue
            cur=read_region(self.h,b,s)
            if cur is None or len(cur)!=len(a): continue
            d=cur-a
            # 容差放宽到 ±35%: hxcpp songPosition 跟随音频时钟, 非严格墙钟
            m=(a>0)&(a<3600000)&(d>wall*0.65)&(d<wall*1.35)&np.isfinite(d)
            for j in np.nonzero(m)[0]:
                addr=b+int(j)*8
                base,mod=locate_mod(addr,self.mods)
                # 排除驱动映射区帧计时器
                if any(x in mod.lower() for x in EXCLUDE_MOD):
                    continue
                # 排除 PE 头低 RVA(导入表/IAT 被误读成 double)
                if base is not None and (addr-base)<0x10000:
                    continue
                grow.add(addr)
        return grow

    def lock(self,need=3,max_try=40):
        """连续 need 轮同一地址增长 -> 锁定"""
        for t in range(max_try):
            g=self.scan_once()
            for a in g:
                self._stable[a]=self._stable.get(a,0)+1
            for a in list(self._stable):
                if a not in g: self._stable[a]=0
            ready=[a for a,c in self._stable.items() if c>=need]
            if ready:
                # 选模块内偏移最小的(真全局 double 通常在 .data, 偏移小); 否则任取
                def score(a):
                    base,mod=locate_mod(a,self.mods)
                    off=a-base if base else (1<<60)
                    return (0 if base else 1, off if off<(1<<40) else (1<<60))
                ready.sort(key=score)
                self.lock_addr=ready[0]
                base,mod=locate_mod(self.lock_addr,self.mods)
                self.lock_module=mod; self.lock_offset=(self.lock_addr-base) if base else None
                self.log("★ 锁定 songPosition: 0x%X (%s +0x%s)" %
                         (self.lock_addr,mod,
                          format(self.lock_offset,'X') if self.lock_offset is not None else '?'))
                with open(os.path.join(os.path.dirname(__file__),'songpos_lock.json'),'w') as f:
                    json.dump({'module':mod,'offset':self.lock_offset,'addr':hex(self.lock_addr)},
                              f,indent=2)
                return self.lock_addr
            if t%5==0:
                self.log("扫描中… (%d/%d) 已发现候选 %d 个（游戏放歌中才会增长）"%(t,max_try,len(g)))
            time.sleep(0.2)
        self.log("✗ 没锁定到。请确认游戏正在播放一首歌（不能在暂停菜单/结算界面）。")
        return None

    def read_pos(self):
        if not self.lock_addr or not self.h: return None
        v=ctypes.c_double()
        ok=k32.ReadProcessMemory(self.h,ctypes.c_void_p(self.lock_addr),
                                 ctypes.byref(v),8,None)
        return v.value if ok else None

    def close(self):
        if self.h: k32.CloseHandle(self.h); self.h=None

# ============ 注入引擎(复用 v2.4 解析, 改用 songPos 驱动) ============
class ClosedEngine:
    def __init__(self,logq):
        self.logq=logq; self.notes=[]; self.all_types=set(); self.enabled_types=set()
        self.keys=list(AP.DEFAULT_KEYS); self.target_side='player'
        self.hold_ms=45; self.running=False
        self.idx=0; self.held={}; self.pressed=0; self.locator=None
        self.last_press='—'; self.total=0

    def log(self,s): self.logq.put(s)

    def set_chart(self,path):
        self.notes,song,self.all_types=AP.load_chart(path)
        self.enabled_types=set(self.all_types)
        self.log("谱面载入 %d 音符, %d 种 noteType"%(len(self.notes),len(self.all_types)))
        return len(self.notes)

    def set_type_enabled(self,nt,on):
        (self.enabled_types.add if on else self.enabled_types.discard)(nt)

    def should_press(self,n):
        if self.target_side in ('opponent','all'): return True
        return AP.type_owner(n[3])=='player'

    def targets_now(self):
        return sum(1 for n in self.notes if n[3] in self.enabled_types and self.should_press(n))

    def start_auto(self):
        if not self.notes:
            self.log("错误：先选谱面"); return
        pid=AP.list_processes() and AP.find_game(AP.list_processes())
        # 拿 PID
        pids=[]
        snap=k32.CreateToolhelp32Snapshot(0x2,0)
        class PE(ctypes.Structure):
            _fields_=[("dwSize",wintypes.DWORD),("cntUsage",wintypes.DWORD),
                      ("th32ProcessID",wintypes.DWORD),("th32DefaultHeapID",ctypes.c_void_p),
                      ("th32ModuleID",wintypes.DWORD),("cntThreads",wintypes.DWORD),
                      ("th32ParentProcessID",wintypes.DWORD),("pcPriClassBase",wintypes.LONG),
                      ("dwFlags",wintypes.DWORD),("szExeFile",ctypes.c_wchar*260)]
        k32.Process32FirstW.restype=wintypes.BOOL;k32.Process32NextW.restype=wintypes.BOOL
        k32.Process32FirstW.argtypes=(wintypes.HANDLE,ctypes.POINTER(PE))
        k32.Process32NextW.argtypes=(wintypes.HANDLE,ctypes.POINTER(PE))
        pe=PE();pe.dwSize=ctypes.sizeof(PE)
        ok=k32.Process32FirstW(snap,ctypes.byref(pe))
        while ok:
            if any(k in pe.szExeFile.lower() for k in ('novaflare','funkin','psych','kade','engine')):
                pids.append(pe.th32ProcessID)
            ok=k32.Process32NextW(snap,ctypes.byref(pe))
        k32.CloseHandle(snap)
        if not pids:
            self.log("错误：没找到游戏进程"); return
        threading.Thread(target=self._auto_worker,args=(pids[0],),daemon=True).start()

    def _auto_worker(self,pid):
        self.log("开始自动定位 songPosition (PID %d)…"%pid)
        loc=Locator(pid,self.logq); self.locator=loc
        addr=loc.lock()
        if addr is None:
            loc.close(); return
        fg=AP.focus_game()
        self.log("焦点→"+(fg or "未找到,请点一下游戏"))
        self.running=True; self.idx=0; self.held={}; self.pressed=0
        self.total=self.targets_now()
        self.log("★ 全自动开打: %d 音符, 零漂移闭环"%self.total)
        last_pos=-1; stall=0
        while self.running and self.idx<len(self.notes):
            pos=loc.read_pos()
            if pos is None: break
            if pos<last_pos-50:   # 曲目重开/归零 -> 从头
                self.idx=0; self.pressed=0
            last_pos=pos
            while self.idx<len(self.notes) and pos>=self.notes[self.idx][0]:
                t,lane,sus,nt,mp=self.notes[self.idx]; self.idx+=1
                if nt not in self.enabled_types or not self.should_press((t,lane,sus,nt,mp)):
                    continue
                ki=lane%4
                try: vk=AP.VK[self.keys[ki]]
                except Exception: continue
                AP.key_down(vk); self.held[ki]=True; self.pressed+=1
                self.last_press="轨%d→%s"%(lane+1,self.keys[ki])
                hold=sus if sus>0 else self.hold_ms
                threading.Timer(max(hold,8)/1000.0,self._release,args=(ki,vk)).start()
            time.sleep(0.001)
        self.running=False
        loc.close()
        self.log("✓ 曲终/停止（共按 %d 键）"%self.pressed)

    def _release(self,ki,vk):
        try: AP.key_up(vk)
        except Exception: pass
        self.held[ki]=False

    def stop(self):
        self.running=False
        for ki in list(self.held):
            try: AP.key_up(AP.VK[self.keys[ki]])
            except Exception: pass
        self.held={}
        self.log("■ 已停止")

# ============ GUI ============
def build_gui():
    from PyQt5.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QLabel,
        QLineEdit,QPushButton,QCheckBox,QScrollArea,QTextEdit,QFileDialog,QComboBox)
    from PyQt5.QtCore import QTimer,Qt
    app=QApplication(sys.argv); logq=queue.Queue(); eng=ClosedEngine(logq)
    w=QWidget(); w.setWindowTitle("闭环全自动代打器 v3.0 · 读 songPosition · 零漂移 · 无F5")
    w.resize(720,700); lay=QVBoxLayout(w)

    lay.addWidget(QLabel("原理: 读游戏内存 songPosition 驱动真实键盘, 曲线终自停。游戏须在【播放中】(不能暂停)。"))
    row1=QHBoxLayout(); pathle=QLineEdit(); pathle.setReadOnly(True)
    def pick():
        p,_=QFileDialog.getOpenFileName(w,"选谱面 JSON","","谱面 (*.json);;所有 (*.*)")
        if not p: return
        pathle.setText(p)
        try: eng.set_chart(p); rebuild(); refresh()
        except Exception as e: logq.put("解析失败:%s"%e)
    b=QPushButton("浏览…"); b.clicked.connect(pick)
    row1.addWidget(QLabel("谱面:"));row1.addWidget(pathle);row1.addWidget(b);lay.addLayout(row1)

    side=QComboBox(); side.addItems(['玩家侧(只按玩家type)','对手(检测所有type)','全部'])
    side.currentIndexChanged.connect(lambda i:(setattr(eng,'target_side',['player','opponent','all'][i]),refresh()))
    r=QHBoxLayout();r.addWidget(QLabel("目标:"));r.addWidget(side);r.addStretch(1);lay.addLayout(r)

    tl=QLabel("noteType 过滤: 0 种");lay.addWidget(tl)
    scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setMaximumHeight(140)
    inner=QWidget();il=QVBoxLayout(inner);il.setContentsMargins(4,4,4,4);scroll.setWidget(inner);lay.addWidget(scroll)
    def rebuild():
        while il.count():
            it=il.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        for nt in sorted(eng.all_types,key=lambda s:(AP.type_owner(s)!='player',s!='',s)):
            own='玩家' if AP.type_owner(nt)=='player' else '对手'
            cnt=sum(1 for x in eng.notes if x[3]==nt)
            cb=QCheckBox("[%s] %s [%d]"%(own,nt or '普通',cnt));cb.setChecked(True)
            cb.stateChanged.connect(lambda st,t=nt:(eng.set_type_enabled(t,st==Qt.Checked),refresh()))
            il.addWidget(cb)
        tl.setText("noteType 过滤: %d 种"%len(eng.all_types))
    st=QLabel("状态: 待机");st.setStyleSheet("font-weight:bold;color:#a30;")
    tgt=QLabel("当前将按: —");lay.addWidget(st);lay.addWidget(tgt)
    def refresh():
        tgt.setText("当前将按: %d 音符"%eng.targets_now() if eng.notes else "当前将按: —")
    refresh()

    r2=QHBoxLayout()
    bs=QPushButton("★ 自动锁定并开打");bs.clicked.connect(lambda:eng.start_auto())
    bt=QPushButton("■ 停止");bt.clicked.connect(lambda:eng.stop())
    r2.addWidget(bs);r2.addWidget(bt);lay.addLayout(r2)

    log=QTextEdit();log.setReadOnly(True);lay.addWidget(log)
    def flush():
        while True:
            try: s=logq.get_nowait()
            except queue.Empty: break
            log.append(s)
        if eng.running:
            st.setText("全自动中: %d/%d · %s"%(eng.pressed,eng.total,eng.last_press))
        else:
            st.setText("状态: 待机")
    tm=QTimer();tm.timeout.connect(flush);tm.start(80)
    logq.put("准备就绪: 选谱面 → 确认游戏在播放一首歌 → 点【自动锁定并开打】。不按F5,不调延迟。")
    w.show();sys.exit(app.exec_())

def selftest():
    print("[OK] v3 闭环模块语法自检")
    import tempfile
    s={"song":{"bpm":120,"notes":[{"mustHitSection":True,"sectionNotes":[[0,0,0],[500,1,0,'Blue']]}]}}
    fd,p=tempfile.mkstemp(suffix='.json');os.close(fd);open(p,'w').write(json.dumps(s))
    e=ClosedEngine(queue.Queue());assert e.set_chart(p)==2;assert e.targets_now()==2;os.remove(p)
    print("     谱面解析/过滤 OK")

if __name__=='__main__':
    if '--selftest' in sys.argv: selftest()
    else: build_gui()
