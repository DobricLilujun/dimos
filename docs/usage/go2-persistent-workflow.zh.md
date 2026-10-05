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
- 自动和手动物品标签共用地图范围限制：若估计的 x/y 超出 world 点云地图的
  二维外包络，沿机器狗到物品的视线求边界，再选该边界交点 20 cm 内的实际点云点，
  将其 x/y 朝机器狗方向偏移 30 cm，z 使用该点云点的高度。范围内的估计不移动。
  优先使用 `global_map`；尚未收到地图时使用本次观测的对齐雷达点云。
  点云不足、机器人在范围外或视线边界附近没有实测点时，不保存该标签并报告原因。
  修正结果包含 `estimated_position`、`boundary_position`、`inset_m`，
  方法为 `pointcloud_map_boundary_inset`，方便在工具输出中检查。
  这是标签坐标约束，不会让狗移动，也不代表目标一定可导航；
  点云外包络不能证明凹形房间或未扫描区域的室内可通行性。旧标签不会自动重写。
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
```

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
- 默认不做持续全局重定位或里程计漂移修正；可选 PGO 回环见 17.5。
- 三维地图与语义标签必须对应同一个世界坐标系。
- 旧错误标签不自动修复；物品坐标只是传感器支持的估计。
- 持久化导航门控不是全局硬件运动锁；直接低层控制仍可能移动机器人。

相关文档：

- [配准和持久化地图](../capabilities/navigation/relocalization.md)
- [CLI 使用](./cli.md)
- [配置](./configuration.md)
- [Agent 系统](../capabilities/agents/index.md)


## 17. SEDAN GROUP Web 控制台

### 17.1 启动独立控制台

先停止已有的 DimOS 机器人栈。控制台只管理自己启动的子进程，不会接管或停止其他实例。

```bash
cd /Users/lujun.li/dimensional-applications/dimos
source venv-dev/bin/activate
python -m dimos.web.console
```

浏览器打开 `http://127.0.0.1:8090`。控制台仅监听本机，不应直接暴露到公网。
机器人停止后页面仍可用；在控制台终端按 `Ctrl+C`、收到 `SIGTERM` 或关闭终端
（`SIGHUP`）时，控制台会默认停止自己启动的机器人栈：先发送 `SIGTERM`，
等待 5 秒仍未退出则升级为 `SIGKILL`，并清理该栈的子进程。退出日志会显示停止结果。
这相当于针对当前控制台管理的栈执行 `dimos stop`，不会停止其他终端启动的实例。
仅关闭浏览器页面不会停止机器人栈。`kill -9` 控制台或机器断电无法执行退出清理；
异常情况下请用 `dimos status` 检查，并在需要时手动执行 `dimos stop`。
使用 `--port 8092` 可以修改控制台端口。

如果仍使用 `dimos run unitree-go2-agentic-persistent-console`，
会保留随机器人栈启动的旧方式，包含 Web Viewer 和操作按钮，但不提供独立的 Settings/启动管理。
**不要同时启动两种控制台。**

### 17.2 Settings

右上角 **Settings / 设置** 提供：

| 分组 | 可配置内容 |
| --- | --- |
| 连接 | Robot IP、Replay、回放数据集、机载避障；只读显示 `.env` 密钥是否已配置 |
| 模型服务 | Agent API base URL、Agent model、VLM API URL、VLM model |
| 地图 | Scene directory、手动或旋转采集、旋转速度/时长、PGO loop correction；New/Restore 在主界面 Robot stack 中选择 |
| 自动标签 | 房间标签开关、物品标签开关、移动距离阈值、分割器 |
| 服务 | MCP 端口、Rerun Web Viewer 端口、Rerun data port (gRPC) |

