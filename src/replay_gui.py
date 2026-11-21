# -*- coding: utf-8 -*-
"""
全自动 Replay 生成器 GUI —— 谱面 -> perfect replay(.rsd)
=============================================================
原理: NF 原生 replay = AES 加密 JSON 的逐帧按键序列。本工具读谱面,
为每个音符在正确时刻生成 press/release 键帧 + sick 判定, 用真实 replay
作 stateRecord 模板(对齐曲目参数/键位), 加密写出 .rsd+.txt。
游戏内回放 = 引擎把按键注入真实输入层(keyPressed/keyReleased/goodNoteHit),
走和真人一模一样的判定管线 —— 纯真人模拟, 非 botplay, 非外部注入。

两档:
  【全自动】本工具生成的 perfect replay —— 不用导谱面到程序、不用按F5、
            不用调延迟、不用开着外挂。生成后放进游戏 replay 目录,
            在游戏里像回放普通 replay 一样选它即可, 全程引擎自己跑。
  【半自动】闭环代打器 v3 —— 读游戏 songPosition 实时注入真键盘,
            当场打、不用生成文件, 但需要游戏处于播放态且扫一次地址。
"""
import sys, os, json, glob, queue, threading, traceback
import replay_tool as RT

GAME_ROOT = RT.GAME_ROOT
MODS_DATA = os.path.join(GAME_ROOT, 'mods')
REPLAYS = os.path.join(GAME_ROOT, 'replays')
DEFAULT_OUT = r'E:\UST_work\fnf_autoplay\replay_output'


def scan_mods():
    """扫游戏目录: 找所有 mod 及其谱面(json)和可用 replay 模板(.rsd)"""
    mods = {}
    if not os.path.isdir(MODS_DATA):
        return mods
    for entry in os.listdir(MODS_DATA):
        mdir = os.path.join(MODS_DATA, entry)
        if not os.path.isdir(mdir):
            continue
        charts = glob.glob(os.path.join(mdir, 'data', '**', '*.json'), recursive=True)
        charts = [c for c in charts if 'events' not in os.path.basename(c).lower()
                  and 'cutscene' not in os.path.basename(c).lower()]
        if not charts:
            continue
        templates = glob.glob(os.path.join(REPLAYS, entry, '**', '*.rsd'), recursive=True)
        mods[entry] = {'charts': sorted(charts), 'templates': sorted(templates)}
    return mods


