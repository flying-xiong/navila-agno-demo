# NaVILA x Agno Supervisor-Executor Demo

一个独立、可运行的最小 demo，用来验证 NaVILA 与 Agno 的最佳协作形式：

> **Agno 是慢思考的监督者/规划器，NaVILA 是快行动的中层执行器，机器狗底层控制器负责实时安全。**

项目默认使用确定性 `MockSupervisor` 和 `MockVLA`，无需 API Key、无需 GPU、无需启动 NaVILA bridge 即可跑通完整闭环。真实 Agno 和真实 NaVILA 接口已经保留。

## 架构

```text
                    ┌──────────────────────────────┐
                    │ Agno Supervisor               │
                    │ 规划 / 重规划 / 记忆 / 工具     │
                    └───────────────┬──────────────┘
                                    │ SubGoal
                                    ▼
┌──────────────────────────────────────────────────────────┐
│ VLAExecutor（闭环运行）                                    │
│   历史帧缓冲 → NaVILA policy → action → robot              │
└───────────────┬──────────────────────────────┬───────────┘
                │ 帧 / 状态                      │ Event
                ▼                                ▼
        Robot / Camera / LiDAR          completed / failed /
                                        blocked / uncertain /
                                        safety_stop / need_help
```

关键点：

- Agno 不进入高频控制循环，只在任务开始、子目标完成或异常事件时介入。
- NaVILA 通过 HTTP bridge 独立运行，不与 Agno 共用 Python 环境。
- 帧记忆分两层：robot 按任务累积整段历史帧，bridge 每次推理把它采样成
  NaVILA 需要的 8 帧片段（见「接入真实 NaVILA」）。
- 中层动作从 NaVILA 的自由文本解析为结构化动作，再交给机器人底层。

## 目录

```text
navila-agno-demo/
├── demo.py                          # CLI 入口
├── ppt_demo.py                      # 生成 PPT 演示视频（NaVILA 回放 + Agno 规划叠加）
├── navila_agno/
│   ├── contracts.py                 # SubGoal / Action / Event 契约
│   ├── artifacts.py                 # 运行目录、帧管理、视频合成、run.json
│   ├── robot.py                     # MockRobot + frame ring buffer
│   ├── vla.py                       # MockVLA / NaVILAHttpClient / VLAExecutor
│   ├── supervisor.py                # MockSupervisor / AgnoSupervisor
│   ├── tools.py                     # map_route / robot_status / ask_human
│   ├── memory.py                    # 轻量任务记忆
│   ├── runtime.py                   # 将 supervisor 与 executor 连接
│   └── skills/navigation/SKILL.md   # Agno 导航 Skill
├── tests/test_demo.py               # 离线闭环测试
├── runs/                            # 每次运行的产物（不提交 Git）
├── requirements.txt
├── pyproject.toml
└── .env.example
```

## 生成 PPT 演示视频

脚本使用 NaVILA 在 Habitat/VLN-CE 中已生成的成功 episode 回放，并实时调用
Agno（DeepSeek-V4-Pro）对同一任务做高层规划，最后用 ffmpeg 合成为
1920x1080 视频（左侧 NaVILA 模拟器回放，右侧 Agno 规划面板，底部同步子目标字幕）。

```bash
cd navila-agno-demo
cp .env.example .env      # 填入 OPENAI_API_KEY（.env 不提交到 Git）
python ppt_demo.py
```

输出文件：

- `output/ppt_demo.mp4`：演示视频
- `output/cover.jpg`：PPT 封面图
- `output/plan.json`：Agno 生成的结构化子目标（缓存，可用 `--no-cache` 重新生成）

常用参数：

```bash
# 重新生成 Agno 规划，不读缓存
python ppt_demo.py --no-cache

# 选择其他成功 episode，或调整慢放倍率
python ppt_demo.py --episode 42 --speed 2.0
```

默认使用 `NaVILA/evaluation/eval_out/.../episode=86-ckpt=0-spl=1.00.mp4`。
如需真实闭环交互（Agno 每个子目标动态调用 NaVILA），可在此脚本基础上把
视频回放替换为 `VLAExecutor + NaVILAHttpClient`。

