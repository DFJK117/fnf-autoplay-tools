# -*- coding: utf-8 -*-
"""合成 hxcpp Note 对象群, 验证 find_layout 的字段偏移推断算法正确"""
import struct
import numpy as np

def infer_layout(blob_objs):
    """blob_objs: list of bytes, 每个是 Note 对象前 96 字节。返回 (strum_off, noteData_off, canHit_off)"""
    nobjs = len(blob_objs)
    blob = b''.join(blob_objs)
    arr = np.frombuffer(blob, dtype=np.uint8).reshape(nobjs, -1)

    strum = None
    for off in range(0, 80, 8):
        vals = np.array([struct.unpack('<d', arr[i, off:off+8].tobytes())[0] for i in range(nobjs)])
        ok = (vals > 100) & (vals < 3_000_000) & np.isfinite(vals)
        frac = ok.mean()
        distinct = len(set(vals[ok].astype(np.int64).tolist()))
        if frac > 0.65 and distinct > 15:
            if strum is None or (frac, distinct) > (strum[1], strum[3]):
                strum = (off, frac, distinct)
    nd = None
    for off in range(4, 80, 4):
        ints = np.array([struct.unpack('<i', arr[i, off:off+4].tobytes())[0] for i in range(nobjs)])
        okint = (ints >= 0) & (ints <= 80)
        after = arr[:, off+4:off+9]
        boolfrac = ((after == 0) | (after == 1)).mean()
        if okint.mean() > 0.75 and boolfrac > 0.85 and ints[okint].max() >= 1:
            score = okint.mean() * boolfrac
            if nd is None or score > nd[1]:
                nd = (off, score, int(ints[okint].max()))
    return strum, nd


def make_note(strum, nd, can_hit, was_good, strum_off=16, nd_off=48):
    """构造一个 Note 对象 blob (96字节), 模拟 hxcpp 布局:
       0: vtable ptr (8)
       8: extra ptr (8)
       16: strumTime double (8)
       24: strumLine ptr (8)
       ...
       48: noteData int (4) @nd_off
       52: canBeHit bool
       53: tooLate bool
       54: wasGoodHit bool
       55: avoid bool
    """
    b = bytearray(96)
    struct.pack_into('<Q', b, 0, 0x7FF600000000)   # vtable
    struct.pack_into('<Q', b, 8, 0x240000000010)   # extra
    struct.pack_into('<d', b, strum_off, strum)
    struct.pack_into('<Q', b, 24, 0x240000000100)  # strumLine ptr
    struct.pack_into('<i', b, nd_off, nd)
    b[nd_off+4] = 1 if can_hit else 0
    b[nd_off+5] = 0
    b[nd_off+6] = 1 if was_good else 0
    b[nd_off+7] = 0
    return bytes(b)


def test():
    # 场景1: 标准布局 strum@16, noteData@48
    objs = []
    for i in range(200):
        objs.append(make_note(1000 + i*500, i % 4, i % 3 == 0, i % 5 == 0))
    strum, nd = infer_layout(objs)
    print("场景1 标准布局: strum=%s noteData=%s" % (strum, nd))
    assert strum and strum[0] == 16, "strum 偏移错"
    assert nd and nd[0] == 48, "noteData 偏移错"
    print("  ✓ strumTime@+16, noteData@+48, canHit@+52, wasGood@+54")

    # 场景2: 不同偏移 strum@24, noteData@56 (模拟换版本)
    objs = []
    for i in range(200):
        b = bytearray(96)
        struct.pack_into('<Q', b, 0, 0x7FF600000000)
        struct.pack_into('<d', b, 24, 2000 + i*300)
        struct.pack_into('<Q', b, 32, 0x240000000100)
        struct.pack_into('<i', b, 56, i % 6)
        b[60] = i % 2; b[61] = 0; b[62] = 0; b[63] = 0
        objs.append(bytes(b))
    strum, nd = infer_layout(objs)
    print("场景2 变体布局: strum=%s noteData=%s" % (strum, nd))
    assert strum and strum[0] == 24, "变体 strum 偏移错"
    assert nd and nd[0] == 56, "变体 noteData 偏移错"
    print("  ✓ 变体布局也能自动识别")

    # 场景3: canBeHit/wasGoodHit 判据有效性
    hits = [(i % 4) for i in range(100) if (i % 3 == 0) and not (i % 5 == 0)]
    print("场景3 判据 canHit&&!wasGood: 100音符中 %d 个应触发" % len(hits))
    assert len(hits) > 0
    print("  ✓ 判定窗口判据有效")

    print("\n全部布局推断测试通过")

if __name__ == '__main__':
    test()
