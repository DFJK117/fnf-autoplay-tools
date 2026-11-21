# -*- coding: utf-8 -*-
"""v4 全自动内存挂 GUI —— 进曲自动检测内存 Note 群, 自动开打, 支持多K"""
import sys, os, queue
import numpy as np
import autoplay as AP
import memory_bot as MB

def build_gui():
    from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QComboBox, QSpinBox, QTextEdit, QCheckBox)
    from PyQt5.QtCore import QTimer, Qt

    app = QApplication(sys.argv)
    logq = queue.Queue()
    bot = MB.MemoryBot(logq, AP)
    w = QWidget()
    w.setWindowTitle("全自动内存代打器 v4 · 进曲自动检测 · 读 Note 判定窗口 · 多K")
    w.resize(760, 640)
    lay = QVBoxLayout(w)

    lay.addWidget(QLabel(
        "【全自动】启动后等游戏进曲 → 自动扫内存 Note 群 → 自动识别玩家侧 → 自动按。\n"
        "零导谱面、零按F5、零调延迟。多K自动识别(从 noteData 与 strumLine 读键数)。"))

    # 键位模式
    r1 = QHBoxLayout()
    r1.addWidget(QLabel("键位方案:"))
    keymode = QComboBox()
    keymode.addItems(["引擎默认(含空格键 DF[J/K])", "经典4K(DFJK无空格)"])
    def on_mode(i):
        bot.key_mode = 'engine' if i == 0 else 'classic'
        logq.put("键位方案 → " + ("引擎默认" if i == 0 else "经典4K"))
    keymode.currentIndexChanged.connect(on_mode)
    r1.addWidget(keymode)
    r1.addStretch(1)
    lay.addLayout(r1)

    # 多K覆盖
    r2 = QHBoxLayout()
    r2.addWidget(QLabel("强制键数(0=自动):"))
    kspin = QSpinBox(); kspin.setRange(0, 18); kspin.setValue(0)
    def on_k(v):
        if v == 0:
            bot.key_override = None
            logq.put("键数 → 自动识别")
        else:
            keys = bot._keys_for(v)
            bot.key_override = {v: keys}
            logq.put("键数 → 强制 %dK: %s" % (v, " ".join(keys)))
    kspin.valueChanged.connect(on_k)
    r2.addWidget(kspin)
    r2.addWidget(QLabel("(FOJC 13K/18K 等如识别不准, 手动指定)"))
    r2.addStretch(1)
    lay.addLayout(r2)

    # 按键保持时长
    r3 = QHBoxLayout()
    r3.addWidget(QLabel("按键保持(ms):"))
    hspin = QSpinBox(); hspin.setRange(8, 200); hspin.setValue(45)
    hspin.valueChanged.connect(lambda v: setattr(bot, 'hold_ms', v))
    r3.addWidget(hspin)
    r3.addStretch(1)
    lay.addLayout(r3)

    # 按钮
    rb = QHBoxLayout()
    bstart = QPushButton("▶ 启动全自动(等待进曲)")
    bstop = QPushButton("■ 停止")
    breset = QPushButton("↻ 重置计数")
    def do_start():
        bot.reset()
        bot.start()
        bstart.setEnabled(False)
    def do_stop():
        bot.stop()
        bstart.setEnabled(True)
    bstart.clicked.connect(do_start)
    bstop.clicked.connect(do_stop)
    breset.clicked.connect(lambda: (bot.reset(), logq.put("计数已重置")))
    rb.addWidget(bstart); rb.addWidget(bstop); rb.addWidget(breset)
    lay.addLayout(rb)

    status = QLabel("状态: 待机")
    status.setStyleSheet("font-weight:bold;color:#a30;")
    lay.addWidget(status)

    log = QTextEdit(); log.setReadOnly(True)
    lay.addWidget(log)

    def flush():
        while True:
            try:
                s = logq.get_nowait()
            except queue.Empty:
                break
            log.append(s)
        if bot.running:
            status.setText("全自动中: 已按 %d 键 · %s · 键数%d · %s" %
                           (bot.pressed, bot.last_press, bot.kcount,
                            ("玩家strum%d条" % len(bot.scanner.cpu_strums)) if bot.scanner else ""))
        else:
            status.setText("状态: 待机")

    tm = QTimer(); tm.timeout.connect(flush); tm.start(80)
    logq.put("准备就绪: 打开游戏并进一首歌 → 点【启动全自动】。")
    logq.put("说明: 直接读引擎算好的 canBeHit 判定窗口, 与真人按键走同一判定, 非 botplay/非回放。")
    w.show()
    sys.exit(app.exec_())


def selftest():
    print("[OK] v4 内存挂语法自检")
    from collections import Counter
    # 键位表自检: 键数索引 = 实际 lane 数
    for k in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 13, 18]:
        keys = MB.MemoryBot(queue.Queue(), AP)._keys_for(k)
        assert len(keys) >= k, "键数不足: %d lane -> %d keys" % (k, len(keys))
        print("  %2d lane: %s" % (k, " ".join(keys[:k])))
    # 布局推断逻辑自检 (用合成对象 blob)
    import struct
    nobjs = 100
    blob = bytearray(nobjs * 96)
    for i in range(nobjs):
        base = i * 96
        struct.pack_into('<d', blob, base + 16, 1000.0 + i * 500)   # strumTime @+16
        struct.pack_into('<Q', blob, base + 24, 0x140000000000 + i % 2)  # strumLine ptr @+24
        struct.pack_into('<i', blob, base + 48, i % 4)               # noteData @+48
        blob[base + 52] = 0   # canHit
        blob[base + 54] = 0   # wasGood
    arr = np.frombuffer(bytes(blob), dtype=np.uint8).reshape(-1, 96)
    # strum 检测
    st_off = None
    for off in range(0, 80, 8):
        vals = np.array([struct.unpack('<d', arr[i, off:off+8].tobytes())[0] for i in range(nobjs)])
        ok = (vals > 100) & (vals < 3_000_000) & np.isfinite(vals)
        if ok.mean() > 0.65 and len(set(vals[ok].astype(np.int64))) > 15:
            st_off = off; break
    assert st_off == 16, "strum 偏移推断错误: %s" % st_off
    print("  布局推断自检: strumTime@+%d OK" % st_off)
    print("全部自检通过")


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        selftest()
    else:
        build_gui()