## 真实闭环运行（Habitat + NaVILA + Agno）

三个进程分别在两个 Python 环境里运行：

```bash
cd navila-agno-demo

# 1. 启动 Agno 规划服务（Python 3.13 + agno）
./scripts/run_supervisor.sh

# 2. 启动 NaVILA 推理服务（navila-eval 环境，加载 checkpoint）
./scripts/run_na_vila_bridge.sh

# 3. 启动 Habitat 闭环 runner（navila-eval 环境）
CUDA_VISIBLE_DEVICES=1 ./scripts/run_closed_loop.sh \
  --episode 86 \
  --output runs/habitat/20260917-120000_demo \
  --max-outer-steps 120
```

输出：

- `runs/habitat/<run_id>/episode=86-ckpt=0-spl=0.00.mp4`

说明：

- `supervisor_server.py` 默认监听 `127.0.0.1:8012`。
- `na_vila_bridge.py` 默认监听 `127.0.0.1:8011`。
- runner 会在 `NaVILA/evaluation` 目录加载配置；输出目录已处理为绝对路径。
- 当前演示闭环能稳定跑通，但单 episode 不一定成功（`spl` 可能为 0），主要用于验证架构和流程。

## 快速开始（无依赖模型）

```bash
cd navila-agno-demo

# 可选：创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 默认 mock 闭环
python demo.py "去 5 楼会议室 A"
```

演示失败重规划：

```bash
python demo.py "去 5 楼会议室 A" --fail-subgoal go-to-elevator
```

运行测试：

```bash
python -m unittest discover -s tests
```

## 接入真实 Agno

1. 安装依赖并配置模型：

```bash
cp .env.example .env
# 编辑 .env：MODEL_PROVIDER / MODEL_ID / OPENAI_API_KEY / OPENAI_BASE_URL
```

2. 使用 Agno supervisor：

```bash
python demo.py "去 5 楼会议室 A" --supervisor agno --vla mock
```

未配置模型时，`AgnoSupervisor` 会自动退回确定性规划，便于先验证架构。

## 接入真实 NaVILA

1. 在另一个进程启动 NaVILA bridge（本 demo 不包含 NaVILA 模型加载代码）：

```bash
# 示例：复用你的 NaVILA bridge，监听 http://127.0.0.1:8011
```

2. 让 executor 通过 HTTP 调用 NaVILA：

```bash
export NAVILA_ENDPOINT=http://127.0.0.1:8011
python demo.py "去 5 楼会议室 A" --supervisor agno --vla http
```

`NaVILAHttpClient` 发送的请求契约：

```json
{
  "instruction": "走到电梯 C 门口并停下",
  "image_paths": ["hist_0.jpg", "hist_1.jpg", "...", "current.jpg"],
  "num_video_frames": 8
}
```

`image_paths` 是**整段任务的历史帧**，`num_video_frames` 是目标片段长度
（`NAVILA_NUM_VIDEO_FRAMES`，默认 8，与 checkpoint 训练时一致）。bridge 负责
按官方 `sample_and_pad_images` 的语义把历史采样成 8 帧：

- 历史不足 8 帧：前面补黑帧，真实帧一帧不丢；
- 历史超过 8 帧：`linspace(0, N-1, num=7, endpoint=False)` 在**整段轨迹**上均匀
  取 7 帧，再拼上最新帧。

采样结果会随 `NavigateResponse.sampled_frame_paths` 返回，`steps.jsonl` 里每步也
记了 `history_frames` / `video_frames`，方便核对模型到底看到了哪几帧。

### 观测记忆模式

`--frame-memory` 决定 VLA 能看到多少历史（默认 `episode`，即 NaVILA 官方行为）：

| 模式 | 送入 VLA 的帧 | 说明 |
| --- | --- | --- |
| `episode` | 整段任务的帧 | 默认。跨度覆盖整条轨迹，走廊起点/经过的门不会被丢掉 |
| `subgoal` | 当前子目标开始后的帧 | 折中，避免上一个子目标的画面干扰新指令 |
| `recent` | 最近 `--frame-buffer-size` 帧 | 旧行为，滑动窗口，只保留最近 8 步 |

