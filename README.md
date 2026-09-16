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
│   frame ring buffer → NaVILA policy → action → robot      │
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
- 中层动作从 NaVILA 的自由文本解析为结构化动作，再交给机器人底层。

## 目录

```text
navila-agno-demo/
├── demo.py                          # CLI 入口
├── ppt_demo.py                      # 生成 PPT 演示视频（NaVILA 回放 + Agno 规划叠加）
├── navila_agno/
│   ├── contracts.py                 # SubGoal / Action / Event 契约
│   ├── robot.py                     # MockRobot + frame ring buffer
│   ├── vla.py                       # MockVLA / NaVILAHttpClient / VLAExecutor
│   ├── supervisor.py                # MockSupervisor / AgnoSupervisor
│   ├── tools.py                     # map_route / robot_status / ask_human
│   ├── memory.py                    # 轻量任务记忆
│   ├── runtime.py                   # 将 supervisor 与 executor 连接
│   └── skills/navigation/SKILL.md   # Agno 导航 Skill
├── tests/test_demo.py               # 离线闭环测试
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
  --output output_closed_loop \
  --max-outer-steps 120
```

输出：

- `output_closed_loop/episode=86-ckpt=0-spl=0.00.mp4`

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
  "image_paths": ["hist_0.jpg", "hist_1.jpg", "current.jpg"],
  "num_video_frames": 3
}
```

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
python demo.py "去 5 楼会议室 A" --supervisor agno --vla navila   --robot go2 --go2-endpoint http://127.0.0.1:8013 --no-go2-dry-run
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
  "estimated_distance_m": 2.0
}
```

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

- 将 `Go2HttpRobot` 的相机帧接入 Go2 bridge `/frame` 的连续流/ROS2 vision bridge。
- 用真实室内语义图或地图 API 扩展 `map_route`，替代当前确定性 waypoint。
- 用 LightNav-0 的 `robot_deploy/` MPC 控制器验证 waypoint 到 Go2 步态的执行层。
- 将 `EpisodeMemory` 换成 Postgres/向量库，承载跨任务导航记忆。