IP 可以设置为机器人实际地址，例如 `192.168.63.218`。
Agent 使用 OpenAI 时，base URL 填 `https://api.openai.com/v1`；
VLM URL 可以填 `https://api.openai.com`。
两个模型名称分别设置，例如 `gpt-5.6-luna`。
独立控制台也将这组 VLM URL/model 用于手动物品标记，避免额外依赖 Alibaba 服务；
模型需要支持图像输入并按提示返回像素坐标。直接启动原有蓝图、未指定
`--navigationskillcontainer.vlm-url` 时，仍保留原来的 Qwen 服务。

普通配置保存至 `~/.config/dimos/robot-console.json`（具体路径遵循系统的 DimOS config 目录）。
`OPENAI_API_KEY` 和 `UNITREE_AES_128_KEY` 从项目根目录的 `.env` 读取。
Settings 只读显示固定掩码与是否配置，API 不返回密钥原文，也不接受页面修改密钥。
修改密钥请直接编辑 `.env`；重新打开 Settings 会刷新配置状态，
下一次启动机器人栈时会重新读取并通过子进程环境传递，不写入普通设置文件或命令参数。
这两个字段不再从启动终端继承。不要提交 `.env` 或在聊天中输入密钥；
建议将 `.env` 文件权限设为仅本人可读写。

保存后在主界面 **Robot stack → Map mode** 选择 **New map** 或 **Restore saved map**，
然后点击 **Start / 启动机器人栈**。运行中修改设置或模式需要先正常停止。
恢复旧地图时，Settings 保持同一 Scene directory，主界面选择 Restore；直接启动，不弹出地图选择提示。
创建新地图时，始终显示确认：空目录显示 **Create new map?**，已有地图或标签显示覆盖确认。
取消不会修改旧数据或启动栈；确认后旧场景整体移至同级 `.backup-<唯一编号>` 目录，
新栈仍使用原场景路径，不加载旧标签。备份位置显示在 Backend console 中。
原有 CLI/demo 的建图保护保持不变。若启动进程失败，控制台恢复旧目录。
默认保留机载避障；关闭避障或选择自动旋转前，请确认现场安全并看护机器人。
如果提示 `Port 9877 is occupied`，在 Settings 的 Local services 中将
**Rerun data port (gRPC)** 改为未占用的端口，例如 `9887`，保存后再启动。
修改 Rerun viewer port 无法解决数据端口冲突，不要结束未知进程。

### 17.3 页面操作对应关系

| 教程操作 | UI 入口 |
| --- | --- |
| 启动/连接 | Settings → Start；连接失败查看 Stack logs |
| 状态、模块和工具列表 | 顶部状态徽标、MCP tools / 工具与模块 |
| 相机、地图和轨迹 | 中央视图区：3D 主画面、Camera 右上角悬浮；点击悬浮窗口交换大小，不重载数据源 |
| 自动标签 | Settings 的房间/物品开关，在下一次启动时生效 |
| 手动物品标签 | Tag object → 输入 `object_name` |
| 手动房间/返回点 | Tag location → 输入 `location_name` |
| 查询与坐标 | Query memory → 空查询列出全部；结果展示 ID 与坐标 |
| 按 ID 导航 | 查询结果的 Navigate，或 Navigate to tag → `location_id`；需人工确认 |
| 取消导航 | Stop navigation；不是硬件急停 |
| 导航状态 | Navigation state；开始导航不代表到达 |
| 配准采集 | 主界面选择 Restore，Settings 选择 manual → 遥控采集 → 停稳 → Finish startup capture |
| 配准检查 | Alignment status 与中央 Rerun 的 alignment_scan/alignment_preview |
| 配准确认/拒绝 | Confirm alignment / Reject alignment，均需要人工确认 |
| 取消原地旋转 | Cancel rotation |
| 保存地图 | Save map，显示 RPC 返回值或明确错误 |
| 暂停/继续永久地图融合 | Pause map fusion / Resume map fusion，需人工确认；不停止机器人、不冻结位姿 |
| 查看地图融合状态 | Map fusion status 与 Map 区状态文字，显示原因及 accepted/skipped 帧数 |
| 正常停止 | Stop / 保存并正常停止；不强制杀进程 |
| 聊天 | 右侧输入框，Enter 发送、Shift+Enter 换行；显示回复、配对工具输入输出和实时工具进度 |
| 日志 | 左侧 Operation log；Rerun 下方常驻 Backend console，显示后台输出、操作和工具进度，可清空显示 |
| 全屏 | 页头 Full screen / Exit full screen；也可用 Esc 退出 |
| 手动键盘控制 | 中央视图区下方 Enable keyboard，人工确认后按住 Space + W/S 前后、A/D 横移、Q/E 转向；Esc 停止并禁用 |
| 自动标注开关 | Tagging 中 Automatic tagging；首次点击读取状态，再次点击暂停/恢复 Settings 中已配置的自动物品/房间标注，手动标注不受影响 |
| 起点查询/返回 | Memory 中 Starting location 查询本次运行首个对齐后的 world 位姿；Navigation 中 Return to start 经确认后发起导航。Agent 可调用同名查询和返回工具 |
| Go2 回复播报 | Agent chat 顶部 Go2 speaker，默认 Off；确认后以 Go2 最大音量 10/10 播报后续最终回复，再点击可关闭并暂停播放 |

