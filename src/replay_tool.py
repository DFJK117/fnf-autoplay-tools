# -*- coding: utf-8 -*-
"""
NF Replay 生成/解析工具 —— 纯真人输入序列, 引擎原生回放
===========================================================
NF(source/games/backend/Replay.hx) 的 .rsd = AES-256-CBC 加密的 JSON:
  { stateRecord:{...}, frameRecord:[{time, songSpeed, playbackRate,
    pressKey:[键名], releaseKey:[键名], noteJudgments:[...]}, ...] }
  加密: key="c138265b0f77cccd86192a7173668090", 随机16B IV + 密文, 整体 base64
回放时引擎把 pressKey/releaseKey 写进 FlxG.keys, 调 PlayState.keyPressed/
  keyReleased/goodNoteHit —— 和真人按键同一套判定管线, 不是 botplay。

用法:
  # 解析已有 replay
  python replay_tool.py decrypt <rsd路径>
  # 从谱面生成 perfect replay 到游戏目录
  python replay_tool.py gen <谱面.json> <歌曲名> <难度> <模组分目录> [选项]
  # 校验生成文件
  python replay_tool.py verify <rsd路径>
"""
import sys, os, json, base64, time, random, shutil
from Crypto.Cipher import AES

AES_KEY = b"c138265b0f77cccd86192a7173668090"
BLOCK = 16
GAME_ROOT = r"E:\UGUSDTIOUIYSRGIUUHSRGIH\Downloads\windows"

# 默认 4K 键位(noteData 0..3)
LANE_KEYS = ['D', 'F', 'J', 'K']


def pkcs7_pad(data: bytes) -> bytes:
    n = BLOCK - (len(data) % BLOCK)
    return data + bytes([n]) * n


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    p = data[-1]
    if 1 <= p <= BLOCK and data[-p:] == bytes([p]) * p:
        return data[:-p]
    return data


