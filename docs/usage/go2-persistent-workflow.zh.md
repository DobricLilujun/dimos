# Go2 持久化建图、标签与 Agent 操作手册

本文适用于 `unitree-go2-agentic-persistent`，使用 Go2 内置雷达，并将地图和语义记忆保存在：

```text
assets/scene_maps/sedan_office_persistent
```

## 1. 工作流概览

**第一次使用：**

```text
创建新地图 → 遥控建图 → 自动/手动标签 → 查询检查 → 保存 → 正常停止
```

**第二次及以后：**

```text
加载旧地图 → 遥控采集 → 停稳 → 结束采集 → 检查配准 → 人工确认
    → 查询/聊天/标签/导航 → 保存 → 正常停止
```

> 安全注意：下面的启动命令使用 `--no-obstacle-avoidance`，关闭 Go2 机载避障。遥控采集没有新增自动避障，请低速操作、保持净空并现场看护。该参数不关闭 DimOS 规划器自身的障碍物处理。紧急情况优先使用现场遥控或硬件停止手段，不要等待 LLM 回复。

## 2. 准备环境

每个终端都进入项目目录，并使用相同的 Python 环境：

```bash
cd /Users/lujun.li/dimensional-applications/dimos
source venv-dev/bin/activate
```

在运行机器人栈的终端设置实际机器人 IP：

```bash
export ROBOT_IP="192.168.123.161"
mkdir -p assets/scene_maps/sedan_office_persistent
```

确保 OpenAI API 凭据已通过项目配置或环境变量设置。不要把密钥写入本文或提交到仓库。

建议分开使用终端：

| 终端 | 用途                                       |
| ---- | ------------------------------------------ |
| A    | 启动和运行机器人栈                         |
| B    | `dimos agentspy` 实时查看 Agent 消息     |
| C    | `dimos shell` 采集、配准、保存和状态检查 |
| D    | `agent-send`、MCP 调用和日志             |

## 3. 首次启动：创建地图

下面的命令去掉了重复的 `scene-map-dir`：

```bash
dimos run unitree-go2-agentic-persistent \
  --robot-ip "$ROBOT_IP" \
  --persistentgo2map.map-file=assets/scene_maps/sedan_office_persistent/map.pc2.lcm \
  --persistentgo2map.create-new=true \
  --spatialmemory.scene-map-dir=assets/scene_maps/sedan_office_persistent \
  --spatialmemory.vlm-url=https://api.openai.com \
  --spatialmemory.vlm-model=gpt-5.6-luna \
  --spatialmemory.vlm-enable-place-tagging=true \
  --spatialmemory.vlm-enable-object-tagging=true \
  --spatialmemory.vlm-distance-m=1.0 \
  --spatialmemory.object-segmenter=yolo \
  --no-obstacle-avoidance
```

两个模型参数独立：

| 参数                          | 用途                       |
| ----------------------------- | -------------------------- |
| `--spatialmemory.vlm-model` | 分析图像、生成自动标签     |
| `--mcpclient.model`         | 处理聊天、选择技能的 Agent |

需要明确指定 Agent 模型时，在启动命令中额外加入：

```bash
--mcpclient.model=gpt-5.6-luna
```

模型名称是否可用，以 API 服务端和账户权限为准。

首次创建注意：

- `create-new=true` 只用于第一次创建指定地图；已存在的地图文件不会被覆盖。
- 新地图无需 alignment，这次启动建立的坐标系就是保存的世界坐标系。
- 遥控缓慢走动和转向，覆盖墙角、房间和走廊等区域。
- 地图默认每 30 秒自动保存，并在正常停止时保存。
- 三维地图和语义记忆应在同一坐标系采集；不要混用来自其他坐标原点的旧语义目录。

## 4. 状态检查和日志

