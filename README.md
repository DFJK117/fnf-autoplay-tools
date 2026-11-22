# FNF AutoPlay Tools — Friday Night Funkin' 自动演奏 / 代打 / Replay 工具集

**⬇️ 直接下载可执行文件：[Releases 页面](../../releases)**（每个 Release 附单个 exe，下载双击即用）

一个仓库收录了针对 FNF（Psych Engine / Kade Engine / NovaFlare Engine）的多种自动演奏方案。这些方案**原理完全不同**，适用场合也不同——别下错。下面按"选哪个"来组织。

> ⚠️ **免责声明**：这些工具向游戏发送真实键盘事件或读取游戏进程内存。仅限**离线、自己机器上、非排名**用途。联网排行榜、比赛、他人账号一律不要用，封号自负。作者不对任何滥用后果负责。

## 选哪个？（先看这张表）

| 方案 | 原理 | 是否真键盘 | 是否全 Sick | 需要外部程序 | 适合 |
|---|---|---|---|---|---|
| **谱面代打器 F5**（v4.0 人味版） | 读谱面 JSON + 系统时钟开环发键 + 随机人味延迟 | ✅ SendInput 真键盘 | ❌ 走正常判定 | ✅ exe | 半自动、想自己控制起停、要上分、要像真人 |
| **闭环代打器 v3** | 读游戏内存 `Conductor.songPosition` 闭环发键 | ✅ 真键盘 | ❌ 正常判定 | ✅ exe | 全自动、零漂移、不用调延迟 |
| **全自动外挂 v5** | 同上，独立 exe 增强版 | ✅ 真键盘 | ❌ 正常判定 | ✅ exe | 全自动、要能上排行榜 |
| **Replay 生成器** | 直接生成 NF 引擎原生 `.rsd` 加密回放文件 | 回放 | ✅ 全 Sick | ✅ exe | NF 引擎、不要任何运行时外挂 |
| **引擎级 Lua**（AutoFullCombo.lua） | 打开引擎自带 BotPlay | ❌ 引擎内部 | ✅ 强制全 Sick | ❌ 只要一个 .lua | NF/PE 引擎、图省事、不在意排行榜 |

## 方案一：谱面代打器 F5（开环，v4.0 人味版）

读谱面 JSON，按 `noteType` 归属区分玩家/对手音符，用 `SendInput` 发真实键盘事件走游戏正常判定管线。F5 随时开停，`Ctrl+↑/↓` 实时调延迟。

### v4.0 新增：模拟真人随机延迟

v3.8 每个音符卡在同一数学时刻，判定分布干净得不像人。v4.0 给每个音符/和弦的按下时刻加**可配置随机偏移**（默认 -20 ~ +60ms，正=晚、负=早）：

- **偏态三角分布**：多数略偏早、少数拖后；每 8 组一次低频手速漂移；抖动严格限制在设定区间内。
- **和弦共享一次抖动**：同刻多键算一个动作，一次 SendInput 发出，不会出现"和弦里每个键各自错开"的假人特征。
- **补偿均值**（默认开）：按分布数学期望补偿，平均仍落在拍点、个体随机 —— 看起来像人，整体分数不偏。
- **偶发漏按**（可选，默认 0）和**按住时长 ±15% 微抖**（默认开）。
- **实测可见**：GUI 实时显示抖动最小/最大/平均、组数、漏按组数。
- **时序安全**：抖动后的按键进待发队列，强制单调（最小 6ms 间隔），随机量不会打乱先后顺序。

| 用途 | 区间 | 补偿均值 | 漏按 |
|---|---|---|---|
| 像真人、基本不掉分（默认） | -20 ~ +60 ms | 开 | 0 |
| 录视频/直播要明显像人 | -30 ~ +90 ms | 开 | 0.5~2% |
| 明显延后、保护成绩 | +20 ~ +80 ms | 关 | 0 |
| 机器级精准（v3.8 行为） | 关掉总开关即可 | — | — |

FNF 判定窗口 Sick ≈ ±45ms、Good ≈ ±90ms。默认配置实测偏差 +1.5 ~ +36.5ms（均值 +7.9ms），绝大多数 Sick；参数填到 ±100ms 以上会大量掉判定。

### 保留的 v3.x 能力