def build_gui():
    from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QComboBox, QLineEdit, QTextEdit, QCheckBox, QFileDialog)
    from PyQt5.QtCore import QTimer, Qt

    app = QApplication(sys.argv)
    logq = queue.Queue()
    w = QWidget()
    w.setWindowTitle("全自动 Replay 生成器 · 谱面→引擎原生回放(真人判定)")
    w.resize(820, 720)
    lay = QVBoxLayout(w)

    lay.addWidget(QLabel("【全自动原理】生成 perfect replay → 放进游戏 replay 目录 → 游戏内回放。引擎按帧注入真实按键，走和真人相同的判定。"))

    mods = scan_mods()
    logq.put("扫到 %d 个带谱面的 mod" % len(mods))

    row0 = QHBoxLayout()
    mod_cb = QComboBox()
    chart_cb = QComboBox()
    tpl_cb = QComboBox()
    row0.addWidget(QLabel("Mod:")); row0.addWidget(mod_cb)
    lay.addLayout(row0)

    row1 = QHBoxLayout()
    row1.addWidget(QLabel("谱面:")); row1.addWidget(chart_cb, 1)
    lay.addLayout(row1)

    row2 = QHBoxLayout()
    row2.addWidget(QLabel("state模板:")); row2.addWidget(tpl_cb, 1)
    lay.addWidget(QLabel("（模板=已有真实 replay，用于对齐曲目参数/键位，强烈建议选）"))
    lay.addLayout(row2)

    def on_mod(i):
        chart_cb.clear(); tpl_cb.clear()
        name = mod_cb.currentText()
        info = mods.get(name, {})
        for c in info.get('charts', []):
            chart_cb.addItem(os.path.basename(c), c)
        for t in info.get('templates', []):
            tpl_cb.addItem(os.path.relpath(t, REPLAYS), t)

    mod_cb.addItems(sorted(mods.keys()))
    mod_cb.currentIndexChanged.connect(on_mod)
    if mods:
        on_mod(0)

    # 参数
    row3 = QHBoxLayout()
    song_le = QLineEdit()
    diff_le = QLineEdit("OLD")
    side_cb = QComboBox(); side_cb.addItems(['玩家(只按玩家音符)', '对手', '全部'])
    mania_le = QLineEdit("4")
    row3.addWidget(QLabel("歌曲ID:")); row3.addWidget(song_le)
    lay.addLayout(row3)
    row3b = QHBoxLayout()
    row3b.addWidget(QLabel("难度:")); row3b.addWidget(diff_le)
    row3b.addWidget(QLabel("侧:")); row3b.addWidget(side_cb)
    row3b.addWidget(QLabel("键数:")); row3b.addWidget(mania_le)
    lay.addLayout(row3b)

    row4 = QHBoxLayout()
    out_le = QLineEdit(DEFAULT_OUT)
    bout = QPushButton("输出目录…")
    def pick_out():
        p = QFileDialog.getExistingDirectory(w, "选择输出目录", out_le.text())
        if p: out_le.setText(p)
    bout.clicked.connect(pick_out)
    row4.addWidget(QLabel("输出到:")); row4.addWidget(out_le); row4.addWidget(bout)
    lay.addLayout(row4)

    auto_copy = QCheckBox("生成后自动复制到游戏 replay 目录（推荐）")
    auto_copy.setChecked(True)
    lay.addWidget(auto_copy)

    # 根据模板自动填歌曲/难度
    def on_tpl(i):
        p = tpl_cb.itemData(i)
        if not p or not os.path.exists(p):
            return
        try:
            obj = RT.aes_decrypt_json(p)
            sr = obj['stateRecord']
            song_le.setText(sr.get('songName', ''))
            diff_le.setText(sr.get('difficulty', 'OLD'))
            mania_le.setText(str(sr.get('mania', 4)))
            logq.put("模板载入: 歌曲=%s 难度=%s mania=%s songSpeed=%s" %
                     (sr.get('songName'), sr.get('difficulty'), sr.get('mania'), sr.get('songSpeed')))
        except Exception as e:
            logq.put("模板读取失败: %s" % e)
    tpl_cb.currentIndexChanged.connect(on_tpl)
    if tpl_cb.count():
        on_tpl(0)

    rowrun = QHBoxLayout()
    bgen = QPushButton("★ 生成 perfect replay")
    def do_gen():
        try:
            chart = chart_cb.currentData()
            if not chart or not os.path.exists(chart):
                logq.put("错误：先选谱面"); return
            song = song_le.text().strip() or 'song'
            diff = diff_le.text().strip() or 'Hard'
            tpl = tpl_cb.currentData()
            side = ['player', 'opponent', 'all'][side_cb.currentIndex()]
            mania = int(mania_le.text()) if mania_le.text().strip().isdigit() else 4
            out = out_le.text().strip() or DEFAULT_OUT
            logq.put("生成中: %s / %s ..." % (song, diff))
            args = [chart, song, diff, '', side, str(mania), '0', '--outdir', out]
            if tpl and os.path.exists(tpl):
                args += ['--template', tpl]
            RT.cmd_gen(args)
            # 自动复制
            if auto_copy.isChecked():
                mod_name = mod_cb.currentText()
                dst_root = os.path.join(REPLAYS, mod_name)
                # 找刚生成的最新 rsd
                import time as _t
                cands = glob.glob(os.path.join(out, '**', '*.rsd'), recursive=True)
                if cands:
                    newest = max(cands, key=os.path.getmtime)
                    rel = os.path.relpath(newest, out)
                    dst = os.path.join(dst_root, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    import shutil
                    shutil.copy2(newest, dst)
                    txt = newest[:-4] + '.txt'
                    if os.path.exists(txt):
                        shutil.copy2(txt, dst[:-4] + '.txt')
                    logq.put("✓ 已复制到游戏: %s" % dst)
                    logq.put("现在进游戏 → Freeplay → 回放列表选它即可（引擎全自动打，无 miss）。")
        except Exception:
            logq.put("生成失败:\n" + traceback.format_exc())
    bgen.clicked.connect(do_gen)
    rowrun.addWidget(bgen)
    lay.addLayout(rowrun)

    log = QTextEdit(); log.setReadOnly(True)
    lay.addWidget(log)

    def flush():
        while True:
            try: s = logq.get_nowait()
            except queue.Empty: break
            log.append(s)
    tm = QTimer(); tm.timeout.connect(flush); tm.start(80)

    logq.put("="*60)
    logq.put("两档说明：")
    logq.put("【全自动】本工具 —— 生成 replay，游戏内回放，引擎原生真人判定，不用开外挂。")
    logq.put("【半自动】闭环代打器 v3.exe —— 读 songPosition 实时注入，当场打。")
    logq.put("="*60)
    logq.put("步骤：选 mod → 谱面 → state模板 → 确认歌曲/难度 → 点生成。")
    w.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    build_gui()