未完成配准时，UI 禁用标记与导航入口；底层规划器仍保留原有对齐门控。

### 静止时地图继续变厚、漂移：自动融合门控

独立控制台默认启用 Settings 中的 **Auto-pause permanent map on low-speed odometry**。
它使用已有 Go2 WebRTC 里程计做低速判断，无需启动额外的 `point_lio_unilidar`；
只控制对齐后永久地图的写入，实时点云、odom 和 TF 仍继续更新。不是定位纠偏，
也不能保证导航位姿正确。位姿明显漂移时应停止导航、检查定位，而不是继续使用地图门控掩盖问题。
原有非 Console CLI/蓝图默认不启用自动门控；可通过
`--persistentgo2map.auto-pause-fusion=true` 显式启用。

默认初始参数如下，**未经过当前机器人实机标定**，需要根据静止噪声与实际最低移动速度调整：

| 参数 | 默认值 |
| --- | --- |
| Motion window | 0.5 秒 |
| Stationary dwell | 1 秒 |
| Odometry timeout | 1 秒，必须不小于窗口 |
| Stationary / Resume speed | 0.02 / 0.04 m/s |
| Stationary / Resume rotation | 2 / 3 deg/s |

低速持续满足条件后暂停；平移或转向超过恢复阈值时继续融合。恢复阈值必须高于静止阈值，
以免反复切换。判定使用短窗口而非距最后融合位置的累计位移，避免缓慢漂移周期性恢复融合。
尚未完成运动判定、odom 过期或异常时暂停写图，界面显示原因；有新鲜有效 odom 时，
允许第一个对齐扫描作为地图种子。确认静止前仍存在窗口与 dwell 带来的检测延迟。
近期运动指令可延迟进入静止，但没有指令不等于机器人静止，指令本身也不能解除已确认的静止暂停。

- **Pause map fusion** 手动锁定暂停，检测到移动也不会自动解除。
- **Resume map fusion** 只解除手动暂停；若自动规则仍判断静止/数据不足，地图仍暂停。
- 要在静止时继续融合环境变化，在 Settings 关闭自动门控并正常重启机器人栈。
- 启动配准的手动/旋转采集不受此门控影响，仍须按原流程完成采集、检查并确认配准。
- PGO 开关两种模式均受融合门控控制；已有图修正仍应用于实时流，但暂停期间不插入新的 PGO 帧。
- Save map 可保存当前已融合地图，但**不会暂停后续融合**；Restore 也不是只读模式。
  自动保存与正常退出保存仍按原规则执行。无已接受扫描时保持原有不可保存的明确错误。