```bash
# 机器人栈状态
dimos status

# MCP 服务状态
dimos mcp status

# 模块与技能映射
dimos mcp modules

# 完整工具名称和参数 schema
dimos mcp list-tools

# 最近 100 行日志
dimos log -n 100

# 持续跟踪日志
dimos log -f

# JSON 日志
dimos log -n 100 --json
```

更新代码后需要重启机器人栈，运行中的 worker 不会自动加载新工具。

## 5. Agent 回复与聊天

### 5.1 实时回复：agentspy

先开监视：

```bash
dimos agentspy
```

再从其他终端发送：

```bash
dimos agent-send "你好，报告当前状态，不要移动。"
```

| 消息类型 | 含义               |
| -------- | ------------------ |
| Human    | 用户指令           |
| Agent    | LLM 回答或工具调用 |
| Tool     | 工具执行结果       |

`agentspy` 监听实时消息，不补回订阅前的历史。`agent-send` 返回的 `Message sent to agent` 只是发送确认，不是最终回答。

已经发送过的消息可以尝试用 `dimos log -n 200` 查看，但日志正文可能截断。`type: reasoning` 和 `encrypted_content` 不是可读回答；关注后续工具结果和文本回答。

### 5.2 命令行聊天

```bash
dimos agent-send "告诉我记忆里有哪些标签，不要移动。"
dimos agent-send "查询 fire extinguisher 标签，列出数量、ID 和坐标，不要移动。"
```

查询时明确说“不要移动”，避免 Agent 把模糊请求理解成运动任务。

### 5.3 在 dimos shell 中聊天

```bash
dimos shell
```

下面是 shell 内的 Python，不是 Bash 命令：

```python
from langchain_core.messages import HumanMessage

app.McpClient.add_message(
    HumanMessage(content="告诉我记忆里有哪些物品，不要移动。")
)
```

回复仍在 `agentspy` 或日志中查看，不会由 `add_message()` 同步返回。

退出 shell：

```python
exit()
```

退出 shell 不会停止机器人栈。Shell 中 RPC 调用立即作用于运行中的模块。

## 6. 自动标签

启动命令已开启自动房间和物品标签：

```bash
--spatialmemory.vlm-enable-place-tagging=true \
--spatialmemory.vlm-enable-object-tagging=true \
--spatialmemory.vlm-distance-m=1.0 \
--spatialmemory.object-segmenter=yolo
```

| 标签        | 坐标来源                             |
| ----------- | ------------------------------------ |
| 房间/place  | 识别该房间时的机器人位置，作为返回点 |
| 物品/object | 检测区域内的点云解算位置             |

物品处理流程：

```text
检测框 → 可用的分割 mask → 相机内参与观测时 TF
    → 时间对齐的点云投影 → 前景表面位置 → world 坐标
```

- 没有有效点云深度时，自动物品标签跳过并记录警告，不用机器人位置或固定距离代替。
- 同名物品、估计坐标距离不超过 1 米时合并；更远则保存为不同标签。
- `vlm-distance-m=1.0` 是移动距离条件，不是每秒调用一次。
- YOLO 不保证能为所有物品提供有效实例 mask；退回检测框时可能混入背景点。
- 保存的是物品表面估计位置，不保证是几何中心。
- 房间即使有检测框，也采用观测时机器人位置，不投影到墙或门框。

## 7. 手动标记物品

让机器人看清目标。恢复旧地图时，先确认 alignment。

通过 Agent：

```bash
dimos agent-send "用 tag_object 标记眼前的灭火器，名称使用 fire extinguisher，不要移动。"
```

直接调用 MCP：

```bash
dimos mcp call tag_object \
  --json-args '{"object_name":"fire extinguisher"}'
```

`tag_object` 不启动导航。它会捕获图像和对应几何信息，在模型检测后通过点云解算物品位置，避免使用检测结束时的新机器人姿态。

检测或有效深度不足时会报错。换一个更清楚的观察角度重试，不要用 `tag_location` 代替物品标签。

## 8. 手动标记房间或返回点