`--vla lightnav` 时建议用 `recent`：LightNav 的 WebSocket 协议要把历史序列逐帧重放，
`episode` 会让每步重放开销随任务长度线性增长。

## 接入 LightNav-0

LightNav-0 使用 WebSocket 协议，返回未来 SE(2) waypoint chunk。本项目把首帧
waypoint 转换为统一的 `MidLevelAction("move")`，再由 Go2 bridge 映射为速度指令。

```bash
# 在 GPU 主机启动 LightNav-0 官方服务
PORT=8050 lightnav-serve --task vln --model_path checkpoints/LightNav-0 --backend vllm_local

# 在本项目环境运行
python demo.py "去 5 楼会议室 A" --supervisor agno   --vla lightnav --lightnav-endpoint ws://127.0.0.1:8050   --robot go2 --go2-endpoint http://127.0.0.1:8013
```

依赖：`pip install -e '.[lightnav]'`（仅需 `websockets`）。

## 接入 Unitree Go2

真实 Go2 控制通过独立的 `go2_bridge.py` 完成，`navila_agno` 包不直接依赖
`unitree_sdk2py`，因此 Agno/VLA 可运行在与机器狗相连的任意主机上。

1. 在 Go2 上位机或联网主机启动 bridge：

```bash
./scripts/run_go2_bridge.sh --network-interface eth0
# 默认监听 http://0.0.0.0:8013，ENABLE_MOTION=0，只做 dry-run
```

2. 先在本地做无运动闭环验证：

```bash
python demo.py "去 5 楼会议室 A" --supervisor mock --vla mock   --robot go2 --go2-endpoint http://127.0.0.1:8013 --go2-dry-run
```

3. 确认安全后，同时打开 bridge 与客户端的真实运动开关：

```bash
# bridge 端
ENABLE_MOTION=1 ./scripts/run_go2_bridge.sh

# 客户端端
python demo.py "去 5 楼会议室 A" \
  --supervisor agno \
  --vla navila \
  --robot go2 \
  --go2-endpoint http://127.0.0.1:8013 \
  --no-go2-dry-run
```

Go2 bridge 提供的接口：

- `GET /health`：SDK/相机/运动开关状态
- `POST /move`：`vx, vy, vyaw, duration_sec, dry_run`
- `POST /stop`：急停
- `POST /stand` / `POST /damp`：站立/趴下
- `GET /state`：`SportModeState_` 的最新位置、速度、模式
- `GET /frame`：前视相机 JPEG，供 VLA 保存为本地帧路径

Go2 SDK 尚未安装时，bridge 仍可启动，`/state` 会返回 `available=false`，
`demo.py --go2-dry-run` 可继续验证上层流程。

### 真机运动排查

真机运动前先确认链路质量：

```bash
ping -c 20 10.81.6.68
curl -w '\nhttp=%{http_code} time=%{time_total}\n' http://10.81.6.68:8013/health
curl -w '\nhttp=%{http_code} size=%{size_download} time=%{time_total}\n' \
  http://10.81.6.68:8013/frame -o /tmp/go2_frame.jpg
```

- 建议 `ping` 丢包为 0%，`/health` 响应小于 1 秒。
- 如果客户端报 `httpx.ConnectTimeout`，通常是 Go2 WiFi 丢包或 bridge 被阻塞，不是运动指令本身的问题。
- Go2 端 bridge 会返回 `rc` 和 `stop_rc`；`rc != 0` 表示 SDK 拒绝了运动指令。
- 客户端报 `HTTP 422` 说明 bridge 的 pydantic 校验没通过（例如 `vyaw` 超出
  ±5 rad/s）。客户端已把响应体带进 `RobotTransportError`，错误信息里能看到
  具体字段；注意转向角速度必须用 rad/s，不要直接填 deg/s。
