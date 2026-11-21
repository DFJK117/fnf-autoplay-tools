# -*- coding: utf-8 -*-
"""
Note 对象群定位器 —— 纯内存扫描, 不硬编码字段偏移
--------------------------------------------------
原理:
  * hxcpp 非压缩 GC, Note 对象地址在生命周期内稳定
  * 每个 Note 对象头 8 字节 = 同一个 vtable 指针 (指向 exe/lime.ndll 映像区)
  * 一首歌几百上千 Note -> 该 vtable 值在堆里出现几百上千次 = 强特征
  * 找到 vtable 群后, 分析对象内容, 自动推断:
      - strumTime 偏移 (double, 值在 0~曲目时长)
      - noteData 偏移 (int 0~N, 后跟连续 canBeHit/tooLate/wasGoodHit bool)

输出 note_layout.json: {vtable, vtable_off, strum_off, noteData_off, canHit_off, count}
"""
import ctypes, sys, json, os, struct
import numpy as np
from ctypes import wintypes
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

def is_image_ptr(v, image_ranges):
    for lo, hi in image_ranges:
        if lo <= v < hi:
            return True
    return False

def main():
    pid = si.find_pid()
    if not pid:
        print("没找到 NovaFlare 进程"); return
    h = k32.OpenProcess(0x1010, False, pid)
    mods = si.modules(h)
    # 映像地址范围 (vtable 指向这里)
    image_ranges = []
    for base, name in mods:
        if name.lower().endswith('.exe') or name.lower().endswith('.dll') or 'ndll' in name.lower():
            image_ranges.append((base, base + 0x10000000))
    print("PID %d, 模块 %d" % (pid, len(mods)))

    # 第一遍: 统计所有 8字节对齐 qword 中指向映像区的值 (= vtable 候选)
    from collections import Counter
    vt_count = Counter()
    regions = list(all_regions(h))
    print("扫描 %d 个可读区, 统计 vtable 候选..." % len(regions))
    for base, size, ty in regions:
        data = read_bytes(h, base, size)
        if not data: continue
        n = len(data) // 8
        if n == 0: continue
        arr = np.frombuffer(data[:n*8], dtype='<u8', count=n)
        # 向量化: 落在任一映像范围
        mask = np.zeros(n, dtype=bool)
        for lo, hi in image_ranges:
            mask |= (arr >= lo) & (arr < hi)
        vals = arr[mask]
        if vals.size:
            vt_count.update(vals.tolist())

    print("\n=== vtable 候选 top 20 (出现次数) ===")
    top = vt_count.most_common(20)
    for vt, c in top:
        mod, off = si.locate_mod(vt, mods)
        print("  0x%X  x%-5d  %s +0x%X" % (vt, c, mod, off))

    # 第二遍: 对每个高频 vtable, 收集对象地址并分析字段偏移
    # 只分析 top 12 中出现>=20次的
    cands = [(vt, c) for vt, c in top if c >= 20][:12]
    print("\n=== 分析对象字段布局 ===")
    results = []
    for vt, cnt in cands:
        addrs = []
        for base, size, ty in regions:
            data = read_bytes(h, base, size)
            if not data: continue
            n = len(data)//8
            if n == 0: continue
            arr = np.frombuffer(data[:n*8], dtype='<u8', count=n)
            idx = np.nonzero(arr == vt)[0]
            if idx.size:
                addrs.extend([base + int(j)*8 for j in idx])
        if len(addrs) < 20: continue
        # 采样对象前 256 字节, 找 strumTime (double) 和 noteData(int)+bool 群
        sample = addrs[:400]
        mem = b''
        for ad in sample:
            b = read_bytes(h, ad, 96)
            mem += b if b else b'\x00'*96
        blob = np.frombuffer(mem, dtype=np.uint8).reshape(-1, 96)
        nobjs = blob.shape[0]

        # 找 strumTime: 某 8字节偏移, double 值大量落在 (100, 2000000) 且彼此不同
        best_strum = None
        for off in range(0, 80, 8):
            vals = np.array([struct.unpack('<d', blob[i, off:off+8].tobytes())[0] for i in range(nobjs)])
            ok = (vals > 100) & (vals < 2_000_000) & np.isfinite(vals)
            frac = ok.mean()
            distinct = len(set(vals[ok].astype(np.int64).tolist()))
            if frac > 0.7 and distinct > 20:
                if best_strum is None or frac > best_strum[1]:
                    best_strum = (off, frac, float(np.median(vals[ok])), distinct)

        # 找 noteData: 某 4字节偏移 int 在 0~30, 后跟 3~5 个连续 bool(0/1)
        best_nd = None
        for off in range(4, 80, 4):
            ints = np.array([struct.unpack('<i', blob[i, off:off+4].tobytes())[0] for i in range(nobjs)])
            okint = (ints >= 0) & (ints <= 30)
            # 后面 8 字节里 bool 连续性
            after = blob[:, off+4:off+9]
            boolfrac = ((after == 0) | (after == 1)).mean()
            if okint.mean() > 0.8 and boolfrac > 0.9 and ints[okint].max() >= 1:
                score = okint.mean() * boolfrac
                if best_nd is None or score > best_nd[1]:
                    best_nd = (off, score, int(ints.min()), int(ints[okint].max()))

        mod, voff = si.locate_mod(vt, mods)
        line = "vtable 0x%X x%d %s+0x%X" % (vt, cnt, mod, voff)
        if best_strum:
            line += " | strum@+%d(%.0f%% med%.0f)" % (best_strum[0], best_strum[1]*100, best_strum[2])
        if best_nd:
            line += " noteData@+%d range%d-%d" % (best_nd[0], best_nd[2], best_nd[3])
        print("  " + line)
        if best_strum and best_nd:
            results.append({
                'vtable': hex(vt), 'vtable_module': mod, 'vtable_off': hex(voff),
                'count': cnt, 'strum_off': best_strum[0],
                'noteData_off': best_nd[0], 'noteData_max': best_nd[3],
                'strum_median': best_strum[2],
            })

    out = {'pid': pid, 'results': results}
    with open(os.path.join(os.path.dirname(__file__), 'note_layout.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n可行布局候选 %d 个 -> note_layout.json" % len(results))
    k32.CloseHandle(h)

if __name__ == '__main__':
    main()