- 暂停不等于 Save map，也不等于急停。需要保留当前地图时，先暂停，再保存。
  已污染的地图不会自动修复，需人工选择干净备份或新场景重建。

人工看护下的验收：先观察静止状态，再静止 60 秒；自动暂停后 accepted 帧数应不再增长，
保存地图应不继续膨胀，而 skipped 与实时传感器流继续更新。检查正常平移、原地转向后能恢复，
停下后再次暂停。慢速运动被误判时降低阈值或关闭自动门控。
如果静止漂移超过恢复阈值，此启发式可能把漂移当移动；使用手动暂停止损，
记录上游位姿/点云后再评估机载 LIO、独立运动观测或扫描配准。不能将 PGO 开关、
增大体素或降低显示频率当作已经验证的静止漂移根治方案。

无需此功能的临时止损流程是：完成必要采集 → Save map → 正常停止机器人栈。

聊天发送成功只代表传输提交，不代表 Agent 已完成回复或机器人动作。
控制台通过 Viewer URL 的 `url` 参数连接 Settings 中的 Rerun 数据端口，
不只打开 Viewer 首页。若只显示 Welcome to Rerun，检查数据服务是否运行及端口是否一致，
再点击 Refresh viewer。此连接不替换机器人栈原有的可视化蓝图与内容。
独立控制台为浏览器加载单独的 3D 视图文件，隐藏其他 Rerun 面板，
Camera 通过已有图像通道显示；不修改原 demo、原生 Rerun 或机器人栈的可视化配置。
桌面聊天区固定在视口内，长回复只在聊天内容区滚动。
键盘控制复用 `tele_cmd_vel` 与 MovementManager，会取消导航；不要求完成配准，
以支持启动时的手动采集。请现场看护，不能将其作为硬件急停。
键盘只在控制台页面获得焦点、未编辑输入框且未打开弹窗时生效，
点击 Rerun iframe 后先点击控制台标题区域重新获得焦点。
松键、失焦、隐藏页面或关闭连接会发送零速度；服务端 0.5 秒未收到指令也会停止并断开。
启用时先请求 Go2 开启摇杆监听；失败会显示错误，不进入可操控状态。
启用后也可以按住方向按钮操控，松开即停止。点击 Rerun iframe 会令页面失焦并禁用控制；
需要回到页面重新启用，不要在 iframe 内按键。标准 Rerun Web Viewer 不会发送导航点击世界坐标，
因此本版未提供 3D 点击导航。

自动标注关闭会停止提交新任务、清空待处理任务并丢弃旧结果；已经发出的模型请求无法撤回。
恢复沿用启动时配置的标注类型，不会自动打开 Settings 中未启用的类型。
Starting location 在每次机器人栈启动时重新记录；恢复旧地图时需先对齐，不能把它当作上次运行的起点。
返回起点仍经过规划器的对齐/就绪检查，发起导航不等于到达。

回复播报使用实际 Go2 WebRTC 音频接口，不使用电脑扬声器；回放/模拟模式会明确拒绝。
TTS 使用 Settings 的 VLM URL（自动补 `/v1`）、`.env` 中的 OpenAI key 和 `tts-1`；
该服务需支持音频生成，否则错误会显示在 Backend console。只播报启用之后的新最终回复，
不播报用户输入、工具调用和工具结果。回复长度上限 4096 字符、生成音频上限 90 秒，
超限会报告错误而不是截断。关闭会取消排队及进行中的回复；已上传的临时音频会在正常完成时清理。

**Puppy 自动吐槽与麦克风对话（仅 console blueprint）：**

- 默认关闭。开启 **Go2 speaker** 并确认最大音量、麦克风及模型隐私提示后，
  同时开启 Puppy 自动吐槽和 Go2 麦克风监听；关闭该按钮会一并停止。