- 客户端对 `/state`、`/frame`、`/move` 默认重试 3 次；重试仍失败会触发 `safety_stop` 事件并调用 `/stop`。

可用环境变量：

```bash
export GO2_FRAME_MAX_WIDTH=1280      # 0 表示不缩放，降低 WiFi 带宽
export GO2_FRAME_JPEG_QUALITY=80
export GO2_MOVE_WATCHDOG_GRACE_S=0.5 # /move 卡住时的兜底停止时间
export GO2_MOVE_REPEAT_PERIOD_S=0.05 # 周期重发 SportClient.Move() 的间隔
```

`SportClient.Move()` 是 fire-and-forget：只发一次速度指令，机器人会在很短的
时间窗口后自行衰减停下（表现为“说走 75 cm，实际只走了 25 cm”）。因此 bridge
会在 `duration_sec` 内每 `GO2_MOVE_REPEAT_PERIOD_S` 重发一次同样的指令，
保证整段位移执行完再 `StopMove()`。间隔越小动作越连贯，但 DDS 通信量越大。

## 运行产物与视频

每次 `demo.py` 运行都会创建独立目录：

```text
runs/
├── go2/
│   └── 20260917-153000_去前方会议室/
│       ├── frames/          # Go2 前视相机帧，按采集顺序命名
│       ├── annotated/       # 已烧入字幕的帧，可直接插到 PPT 当截图
│       ├── video.mp4        # 自动合成的 MP4（带字幕，节奏放慢）
│       ├── steps.jsonl      # 逐步调试记录，每行一个 JSON
│       └── run.json         # mission / subgoals / steps / events / 视频路径
└── mock/
    └── ...
```

### 带字幕的 PPT 视频

默认会把 `任务 / 子目标 / 当前动作` 烧进每一帧，方便对着视频讲“现在走到哪一步”：

- 顶部：任务原文（左）+ 机器人状态（右，例如 `mode=walk`）
- 底部：`子目标 sg-1 (1/3)`、`step 2/4`、子目标原文、NaVILA 输出的原句、
  映射出的动作、下发的 Go2 指令、剩余距离与当前位姿
- 最底边：细进度条表示该子目标内的步数进度
- 片头是任务卡，片尾是结果卡（成功与否、子目标清单）

节奏靠每步停留时长控制，默认 `--video-hold-s 1.2`（停下动作自动延长 1.6 倍），
这样每一步都能停下来讲；配合 `--video-intro-s` / `--video-outro-s` 调片头片尾。

```bash
# 更慢、更适合逐帧讲解
python demo.py "去前方会议室" \
  --supervisor agno --vla navila --robot go2 \
  --go2-endpoint http://10.81.6.68:8013 --no-go2-dry-run \
  --video-hold-s 2.0

# 不要字幕，回到原始固定帧率
python demo.py "去前方会议室" --robot go2 --go2-dry-run --no-video-overlay --video-fps 4
```

已跑完的任务可以离线重渲染，不用再连机器人：

```bash
python scripts/render_run_video.py runs/go2/20260917-153000_去前方会议室 --hold-s 2.0
# 输出 runs/go2/<run_id>/video_annotated.mp4 与 annotated/ 下的静帧
```

字幕用 Pillow 直接画进像素，不走 ffmpeg `drawtext`。原因是这台机器上常见的
CJK 兜底字体（Droid Sans Fallback）不含任何拉丁字母和数字字形，直接用会把
所有英文和数字渲染成方框；`navila_agno/overlay.py` 因此按字符在“拉丁字体”和
“CJK 字体”之间切换，保证中英文都正常。

依赖：`pillow`（已在 `requirements.txt`）。如果当前环境缺 Pillow，`demo.py` 会打印
提示并自动退回无字幕的固定帧率合成，不会中断任务；补装即可：

```bash
python -m pip install pillow
```

默认开启视频合成，可用参数调整：

