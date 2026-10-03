# DimOS 项目架构（最简单的说法）

> 一句话：**DimOS 是一个"机器人操作系统"，让很多模块通过"数据流"连在一起，组成能跑在真实机器人上的系统，还能让 AI（Agent）用自然语言控制机器人。**

---

## 1. 打个比方

把机器人想象成一个人：

| 概念 | 比喻 | 是什么 |
|------|------|--------|
| **Module（模块）** | 器官（眼睛、腿、大脑） | 一个独立功能单元，比如"看"、"走"、"说话" |
| **Stream（数据流）** | 神经 / 血管 | 模块之间传数据的"管道"，有类型 |
| **Blueprint（蓝图）** | 身体构造图 | 把一堆模块拼成一个完整"机器人大脑+身体" |
| **Skill（技能）** | 会做的事（`grab`、`walk`、`follow`） | 给 AI 暴露出来的、可被调用的功能 |
| **Agent（智能体）** | 大脑 / 指挥 | 用自然语言理解任务，再调用 Skill 去执行 |

**核心思想：模块各自独立，靠"流"通信；蓝图把它们拼起来；AI 通过 Skill 驱动整个系统。**

---

## 2. 三个核心概念

### ① Module（模块）——最小的功能单元
- 每个模块是**一个子系统**（感知、导航、控制、说话……）。
- 模块之间**不直接调用**，而是通过 **Stream** 收发数据。
- 数据用**类型标注**，例如 `In[Image]`（接收图像）、`Out[Pose]`（输出位置）。

```python
class MyModule(Module):
    color_image: In[Image]      # 接收图像
    processed:   Out[Image]     # 输出处理后的图像

    def _process(self, img):
        self.processed.publish(do_something(img))
```

### ② Stream（流）——模块之间的"管道"
- 三种类型：
  - `In[T]`  —— 输入（只收）
  - `Out[T]` —— 输出（只发）
  - `Transport[T]` —— 传输（既收又发，桥接不同模块/进程）
- 底层可以跑在 **LCM / SHM / ROS / DDS** 等传输方式上（可切换）。
- 用 `In`/`Out` 的名字和类型自动匹配连接，不用手写连线。

### ③ Blueprint（蓝图）——把模块拼成系统
```python
my_blueprint = autoconnect(module_a(), module_b(), module_c())
```
- `autoconnect()` 会自动把**名字+类型匹配**的流连起来。
- `.build()` 部署所有模块（在独立 worker 进程里），`.loop()` 运行。

---

## 3. 数据是怎么流动的

```
摄像头/传感器
     │
     ▼
[感知模块] ──Out[Image]──► [AI/Agent] ──调用 Skill──► [控制/运动模块] ──► 电机
     │                        ▲                              ▲
     └───────────── Stream（流）─────────────────────────────┘
                    （所有模块都靠"流"通信，互不耦合）
```

1. 传感器产生数据 → 进某个模块的 `In`。
2. 模块处理完 → 从 `Out` 发出去。
3. `autoconnect` 自动把上一个模块的 `Out` 接到下一个模块的 `In`。
4. Agent 订阅/观察这些流，需要时**调用 Skill** 让机器人动作。

---

## 4. Agent + Skill（AI 控制机器人的部分）

- **Skill（技能）**：用 `@skill` 装饰一个函数，它就变成一个"AI 能调用的工具"。
  ```python
  @skill
  def move(self, x: float, duration: float = 2.0) -> str:
      """Move the robot forward or backward."""   # 必须有 docstring
      return "Moving"
  ```
- **Agent（智能体）**：一个 LLM（大模型），通过 **MCP**（McpServer + McpClient）拿到所有 Skill 当"工具"。
- 流程：人说话 → Agent 理解 → 选 Skill 调用 → 机器人动作。
- **RPC / Spec**：模块之间要调用对方的方法时，用 `Spec`（Protocol）声明接口，蓝图在构建时**自动注入**对应模块，类型安全、构建期就发现错误。

---

## 5. 目录结构（重点部分）

```
dimos/
├── core/            # 地基：模块系统、流、传输、蓝图、worker
│   ├── module.py          # 模块基类、In/Out
│   ├── stream.py          # 流定义（In/Out/Transport）
│   ├── transport.py       # LCM/SHM/ROS/DDS 传输
│   ├── core.py            # @rpc 装饰器
│   ├── global_config.py   # 全局配置
│   └── coordination/      # 蓝图、协调器、worker
│
├── agents/        # AI 智能体
│   ├── mcp/          # McpServer / McpClient / McpAdapter（Agent 在这里）
│   ├── skills/       # 各种 Skill 容器
│   └── system_prompt.py   # 给 AI 的提示词
│
├── robot/         # 各家机器人实现
│   ├── unitree/    # 宇树 Go2 / G1 / B1
│   ├── drone/      # 无人机
│   └── manipulators/ # 机械臂 xArm 等
│
├── perception/    # 感知：目标检测、跟踪
├── navigation/    # 导航：路径规划
├── control/       # 控制
├── manipulation/  # 机械操作
├── mapping/       # 建图
├── msgs/          # 消息类型定义
└── cli/           # `dimos` 命令行工具
```

**一句话记忆：`core` 是地基，`agents` 是大脑，`robot` 是身体，中间那堆（perception/navigation/control…）是各种器官能力。**

---

## 6. 怎么跑起来

```bash
dimos list                      # 列出所有能跑的蓝图
dimos run unitree-go2-agentic  # 运行一个蓝图（机器人=Go2，带 AI）
dimos --replay run unitree-go2 # 回放数据跑（不需要真机）
dimos agent-send "walk forward 2 meters"   # 用自然语言指挥机器人
dimos status / stop / log      # 查看 / 停止 / 看日志
```

- **Blueprint（蓝图）** 就是"某个机器人 + 一堆模块 + AI"的预设组合。
- `--replay`（回放）、`--simulation`（仿真）、`--robot-ip`（真机）切换运行方式。

---

## 7. 总结（一张图记住）

```
        ┌──────────── Agent (AI 大脑，MCP) ────────────┐
        │          理解自然语言，调用 Skill             │
        ▼                                              │
   ┌─────────┐   流   ┌─────────┐   流   ┌──────────┐
   │ 感知模块 │ ────► │ 导航模块│ ────► │ 控制模块 │  ──► 机器人动作
   └─────────┘       └─────────┘       └──────────┘
        每个 Module 通过 Stream 通信；Blueprint 把它们拼起来
```

- **Module** = 独立功能单元
- **Stream** = 模块间的管道（带类型）
- **Blueprint** = 把模块拼成系统
- **Skill** = 给 AI 调用的功能
- **Agent** = 用自然语言驱动一切的 AI