- 聊天区另有 **Murmur: On/Off** 开关，只暂停/恢复环境吐槽，
  不关闭已启用的麦克风对话及 Agent 回复播报。状态行显示模型、摄像头是否新鲜、
  播报忙碌和最近错误。若提示 **Old/non-Puppy stack**，必须停止机器人栈、
  重启 console 后再启动栈；单独刷新页面或只开启旧栈的扬声器不会启动 murmur。
- Puppy 自称 **“My name is puppy, built from sedan”**。自动吐槽、语音回复及
  console Agent 的最终回复统一使用英文；本地 Whisper 仍可识别中英文。
- 在空闲、摄像头画面新鲜时，约每 10 秒生成一句可爱的环境评论。
  模型请求和播放不重叠；播放结束后重新计时，因此不是严格每 10 秒发声。
- 环境评论及对话默认使用 `gpt-4o-mini`，复用 Settings 中的 VLM URL 与 OpenAI key。
  本地语音识别使用 `faster-whisper` 的 `base` 模型，CPU/int8；
  首次开启可能需要下载模型，请等待。可通过启动参数
  `--go2connection.puppy-model` 和 `--go2connection.puppy-whisper-model` 调整模型。
- 原始麦克风音频仅在内存中进行本地识别，不上传、不保存录音；
  识别出的文本及当前摄像头画面会发送到配置的模型服务，回复文本会发送给 TTS。
  请勿在私人谈话或敏感场景中开启。没有唤醒词，听到的有效语音可能触发回复。
- 采用半双工防回声：机器人讲话期间及结束后约 1 秒不监听，
  不能打断播报。麦克风原文、Puppy 回复和错误会显示在聊天面板，不会重复播报。
- **麦克风对话不调用运动工具**；导航指令请在网页 Agent chat 中输入。
  无麦克风数据、模型或音频接口失败会显示提示；离线测试不代表实机扬声器/
  麦克风已验收，固件支持情况仍需连接 Go2 验证。

**Agent 的 1 米附近导航：**

console Agent 默认先用 `query_memory_tags` 查询，再调用新工具
`navigate_near_memory_tag(location_id)`。机器人与所选标记的水平距离
不超过所配置的阈值时停止，不要求精确高度或朝向；已经在范围内不会开始移动。
聊天面板的 **Nearby stop distance** 滑块范围为 **0.3–3.0 米**、步进 0.1 米，
默认 **1 米**。滑动完成后实时应用到下一次附近导航；当前行程保持开始时的阈值，
PGO 回环后仍通过同一标记 ID 刷新目标并保持该阈值。
滑块的临时设置重启后不保留；在 Settings 的 **Nearby stop distance**
保存后可设置重启默认值（也可用 `--persistentgo2planner.nearby-arrival-distance`）。
原有 `navigate_to_memory_tag`、`navigate_with_text` 等精确导航工具保持兼容，
显式要求精确导航时仍可使用。该功能不绕过地图对齐、避障和规划器就绪检查，
工具报告“开始导航”不代表已经到达。

独立启动的 `python -m dimos.web.console` 也会向机器人子进程传入 Puppy 启用配置
和相同的英文/附近导航 Agent 提示词；网页主导航按钮及查询列表中的
**Navigate nearby** 均使用附近导航工具，**Navigate precisely** 保留精确导航。
启用 Go2 speaker 后，规划器确认到达时会播报 **“Woof! We've arrived!”**，
取消、失败或 PGO 暂停不会播报到达。修改后需重启 console 和机器人栈，
运行中的旧进程不会自动载入新配置。

**可选的图像确认到达：**

默认 **Visual arrival: Off**，只按距离阈值停止。
开启聊天区 **Visual arrival** 并确认转动提示后，下一次附近导航改为两阶段：

1. 到达距离阈值，停止原路径规划和前进，但暂不宣告到达。
2. 先比较当前摄像头和所选 tag 的参考图片。不匹配时以 **0.15 rad/s**
   短脉冲原地转动，每次转动最多 0.5 秒后停车进行匹配；最多搜索 **20 秒**。
   图像计算期间不转动。匹配成功后停止并播报到达。

