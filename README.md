# NaVILA × Agno 导航 Demo

> **Agno 负责慢思考（规划 / 记忆 / 工具），NaVILA 负责快行动（中层视觉导航），机器狗底层控制器负责实时安全。**

长期目标是把 VLN 模型部署到宇树 Go2 上，实现跨楼宇导航。跨楼宇不只是"往前走"：
上楼要等电梯、过门要判断开合、走不通要换路、到了要能确认到了。这些决策变化很慢、
需要常识和额外信息（楼层、地图、电梯状态），适合交给 agent；而"当前这一段怎么走"变化
很快、需要密集的视觉推理，适合交给 VLN。这个仓库就是验证这两层如何分工、如何交换信息。

仓库同时是一份**可运行的最小骨架**：默认使用确定性 `MockSupervisor` + `MockVLA`，
不需要 GPU、不需要 API Key、不需要装 NaVILA，就能跑通完整闭环。

---

## 目录

- [当前状态](#当前状态)
- [30 秒跑起来](#30-秒跑起来)
- [架构与分工](#架构与分工)
- [环境准备](#环境准备)
- [配置（.env）](#配置env)
- [运行模式](#运行模式)
- [多设备部署](#多设备部署)
- [命令行参数](#命令行参数)
- [服务接口](#服务接口)
- [运行产物](#运行产物)
- [制作演示视频与 PPT](#制作演示视频与-ppt)
- [上层契约](#上层契约)
- [排错手册](#排错手册)
- [测试](#测试)
- [目录结构](#目录结构)
- [已知限制与下一步](#已知限制与下一步)

---

## 当前状态

| 能力 | 状态 | 怎么用 |
| --- | --- | --- |
| 离线 mock 闭环 | ✅ 稳定，秒级 | `python demo.py "去 5 楼会议室 A"` |
| Agno 真实规划（DeepSeek） | ✅ | `--supervisor agno` |
| NaVILA bridge 真实推理 | ✅（加载 checkpoint 约 5 分钟） | `--vla navila` |
| Go2 dry-run（真实相机 + 只打印指令） | ✅ | `--robot go2 --go2-dry-run` |
| Go2 真实运动 | ⚠️ 链路已通，长任务的完成判定仍在迭代 | `--robot go2 --no-go2-dry-run` |
| LightNav-0 接入 | 🟡 适配层已写，未上真机 | `--vla lightnav` |
| Habitat 闭环回放 | 🟡 流程可跑通，单 episode 不一定成功（`spl` 可能为 0） | `./scripts/run_closed_loop.sh` |

---

## 30 秒跑起来

不需要 GPU、不需要模型、不需要 API Key：

```bash
git clone https://github.com/flying-xiong/navila-agno-demo.git
cd navila-agno-demo

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 只有 httpx / python-dotenv 是必需的

python demo.py "去 5 楼会议室 A" --supervisor mock
python -m unittest discover -s tests
```

预期输出：

```text
任务：去 5 楼会议室 A
supervisor=MockSupervisor vla=MockVLA robot=MockRobot

  [subgoal] go-to-elevator: 走到电梯 C 门口并停下 (max_steps=8, est=2.0)
  [step] go-to-elevator step=1 vla='move forward 0.50 m.' -> move_forward
  ...
  [event] subgoal_completed @ go-to-elevator: 完成子目标：走到电梯 C 门口并停下
  [subgoal] take-elevator: 进入电梯并选择 5 楼 (max_steps=3, est=0.2)
  [event] subgoal_completed @ take-elevator: 完成子目标：进入电梯并选择 5 楼
  [subgoal] go-to-meeting-room: 沿走廊走到 502 会议室并停下 (max_steps=10, est=2.5)
  [event] subgoal_completed @ go-to-meeting-room: 完成子目标：沿走廊走到 502 会议室并停下

执行摘要
  成功：True
  完成子目标：3/3
  运行目录：runs/mock/<run_id>
```

顺带看一眼失败重规划是怎么工作的——故意让第一个子目标被阻塞：

```bash
python demo.py "去 5 楼会议室 A" --supervisor mock --fail-subgoal go-to-elevator
```

`go-to-elevator` 会在第 3 步报 `blocked`，Agno 收到事件后返回 `reroute-via-stairs`
作为替代子目标，闭环继续往下走。

---

## 架构与分工

```text
                      ┌───────────────────────────────────┐
                      │ Agno Supervisor                    │
                      │  规划 / 重规划 / 记忆 / 工具调用     │
                      │  （低频：任务开始、异常、子目标完成）  │
                      └────────────────┬──────────────────┘
                                       │ SubGoal JSON
                                       ▼
        ┌──────────────────────────────────────────────────────────┐
        │ VLAExecutor（中频闭环，每步一次推理）                        │
        │  取帧 → NaVILA → 解析动作 → 判完成 → 发给机器人              │
        └──────────┬───────────────────────────────────┬───────────┘
                   │ 帧 / 机器人状态                     │ Event
                   ▼                                   ▼
        NaVILA bridge / LightNav-0              subgoal_completed
                   │                            subgoal_failed / blocked
                   ▼                            uncertain / safety_stop / need_help
        Go2 bridge → SportClient → 机器狗                  │
                   │                                      │
                   └────────────── 事件回到 Supervisor ────┘
```

三条边界规则：

1. **LLM 不进入高频控制循环。** Agno 只在任务开始、子目标结束、或收到异常事件时介入；
   每个控制步（约 1~2 秒一次）都由 `VLAExecutor` 自己判断，不调用 LLM。
2. **每一层只依赖下一层的抽象接口。** `navila_agno` 不 import `unitree_sdk2py`，
   所以规划层可以和机器狗跑在不同机器、不同 Python 环境里。
3. **所有跨层通信都是 HTTP / JSON。** 每层都能单独重启、单独测试、单独替换实现。

分工速查：

| 层 | 实现 | 时间粒度 | 负责什么 |
| --- | --- | --- | --- |
| 高层 | `AgnoSupervisor` | 10 秒 ~ 分钟 | 任务分解、跨楼层逻辑、调用地图/电梯工具、失败后重规划、长期记忆 |
| 中层 | `NaVILA` / `LightNav-0` | 每步 1~2 秒 | 从当前视角判断"这一小段该怎么走"，输出自然语言动作或 waypoint |
| 底层 | `Go2HttpRobot` → `go2_bridge.py` | 20 Hz | 速度指令下发、DDS 通信、看门狗急停 |

---

## 环境准备

仓库里有三个可执行部分，依赖完全不同，**建议分成不同环境**（尤其是 NaVILA 需要固定
Python 3.10 和它自己的 CUDA 依赖）：

| 组件 | 文件 | 建议环境 | 说明 |
| --- | --- | --- | --- |
| 上层 demo / 规划 | `demo.py`、`navila_agno/` | Python ≥ 3.10（实测 3.13 可用） | 只需 `httpx`、`python-dotenv`；接 Agno 时加 `agno`、`openai`、`sqlalchemy` |
| NaVILA bridge | `na_vila_bridge.py` | 复用 NaVILA 自己的环境（实测 py3.10） | 必须能 `import llava`，且加载 `ckpt` |
| Go2 bridge | `go2_bridge.py` | Go2 上装好 `unitree_sdk2py` 的环境 | 需要 `fastapi`、`uvicorn`、`unitree_sdk2py` |

安装上层依赖：

```bash
# 方式一：requirements.txt（推荐，覆盖 agno 全套）
pip install -r requirements.txt

# 方式二：按需安装 extras
pip install -e '.[agno]'      # 上层 + Agno
pip install -e '.[lightnav]'  # 加 websockets
pip install -e '.[bridge]'    # 加 fastapi / uvicorn（跑 bridge 用）
```

> 如果 `pip install -e .` 报 `Multiple top-level packages discovered in a flat-layout`，
> 说明 setuptools 自动发现包时被 `runs/`、`data/` 等目录干扰。本仓库已在
> `pyproject.toml` 里用 `[tool.setuptools.packages.find]` 限定为 `navila_agno*`；
> 如果本地是旧版本，直接 `pip install -r requirements.txt` 即可绕过。

---

## 配置（.env）

复制模板后填写。`.env` 已在 `.gitignore` 里，不会提交：

```bash
cp .env.example .env
```

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `MODEL_PROVIDER` | `openai_like` | Agno 的模型类型；DeepSeek 走 OpenAI 兼容接口填 `openai_like` |
| `MODEL_ID` | `deepseek-v4-pro` | 模型名 |
| `OPENAI_API_KEY` | 空 | 模型服务的 Key |
| `OPENAI_BASE_URL` | `https://api.deepseek.com/v1` | 模型服务的 base url |
| `DB_FILE` | `data/supervisor_memory.db` | Agno 的会话/记忆库（SQLite） |
| `NAVILA_ENDPOINT` | `http://127.0.0.1:8011` | NaVILA bridge 地址 |
| `LIGHTNAV_ENDPOINT` | `ws://127.0.0.1:8050` | LightNav-0 服务地址 |
| `GO2_ENDPOINT` | `http://127.0.0.1:8013` | Go2 bridge 地址 |

**没配 `.env` 也不会报错**：`--supervisor auto`（默认）会在拿不到模型时退回确定性规划，
在没装 `agno` 时退回 `MockSupervisor`。这是为了让别人 clone 下来就能先看架构。

bridge 端的变量见 [服务接口](#服务接口) 与 [多设备部署](#多设备部署)。

---

## 运行模式

### 1. 全 mock（默认，最快）

```bash
python demo.py "去 5 楼会议室 A" --supervisor mock
```

`--vla mock` 和 `--robot mock` 本来就是默认值。显式写 `--supervisor mock` 是为了
不依赖 `.env`：默认的 `auto` 在配了模型时会去调 Agno。

### 2. 真实 Agno 规划 + mock VLA

```bash
python demo.py "去 5 楼会议室 A" --supervisor agno --vla mock
```

只验证"规划粒度是否合理"，不依赖 GPU。推荐用它来调 prompt。

> 注意 `MockSupervisor` 写死的子目标 `instruction` 是中文，只适合配 `MockVLA`。
> **要让 NaVILA 真正干活，必须用 `--supervisor agno`**——它输出的 `instruction`
> 是英文（`note_zh` 才是中文，仅用于日志和字幕）。

### 3. 真实 Agno + 真实 NaVILA

前置条件：先起 NaVILA bridge（见[服务接口](#服务接口)），并确保它 `status=ready`。
`--vla navila` 需要**真实的相机帧文件**，所以不能配 `--robot mock`
（`MockRobot` 给的帧路径是 `mock://frame/N` 这种占位符，bridge 打不开会返回 500）。
两个可行组合：

```bash
# a) 用机器狗的相机（不运动，只推理）
python demo.py "穿过大门后右转，走到走廊尽头" \
  --supervisor agno --vla navila --robot go2 \
  --go2-endpoint http://10.81.6.68:8013 --go2-dry-run

# b) 用 Habitat 回放（见 closed_loop_runner.py）
CUDA_VISIBLE_DEVICES=1 ./scripts/run_closed_loop.sh --episode 86
```

`--vla http` 是 `--vla navila` 的别名。

### 4. Habitat 闭环（三进程，不需要机器狗）

用 Habitat 里的 VLN-CE episode 当"环境"，把 Agno 规划、NaVILA 推理、闭环执行串起来。
三个进程分别启动（前两个可以复用上面已经起好的）：

```bash
./scripts/run_supervisor.sh                 # Agno 规划服务  :8012
./scripts/run_na_vila_bridge.sh             # NaVILA 推理服务 :8011

CUDA_VISIBLE_DEVICES=1 ./scripts/run_closed_loop.sh \
  --episode 86 \
  --output runs/habitat/20260917-120000_demo \
  --max-outer-steps 120
```

- 输出视频与 `metrics` 落在 `--output` 指定的目录（默认 `runs/habitat`）。
- runner 需要在 `NaVILA/evaluation` 下加载配置，输出目录已按绝对路径处理。
- 这个闭环能稳定跑通流程，但**单 episode 不一定成功**（`spl` 可能是 0），
  主要用途是验证架构而不是刷指标。

### 5. 真机（Go2）

先 dry-run（真实相机、真实规划、真实推理，但**不下发运动**）：

```bash
python demo.py "穿过大门后右转，走到走廊尽头" \
  --supervisor agno --vla navila --robot go2 \
  --go2-endpoint http://10.81.6.68:8013 \
  --go2-dry-run
```

确认 `/health`、`/frame` 都正常后再打开真实运动（bridge 和客户端**两个开关都要开**）：

```bash
# Go2 端
ENABLE_MOTION=1 ./scripts/run_go2_bridge.sh

# gnode7 端
python demo.py "穿过大门后右转，走到走廊尽头" \
  --supervisor agno --vla navila --robot go2 \
  --go2-endpoint http://10.81.6.68:8013 \
  --no-go2-dry-run
```

`--robot go2` 默认是 `--go2-dry-run`（只打印 `vx / vy / vyaw / duration_sec`），
必须显式写 `--no-go2-dry-run` 才会真实运动。这是刻意设计的安全默认。

---

## 多设备部署

典型拓扑：一台带 GPU 的服务器（下面叫 gnode7）+ 机器狗 Go2。

```text
┌─── gnode7（GPU 服务器） ────────────────┐        ┌─── Go2 ────────────────┐
│  demo.py           （上层，按需启动）     │  HTTP  │  go2_bridge.py  :8013  │
│  na_vila_bridge.py :8011（常驻，占 cuda:0）│ ─────► │  ↓ unitree_sdk2py      │
│  supervisor_server.py :8012（可选，常驻）  │        │  ↓ SportClient.Move()  │
└─────────────────────────────────────────┘        └────────────────────────┘
```

| 机器 | 组件 | 端口 | 启动方式 | 是否需要常驻 |
| --- | --- | --- | --- | --- |
| gnode7 | `na_vila_bridge.py` | 8011 | `./scripts/run_na_vila_bridge.sh` | 常驻（加载约 5 分钟） |
| gnode7 | `supervisor_server.py` | 8012 | `./scripts/run_supervisor.sh` | 可选，只给 Habitat runner 用 |
| gnode7 | `demo.py` | — | 每次手动 / 脚本 | 不需要，每次都是新进程 |
| Go2 | `go2_bridge.py` | 8013 | `ENABLE_MOTION=1 ./scripts/run_go2_bridge.sh` | 常驻 |

**gnode7 上的启动顺序**

```bash
cd /storage/tyxiong_data/navila-agno-demo

# 1) NaVILA bridge：先指定 NaVILA 仓库路径，否则脚本会直接退出
export NAVILA_ROOT=/storage/tyxiong_data/NaVILA
export NAVILA_CUDA_VISIBLE_DEVICES=0        # 只占一张卡，见“排错手册”
./scripts/run_na_vila_bridge.sh
# 等到打印 Application startup complete，再验证：
curl -s http://127.0.0.1:8011/health

# 2) 跑任务
python demo.py "穿过大门后右转，走到走廊尽头" \
  --supervisor agno --vla navila --robot go2 \
  --go2-endpoint http://10.81.6.68:8013 --go2-dry-run
```

**Go2 上的启动顺序**

```bash
ssh unitree@192.168.123.18        # 有线；无线在校园网下是 10.81.6.68
cd ~/navila-agno-demo
git pull --ff-only                # 更新代码
pkill -f go2_bridge.py            # 老进程要先停掉

ENABLE_MOTION=1 PYTHON_BIN=python3 \
  ./scripts/run_go2_bridge.sh --network-interface <网卡名>

curl -s http://127.0.0.1:8013/health
```

`go2_bridge.py` 的 `--network-interface` 要填机器狗上联通 DDS 的网卡（有线常见 `eth0`，
无线场景用 `ip a` 确认）。`ENABLE_MOTION=0`（默认）时 bridge 会正常启动、能取帧、
但拒绝一切运动指令。

---

## 命令行参数

```bash
python demo.py [mission] [options]
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `mission` | `去 5 楼会议室 A` | 高层任务，中文即可 |
| `--supervisor {auto,mock,agno}` | `auto` | `auto`：能用 Agno 就用，否则退回 mock |
| `--vla {mock,http,navila,lightnav}` | `mock` | 中层 VLA 实现 |
| `--navila-endpoint` | `$NAVILA_ENDPOINT` | NaVILA bridge 地址 |
| `--lightnav-endpoint` | `$LIGHTNAV_ENDPOINT` | LightNav-0 WebSocket 地址 |
| `--robot {mock,go2}` | `mock` | 机器人实现 |
| `--go2-endpoint` | `$GO2_ENDPOINT` | Go2 bridge 地址 |
| `--go2-dry-run / --no-go2-dry-run` | `--go2-dry-run` | 安全默认：只打印指令，不真实运动 |
| `--fail-subgoal <id>` | 无 | 让某个子目标在若干步后被判为 blocked，用来演示重规划 |
| `--frame-buffer-size` | `8` | 送给 NaVILA 的最近观测帧窗口大小 |
| `--memory` | `data/episode_memory.json` | 任务记忆落盘位置 |
| `--runs-dir` | `runs` | 产物根目录 |
| `--run-name` | 用 mission 命名 | 产物目录后缀 |
| `--record-video / --no-record-video` | `--record-video` | 结束后把相机帧合成 MP4 |
| `--video-fps` | `4.0` | 视频帧率；真实推理较慢，建议 3~5 |

---

## 服务接口

### NaVILA bridge（`na_vila_bridge.py`，默认 `0.0.0.0:8011`）

| 接口 | 说明 |
| --- | --- |
| `GET /health` | `{"status": "ready"|"loading", "model_path": ..., "device": ...}` |
| `POST /v1/navigate` | 请求体见下，返回 `{"action": "...", "model_path": "..."}` |
| `GET /docs` | FastAPI 自带的交互文档 |

请求契约（客户端 `navila_agno/vla.py:build_navila_payload` 生成，只发前三个字段）：

```json
{
  "instruction": "walk to the elevator C and stop",
  "image_paths": ["frame_0001.jpg", "...", "frame_0008.jpg"],
  "num_video_frames": 8
}
```

`num_video_frames` 必须等于 `image_paths` 的长度，否则 bridge 会返回 400。
bridge 还接受可选的 `temperature`（默认 0.0）和 `max_new_tokens`（默认 64），
客户端目前不发送、用默认值。
`instruction` 必须是英文——NaVILA 的训练语言是英文，中文会显著掉点，所以
`AgnoSupervisor` 输出的子目标一律用英文，`note_zh` 只用于日志和 PPT 字幕。

### Agno supervisor server（`supervisor_server.py`，默认 `127.0.0.1:8012`）

把 Agno 规划器包成 HTTP 服务，供 `closed_loop_runner.py`（Habitat 闭环）调用：

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 服务与模型状态 |
| `POST /plan` | `{"mission": "..."}` → 子目标列表 |
| `POST /replan` | 事件 + 原任务 → 替代子目标 |

环境变量：`AGENT_SUPERVISOR_HOST`、`AGENT_SUPERVISOR_PORT`。

### Go2 bridge（`go2_bridge.py`，默认 `0.0.0.0:8013`）

| 接口 | 说明 |
| --- | --- |
| `GET /health` | `sdk_available` / `motion_enabled` / `video_enabled` |
| `POST /move` | `{vx, vy, vyaw, duration_sec, dry_run}`；`vyaw` 单位是 **rad/s** |
| `POST /stop` | 急停 |
| `POST /stand` / `POST /damp` | 站立 / 趴下 |
| `GET /state` | `SportModeState_` 最新位置、速度、模式 |
| `GET /frame` | 前视相机 JPEG（供上层存成帧路径） |

可用环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GO2_BRIDGE_HOST` / `GO2_BRIDGE_PORT` | `0.0.0.0` / `8013` | 监听地址 |
| `GO2_NETWORK_INTERFACE` | 空 | DDS 网卡 |
| `ENABLE_MOTION` | `0` | `1` 才允许真实运动 |
| `ENABLE_VIDEO` | `1` | 是否开放 `/frame` |
| `GO2_FRAME_MAX_WIDTH` | `1280` | 帧缩放宽度，`0` 表示不缩放（无线带宽紧张时调小） |
| `GO2_FRAME_JPEG_QUALITY` | `80` | JPEG 质量 |
| `GO2_MOVE_WATCHDOG_GRACE_S` | `0.5` | `/move` 卡住后的兜底停止时间 |
| `GO2_MOVE_REPEAT_PERIOD_S` | `0.05` | 周期重发 `SportClient.Move()` 的间隔 |

> `SportClient.Move()` 是 fire-and-forget：只发一次速度指令，机器狗会在很短的
> 时间窗口后自行衰减停下，表现为"说走 75 cm，实际只走了 25 cm 就顿一下"。
> 因此 bridge 会在 `duration_sec` 内每 `GO2_MOVE_REPEAT_PERIOD_S` 重发一次同样的
> 指令，保证整段位移执行完再 `StopMove()`。间隔越小越连贯，代价是 DDS 通信量变大。

### LightNav-0

LightNav-0 用 WebSocket 返回未来 SE(2) waypoint chunk。本项目把首个 waypoint
转成统一的 `MidLevelAction("move")`，再由 Go2 bridge 映射成速度指令：

```bash
# GPU 主机
PORT=8050 lightnav-serve --task vln --model_path checkpoints/LightNav-0 --backend vllm_local

# 本项目
python demo.py "去 5 楼会议室 A" --supervisor agno --vla lightnav \
  --lightnav-endpoint ws://127.0.0.1:8050 --robot go2 --go2-endpoint http://127.0.0.1:8013
```

---

## 运行产物

每次 `demo.py` 运行都会建一个独立目录，方便事后调试和做 PPT：

```text
runs/
├── go2/
│   └── 20260917-153000_去前方会议室/
│       ├── frames/       # Go2 前视相机帧，按采集顺序命名
│       ├── video.mp4     # 结束后自动合成
│       ├── steps.jsonl   # 逐步记录，每行一个 JSON
│       └── run.json      # mission / subgoals / steps / events / 视频路径
└── mock/
    └── ...
```

`run.json` 和 `steps.jsonl` 记录的内容：

- Agno 规划出的完整 `subgoals`（含中英文说明与约束）
- 每一步的 `raw_vla`：NaVILA 的原始文本输出
- 解析后的动作：`move_forward` / `turn_left` / `turn_right` / `move` / `stop`
- 发给 Go2 的指令：`vx` / `vy` / `vyaw` / `duration_sec` / `dry_run`
- Go2 bridge 的返回：`rc` / `stop_rc`
- 每一步的机器人状态、帧路径、所属子目标和步数

`runs/` 与 `data/` 都在 `.gitignore` 里，不会污染仓库。

旧版本的 `data/go2_frames/`、`output/output_closed_loop/`、`output_closed_loop/`
已不再被新流程使用，可以一键归档（不会直接删除）：

```bash
./scripts/archive_legacy_outputs.sh
```

---

## 制作演示视频与 PPT

`ppt_demo.py` 用 NaVILA 在 Habitat/VLN-CE 里已经跑成功的 episode 视频做回放，
同时实时调用 Agno 对同一任务做高层规划，最后用 ffmpeg 合成 1920x1080 视频：
左侧是模拟器回放，右侧是 Agno 规划面板，底部同步子目标字幕。

```bash
cp .env.example .env          # 填入 OPENAI_API_KEY
python ppt_demo.py            # 默认使用 episode 86
```

产物（都在 `output/` 下）：`ppt_demo.mp4`（演示视频）、`cover.jpg`（封面图）、
`plan.json`（Agno 生成的结构化子目标，作为缓存）、`text/`（字幕等中间文件）。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--episode` | `86` | 选择哪个成功 episode |
| `--speed` | `2.5` | 慢放倍率，越大越慢、字幕越好读 |
| `--no-cache` | 关 | 忽略 `plan.json`，重新调用 Agno 规划 |
| `--output` | `output` | 产物目录 |
| `--navila-root` | `$NAVILA_ROOT` | NaVILA 仓库路径 |

```bash
# 重新生成规划 + 换一个 episode + 放慢
python ppt_demo.py --no-cache --episode 42 --speed 3.0
```

`make_report_ppt.py` 直接生成汇报用的 PPT（需要 `pip install python-pptx`），
它会引用上面的视频和封面：

```bash
python make_report_ppt.py      # 输出 output/Agentic_Navigation_Report.pptx
```

> 注意 `ppt_demo.py` 用的是**回放**，不是真实闭环交互；真实闭环见
> [运行模式](#运行模式) 的 Go2 部分。

---

## 上层契约

Agno 输出给 NaVILA 的子目标：

```json
{
  "id": "go-to-elevator",
  "instruction": "walk to elevator C and stop in front of its doors",
  "note_zh": "走到电梯 C 门口并停下",
  "context": "当前在 3 楼走廊，靠右行驶",
  "constraints": ["避开楼梯间", "遇到施工区停下"],
  "completion_criteria": "距电梯门口小于 0.5 米且正对电梯门",
  "max_steps": 8,
  "estimated_distance_m": 2.0
}
```

- `instruction` 必须是英文，直接送给 NaVILA。
- `estimated_distance_m` 既要给规划做参考，也用于执行器判断"是否已经走得够远"，
  纯转向子目标设 `0.5~1.0`，不要设 0。
- `completion_criteria` 是给人看的中文说明；执行器实际用的是代码内的判定逻辑。

Executor 向上层发出的事件（`EventType`）：

| 事件 | 含义 | 上层该怎么反应 |
| --- | --- | --- |
| `subgoal_completed` | 子目标完成 | 继续下一个子目标 |
| `subgoal_failed` | 步数耗尽仍未完成 | 重规划或降低目标难度 |
| `blocked` | 检测到不可通行障碍 | 换路 |
| `uncertain` | 有歧义（例如第一步就返回 stop 但还差很远） | 重规划，或换更细/更粗的粒度 |
| `safety_stop` | 与机器人通信失败，已急停 | 先排查链路，不要盲目重试 |
| `need_help` | Agno 认为自己无法决策 | 叫人 |

当前执行器的完成判定（`navila_agno/vla.py`）：

- `robot.is_complete()`：剩余距离 ≤ 0.05 m，或机器人已标记完成。
- VLA 返回 `stop` 时，如果这是**第一步**、剩余距离 > 1.0 m、且不是纯转向子目标
  （指令里有 `turn` 且没有 `walk/move/go/proceed/forward`），则报 `uncertain`
  而不是直接判定完成——这是为了拦住"NaVILA 一上来就说到了"的假到达。
- 步数耗尽报 `subgoal_failed`，并带上剩余距离，方便判断是走不到还是识别不到。

动作解析（`parse_action`）从 NaVILA 的自由文本里抽取：

| 文本里出现 | 解析结果 |
| --- | --- |
| `stop` / `complete` / `arrived` | `stop` |
| `left`（且没有 `right`） | `turn_left`，默认 30° |
| `right` | `turn_right`，默认 30° |
| `forward` / `move` / `ahead` | 带 `cm` 时按厘米，否则按米，默认 0.25 m |

---

## 排错手册

按症状查。

### `ModuleNotFoundError: No module named 'sqlalchemy'` / `'openai'`

`agno` 装了但依赖不全，或者跑在了另一个环境里。先确认解释器：

```bash
which python && python -c "import sys; print(sys.executable)"
pip install -r requirements.txt
```

### `ImportError: AgnoSupervisor requires 'agno'`

同上；或者用 `--supervisor mock` 先跑通流程，再回来装 Agno。

### `pip install -e '.[agno]'` 报 `Multiple top-level packages discovered`

setuptools 的包发现在 flat-layout 下被 `runs/`、`data/`、`output/` 干扰。
本仓库的 `pyproject.toml` 已限定 `include = ["navila_agno*"]`；若仍报错，改用
`pip install -r requirements.txt`。

### NaVILA bridge 启动时报 `NameError: name 'load_model' is not defined`

这是历史上 `na_vila_bridge.py` 里的函数名拼写错误，已修复。确认 `git pull` 到最新，
并注意 bridge 必须用 **NaVILA 自己的 Python 环境**启动（默认 `python3`，
可用 `NAVILA_PYTHON` 指定），否则会缺 `llava` 相关依赖。

### bridge 起来了但 `/health` 的 `status` 不是 `ready`

checkpoint 还没加载完。加载约 5 分钟，看到 `Application startup complete` 再开始跑任务。

### 每张 GPU 都占了显存，影响别人用

`scripts/run_na_vila_bridge.sh` 只把 NaVILA 固定到一张卡上：

```bash
export NAVILA_CUDA_VISIBLE_DEVICES=0   # 或 1
./scripts/run_na_vila_bridge.sh
```

脚本内部的 `NAVILA_DEVICE` 会跟着变成 `cuda:0`（因为对进程而言可见的只有那一张卡）。
另外 bridge 在空闲时不会主动释放显存，不用时请把进程停掉。

### 任务莫名其妙报 `uncertain`，日志里却看不到任何报错

很可能是取帧失败，但当前版本**不会打印任何警告**：

```text
Go2HttpRobot._request      连接/超时类错误重试 max_retries 次（默认 3）
Go2HttpRobot._fetch_frame  出任何异常都返回 ""（静默）
Go2HttpRobot.observe()     回退到最近一帧；从来没有帧时 frame_path 为空
HttpVLAClient.predict      帧列表为空时只能返回 "stop"
```

也就是说，取帧坏掉时上层只会看到一个"毫无理由的 stop"。判断依据：

- `run.json` 的 `frames` 字段为 0，或 `steps.jsonl` 里 `frame_path` 为空；
- 事件是 `uncertain @ <子目标>: VLA 在第一步返回 stop，但估计剩余距离 X.XXm`。

这个保护只在第一步生效（且子目标不是纯转向）；相机如果是在子目标中途坏掉的，
仍可能被误判成"已到达"，排查时要特别小心。先量链路质量：

```bash
ping -c 20 10.81.6.68
curl -w '\nhttp=%{http_code} time=%{time_total}\n' http://10.81.6.68:8013/health
curl -w '\nhttp=%{http_code} size=%{size_download} time=%{time_total}\n' \
  http://10.81.6.68:8013/frame -o /tmp/go2_frame.jpg
```

建议 `ping` 丢包 0%、`/health` 响应 < 1 秒。带宽紧张时调小帧：

```bash
export GO2_FRAME_MAX_WIDTH=640 GO2_FRAME_JPEG_QUALITY=70
```

### 客户端报 `HTTP 422 Unprocessable Entity`

bridge 的 pydantic 校验没过，常见原因是 `vyaw` 单位错了：**必须是 rad/s，不是 deg/s**。
客户端已经把响应体带进 `RobotTransportError`，错误信息里能看到具体字段。
注意 `_request` 只在连接/超时类错误上重试，**任何非 2xx 都会立刻抛出**，
所以 422 会直接失败而不是重试，改完字段重跑即可。

### 机器人动起来了，但每次只走 25 cm 就停一下

两种可能，先看日志里的 `raw_vla`：

1. NaVILA 本身就只说了 25 cm。`parse_action` 在没有明确数字时默认 0.25 m，
   可以在 prompt 或动作解析里调整。
2. NaVILA 说了 75 cm 但实际只走了 25 cm。这是 `SportClient.Move()` 的
   fire-and-forget 特性导致的，bridge 已经用周期重发解决；如果仍然存在，
   确认 `GO2_MOVE_REPEAT_PERIOD_S` 没被调得过大。

### 子目标还没完成就跳到下一个

看 `run.json` 里上一个子目标的结束事件和 `remaining_distance_m`：

- 如果是 `subgoal_completed` 且原因是 VLA 返回 `stop`，说明 NaVILA 判断"到了"，
  但距离还很远 → 这是假到达，需要收紧完成判定或让 Agno 把子目标写得更明确
  （带可识别的地标）。
- 如果是第一步就返回 `stop`，当前逻辑会报 `uncertain` 并交给 Agno 重规划。

### `AttributeError: 'Go2HttpRobot' object has no attribute '_start_position'`

历史版本在初始化顺序上有 bug，已修复（子目标开始前先初始化机器人状态）。

### ssh 连不上机器狗

- 校园网里 ping 得通但 ssh 不上：Go2 无线口常不跑 sshd，只有有线口监听。
- 接网线后可用 `192.168.123.18`（Unitree 默认网段）。
- 想做无线调试，需要在 Go2 上把 sshd 开到无线网卡并放行防火墙，或者用
  `ssh -o BindAddress=...` 之外的方式（例如在 Go2 上跑一个反向隧道）绕过去。

### 装 `unitree_sdk2py` 失败（`Could not locate cyclonedds`）

这是 Go2 环境的问题，和本仓库无关。要点：

- 优先用 `pip install unitree_sdk2py` 的预编译 wheel，别默认走源码编译。
- 必须源码编译时，先装 CycloneDDS 到系统路径（`CMAKE_PREFIX_PATH` 指到它的
  install 目录），再编译。
- 编译中若报 `idlc: undefined symbol: DDXTypes_Typeobject_desc`，说明系统里
  混了多个版本的 CycloneDDS，`idlc` 和库不是同一份。清理干净再重装，不要复用
  上一次的 `build/` 目录。

### 视频里的英文/数字变成方框

绘图用的中文字体缺拉丁字形（本机历史上踩过的 `Droid Sans Fallback` 就是这种情况）。
本仓库自带 `assets/fonts/NotoSansCJKsc-Regular.otf`，中英文和数字都覆盖，
`ppt_demo.py` 固定用它，`closed_loop_runner.py` 默认用它（可用 `--font` 覆盖）。
如果换成别的字体，先用一小段文字渲染验证再批量生成。

---

## 测试

```bash
python -m unittest discover -s tests
```

- `tests/test_demo.py`：NaVILA 文本→动作解析、mock 闭环跑完、blocked 触发重规划。
- `tests/test_backends.py`：VLA 后端选择、NaVILA payload 构造、LightNav waypoint
  转换、Go2 速度指令映射（含转向符号与限幅）、机器人生命周期。

测试完全离线，不需要 GPU、模型或网络。

---

## 目录结构

```text
navila-agno-demo/
├── demo.py                   # 主入口：跑一次任务
├── na_vila_bridge.py         # NaVILA HTTP 封装（FastAPI）
├── supervisor_server.py      # Agno 规划器的 HTTP 封装（给 Habitat runner 用）
├── closed_loop_runner.py     # Habitat/NaVILA/Agno 三进程闭环 runner
├── go2_bridge.py             # Go2 HTTP bridge（unitree_sdk2py）
├── ppt_demo.py               # 生成 PPT 演示视频（用成功 episode 回放）
├── make_report_ppt.py        # 生成汇报 PPT
├── navila_agno/
│   ├── contracts.py          # SubGoal / Action / Event 契约
│   ├── artifacts.py          # 运行目录、帧管理、视频合成、run.json
│   ├── robot.py              # MockRobot + Go2HttpRobot
│   ├── vla.py                # MockVLA / NaVILAHttpClient / LightNav / VLAExecutor
│   ├── supervisor.py         # MockSupervisor / AgnoSupervisor
│   ├── runtime.py            # 把 supervisor 和 executor 连起来
│   ├── memory.py             # 轻量任务记忆
│   ├── tools.py              # map_route / robot_status / ask_human
│   └── skills/navigation/SKILL.md   # Agno 导航 Skill
├── scripts/                  # 启动与运维脚本
├── tests/                    # 离线测试
├── assets/fonts/             # 视频字幕字体（CJK + 拉丁字形都全，别换掉）
├── runs/                     # 运行产物，按 run 分目录（不提交）
├── data/                     # 记忆库与临时数据（不提交）
├── output/                   # ppt_demo.py 的产物（不提交）
├── requirements.txt
├── pyproject.toml
└── .env.example
```

---

## 已知限制与下一步

已知限制：

- 完成判定仍偏启发式（步数 / 距离 / NaVILA 的 stop），没有真正的到位识别。
- `map_route` 目前是确定性 waypoint，还没有接真实室内地图或地图软件 API。
- 没有电梯、闸机这类需要外部交互的设备的真实接口。
- Go2 真实运动只在直走廊验证过，长任务的成功率还在迭代。

下一步：

- 给每个子目标加机器可判定的停止条件，并让 Agno 对"切分不下"的段落直接合并。
- 把 `Go2HttpRobot` 的取帧换成 `/frame` 连续流或 ROS2 vision bridge，降低 Wi-Fi 抖动影响。
- 用真实室内语义图或地图 API 扩展 `map_route`，替代确定性 waypoint。
- 用 LightNav-0 的 `robot_deploy/` MPC 控制器验证 waypoint 到 Go2 步态的执行层。
- 把 `EpisodeMemory` 换成 Postgres/向量库，承载跨任务、跨楼层的导航记忆。