- **v3.8 关键修复**：自动翻转游戏存档 `funkin.sol` 的 `playOpponent=f` → `t`（否则 `keyPressed` 只判玩家侧、对手音符打不到）。运行前若游戏正在运行会拒绝启动。
- 分轨按 noteType 归属判定（非 `mustHitSection`）：玩家 type = 普通音符 + 机制类（CupNote/Blue/Orange/Ink/Parry…）；对手 type 单独勾选。
- 支持四种谱面格式：标准 PE/Kade `{"song":{...}}`、`psych_v1_convert`、`strumTime` 对象数组、`sectionNotes` 段。
- 默认键位 D F J K，可改；游戏/程序权限须一致（同为管理员或同为普通）。

用法见 [`docs/谱面代打器_v4.0_人味版_使用说明.txt`](docs/谱面代打器_v4.0_人味版_使用说明.txt)（完整参数/判定窗口/实测数据）、[`docs/使用说明.txt`](docs/使用说明.txt)。源码 `src/autoplay_v40.py`（v4.0 人味版）、`src/autoplay_v25.py` ~ `autoplay_v32.py`（历史演进）。测试 `src/test_v40_timing.py`（时序仿真）与源码内 `--selftest`。

## 方案二：闭环代打器 v3 / 全自动外挂 v5（读内存时钟）

NF/PE 是 hxcpp 编译，`Conductor.songPosition`（歌曲已播毫秒）是数据段里的原生 `double`。程序启动后：

1. 双快照扫描游戏进程内存，找"随墙钟 1:1 增长"的 double；
2. 排除显卡驱动（nvoglv64 等）帧计时器干扰；
3. 同地址连续 3 轮稳定增长 → 锁定；
4. 每 1ms 读它，当 `songPos >= 音符 strumTime` 发真键 → 闭环零漂移；
5. 曲终自动停、重开曲目自动从头。锁定结果存 `songpos_lock.json`。

**关键**：启动前游戏必须停在**正在播放**的曲目（暂停/结算界面 songPosition 不增长，扫不到）。换引擎/版本会自动重扫。

源码 `src/autoplay_closed.py`、`src/fullauto_v5.py`，说明见 [`docs/闭环版_使用说明.txt`](docs/闭环版_使用说明.txt)、[`docs/全自动外挂v5_使用说明.txt`](docs/全自动外挂v5_使用说明.txt)。

## 方案三：全自动 Replay 生成器（NF 引擎原生回放）

NovaFlare 引擎原生支持 replay：`windows/replays/<mod>/<歌>/<难度>/<时间戳>.rsd`（AES-256-CBC 加密 JSON，密钥在引擎源码 `EncryptUtil.hx`）。明文含 `stateRecord`（歌曲/难度/速度/键位/游戏设置）+ `frameRecord`（每帧 time / pressKey[] / releaseKey[] / noteJudgments）。

生成器扫描游戏谱面 + 真实 replay 模板 → 选谱面和模板 → 生成 perfect replay（每个音符在正确时刻 press/release、全 sick 判定）→ 自动复制到游戏 replay 目录 → 游戏内回放。**零外挂、零 F5、零调延迟**，因为引擎走的是 `Replay.handleInput()` → `PlayState.replayApplyInput()` → `keyPressed()/keyReleased()/goodNoteHit()` 真人判定管线。

源码 `src/replay_tool.py` / `src/replay_gui.py`，说明见 [`docs/两档说明_全自动与半自动.txt`](docs/两档说明_全自动与半自动.txt)。

## 方案四：引擎级 Lua（BotPlay）

一个 `AutoFullCombo.lua` 放进引擎全局脚本目录（`mods/<已启用mod>/scripts/*.lua` 会被自动加载），进曲自动开打、藏 BOTPLAY 字样、F9 切换。原理是打开引擎自带 `cpuControlled`，引擎自己判定全 Sick，不读内存不发键盘不依赖窗口焦点，支持任意 K。

> 注：此方案在 NF 引擎实测有效；PE/Legacy 等引擎的 `runHaxeCode` 路径可能不可用，需要纯 Lua 实现。

说明见 [`docs/全自动_引擎级_使用说明.txt`](docs/全自动_引擎级_使用说明.txt)。

## 目录结构

```
src/     全部 Python 源码（autoplay_v40 人味版 + v25~v32 历史演进 + 时序仿真测试）
docs/    各方案/各版本的中文使用说明（含 v4.0 人味版完整说明）
```

## License

MIT · 作者 DFJK117
