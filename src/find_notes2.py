# -*- coding: utf-8 -*-
"""
Note 群定位器 v2 —— 修正版
----------------------------
v1 的致命 bug:
  1. 只看 top12 vtable, 首名 32万个对象是高频小对象(FlxBasic/Array等), 真 Note 几千个进不了榜
  2. 只采样对象前 96 字节, 但 codename Note 继承链深, strumTime 在 200+ 字节之后
v2 修正:
  * 一次遍历, 对【每个】vtable 候选存档样本地址(不只top12)
  * 对象采样前 640 字节, 偏移搜索到 600
  * 按 "候选数量 100~8000" 优先, 且 strumTime(double) + noteData(int+bool群) 双判据
  * 输出 note_layout.json
只读扫描, 不发按键, 不干扰游戏。
"""
import ctypes, sys, json, os, struct, time
from collections import Counter, defaultdict
import numpy as np
import scan_image as si

k32 = si.k32
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x400000

def all_regions(h, include_image=False):
    addr = 0; mbi = si.MBI()
    types = (MEM_PRIVATE, MEM_MAPPED) + ((MEM_IMAGE,) if include_image else ())
    while addr < 0x7FFFFFFF0000:
        n = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if n == 0: break
        base = mbi.BaseAddress or 0; size = mbi.RegionSize
        if (mbi.State == 0x1000 and mbi.Protect not in (0x01, 0)
                and mbi.Type in types and 0 < size <= 256 * 1024 * 1024):
            yield base, size, mbi.Type
        nxt = base + size
        if nxt <= addr: break
        addr = nxt

def read_bytes(h, base, size):
    buf = ctypes.create_string_buffer(size); got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(got)):
        return None
    return buf.raw[:got.value]

SAMPLE_PER_VT = 24      # 每个 vtable 存多少样本地址
OBJ_BYTES = 640         # 对象采样字节数

def main():
    pid = si.find_pid()
    if not pid:
        print("没找到 NovaFlare 进程"); return
    h = k32.OpenProcess(0x1010, False, pid)
    mods = si.modules(h)
    image_ranges = []
    for base, name in mods:
        if name.lower().endswith(('.exe', '.dll')) or 'ndll' in name.lower():
            image_ranges.append((base, base + 0x10000000))
    print("PID %d, 模块 %d" % (pid, len(mods)))

    regions = list(all_regions(h))
    print("扫描 %d 个可读区 (一次遍历, 给每个 vtable 留样本)..." % len(regions))
    vt_count = Counter()
    vt_sample = defaultdict(list)
    for base, size, ty in regions:
        data = read_bytes(h, base, size)
        if not data: continue
        n = len(data) // 8
        if n == 0: continue
        arr = np.frombuffer(data[:n*8], dtype='<u8', count=n)
        mask = np.zeros(n, dtype=bool)
        for lo, hi in image_ranges:
            mask |= (arr >= lo) & (arr < hi)
        idx = np.nonzero(mask)[0]
        if idx.size == 0: continue
        vals = arr[idx]
        vt_count.update(vals.tolist())
        # 存样本地址 (均匀取, 跨区)
        step = max(1, len(idx) // SAMPLE_PER_VT)
        for j in idx[::step][:SAMPLE_PER_VT]:
            vt = int(vals[np.searchsorted(idx, j)])
            if len(vt_sample[vt]) < SAMPLE_PER_VT:
                vt_sample[vt].append(base + int(j)*8)
    print("vtable 候选总数 %d" % len(vt_count))

    # 分析: 数量在合理区间的候选优先 (Note 群通常 100~8000)
    # 但也不排除其它数量级, 全分析 (候选数可控)
    cands = [(vt, c) for vt, c in vt_count.items() if c >= 60]
    cands.sort(key=lambda x: -(min(x[1], 8000) if x[1] >= 60 else 0))
    print("分析 %d 个候选 (count>=60)..." % len(cands))

    results = []
    diag = []
    for vt, cnt in cands:
        addrs = vt_sample.get(vt, [])
        if len(addrs) < 8: continue
        mem = b''
        for ad in addrs:
            b = read_bytes(h, ad, OBJ_BYTES)
            mem += b if b and len(b) == OBJ_BYTES else b'\x00'*OBJ_BYTES
        blob = np.frombuffer(mem, dtype=np.uint8).reshape(-1, OBJ_BYTES)
        nobjs = blob.shape[0]

        # strumTime: 8字节偏移 double 大量落 (100, 3e6) 且多样
        best_strum = None
        for off in range(8, OBJ_BYTES-8, 8):
            vals = np.array([struct.unpack('<d', blob[i, off:off+8].tobytes())[0] for i in range(nobjs)])
            ok = (vals > 100) & (vals < 3_000_000) & np.isfinite(vals)
            frac = ok.mean()
            if frac < 0.65: continue
            distinct = len(set(vals[ok].astype(np.int64).tolist()))
            if distinct < max(5, nobjs//4): continue
            if best_strum is None or frac > best_strum[1]:
                best_strum = (off, frac, float(np.median(vals[ok])), distinct)

        # noteData: 4字节偏移 int 在 0~200 (多K), 后跟连续 bool(0/1)
        best_nd = None
        for off in range(8, OBJ_BYTES-9, 4):
            ints = np.array([struct.unpack('<i', blob[i, off:off+4].tobytes())[0] for i in range(nobjs)])
            okint = (ints >= 0) & (ints <= 200)
            after = blob[:, off+4:off+9]
            boolfrac = ((after == 0) | (after == 1)).mean()
            if okint.mean() > 0.75 and boolfrac > 0.85 and ints[okint].max() >= 1:
                score = okint.mean() * boolfrac
                if best_nd is None or score > best_nd[1]:
                    best_nd = (off, score, int(ints[okint].min()), int(ints[okint].max()))

        mod, voff = si.locate_mod(vt, mods)
        if best_strum or (cnt <= 12000 and cnt >= 100):
            line = "vt 0x%X x%-6d %s+0x%X" % (vt, cnt, mod, voff)
            if best_strum:
                line += " | strum@+%d(%.0f%% med%.0f d%d)" % (best_strum[0], best_strum[1]*100, best_strum[2], best_strum[3])
            if best_nd:
                line += " nd@+%d(%d-%d)" % (best_nd[0], best_nd[2], best_nd[3])
            print("  " + line)
            diag.append(line)
        if best_strum and best_nd:
            results.append({
                'vtable': hex(vt), 'module': mod, 'vtable_off': hex(voff),
                'count': cnt, 'strum_off': best_strum[0], 'strum_frac': best_strum[1],
                'strum_median': best_strum[2], 'noteData_off': best_nd[0],
                'noteData_max': best_nd[3],
            })

    out = {'pid': pid, 'results': results, 'diag': diag}
    with open(os.path.join(os.path.dirname(__file__), 'note_layout.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n========== 可行布局 %d 个 ==========" % len(results))
    for r in results:
        print("  vt=%s x%d strum@+%d nd@+%d(0-%d) %s" % (
            r['vtable'], r['count'], r['strum_off'], r['noteData_off'], r['noteData_max'], r['module']))
    k32.CloseHandle(h)

if __name__ == '__main__':
    main()