匹配使用本地 OpenCV ORB 特征及 RANSAC 几何一致性，不上传图片。
物品参考图取标注框内的裁剪图，房间参考图使用标注时的画面。
它是保守的图像相似性确认，不保证物品身份；纯色、无纹理、重复图案、
光照/视角大幅变化时可能无法匹配。不会为了匹配继续前进或绕物品行走。
超时、旧 tag 无参考图、参考图缺失、相机画面超过 3 秒未更新或匹配接口失败，
会停车并在聊天工具记录中报告原因，**不会播报到达**。
缺少参考图片的历史 tag 可重新标注以补充图片。
Stop navigation/键盘停止会取消搜索；关闭 Visual arrival 会立即取消正在进行的搜索。
PGO 回环暂停会中断搜索，地图与 tag 同步完成后按同一目标重新检查距离并搜索，
过期的匹配结果不会恢复运动。开启之前请确认机器狗周围可安全转动。

消息流显示当前页面订阅后的内容；刷新页面不恢复此前的聊天历史。
保存/配准失败不会显示为成功，按错误提示处理后重试。

### 17.4 离线测试与实机验收

#### Demo 的新版探索

左侧控制按钮只显示名称；鼠标停留 1 秒后显示悬浮说明和安全提示，
键盘聚焦同样支持。移开鼠标、点击、滚动或按 Escape 关闭提示。
操作确认弹窗仍保留详细说明；触屏用户可在执行前的确认弹窗查看提示。

Settings 的 Mapping & tagging 中新增 **Demo planner width**：
默认 0.30 m，可配置范围 0.05–1.00 m，保存并重启机器人栈生效。
只通过 `persistentgo2planner.robot-width` 覆盖 demo 启动的规划器，
不修改全局默认值、原有蓝图、机载避障或旋转净空。
调小可减小路径膨胀和路径净空要求，但默认本来就是 0.30 m；
低于实际机身及载荷所需宽度会低估占用空间，有碰撞风险，不建议实机使用。
0.05 m 是可配置下限，不是安全宽度建议。这个值不会提高行走速度，
也不修改探索模块固定的 25 cm 障碍物膨胀或到达距离阈值。

聊天区域的 **Live navigation speed limit** 滑块提供实时速度上限：
0.10–0.55 m/s，默认 0.55 m/s。松开滑块后 RPC 更新立即作用于随后发布的导航
平移指令，包括正在进行的精确导航、附近导航和探索，不取消或重建当前目标。
这是平移速度上限，转弯时或受原控制器/`nerf_speed` 限制时实际速度可能更低；
不会改变遥控、原地旋转、视觉搜索转向或机载避障。
低于控制器原有最低速度 0.20 m/s 的设置也会在输出端限幅，不会被最低速度抬高。
设置失败显示错误并回退滑块；未运行栈或未启用此功能的旧蓝图禁用滑块。
实时调节仅本次运行生效；Settings 的 **Navigation speed limit** 用于保存下次启动默认值。
原有非 demo 蓝图默认不启用限速覆盖，行为保持不变。增大上限不会自动开始运动，
但可能使正在导航的机器狗加速，请现场看护并保持净空。

独立控制台启动 `unitree-go2-agentic-persistent-demo`；嵌入控制台的蓝图也使用
`DemoExplorer`。原有 `unitree-go2-agentic-persistent` 和 Wavefront 探索保持不变。

地图配准完成后，在 **Exploration → Explore building** 中选择策略和本次参数：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| strategy | frontier | 沿用前沿大小、距离、障碍物距离、方向等综合评分；只选择可达自由空间目标 |
| min_goals | 10 | 成功到达这么多个目标后开始检查低收益，不是总目标数上限；失败不计数 |
| gain_percent | 1 | 每次成功导航的地图信息增长百分比阈值，1 表示 1% |
| no_gain_attempts | 2 | 连续低收益次数达到此值时结束 |
| check_interval | 3 秒 | 进度检查间隔，不是单个目标的硬超时；明确的失败反馈立即唤醒并换目标 |

