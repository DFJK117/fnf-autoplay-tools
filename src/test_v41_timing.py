# -*- coding: utf-8 -*-
"""v4.1 时序仿真验证：可控假时钟跑完整 _player，不真发键。
核心回归：v4.0 负抖动(早按)被 max(now,fire_at) 吃掉；v4.1 预读排队后必须真正生效。
验证：纯早按落地 / 随机抖动真实 / 和弦一次 SendInput / 发键单调 / 区间限制 / 机器模式。"""
import importlib.util, queue, time

P = r'E:\UST_work\fnf_autoplay\autoplay_v41.py'
spec = importlib.util.spec_from_file_location('v41', P)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class FakeClock:
    def __init__(self): self.t = 0.0
    def pc(self):
        self.t += 0.0005          # 忙等每次读时钟前进 0.5ms，避免死循环
        return self.t
    def sl(self, sec):
        self.t += max(float(sec), 0.0005)

fc = FakeClock()
old_pc, old_sl = time.perf_counter, time.sleep
time.perf_counter, time.sleep = fc.pc, fc.sl
try:
    # 30 音符：3 组和弦（同刻 2 键），其余单键
    notes, groups = [], 0
    t = 500.0
    for i in range(30):
        lane = i % 4
        notes.append((t, lane, 0.0, '', True))
        if i in (5, 14, 23):
            notes.append((t, lane + 1, 0.0, '', True))
        t += 160.0
    notes.sort()

    ev = []
    cases = [
        # label      lo     hi    comp   miss   期望偏差域(补偿后)
        # 补偿后理论区间 = [lo - center, hi - center]，center = lo + 0.42667*(hi-lo)
        ('默认人味',  -20.0, 60.0, True,  0.0,  (-34.1, 45.9)),
        ('纯早按',    -80.0,-20.0, False, 0.0,  (-80.0,-20.0)),   # ★ v4.0 这里会全变 0
        ('纯延后',     20.0, 80.0, False, 0.0,  ( 20.0, 80.0)),
        ('宽区间双向',-60.0, 60.0, False, 0.0,  (-60.0, 60.0)),
        ('带漏按',    -20.0, 60.0, True, 10.0, (-34.1, 45.9)),
        ('机器精准',    0.0,  0.0, True,  0.0,  (-3.0,  3.0)),
    ]
    results = {}
    for label, lo, hi, comp, miss, expect in cases:
        ev.clear()
        eg = m.Engine(queue.Queue())
        eg.notes = notes
        eg.enabled_types = {''}
        eg.target_side = 'all'
        eg.keys = ['D', 'F', 'J', 'K']
        eg.lane_count = 4
        eg.human.configure(lo, hi, comp, miss, True)
        eg.human.seed = 20261122
        eg.human.reset()
        eg.running = True
        eg.t0 = 0.0
        eg.idx = 0
        eg.pressed_count = 0
        eg.dev_last = eg.dev_avg = 0.0
        eg.dev_cnt = 0
        eg.human_devs = []
        eg.human_missed = 0
        eg.sched.tap_multi = lambda items, now, e=ev: e.append((now, [v for v, _h in items]))
        fc.t = 0.0
        eg._player()

        devs = eg.human_devs
        assert devs, label + ' 无发键事件'
        dmin, dmax, davg = min(devs), max(devs), sum(devs) / len(devs)
        results[label] = (dmin, dmax, davg, len(ev), eg.human_missed)
        # 区间限制（放宽 6ms 给假时钟步进误差）
        assert dmin >= expect[0] - 6.0, (label, '早按越界', dmin, expect[0])
        assert dmax <= expect[1] + 8.0, (label, '晚按越界', dmax, expect[1])
        # 发键顺序单调
        assert all(ev[i][0] <= ev[i + 1][0] + 1e-9 for i in range(len(ev) - 1)), label + ' 发键逆序'
        # 不丢音符
        assert sum(len(keys) for _t, keys in ev) + eg.human_missed * 2 >= 30, label + ' 丢音符'
        assert any(len(keys) >= 2 for _t, keys in ev), label + ' 和弦没发出'
        if label == '机器精准':
            assert max(abs(d) for d in devs) < 3.0, ('机器模式不该抖', dmin, dmax)
        else:
            assert dmax - dmin > 10.0, ('人味模式抖幅不足', dmin, dmax)
        if label == '纯早按':
            # ★ v4.0 死穴：负区间今天必须真的落在负域，且平均值也必须为负
            assert dmax < -5.0 and davg < -15.0, ('负抖动没生效!', dmin, dmax, davg)
        if label == '纯延后':
            assert dmin > 5.0 and davg > 25.0, ('正抖动没生效!', dmin, dmax, davg)
        if label == '带漏按':
            assert eg.human_missed > 0, '漏按没生效'

    for label, (dmin, dmax, davg, ne, ms) in results.items():
        print('%-10s 事件=%2d 漏按=%d 偏差 %+7.1f~%+7.1f 均值%+7.1f ms' %
              (label, ne, ms, dmin, dmax, davg))
    print('[OK] v4.1 时序仿真：纯早按落地/双向抖动/区间/单调/和弦/漏按/机器模式 全过')
finally:
    time.perf_counter, time.sleep = old_pc, old_sl
