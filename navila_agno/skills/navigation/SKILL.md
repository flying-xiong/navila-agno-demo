---
name: navigation
description: 高层导航监督与子目标分解流程。当需要把高层目的地分解为可执行的短段、处理电梯/楼层切换或从导航失败中恢复时使用。
---

# Navigation Supervisor Skill

## 何时使用

- 用户给出目的地或空间目标。
- 需要把长程任务分解为 NaVILA 可执行的短子目标。
- 导航遇到阻塞、失败或不确定状态，需要重规划。

## 流程

1. 调用 `map_route` 获取统一路点和楼层切换信息。
2. 将路线切成短子目标，每个子目标包含：
   - 简短的 `instruction`；
   - 当前楼层/朝向等上下文；
   - 可验证的 `completion_criteria`；
   - 必要的约束与 `max_steps`。
3. 调用 `robot_status` 确认起点与电量。
4. 将子目标交给中层 VLAExecutor，不要逐步控制 NaVILA。
5. 监听 `subgoal_completed`、`blocked`、`subgoal_failed` 等事件。
6. 失败时生成替代子目标；无法决策时调用 `ask_human`。
7. 任务结束后总结成功路径与失败原因，写入长期记忆。

## 约束

- 一次只给一个短子目标，避免一次规划过远。
- 不把整张地图或长期对话塞进 NaVILA 指令。
- 遇到安全相关动作，优先请求人工确认。
