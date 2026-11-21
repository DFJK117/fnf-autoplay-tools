# -*- coding: utf-8 -*-
"""用 Conductor 数学不变量定位 songPosition（只读探针）

hxcpp 把 Conductor 的 static Float 编译成连续排布的原生 double：
    bpm, crochet=60000/bpm, stepCrochet=crochet/4, songPosition, ...
所以只要在全进程找满足 crochet*4==stepCrochet 且 bpm*crochet==60000 的
连续 double 三元组，就能锁定结构体，songPosition 就在紧邻位置。

不需要进曲也能定位（bpm/crochet 在加载歌曲时就定好）。
"""
import ctypes, struct, sys, time
from ctypes import wintypes
import numpy as np
import scan_image as si
from find_notes2 import all_regions, read_bytes

u32 = ctypes.windll.user32

def scan_invariant(h, regs, mods):
    hits = []
    for base, size, ty in regs:
        data = read_bytes(h, base, size)
        if not data or len(data) < 32:
            continue
        n = len(data) // 8
        arr = np.frombuffer(data[:n*8], dtype='<f8', count=n)
        if n < 4: continue
        a = arr[:-2]; b = arr[1:-1]; c = arr[2:]
        m = ((a>40)&(a<400)&(b>100)&(b<2000)&
             (np.abs(a*b-60000)<1.0)&(np.abs(b*4-c)<0.5)&
             np.isfinite(a)&np.isfinite(b)&np.isfinite(c))
        idx = np.nonzero(m)[0]
        for i in idx.tolist():
            ad = base + i*8
            mod, off = si.locate_mod(ad, mods)
            hits.append(dict(addr=ad, bpm=float(a[i]), crochet=float(b[i]),
                             step=float(c[i]), mod=mod, off=off,
                             tail=[float(x) for x in arr[i+3:i+9]]))
    return hits

def main():
    pid = int(sys.argv[1]) if len(sys.argv)>1 and sys.argv[1].isdigit() else si.find_pid()
    print('PID', pid)
    h = si.k32.OpenProcess(0x1010, False, pid)
    mods = si.modules(h)
    regs = list(all_regions(h))
    print('区数', len(regs))
    hits = scan_invariant(h, regs, mods)
    print('\n=== Conductor 不变量命中 %d ===' % len(hits))
    for x in hits[:30]:
        print('  0x%X bpm=%.2f crochet=%.3f step=%.3f %s+0x%X tail=%s' %
              (x['addr'], x['bpm'], x['crochet'], x['step'], x['mod'], x['off'],
               ','.join('%.1f' % v for v in x['tail'])))
    # 验证：tail 里哪个 double 在 0.35s 内 1:1 增长 = songPosition
    if hits:
        print('\n=== 验证 tail 增长（0.4s）===')
        for x in hits[:12]:
            base = x['addr'] + 3*8
            addrs = [base + k*8 for k in range(6)]
            def rd():
                out=[]
                for ad in addrs:
                    d=ctypes.c_double()
                    ok=si.k32.ReadProcessMemory(h, ctypes.c_void_p(ad), ctypes.byref(d), 8, None)
                    out.append(d.value if ok else float('nan'))
                return out
            v1=rd(); time.sleep(0.40); v2=rd()
            for k,(p,q) in enumerate(zip(v1,v2)):
                if np.isfinite(p) and np.isfinite(q) and 280 < (q-p) < 520:
                    print('  >>> songPosition 候选 %s+0x%X (Conductor+0x%X) %.1f->%.1f (+%.1f)' %
                          (x['mod'], x['off']+24+k*8, 24+k*8, p, q, q-p))
    si.k32.CloseHandle(h)

if __name__ == '__main__': main()