**efficient** 策略使用可通行自由空间上的 Dijkstra 路径距离，按边界收益与实际
路径距离评分，不用穿墙的直线距离估计成本；对角线不能穿越障碍物角点。
已到达区域被排除，但同一长边界的其他区域仍可继续探索。失败区域冷却 60 秒，
避免反复选同一不可达点。两种模式均保留 25 cm 障碍物膨胀，不绕过机载避障。
这是局部选择策略，不保证全局最短巡游或所有房间都可达，实机效率仍需现场验证。

默认 15 秒没有至少 15 cm 的平移进展会取消当前目标并换点；里程计超过
5 秒未更新会停止并报错。地图必须存在，暂停融合时允许沿用静态地图。
连续 10 次找不到合格目标会结束并显示原因。
恢复的大地图可能仍较快触发低收益条件，可以降低 gain_percent 或增大
min_goals/no_gain_attempts。参数用于本次探索，可再次打开表单修改。
状态和停止原因显示在 Exploration 面板及工具日志；**Stop exploration** 取消探索。
PGO 故障或遥控停止信号仍会终止探索，不自动恢复，以免意外运动。

主界面选择 New 时始终弹出确认。首次创建且目录为空时，选择 **Yes — create new map**；
无旧地图可恢复时不显示 No。目录已经存在地图/标记时，**Overwrite existing scene?** 对话框提供：

- **Yes — overwrite**：备份旧目录后创建新地图。
- **No — use existing map**：本次启动改为 Restore，保留原地图和标记，然后按
  Settings 的手动/旋转采集模式进行 alignment。不会备份或替换目录。
  只有目录含 `map.pc2.lcm` 才显示这个选项。
- **Cancel**：取消启动，不改变目录。

选择 No 后直接进行 Restore 启动和配准，不再弹出第二个启动确认，也不改写主界面的 New 选择。
主界面模式会在启动或保存设置时保存；若希望下次默认沿用旧地图，请在主界面选择 Restore。
Restore 不弹出地图选择提示；如果缺少 `map.pc2.lcm`，明确报错且不会自动创建或覆盖数据。
点击 Start 表示同意启动：Restore 的旋转采集可能自动转动机器人，自动标签可能调用模型服务，
启动前应查看主界面安全提示并现场看护。

不调用云模型的基础回归：

```bash
source venv-dev/bin/activate
python -m pytest dimos/web/console dimos/agents/mcp/test_mcp_server.py \
  dimos/agents/mcp/test_mcp_client_unit.py \
  dimos/mapping/relocalization/go2/test_persistent.py \
  dimos/perception/experimental/test_spatial_tags.py -q
```

浏览器测试需要项目已有的 `browser-tests` 依赖组与 Chromium：

```bash
python -m playwright install chromium
python -m pytest -m web_browser dimos/web/console/test_console_browser.py -q
```

这些测试的机器人、LLM 和生命周期边界均被隔离，不启动真实机器人或调用 OpenAI API。
它们验证 UI/HTTP/SSE、参数、保存与密钥处理，不能证明实机导航或视觉识别效果。

使用本地 vLLM 做实际回放测试时，在 Settings 中：

- 勾选 Replay，先选择 New map 和一个新的空测试目录；
- Agent URL 指向本地 OpenAI-compatible `/v1`，例如 `http://127.0.0.1:8000/v1`；
- Agent model 填 `openai:<vLLM 实际加载的模型名>`；
- VLM URL、VLM model 指向支持图像与工具所需能力的本地服务；
- 如果本地服务无需鉴权，在 `.env` 中将 `OPENAI_API_KEY` 设置为测试占位值即可；
- 不使用真实密钥，也不将模型 URL 指向云 API。