def aes_encrypt_json(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    data = pkcs7_pad(text.encode('utf-8'))
    iv = bytes(random.randrange(256) for _ in range(BLOCK))
    ct = AES.new(AES_KEY, AES.MODE_CBC, iv).encrypt(data)
    return base64.b64encode(iv + ct).decode('ascii')


def aes_decrypt_json(rsd_path):
    raw = base64.b64decode(open(rsd_path, 'rb').read())
    iv, ct = raw[:BLOCK], raw[BLOCK:]
    pt = pkcs7_unpad(AES.new(AES_KEY, AES.MODE_CBC, iv).decrypt(ct))
    s = pt.decode('utf-8', errors='replace')
    s = s[:s.rfind('}') + 1]
    return json.loads(s)


# ---------- 谱面解析(复用 v2.4 逻辑, 独立复制避免依赖) ----------
def load_chart(path):
    raw = open(path, 'rb').read()
    if raw.startswith(b'\xef\xbb\xbf'):
        raw = raw[3:]
    data = json.loads(raw.decode('utf-8', 'replace'))
    song = data.get('song') if isinstance(data, dict) else None
    if not isinstance(song, dict):
        song = data if isinstance(data, dict) else {}
    notes = []
    for src in (song, data):
        if not isinstance(src, dict):
            continue
        cont = src.get('notes')
        if not isinstance(cont, list):
            continue
        for sec in cont:
            if isinstance(sec, dict) and isinstance(sec.get('sectionNotes'), list):
                must = bool(sec.get('mustHitSection', True))
                for n in sec['sectionNotes']:
                    t = None
                    def num(v, default=0.0):
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            return default
                    if isinstance(n, dict) and 'strumTime' in n:
                        t = num(n['strumTime'])
                        lane = int(num(n.get('noteData', 0)))
                        sus = num(n.get('sustainLength', 0))
                        nt = str(n.get('noteType', '') or '').strip()
                    elif isinstance(n, (list, tuple)) and len(n) >= 2:
                        t = num(n[0]); lane = int(num(n[1]))
                        sus = num(n[2]) if len(n) > 2 else 0.0
                        nt = str(n[3]).strip() if len(n) > 3 and n[3] else ''
                    else:
                        continue
                    if t is None or not (0 <= lane <= 7):
                        continue
                    # 4K mod: lane 0-3 = 玩家(BF), lane 4-7 = 对手(noteType 多为 opponent/sing)
                    # 玩家音符 = lane<4 且 noteType 非对手; mustHitSection 只管镜头, 不改归属
                    owner = type_owner(nt)
                    must_press = (lane < 4) and (owner != 'opponent')
                    notes.append({'t': t, 'lane': lane, 'sus': sus, 'nt': nt,
                                  'must': must, 'must_press': must_press})
    notes.sort(key=lambda x: x['t'])
    return notes, song


def type_owner(nt):
    s = (nt or '').strip().lower()
    if not s:
        return 'player'
    if 'opponent' in s or 'gf sing' in s:
        return 'opponent'
    if s.endswith('sing') or s.endswith('p'):
        return 'player' if (s.startswith('bf') or s.startswith('player')) else 'opponent'
    return 'player'


# ---------- 生成 perfect replay ----------
def gen_frames(notes, side='player', mania=4, offset_ms=0.0, pressLead=28.0, holdTail=60.0):
    """
    为每个目标音符生成按键帧:
      press 帧: strumTime - pressLead 按下对应 lane 键
      release 帧: strumTime + sustain + holdTail 松开
      judgment 帧: strumTime 处写 noteJudgments(rating=sick)
    时长坐标 = 谱面毫秒(与 Conductor.songPosition 同基准)
    """
    keys = LANE_KEYS[:mania] if mania <= 4 else LANE_KEYS
    events = []  # (time, kind, lane, note)
    for n in notes:
        mp = n.get('must_press', (n['lane'] < 4) == n.get('must', True))
        if side == 'player' and not mp:
            continue
        if side == 'opponent' and mp:
            continue
        lane = n['lane'] % mania
        t = n['t'] + offset_ms
        events.append((t - pressLead, 'press', lane, n))
        rel = t + max(n['sus'], 0) + holdTail
        events.append((rel, 'release', lane, n))
        events.append((t, 'judge', lane, n))
    events.sort(key=lambda e: e[0])

    # 按时间聚合成帧; 同一时刻的 press/release 合并, judge 独立带 noteJudgments
    frames = []
    for t, kind, lane, n in events:
        # 找/建该时刻附近的帧(容差 1ms)
        frame = None
        for f in reversed(frames[-8:]):
            if abs(f['time'] - t) < 1.0:
                frame = f
                break
        if frame is None:
            frame = {'time': t, 'songSpeed': 1, 'playbackRate': 1,
                     'pressKey': [], 'releaseKey': []}
            frames.append(frame)
        key = keys[lane]
        if kind == 'press':
            if key not in frame['pressKey']:
                frame['pressKey'].append(key)
        elif kind == 'release':
            if key not in frame['releaseKey']:
                frame['releaseKey'].append(key)
        elif kind == 'judge':
            nj = frame.setdefault('noteJudgments', [])
            nj.append({'rating': 'sick', 'hitDiff': 0.0,
                       'strumTime': n['t'], 'noteData': n['lane'],
                       'isSustain': bool(n['sus'] > 0)})
    frames.sort(key=lambda f: f['time'])
    # 起始帧(曲目开始前)
    if not frames or frames[0]['time'] > 0:
        frames.insert(0, {'time': -1000, 'songSpeed': 1, 'playbackRate': 1,
                          'pressKey': [], 'releaseKey': []})
    return frames


def build_state(song, song_name, difficulty, mania=4, song_length=300000):
    return {
        'songName': song_name,
        'difficulty': difficulty,
        'playDate': time.strftime('%Y-%m-%d-%H.%M.%S'),
        'modDir': '',
        'songLength': float(song.get('length', song_length)) if isinstance(song, dict) else song_length,
        'songSpeed': 1,
        'playbackRate': 1,
        'healthGain': 1,
        'healthLoss': 1,
        'cpuControlled': False,
        'practiceMode': True,
        'instakillOnMiss': False,
        'playOpponent': False,
        'flipChart': False,
        'songScore': 0,
        'ratingPercent': 1.0,
        'ratingFC': 'MFC',
        'songHits': 0,
        'highestCombo': 0,
        'songMisses': 0,
        'hitMapTime': [],
        'hitMapMs': [],
        'replayVersion': 2,
        'sickCount': 0,
        'goodCount': 0,
        'badCount': 0,
        'shitCount': 0,
        'mania': mania,
        'safeFrames': 10,
        'ratingOffset': 0,
        'noteOffset': 0,
        'botPlay': False,
        'gameplaySettingsJson': '',
    }


def write_replay(outdir, song_name, difficulty, state, frames):
    safe = lambda s: str(s).replace(' ', '-').replace(':', '.').replace('/', '').replace('\\', '')
    folder = os.path.join(outdir, safe(song_name), safe(difficulty))
    os.makedirs(folder, exist_ok=True)
    rsd_path = os.path.join(folder, state['playDate'] + '.rsd')
    txt_path = os.path.join(folder, state['playDate'] + '.txt')
    with open(rsd_path, 'w', encoding='utf-8') as f:
        f.write(aes_encrypt_json({'stateRecord': state, 'frameRecord': frames}))
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('Song Name: %s\n' % state.get('songName', song_name))
        f.write('Difficulty: %s\n' % state.get('difficulty', difficulty))
        f.write('Song Length: %s\n' % state.get('songLength', 0))
        f.write('Date: %s\n' % state.get('playDate', ''))
        f.write('Song Speed: %s\n' % state.get('songSpeed', 1))
        f.write('Playback Rate: %s\n' % state.get('playbackRate', 1))
        f.write('Health Gain: %s\n' % state.get('healthGain', 1))
        f.write('Health Loss: %s\n' % state.get('healthLoss', 1))
        f.write('CPU Controlled: %s\n' % state.get('cpuControlled', False))
        f.write('Practice Mode: %s\n' % state.get('practiceMode', True))
        f.write('Instakill On Miss: %s\n' % state.get('instakillOnMiss', False))
        f.write('Play Opponent: %s\n' % state.get('playOpponent', False))
        f.write('Flip Chart: %s\n' % state.get('flipChart', False))
        f.write('Score: %s\n' % state.get('songScore', 0))
        f.write('Rating: %s (%s)\n' % (state.get('ratingPercent', 1), state.get('ratingFC', 'MFC')))
        f.write('Hits: %s\n' % state.get('songHits', 0))
        f.write('Highest Combo: %s\n' % state.get('highestCombo', 0))
        f.write('Misses: %s\n' % state.get('songMisses', 0))
        f.write('Replay Version: %s\n' % state.get('replayVersion', 2))
        f.write('Sick Count: %s\n' % state.get('sickCount', 0))
        f.write('Good Count: %s\n' % state.get('goodCount', 0))
        f.write('Bad Count: %s\n' % state.get('badCount', 0))
        f.write('Shit Count: %s\n' % state.get('shitCount', 0))
        f.write('Mania: %s\n' % state.get('mania', 4))
        f.write('Safe Frames: %s\n' % state.get('safeFrames', 10))
        f.write('Rating Offset: %s\n' % state.get('ratingOffset', 0))
        f.write('Note Offset: %s\n' % state.get('noteOffset', 0))
        f.write('Bot Play: %s\n' % state.get('botPlay', False))
    return rsd_path, txt_path


def cmd_gen(args):
    chart = args[0]
    song_name = args[1]
    difficulty = args[2] if len(args) > 2 else 'Hard'
    mod_dir = args[3] if len(args) > 3 else ''
    side = args[4] if len(args) > 4 else 'player'
    mania = int(args[5]) if len(args) > 5 else 4
    offset = float(args[6]) if len(args) > 6 else 0.0
    notes, song = load_chart(chart)
    frames = gen_frames(notes, side=side, mania=mania, offset_ms=offset)
    state = build_state(song, song_name, difficulty, mania)
    if mod_dir:
        state['modDir'] = mod_dir
    # --template <真实rsd>: 复用其 stateRecord(曲目参数/键位/游戏设置全对齐),
    # 只覆盖 songName/difficulty/modDir/日期, 避免回放时参数不匹配
    if '--template' in args:
        ti = args.index('--template')
        tpl = aes_decrypt_json(args[ti + 1])
        base = dict(tpl['stateRecord'])
        # modDir: 模板里没有就不加(与真实文件字段集完全一致); 有则保留
        if mod_dir:
            base['modDir'] = mod_dir
        base.update({
            'songName': song_name, 'difficulty': difficulty,
            'playDate': state['playDate'],
            # songSpeed/playbackRate 必须保留录制值, 否则回放速率错乱
            'cpuControlled': False, 'botPlay': False, 'playOpponent': False,
            'replayVersion': 2,
        })
        # 帧里的 songSpeed/playbackRate 也要跟 state 一致
        spd = base.get('songSpeed', 1); rate = base.get('playbackRate', 1)
        for fr in frames:
            fr['songSpeed'] = spd; fr['playbackRate'] = rate
        state = base
        mania = int(base.get('mania', mania))
    # --outdir <路径> 可覆盖(测试用, 不污染游戏目录)
    if '--outdir' in args:
        oi = args.index('--outdir')
        outdir = args[oi + 1]
    else:
        outdir = os.path.join(GAME_ROOT, 'replays', mod_dir) if mod_dir else os.path.join(GAME_ROOT, 'replays', 'originFunkin')
    rsd, txt = write_replay(outdir, song_name, difficulty, state, frames)
    print('谱面音符:', len(notes), '生成帧:', len(frames))
    print('写入:', rsd)
    print('元数据:', txt)
    # 自校验
    back = aes_decrypt_json(rsd)
    assert len(back['frameRecord']) == len(frames)
    print('AES 回读校验: OK, 帧数', len(back['frameRecord']))


def cmd_decrypt(args):
    obj = aes_decrypt_json(args[0])
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:4000])


def cmd_verify(args):
    obj = aes_decrypt_json(args[0])
    sr = obj['stateRecord']; fr = obj['frameRecord']
    print('song=%s diff=%s mania=%s ver=%s botPlay=%s' %
          (sr.get('songName'), sr.get('difficulty'), sr.get('mania'),
           sr.get('replayVersion'), sr.get('botPlay')))
    print('帧数=%d, 带判定帧=%d' %
          (len(fr), sum(1 for f in fr if f.get('noteJudgments'))))
    print('校验 OK')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(0)
    cmd = sys.argv[1]
    if cmd == 'gen':
        cmd_gen(sys.argv[2:])
    elif cmd == 'decrypt':
        cmd_decrypt(sys.argv[2:])
    elif cmd == 'verify':
        cmd_verify(sys.argv[2:])
    else:
        print('未知命令', cmd)