让机器人站在房间内安全、方便以后返回的位置。

通过 Agent：

```bash
dimos agent-send "用 tag_location 把当前位置标记为 office，不要移动。"
```

直接 MCP：

```bash
dimos mcp call tag_location \
  --json-args '{"location_name":"office"}'
```

区别：

```text
tag_object("fire extinguisher") → 物品的点云估计位置
tag_location("office")         → 机器人当前位置
```

房间标签代表返回点，不是房间中心。已有错误标签不会被自动删除或改写。

## 9. 查询标签、数量和坐标

```bash
# 全部标签
dimos mcp call query_memory_tags

# 所有匹配的灭火器标签
dimos mcp call query_memory_tags \
  --json-args '{"query":"fire extinguisher"}'
```

返回标签 ID、名称、world 坐标、类别、描述和匹配数量。查询从持久化标签库读取，包含以前会话的标签。

过滤是名称的不区分大小写子串匹配，不是自动翻译。直接 MCP 查询时使用实际保存的名称。

让 Agent 整理：

```bash
dimos agent-send "列出全部已保存标签和坐标，不要移动。"
dimos agent-send "fire extinguisher 标签有几个？列出每个 ID 和位置，不要移动。"
```

**数量是保存的标签数，不保证等于真实物品数。** 旧记录可能没有类别信息。以前因同名去重而没有保存的第二个物品，需要重新观察才能补存。

## 10. 导航

### 10.1 通过 Agent

```bash
dimos agent-send "导航到记忆中的灭火器。"
dimos agent-send "导航到已标记的 office。"
```

Agent 应先查询标签，再按 ID 导航。多个匹配、没有选择条件时，应先询问选哪个。

指定 ID：

```bash
dimos agent-send "导航到标签 ID 为 loc_XXXXXXXX 的灭火器。"
```

### 10.2 直接 MCP：查坐标再导航

```bash
dimos mcp call query_memory_tags \
  --json-args '{"query":"fire extinguisher"}'
```

将返回的真实 ID 填入：

```bash
dimos mcp call navigate_to_memory_tag \
  --json-args '{"location_id":"loc_XXXXXXXX"}'
```

此工具查询标签坐标并立即提交导航目标。使用目标 x/y 和当前机器人高度，避免把物品高度当作机器人目标高度。仍经过原规划器和地图对齐门控。

`navigate_with_text` 是另一条现有路径，会依次尝试标签、当前可见物品和语义图像记忆；要明确选择已保存标签，优先使用上述查询与 ID 导航流程。

### 10.3 到达与停止

正常到达判据约为：

- 目标距离小于 0.2 米；
- 朝向误差小于 15 度。

到位置后可能继续转向。规划器可能选择物品附近的可达目标，而不是占据障碍物的位置。`Started navigating` 只是开始，不是到达。记忆导航没有统一的 30 秒自动结束保证。

取消导航：

```bash
dimos mcp call stop_navigation
```

或在 `dimos shell`：

```python
app.PersistentGo2Planner.cancel_goal()
app.PersistentGo2Planner.is_goal_reached()
```

`is_goal_reached()` 返回 `False` 也可能是目标已取消，不表示机器人一定还在运动。

## 11. 保存与正常停止

在 `dimos shell`：

```python
app.PersistentGo2Map.save_map()
```

不能在未对齐、尚无已接受扫描时保存地图。

正常停止：

```bash
dimos stop
```

前台运行也可以在运行终端按 `Ctrl+C`。强制停止或杀进程可能丢失上次保存后的变化。

## 12. 下次启动：恢复旧地图

使用相同路径，**不加 `create-new=true`**，加入手动采集：

