# -*- coding: utf-8 -*-
"""
songPosition 稳定地址扫描器 —— 聚焦 exe/dll 映像区 (MEM_IMAGE)
--------------------------------------------------------------
hxcpp 里 Conductor.songPosition 若为 static Float，通常编译成原生 double 全局变量，
落在 exe/dll 的 .data/.bss 段 -> 地址 = 模块基址 + 固定偏移，重开游戏不变 = 可直接写死。

三轮采样：只有连续两轮都按 1:1(±12ms) 增长的地址才保留，排除音频缓冲/帧计时噪声。
输出同时给绝对地址和「模块名 + 偏移」，偏移才是要记进注入器的稳定值。

用法:
  python scan_image.py            # 自动找 NovaFlare 进程
  python scan_image.py <pid>
"""
import ctypes, sys, time
import numpy as np
from ctypes import wintypes

k32 = ctypes.windll.kernel32
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_IMAGE = 0x400000
PAGE_NOACCESS = 0x01

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

class PE32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

def find_pid(key='novaflare'):
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    k32.Process32FirstW.restype = wintypes.BOOL; k32.Process32NextW.restype = wintypes.BOOL
    k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PE32))
    k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PE32))
    pe = PE32(); pe.dwSize = ctypes.sizeof(PE32)
    ok = k32.Process32FirstW(snap, ctypes.byref(pe))
    while ok:
        if key in pe.szExeFile.lower():
            k32.CloseHandle(snap); return pe.th32ProcessID
        ok = k32.Process32NextW(snap, ctypes.byref(pe))
    k32.CloseHandle(snap); return None

def modules(h):
    """用 EnumProcessModulesEx 拿 (base,size,name) —— 需要 psapi"""
    psapi = ctypes.windll.psapi
    HMOD = wintypes.HMODULE
    psapi.EnumProcessModulesEx.argtypes = (wintypes.HANDLE, ctypes.POINTER(HMOD),
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
    psapi.GetModuleBaseNameW.argtypes = (wintypes.HANDLE, HMOD, ctypes.c_wchar_p, wintypes.DWORD)
    mods = (HMOD * 512)(); need = wintypes.DWORD()
    psapi.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods), ctypes.byref(need), 0x03)
    out = []
    for i in range(need.value // ctypes.sizeof(HMOD)):
        base = int(mods[i])
        buf = ctypes.create_unicode_buffer(260)
        psapi.GetModuleBaseNameW(h, mods[i], buf, 260)
        out.append((base, buf.value))
    return out

def locate_mod(addr, mods):
    best = None
    for base, name in mods:
        if base <= addr and (best is None or base > best[0]):
            best = (base, name)
    if best:
        return best[1], addr - best[0]
    return "?", addr

def image_regions(h):
    addr = 0; mbi = MBI()
    while addr < 0x7FFFFFFF0000:
        n = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if n == 0: break
        base = mbi.BaseAddress or 0; size = mbi.RegionSize
        if (mbi.State == MEM_COMMIT and mbi.Type == MEM_IMAGE
                and mbi.Protect not in (PAGE_NOACCESS, 0) and 0 < size < 64 * 1024 * 1024):
            yield base, size
        nxt = base + size
        if nxt <= addr: break
        addr = nxt

def read_doubles(h, lo=0.0, hi=1e7):
    addrs_l, vals_l = [], []
    for base, size in image_regions(h):
        buf = ctypes.create_string_buffer(size); got = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(got)):
            continue
        data = buf.raw[:got.value]; n = len(data) // 8
        if n == 0: continue
        arr = np.frombuffer(data, dtype='<f8', count=n)
        mask = (arr > lo) & (arr < hi) & np.isfinite(arr)
        idx = np.nonzero(mask)[0]
        if idx.size:
            addrs_l.append(base + idx.astype(np.uint64) * 8); vals_l.append(arr[idx])
    if not addrs_l:
        return np.array([], dtype=np.uint64), np.array([], dtype=np.float64)
    return np.concatenate(addrs_l), np.concatenate(vals_l)

def read_at(h, addrs):
    one = ctypes.c_double(); out = []
    for ad in addrs:
        ok = k32.ReadProcessMemory(h, ctypes.c_void_p(int(ad)), ctypes.byref(one), 8, None)
        out.append(one.value if ok else float('nan'))
    return np.array(out, dtype=np.float64)

def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].lstrip('-').isdigit() else find_pid()
    if not pid:
        print("没找到 NovaFlare 进程，请先开游戏并进入一首歌。"); return
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        print("OpenProcess 失败", k32.GetLastError()); return
    mods = modules(h)
    print("模块数", len(mods), " PID", pid)

    addrs, a = read_doubles(h)
    print("映像区 double 候选 %d 个" % len(a))
    if len(a) == 0:
        k32.CloseHandle(h); return

    wait = 0.4
    time.sleep(wait); wall1 = wait * 1000
    b = read_at(h, addrs)
    time.sleep(wait); wall2 = wait * 1000
    c = read_at(h, addrs)

    d1, d2 = b - a, c - b
    tol = 12.0
    # 连续两轮都按 1:1 增长；首值>50 排除0附近噪声
    mask = (d1 > 0) & (np.abs(d1 - wall1) < tol) & (d2 > 0) & (np.abs(d2 - wall2) < tol) & (a > 50)
    idx = np.nonzero(mask)[0]
    order = idx[np.argsort(np.abs(d1[idx] - wall1) + np.abs(d2[idx] - wall2))]
    print("\n=== 稳定增长的映像区 double (连续两轮≈+%.0fms, 容差±%.0fms) ===" % (wall1, tol))
    if len(order) == 0:
        print("  映像区没找到。说明 songPosition 是 boxed 对象在 GC 堆里 -> 需要指针链方案。")
    for j in order[:40]:
        mod, off = locate_mod(int(addrs[j]), mods)
        print("  0x%X  %-24s +0x%X   %.1f -> %.1f -> %.1f  (+%.0f/+%.0f)" %
              (addrs[j], mod, off, a[j], b[j], c[j], d1[j], d2[j]))
    k32.CloseHandle(h)

if __name__ == '__main__':
    main()
