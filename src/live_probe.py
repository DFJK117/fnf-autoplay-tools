# -*- coding: utf-8 -*-
"""播放态综合探针: 切前台发ESC恢复 -> 后台读内存
  A) 双快照找 1:1 增长的 double = songPosition (证明在播放)
  B) 在真实播放态扫 Note 群 vtable + 字段布局 (strumTime/noteData/canBeHit)
只读, 除 ESC 恢复外不注入任何游戏按键。
"""
import ctypes, time, struct
from ctypes import wintypes
from collections import Counter, defaultdict
import numpy as np
import scan_image as si
from find_notes2 import all_regions, read_bytes

u = ctypes.windll.user32
k32 = si.k32

def focus_and_resume():
    hits=[]
    WND=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    def cb(h,l):
        n=u.GetWindowTextLengthW(h)
        if n:
            b=ctypes.create_unicode_buffer(n+1); u.GetWindowTextW(h,b,n+1)
            if b.value.strip()=='NovaFlare Engine': hits.append(h)
        return True
    u.EnumWindows(WND(cb),0)
    if not hits: print('没找到游戏窗口'); return False
    hw=hits[0]
    for _ in range(3):
        u.ShowWindow(hw,9)
        u.SetForegroundWindow(hw)
        time.sleep(0.2)
    fg=u.GetForegroundWindow()
    print('前台窗口=',hex(fg),'游戏=',hex(hw),'匹配' if fg==hw else '不匹配(仍可能被抢)')
    u.keybd_event(0x1B,0,0,0); time.sleep(0.06); u.keybd_event(0x1B,0,2,0)
    print('已发 ESC')
    return True

def snapshot_doubles(h, regs):
    vals=[]; addrs=[]
    for base,size,ty in regs:
        data=read_bytes(h,base,size)
        if not data: continue
        n=len(data)//8
        if n==0: continue
        arr=np.frombuffer(data[:n*8],dtype='<f8',count=n)
        # 只保留合理时钟值
        m=(arr>100)&(arr<2_000_000)&np.isfinite(arr)
        idx=np.nonzero(m)[0]
        for j in idx:
            addrs.append(base+int(j)*8); vals.append(float(arr[j]))
    return np.array(addrs,dtype=np.uint64), np.array(vals)

def find_growing_clock(h, regs):
    a1,v1=snapshot_doubles(h,regs)
    time.sleep(0.35)
    a2,v2=snapshot_doubles(h,regs)
    # 同地址匹配
    d1={int(x):float(y) for x,y in zip(a1,v1)}
    d2={int(x):float(y) for x,y in zip(a2,v2)}
    common=set(d1)&set(d2)
    wall=350.0
    grow=[(ad,d1[ad],d2[ad],d2[ad]-d1[ad]) for ad in common if 0.7*wall<(d2[ad]-d1[ad])<1.3*wall]
    grow.sort(key=lambda x:-abs(x[3]-wall))
    return grow

def main():
    focus_and_resume()
    time.sleep(1.0)
    pid=si.find_pid()
    print('PID',pid)
    h=k32.OpenProcess(0x1010,False,pid)
    mods=si.modules(h)
    img=[]
    for base,name in mods:
        if name.lower().endswith(('.exe','.dll')) or 'ndll' in name.lower():
            img.append((base,base+0x10000000))
    regs=list(all_regions(h))
    print('区数',len(regs))

    print('\n=== A) 找增长时钟 songPosition ===')
    grow=find_growing_clock(h,regs)
    print('1:1增长 double 数:',len(grow))
    for ad,v1,v2,d in grow[:8]:
        mod,off=si.locate_mod(ad,mods)
        print('  0x%X %.1f->%.1f (+%.1f) %s+0x%X'%(ad,v1,v2,d,mod,off))

    print('\n=== B) 播放态扫 Note 群 ===')
    vt_count=Counter(); vt_sample=defaultdict(list)
    for base,size,ty in regs:
        data=read_bytes(h,base,size)
        if not data: continue
        n=len(data)//8
        if n==0: continue
        arr=np.frombuffer(data[:n*8],dtype='<u8',count=n)
        mask=np.zeros(n,dtype=bool)
        for lo,hi in img: mask|=(arr>=lo)&(arr<hi)
        idx=np.nonzero(mask)[0]
        if idx.size==0: continue
        vals=arr[idx]; vt_count.update(vals.tolist())
        step=max(1,len(idx)//48)
        for j in idx[::step][:48]:
            vt=int(vals[np.searchsorted(idx,j)])
            if len(vt_sample[vt])<48: vt_sample[vt].append(base+int(j)*8)
    # 找 Note: 数量100~8000, 排除驱动, 采样640字节, strumTime在深层
    found=[]
    for vt,c in vt_count.items():
        if not (100<=c<=8000): continue
        mod,off=si.locate_mod(vt,mods)
        if any(x in mod.lower() for x in ('nvogl','nvwgf','nvidia','amd','radeon','intel','dxgi','d3d','ntdll','dwmapi')): continue
        addrs=vt_sample.get(vt,[])
        if len(addrs)<8: continue
        mem=b''
        for ad in addrs:
            b=read_bytes(h,ad,640); mem+=b if b and len(b)==640 else b'\x00'*640
        blob=np.frombuffer(mem,dtype=np.uint8).reshape(-1,640)
        best=None
        for so in range(120,600,8):
            st=np.array([struct.unpack('<d',blob[i,so:so+8].tobytes())[0] for i in range(len(addrs))])
            ok=(st>100)&(st<3_000_000)&np.isfinite(st)
            if ok.mean()<0.7: continue
            distinct=len(set(st[ok].astype(np.int64).tolist()))
            if distinct<max(5,len(addrs)//4): continue
            if best is None or ok.mean()>best[1]: best=(so,ok.mean(),float(np.median(st[ok])),distinct)
        if best:
            # noteData: strumTime 后 8~40 字节内的 int, 后跟 bool 群
            nd=None
            for noff in range(best[0]+8,best[0]+48,4):
                ints=np.array([struct.unpack('<i',blob[i,noff:noff+4].tobytes())[0] for i in range(len(addrs))])
                okint=(ints>=0)&(ints<=40)
                after=blob[:,noff+4:noff+9]
                bf=((after==0)|(after==1)).mean()
                if okint.mean()>0.8 and bf>0.9 and ints[okint].max()>=1:
                    nd=(noff,okint.mean(),int(ints[okint].max()))
            print('  vt=0x%X x%d %s+0x%X strum@+%d(%.0f%% med%.0f) nd@+%s'%(
                vt,c,mod,off,best[0],best[1]*100,best[2],
                ('%d max%d'%(nd[0],nd[2])) if nd else '?'))
            found.append((vt,c,best,nd))
    print('\nNote候选数:',len(found))
    k32.CloseHandle(h)

if __name__=='__main__': main()
