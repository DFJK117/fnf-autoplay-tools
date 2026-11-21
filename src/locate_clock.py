# -*- coding: utf-8 -*-
"""三重校验定位 songPosition：播放增长 -> 暂停停走 -> 恢复续走。
显卡驱动计时器(nvogl)不会因游戏暂停停走，据此排除。只读探针。
"""
import ctypes, time, sys
from ctypes import wintypes
import numpy as np
import scan_image as si
from find_notes2 import all_regions, read_bytes

u = ctypes.windll.user32
u.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, wintypes.UINT)

HWND = None
def find_game():
    global HWND
    hits = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        n = u.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1); u.GetWindowTextW(h, b, n + 1)
            if 'NovaFlare' in b.value: hits.append(h)
        return True
    u.EnumWindows(CB(cb), 0)
    HWND = hits[0] if hits else None
    return HWND

def topmost(on=True):
    if not HWND: return
    # HWND_TOPMOST=-1, HWND_NOTOPMOST=-2 ; SWP_NOMOVE|SWP_NOSIZE|SWP_SHOWWINDOW
    u.SetWindowPos(HWND, wintypes.HWND(-1 if on else -2), 0, 0, 0, 0, 0x0001|0x0002|0x0040)
    if on:
        u.SetForegroundWindow(HWND)
    time.sleep(0.25)

def key(vk):
    u.keybd_event(vk, 0, 0, 0); time.sleep(0.05); u.keybd_event(vk, 0, 2, 0)

def snap(h, regs):
    addrs = []; vals = []
    for base, size, ty in regs:
        d = read_bytes(h, base, size)
        if not d or len(d) < 8: continue
        n = len(d)//8
        a = np.frombuffer(d[:n*8], dtype='<f8', count=n)
        m = (a > 100) & (a < 3_000_000) & np.isfinite(a)
        idx = np.nonzero(m)[0]
        if idx.size:
            addrs.append(base + idx.astype(np.uint64)*8); vals.append(a[idx])
    if not addrs:
        return np.array([], dtype=np.uint64), np.array([])
    return np.concatenate(addrs), np.concatenate(vals)

def readat(h, addrs):
    one = ctypes.c_double(); out = np.empty(len(addrs))
    for i, ad in enumerate(addrs):
        ok = si.k32.ReadProcessMemory(h, ctypes.c_void_p(int(ad)), ctypes.byref(one), 8, None)
        out[i] = one.value if ok else np.nan
    return out

def main():
    if not find_game(): print('没找到游戏窗口'); return
    pid = si.find_pid(); print('PID', pid, 'hwnd', hex(HWND))
    h = si.k32.OpenProcess(0x1010, False, pid)
    mods = si.modules(h)
    regs = list(all_regions(h))
    print('区数', len(regs))

    # 阶段1：确保在播放（游戏若暂停，先按ESC恢复）
    topmost(True)
    key(0x1B); time.sleep(0.6)   # 若暂停则恢复；若在播放则暂停 -> 后面统一处理
    key(0x1B); time.sleep(0.6)   # 再按一次回到播放态（两次抵消）
    print('已进入（ presumed 播放态）')

    # 快照A -> B（播放，应增长）
    a1, v1 = snap(h, regs)
    print('快照double数', len(v1))
    time.sleep(0.45); v2 = readat(h, a1)
    d_play = v2 - v1
    growing = (d_play > 380) & (d_play < 520) & np.isfinite(d_play)
    gaddr = a1[growing]
    v2g = v2[growing]
    v1g = v1[growing]
    print('播放增长候选', len(gaddr))
    for ad, p, q in zip(gaddr, v1g, v2g):
        mod, off = si.locate_mod(int(ad), mods)
        print('  播放增长 0x%X %s+0x%X %.1f->%.1f (+%.1f)' % (ad, mod, off, p, q, q-p))

    # 暂停 -> 快照C（应停走）
    key(0x1B); time.sleep(0.5)
    v3 = readat(h, gaddr)
    frozen = (np.abs(v3 - v2g) < 15) & np.isfinite(v3)
    print('暂停停走候选', int(frozen.sum()))
    cand = gaddr[frozen]
    vc = v3[frozen]

    # 恢复 -> 快照D（应续走）
    key(0x1B); time.sleep(0.5)
    v4 = readat(h, cand)
    resume = (v4 - vc > 380) & (v4 - vc < 520) & np.isfinite(v4)
    print('恢复续走候选', int(resume.sum()))
    final = cand[resume]; vf = v4[resume]
    vc2 = vc[resume]

    print('\n=== 三重锁定 songPosition ===')
    out = []
    for ad, p, q in zip(final, vc2, vf):
        mod, off = si.locate_mod(int(ad), mods)
        out.append((int(ad), mod, off, float(p), float(q)))
    out.sort(key=lambda x: x[0])
    for ad, mod, off, p, q in out[:40]:
        print('  0x%X  %-28s +0x%-8X  %.1f -> %.1f' % (ad, mod, off, p, q))
    with open('I:/_trash_ust_work/fnf_autoplay_logs/clock_candidates.txt', 'w') as f:
        for ad, mod, off, p, q in out:
            f.write('0x%X %s +0x%X %.1f %.1f\n' % (ad, mod, off, p, q))
    print('\n已写入 clock_candidates.txt', len(out))
    si.k32.CloseHandle(h)

if __name__ == '__main__': main()