实机验收建议顺序：

1. 在 `.env` 中设置更新后的密钥，在 Settings 设置 Robot IP，创建新地图并启动，检查相机/雷达/Rerun。
2. 聊天“报告状态，不要移动”，检查回复、thinking/idle 和工具 I/O。
3. 观察自动标签；分别手动标记物品和房间，再查询确认 ID/坐标。
4. 选择标签导航，检查规划器状态及实际到达；测试取消导航。
5. 保存并正常停止，选择 Restore，重启并遥控采集。
6. 停稳、结束采集，检查两片点云；测试拒绝，再检查新的候选并人工确认。
7. 确认 world 坐标一致、旧标签可查询、导航恢复；最后正常停止。

### 17.5 可选 PGO 回环修正

在 **Settings → Mapping & tagging** 勾选
**PGO loop correction (fixed world; applies on restart)**，保存后停止并重新启动机器人栈。
默认关闭，不改变原有建图方式。直接启动持久化蓝图时，也可添加
`--persistentgo2map.pgo-enabled=true`。需要当前 Python 环境安装 GTSAM；缺少依赖会报错，
不会悄悄切回普通建图。首次使用建议先备份整个 Scene directory。

启用后：

- `PGOMap` 对本次运行的关键帧做 ICP 回环检测与位姿图优化。
  本次运行的第一个关键帧固定在已对齐的世界坐标系，后续点云、机器人位姿与 TF 一起修正。
- 本次运行新增的房间/物品标签和图像检索坐标根据采集时间、原始位姿同步更新；
  慢速模型返回的标签也会在保存时应用最新修正，不会重复叠加同一次修正。
- Restore 模式保留上次保存的点云和旧标签，不用本次回环整体拖动它们。
  新点云经过修正后与旧地图一起保存；`starting location` 保持在本次起始世界坐标。
- 接受回环后短暂停车，同步地图与标签、保存地图，再自动重新规划当前导航。
  标签导航通过原来的标签 ID 重新查询修正后的坐标，不按名称重新匹配，也不继续使用旧路径；
  直接指定的世界坐标目标（包括 starting location）保持固定。
  重新规划前同步计算修正地图的 costmap 并更新机器人位姿。日志显示
  `Navigation resumed after PGO`，不代表已到达。
  用户取消、遥控接管、目标已经结束，或暂停期间提交了新目标时，不会恢复旧导航；
  暂停期间的新目标会明确拒绝，待同步结束后可重新提交。
- 回环后立即保存地图，普通 Save map 和正常停止也保存修正后的地图。
  标签同步、回环检查点保存或导航刷新失败时，停止运动、阻止导航与后续地图保存；
  日志给出具体错误，修复后需要重启并重新检查配准。

这不是持续匹配旧地图的全局重定位：PGO 只检测本次运行之间的回环，
重新连接时仍需采集、检查并人工确认启动配准。错误的启动配准或错误的 ICP 回环
不保证被自动修复；首次验收请在空旷安全区域、有人看护并可急停的情况下进行。

回环不是固定周期：位移超过 0.5 米或转向超过 45° 才添加关键帧，
至少 10 个关键帧后搜索 2 米内、采集时间相隔超过 20 秒的历史帧。
成功回环之间至少相隔 5 秒（按传感器时间），每次仍需 ICP 通过；
demo 接受回环后立即应用修正，不是每 20 秒必然修正一次。

地图文件采用原子替换，但地图和 Chroma 数据库不是一个跨文件事务。
不要在回环同步期间强制杀进程或断电；异常中断后应检查地图与标签是否一致，
必要时恢复完整 Scene directory 备份。`vlm_tags_*.jsonl` 是当时估计的诊断记录，
不会随之后的回环重写；Agent 查询与导航使用更新后的数据库坐标。