```bash
# 关闭视频合成
python demo.py "去前方会议室" --robot go2 --go2-dry-run --no-record-video

# 调整合成帧率（真实推理较慢建议 3~5）
python demo.py "去前方会议室" --robot go2 --go2-dry-run --video-fps 3

# 自定义运行目录名后缀
python demo.py "去前方会议室" --robot go2 --go2-dry-run --run-name demo1

# 修改产物根目录
python demo.py "去前方会议室" --robot go2 --go2-dry-run --runs-dir /data/navila_runs
```

`run.json` 和 `steps.jsonl` 会记录：

- Agno 规划出的完整 `subgoals`
- 每一步的 `raw_vla`：NaVILA 原始文本输出
- 解析后的 `action`：`move_forward` / `turn_left` / `turn_right` / `move` / `stop`
- 发送给 Go2 的 `command`：`vx` / `vy` / `vyaw` / `duration_sec` / `dry_run`
- Go2 bridge 返回的 `command_response`：`rc` / `stop_rc`
- 每一步的机器人状态、帧路径、事件和步数

如果 NaVILA 在第一步就返回 `stop`，但 `remaining_distance_m` 仍大于
`0.5m`，会触发 `uncertain` 事件并请求 Agno 重新规划，而不是直接进入下一个子目标。

旧版本的 `data/go2_frames/`、`output/output_closed_loop/` 和
`output_closed_loop/` 已不再被新流程使用。可以一键归档：

```bash
./scripts/archive_legacy_outputs.sh
```

归档后会移动到 `archive/legacy_<timestamp>/`，不会直接删除。

## 上层契约示例

Agno 输出给 NaVILA 的子目标：

```json
{
  "id": "go-to-elevator",
  "instruction": "走到电梯 C 门口并停下",
  "context": "当前在 3 楼走廊，靠右行驶",
  "constraints": ["避开楼梯间", "遇到施工区停下"],
  "completion_criteria": "距电梯门口小于 0.5 米且正对电梯门",
  "max_steps": 8,
  "estimated_distance_m": 2.0,
  "stop_condition": {
    "kind": "vla_stop_after_distance",
    "distance_m": 2.0,
    "description": "走到电梯 C 门口并停下"
  }
}
```

`stop_condition` 是执行器判断子目标何时结束的唯一依据，四类取值：

- `vla_stop_after_distance`：VLA 说 stop **且**走够 `distance_m`。粗粒度、
  以地标结尾的段落的默认规则。`distance_m` 同时是下限（提前 stop 会交回重规划）
  和上限（超预算 30% 仍没等到 stop 也会交回），所以估计宁可略大。
- `distance`：走够 `distance_m` 就停，不等 VLA。适合沿走廊直行的固定段落。
- `steps`：执行固定步数。只用于既不需要识别地标、也不用判断转向是否到位的段落。
- `vla_stop`：完全信任 VLA 的 stop。只用于几米内、外观极明确的目标。

分解原则是**宁粗勿细**：模型写不出可判定停止条件的切分点会被自动并入前一段；
用步数表示"转向完成"也会被并入（步数无法证明转到位）。
近期的 dry-run 记录见 `runs/go2/`：`20260917-144205_coarse-merge` 里合并后的
长段一直等不到 VLA 的 stop，共走 82 步、3 次耗尽 `max_steps` 硬失败；
`20260917-150617_coarse-final` 用同一任务只走 33 步，超过预算就交回 Agno 重规划。

Executor 发出的事件：

```text
subgoal_completed
subgoal_failed
blocked
uncertain
safety_stop
need_help
```

## 下一步

- NaVILA 记忆已对齐官方（全历史 + 8 帧均匀采样）；下一步接你 NaVILA 分支里的
  子模记忆采样器 `llava/vlnce_memory_sampler.py`，与均匀采样做 A/B 对比。
- 将 `Go2HttpRobot` 的相机帧接入 Go2 bridge `/frame` 的连续流/ROS2 vision bridge。
- 用真实室内语义图或地图 API 扩展 `map_route`，替代当前确定性 waypoint。
- 用 LightNav-0 的 `robot_deploy/` MPC 控制器验证 waypoint 到 Go2 步态的执行层。
- 将 `EpisodeMemory` 换成 Postgres/向量库，承载跨任务导航记忆。
