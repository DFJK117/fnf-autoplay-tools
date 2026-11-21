# -*- coding: utf-8 -*-
"""
Note 群定位器 v3 —— 综合诊断版
一次遍历给每个 vtable 留样本, 降低阈值, 对每个候选:
  - 找 strumTime(double) 偏移 (0~1e7 多样)
  - 用"和弦测试"定位 noteData: 相同 strumTime 的一组对象里, 哪个 int 偏移取值不同 = lane
输出所有可疑布局到 note_layout.json
只读, 不发按键。
"""
import ctypes, json, os, struct
from collections import Counter, defaultdict
import numpy as np
import scan_image as si
from find_notes2 import all_regions, read_bytes

k32 = si.k32
SAMPLE = 64
OBJ = 640

def main():
    pid = si.find_pid()
    if not pid:
        print("没找到 NovaFlare"); return
    h = k32.OpenProcess(0x1010, False, pid)
    mods = si.modules(h)
    img = []
    for base, name in mods:
        if name.lower().endswith(('.exe','.dll')) or 'ndll' in name.lower():
            img.append((base, base+0x10000000))
    print("PID", pid)

    regions = list(all_regions(h))
    vt_count = Counter(); vt_sample = defaultdict(list)
    for base, size, ty in regions:
        data = read_bytes(h, base, size)
        if not data: continue
        n = len(data)//8
        if n==0: continue
        arr = np.frombuffer(data[:n*8], dtype='<u8', count=n)
        mask = np.zeros(n, dtype=bool)
        for lo, hi in img: mask |= (arr>=lo)&(arr<hi)
        idx = np.nonzero(mask)[0]
        if idx.size==0: continue
        vals = arr[idx]; vt_count.update(vals.tolist())
        step = max(1, len(idx)//SAMPLE)
        for j in idx[::step][:SAMPLE]:
            vt = int(vals[np.searchsorted(idx,j)])
            if len(vt_sample[vt])<SAMPLE: vt_sample[vt].append(base+int(j)*8)
    # 排除驱动/系统 dll 的 vtable
    cands=[]
    for vt,c in vt_count.items():
        mod,off = si.locate_mod(vt,mods)
        ml = mod.lower()
        if any(x in ml for x in ('nvogl','nvwgf','nvidia','amd','radeon','intel','dxgi','d3d','ntdll','dwmapi')): continue
        if c>=8: cands.append((vt,c,mod,off))
    cands.sort(key=lambda x:-x[1])
    print("候选(排除驱动) %d, top:"%len(cands))
    for vt,c,mod,off in cands[:15]: print("  x%-6d %s+0x%X"%(c,mod,off))

    results=[]
    for vt,c,mod,off in cands:
        addrs = vt_sample.get(vt,[])
        if len(addrs)<8: continue
        mem=b''
        for ad in addrs:
            b=read_bytes(h,ad,OBJ); mem += b if b and len(b)==OBJ else b'\x00'*OBJ
        blob=np.frombuffer(mem,dtype=np.uint8).reshape(-1,OBJ)
        no=blob.shape[0]
        # strumTime: 8字节偏移 double 多样
        strums=[]
        for so in range(8,OBJ-8,8):
            vals=np.array([struct.unpack('<d',blob[i,so:so+8].tobytes())[0] for i in range(no)])
            ok=(vals>50)&(vals<20_000_000)&np.isfinite(vals)
            if ok.mean()<0.6: continue
            distinct=len(set(vals[ok].astype(np.int64).tolist()))
            if distinct<max(4,no//5): continue
            strums.append((so,ok.mean(),float(np.median(vals[ok])),distinct,vals))
        if not strums: continue
        # 取 frac 最高的 strum 偏移
        strums.sort(key=lambda x:-x[1])
        so,frac,med,dist,vals = strums[0]
        # 和弦测试: 按 strumTime 整数分组, 组内找取值不同的 int 偏移 = noteData
        groups=defaultdict(list)
        for i in range(no):
            if vals[i]>50 and vals[i]<20_000_000 and np.isfinite(vals[i]):
                groups[int(vals[i])].append(i)
        chord_groups=[g for g in groups.values() if len(g)>=2]
        nd_best=None
        if chord_groups:
            for noff in range(so, OBJ-4, 4):
                vary=0; maxv=0
                for g in chord_groups[:40]:
                    vv=[struct.unpack('<i',blob[i,noff:noff+4].tobytes())[0] for i in g]
                    if min(vv)>=0 and max(vv)<40 and max(vv)!=min(vv):
                        vary+=1; maxv=max(maxv,max(vv))
                if vary>=2:
                    if nd_best is None or vary>nd_best[1]: nd_best=(noff,vary,maxv)
        # 全局 noteData 分布 (用候选偏移)
        nd_off = nd_best[0] if nd_best else None
        dist_nd = {}
        if nd_off is not None:
            allnd=[struct.unpack('<i',blob[i,nd_off:nd_off+4].tobytes())[0] for i in range(no) if vals[i]>50]
            dist_nd=dict(sorted(Counter(allnd).items())[:12])
        line="vt=0x%X x%d %s+0x%X | strum@+%d(%.0f%% med%.0f d%d)"%(vt,c,mod,off,so,frac*100,med,dist)
        if nd_best: line+=" nd@+%d(chord%d max%d) %s"%(nd_best[0],nd_best[1],nd_best[2],dist_nd)
        print("  "+line)
        results.append({'vt':hex(vt),'count':c,'module':mod,'strum_off':so,
                        'strum_frac':frac,'strum_median':med,
                        'noteData_off':nd_off,'noteData_chord':nd_best[1] if nd_best else 0,
                        'noteData_dist':dist_nd,'n_chord_groups':len(chord_groups)})
    out={'pid':pid,'results':results}
    with open(os.path.join(os.path.dirname(__file__),'note_layout3.json'),'w',encoding='utf-8') as f:
        json.dump(out,f,ensure_ascii=False,indent=2)
    # 排序: 有 nd 且和弦组多的优先
    results.sort(key=lambda r:-(r['noteData_chord']*1000+r['count'] if r['noteData_off'] is not None else 0))
    print("\n=== 最优候选 ===")
    for r in results[:6]:
        print("  %s x%d strum@+%d nd@+%s 和弦组%d 分布%s"%(r['vt'],r['count'],r['strum_off'],r['noteData_off'],r['n_chord_groups'],r['noteData_dist']))
    k32.CloseHandle(h)

if __name__=='__main__': main()