```bash
dimos run unitree-go2-agentic-persistent \
  --robot-ip "$ROBOT_IP" \
  --persistentgo2map.map-file=assets/scene_maps/sedan_office_persistent/map.pc2.lcm \
  --persistentgo2map.manual-capture=true \
  --spatialmemory.scene-map-dir=assets/scene_maps/sedan_office_persistent \
  --spatialmemory.vlm-url=https://api.openai.com \
  --spatialmemory.vlm-model=gpt-5.6-luna \
  --spatialmemory.vlm-enable-place-tagging=true \
  --spatialmemory.vlm-enable-object-tagging=true \
  --spatialmemory.vlm-distance-m=1.0 \
  --spatialmemory.object-segmenter=yolo \
  --no-obstacle-avoidance
```

或者去掉很多的Spatial Memory

```bash
dimos run unitree-go2-agentic-persistent \
  --robot-ip "$ROBOT_IP" \
  --persistentgo2map.map-file=assets/scene_maps/sedan_office_persistent/map.pc2.lcm \
  --persistentgo2map.manual-capture=true \
  --spatialmemory.scene-map-dir=assets/scene_maps/sedan_office_persistent \
  --mcpclient.model=gpt-5.6-luna

此时程序加载旧地图、积累手动采集点云，但导航、对齐后的传感器转发和地图更新保持阻塞。没有自动走动，低层手动控制并未全部禁用。

不要在配准确认前请求导航或新物品标记。

## 13. 重新定位：采集与 alignment

### 13.1 遥控采集

在旧地图覆盖范围内缓慢走动和转向，观察不同墙面、角落和结构。

```bash
dimos shell
```

```python
app.PersistentGo2Map.alignment_status()
```

状态会显示累计扫描数和点数。

### 13.2 停稳并结束采集

**松开遥控，确认机器人停稳**，然后：

```python
app.PersistentGo2Map.finish_startup_capture()
```

程序冻结累计点云后开始匹配。点数不足会报错并保留采集状态，可继续采集。该 RPC 会发布零速度，但不能覆盖一直按住的物理遥控。

### 13.3 检查候选

等待日志中的 `Alignment candidate ready`，查看：

```python
app.PersistentGo2Map.alignment_status()
```

在 Rerun 比较：

| 实体                        | 内容                   |
| --------------------------- | ---------------------- |
| `world/alignment_scan`    | 本次采集点云           |
| `world/alignment_preview` | 按候选变换放置的旧地图 |

检查整体墙面、拐角和地面是否一致。高 fitness 或局部重合不能证明配准正确。

### 13.4 人工确认或拒绝

正确：

```python
app.PersistentGo2Map.confirm_alignment()
app.PersistentGo2Map.navigation_ready()
```

确认后 `navigation_ready()` 应为 `True`。

错误：

```python
app.PersistentGo2Map.reject_alignment()
```

拒绝会用同一段点云重试，不会再次自动移动。要换一段采集数据，正常停止并重新启动恢复流程。已批准的 placement 不能在线撤回，需停止再重启。

Alignment 确认只提供人工 RPC，不暴露成 Agent 技能。确认后新观察会更新旧地图中看到的部分，未观察区域保留；坐标继续使用首次地图的世界坐标。

### 13.5 可选的原地旋转采集

如果不使用手动采集，可移除 `manual-capture=true`，替换为：

```bash
--persistentgo2map.startup-rotation=true \
--persistentgo2map.rotation-speed=0.15 \
--persistentgo2map.rotation-duration=20.0
```

两种采集模式不能同时启用。旋转只在恢复地图时使用，默认不平移；旋转停止后匹配，仍需人工确认。

在 shell 取消：

```python
app.PersistentGo2Map.cancel_startup_rotation()
```

旋转取消或传感器中断后不自动重试，需要重启。当前推荐手动走动加转向，覆盖范围通常更大。

## 14. 常见问题

| 问题                                 | 检查或处理                                                                       |
| ------------------------------------ | -------------------------------------------------------------------------------- |
| `create-new` 提示地图已存在        | 使用恢复命令，不删除已有地图                                                     |
| `No module named persistentgo2map` | Shell 属性使用`app.PersistentGo2Map`；CLI 配置使用 `--persistentgo2map.*`    |
| 导航被拒绝                           | 检查`alignment_status()`，人工确认后再导航                                     |
| 新工具找不到                         | 重启栈，并检查`dimos mcp list-tools`                                           |
| Agent 只返回发送成功                 | 用`dimos agentspy` 查看真正回答                                                |
| 物品无法取得深度                     | 检查 camera_info、TF、雷达时间对齐、目标可见度和点云覆盖；换观察角度，不伪造坐标 |
| 查询不到中文名称                     | 空查询先列出名称，再用实际保存的英文或中文名称查询                               |
| 配准反复失败                         | 换有辨识度的区域重新采集；不要为了通过而任意降低阈值                             |
| VLM HTTP 400                         | 看完整服务端错误；检查模型名称、权限和请求参数                                   |
| 5555/9990 被占用                     | 用下列命令检查监听进程，不要杀`grep` 或随意结束未知进程                        |

```bash
lsof -nP -iTCP:5555 -sTCP:LISTEN
lsof -nP -iTCP:9990 -sTCP:LISTEN
```

如果明确是旧 DimOS，先 `dimos stop`。其他应用占用端口时，优先正常关闭应用或更改配置。

## 15. 常用命令速查

| 操作            | 命令                                 |
| --------------- | ------------------------------------ |
| 运行状态        | `dimos status`                     |
| MCP 状态        | `dimos mcp status`                 |
| MCP 工具列表    | `dimos mcp list-tools`             |
| 模块技能映射    | `dimos mcp modules`                |
| 实时 Agent 消息 | `dimos agentspy`                   |
| 最近日志        | `dimos log -n 100`                 |
| 日志跟踪        | `dimos log -f`                     |
| 聊天            | `dimos agent-send "..."`           |
| Python 控制台   | `dimos shell`                      |
| 所有标签        | `dimos mcp call query_memory_tags` |
| 取消导航        | `dimos mcp call stop_navigation`   |
| 正常停止        | `dimos stop`                       |

## 16. 限制与相关文档

- Go2 内置雷达的旧地图配准仍是实验功能；现有匹配预设来自 MID360。
- 不提供持续全局重定位、回环或里程计漂移修正。
- 三维地图与语义标签必须对应同一个世界坐标系。
- 旧错误标签不自动修复；物品坐标只是传感器支持的估计。
- 持久化导航门控不是全局硬件运动锁；直接低层控制仍可能移动机器人。

相关文档：

- [配准和持久化地图](../capabilities/navigation/relocalization.md)
- [CLI 使用](./cli.md)
- [配置](./configuration.md)
- [Agent 系统](../capabilities/agents/index.md)


/goal 这是使用教程。 docs/usage/go2-persistent-workflow.zh.md                                                                                                                                               
                                                                                                                                                                                                            
整体包含了，连接 机器人，机器狗，通过rerun进行 UI展示以及控制， 机器狗的自动和手动 tagging 物品和房间，agent talking ， 导航以及查询等，  重新连接的alignment。                                             
                                                                                                                                                                                                            
现在我需要你在保留原来rerun的界面的内容基础上，重新加入对于这个使用教程里的很多操作的UI的支持，比如说可以通过按钮等。                                                                                       
                                                                                                                                                                                                            
此外，加入一个交互良好的类似于chatgpt的那种chatbot，可以让用户直接对话，以及看到机器人的反馈以及 内部的一些工具输入输出等。                                                                                 
                                                                                                                                                                                                            
你可以探索，然后根据你的理解将UI设计到最好最美观最高效，最后这个·UI要 有SEDAN GROUP的标志，整体是一个科技风格的看板和操作台。                                                                               
                                                                                                                                                                                                            
写完代码后自己进行测试，可以使用dimos 自己的replay进行测试。 最后我会验收，所以你要将所有的能力都测试完。 在测试期间不要使用chatgpt api，使用vllm 本地模型。     