# 怎么使用这 3 个新功能（unitree-go2-agentic）

> 分支：`learning-branch`。3 个功能都**默认关闭（inert）**，不开就完全不影响原来的
> `unitree-go2-agentic`，行为和 e2e 测试都不变。下面每个功能只在一个开关打开时才生效。

---

## 0. 先启动蓝图（照旧）

```bash
# 真实 Go2（需要机器人 IP）
dimos run unitree-go2-agentic --robot-ip 192.168.123.161

# 或用 replay / 后台
dimos --replay run unitree-go2-agentic --daemon
dimos status            # 查看运行状态
dimos log -f            # 实时看日志
dimos stop              # 停止
```

启动后，三个功能的开关都是 `None`，所以不会启动任何新东西（不占端口、不占模型）。

---

## 功能一：加载已生成的场景图 / 场景地图（scene map）

DimOS 的 `SpatialMemory` 本来就会**实时生成**一个原生场景图（场景地图，存在 ChromaDB
里）。这个开关让你**加载一个之前已经生成好的场景图**，不用重新建。

**开关（`SpatialConfig`）：** `--spatial-memory.scene-map-dir <path>`

- `<path>` 是一个目录，里面要有 `chromadb_data/` 和 `visual_memory.pkl`。
- 打开后会自动推导 `db_path = <path>/chromadb_data`、
  `visual_memory_path = <path>/visual_memory.pkl`，并且强制 `new_memory=False`
  （**加载**而不是重建）。
- 默认 `None` → 完全不变。

```bash
# 加载一个之前生成的场景图
dimos run unitree-go2-agentic --robot-ip 192.168.123.161 \
  --spatial-memory.scene-map-dir ./my_saved_scene
```

---

## 功能二：识别“有名的人”并说话（named person recognition）

复用已有的：人检测 + 嵌入/识别模型 + 说话（`SpeakSkill`）。对每个被检测到的脸，
和“相册”里的人做匹配，超过阈值就用 Go2 Pro 的喇叭说 “I found <name>”（同一人有
冷却时间，不会刷屏）。

**支持两种后端（`recognition_backend`，默认 `reid`）：**

| 后端 | 用什么 | 需要下载模型？ | 适用 |
|---|---|---|---|
| `reid`（默认） | `TorchReIDModel`（osnet，深度学习 ReID）+ 余弦相似度 | 是（`osnet_x1_0.pth`） | 有模型/联网时 |
| `cv`（**本地、传统 CV**） | OpenCV `cv2.face.LBPHFaceRecognizer`（LBPH，CPU） | **否** | 没模型/离线时 |

> **`cv` 后端是本地的“传统 CV 模型”**：OpenCV 的 LBPH（Local Binary Patterns
> Histograms）是经典 ML/传统 CV 方法，**直接在相册照片上训练、CPU 运行、不需要任何
> 模型下载**，所以完全自给自足（self-contained）。这就是本次验证用的“本地模型”。

**开关（`NamedPersonConfig`）：** `--named-person-recognizer-skill-container.gallery-dir <path>`

- `<path>` 是相册目录，里面**每个人一个子文件夹**，子文件夹里放该人的照片：
  ```
  <path>/
  ├── alice/
  │   ├── a1.jpg
  │   └── a2.jpg
  └── bob/
      └── b1.jpg
  ```
- 其他可调参数（都有默认值）：
  - `--named-person-recognizer-skill-container.recognition-backend cv`（`reid` 或 `cv`；默认 `reid`）
  - `--named-person-recognizer-skill-container.threshold 0.5`（匹配阈值，0–1）
  - `--named-person-recognizer-skill-container.announce-cooldown-s 15`（同一人两次播报的最小间隔，秒）
  - `--named-person-recognizer-skill-container.padding 20`（裁剪框的像素 padding）
  - `--named-person-recognizer-skill-container.max-images-per-person 10`（每人最多用几张参考图，越小越快）
- 默认 `gallery_dir=None` → 完全不变（不加载相册、不匹配、不播报）。
- `cv` 后端默认 `threshold 0.1`（LBPH 分数是 0–1 归一化，阈值比 reid 小）。

```bash
# 用默认深度学习 ReID 后端
dimos run unitree-go2-agentic --robot-ip 192.168.123.161 \
  --named-person-recognizer-skill-container.gallery-dir ./people \
  --named-person-recognizer-skill-container.threshold 0.6

# 用本地“传统 CV”后端（LBPH，无需下载模型）
dimos run unitree-go2-agentic --robot-ip 192.168.123.161 \
  --named-person-recognizer-skill-container.gallery-dir ./people \
  --named-person-recognizer-skill-container.recognition-backend cv \
  --named-person-recognizer-skill-container.threshold 0.1
```

> 想测试但没模型：`TorchReIDModel`/`Yolo2DDetector` 是懒加载的；单测里用
> 假模型注入即可（见 `dimos/agents/skills/test_person_recognition.py`）。
> **`cv` 后端连模型都不需要**（LBPH 在相册照片上训练），单测直接用真
> `cv2.face.LBPHFaceRecognizer`（见 `dimos/agents/skills/test_person_recognition_cv.py`）。

---

## 功能四：VLM 自动场景标注（自动 location tagging）

把原来需要手动打 tag 的 **named location** 变成自动识别：用本地/局域网 VLM
（OpenAI-compatible vision API）看当前画面，生成 caption，并自动推断“这是哪个
房间/地点”（如 `kitchen`、`living room`、`office`）。

**开关（`SpatialConfig`）：**

- `--spatialmemory.vlm-url <url>` — VLM endpoint，例如 `http://10.6.32.16:8000`
- `--spatialmemory.vlm-model <model>` — 模型名，默认 `Inferact/Qwen3.8-27B-NVFP4`
- `--spatialmemory.vlm-enable-place-tagging` — 如果 VLM 返回 `place`，自动 `add_named_location`
- `--spatialmemory.vlm-distance-m <m>` — 机器人移动多少米后再次调用 VLM，默认 `1.0`
- `--spatialmemory.vlm-timeout <sec>` — 请求超时，默认 `120`
- `--spatialmemory.vlm-max-tokens <n>` — 最大输出 token，默认 `512`
- `--spatialmemory.vlm-prompt <str>` — 自定义 prompt（默认要求输出 JSON）

默认 `vlm_url=None` → 完全不变（不调用 VLM）。

```bash
# 只生成 caption，不自动创建 named location
. venv_dev/bin/activate
dimos --replay run unitree-go2-agentic \
  --spatialmemory.vlm-url http://10.6.32.16:8000

# 生成 caption + 自动 named location tagging
. venv_dev/bin/activate
dimos --replay run unitree-go2-agentic \
  --spatialmemory.vlm-url http://10.6.32.16:8000 \
  --spatialmemory.vlm-enable-place-tagging

# 搭配加载已有 scene map + scene graph web dialog
. venv_dev/bin/activate
dimos --replay run unitree-go2-agentic \
  --spatialmemory.scene-map-dir assets/scene_maps/test_map \
  --spatialmemory.vlm-url http://10.6.32.16:8000 \
  --spatialmemory.vlm-enable-place-tagging \
  --scene-graph-server.port 5556
```

VLM 输出结构化 JSON：

```json
{"caption": "A living room with a sofa and coffee table.", "place": "living room"}
```

- `caption` 会写进 `SpatialMemory` 每一帧的 ChromaDB metadata 里。
- `place` 非空且 `--spatialmemory.vlm-enable-place-tagging` 开启时，会自动注册 named location。
- 纯色/无意义画面通常会返回 `"place": null`。

> 这个特性是纯增量的：不设置 `--spatialmemory.vlm-url` 时，`SpatialMemory` 行为
> 和原来完全一致。

### 后台进程与报告

VLM 调用放在后台线程里，不阻塞建图主循环。每次处理识别结果后立即追加并刷新 JSONL，
不需要等程序结束；机器人停止移动时也会继续处理已返回的结果。
设置 `scene_map_dir` 时报告保存在该地图目录下，否则保存在
`assets/output/memory/spatial_memory/vlm_tags_YYYYMMDD_HHMMSS.jsonl`：

```bash
cat assets/output/memory/spatial_memory/vlm_tags_*.jsonl
```

每行一个 JSON，例如：

```json
{"frame_id": "frame_20261002_081234_a1b2c3d4", "caption": "A kitchen with white cabinets.", "place": "kitchen", "position": [1.23, 0.45, 0.0], "rotation": [0.0, 0.0, 0.12], "timestamp": 1756840354.12}
```

### 导航回已标记的地点

VLM 自动标记的地点会同步写入 `SpatialMemory` 的 location collection，因此
LLM/agent 可以直接调用：

```python
# 让机器人导航到 "kitchen"
go_to_place(place_name="kitchen")
```

等价的 MCP/agent 调用：

```bash
dimos mcp call go_to_place --arg place_name=kitchen
```

机器人会先精确匹配地点名，再语义查询，最后导航到该 3D 位置。

### 物品和地点的位置估计（Box + 点云 + 机器人位姿）

开启 `--spatialmemory.vlm-enable-object-tagging=true` 后，VLM 返回物品和像素坐标 Box。
系统保存拍摄时的相机内参、世界坐标变换和点云快照，避免 VLM 返回期间机器人移动导致偏差。
点云若在 lidar 等局部坐标系中，会先按对应时间的 TF 转换到 `world`。

将点云投影到图像，筛选 Box 内的有效前景点；启用 YOLO 分割时，还会用该帧的分割掩码过滤。
用前景深度的中位数和 Box 中心射线估计目标的世界坐标 `(x, y, z)`。
标签保存的是估计的目标坐标，不再被机器人的当前位置覆盖。

地点如果有可见的局部区域或入口，VLM 可返回 `place_bbox`，按同样的流程估计位置。
仅从整幅画面判断房间类型时，不能确定房间中心，所以使用拍摄时的机器人位置作为地点标签。
没有有效点云时使用默认距离沿相机射线估计，报告标记为 `default_distance`，不能当作实测距离。

```bash
. venv_dev/bin/activate
dimos --replay run unitree-go2-agentic \
  --spatialmemory.vlm-url=https://api.openai.com \
  --spatialmemory.vlm-model=gpt-4o-mini \
  --spatialmemory.vlm-enable-place-tagging=true \
  --spatialmemory.vlm-enable-object-tagging=true \
  --spatialmemory.vlm-distance-m=1.0 \
  --spatialmemory.scene-map-dir="$PWD/assets/scene_maps/test_map" \
  --spatialmemory.object-segmenter=yolo
```

OpenAI API key 由 `.env` 的 `OPENAI_API_KEY` 读取。自定义 VLM prompt 时，应要求返回
像素坐标 `items[].bbox`，地点可选返回 `place_bbox`。

新增的 `SpatialConfig` 参数：

- `--spatialmemory.vlm-enable-object-tagging` — 开启物品级 3D 标注
- `--spatialmemory.object-default-distance-m <m>` — lidar 无 hit 时的默认距离，默认 `2.0`
- `--spatialmemory.object-max-distance-m <m>` — 忽略超过该距离的点，默认 `10.0`
- `--spatialmemory.object-sensor-time-tolerance-s <sec>` — 图像、点云和 TF 的时间容差，默认 `1.0` 秒；过期点云不用于深度估计

导航到物品：

```bash
dimos mcp call go_to_object --arg object_name="red chair"
```

报告包含 `objects`、`object_estimates`、`place_position`、`place_estimate` 和
`coordinate_frame="world"`。`position` 仍是观察时的机器人位置，`detections` 保留识别出的原始物品列表：

```json
{"frame_id":"...","coordinate_frame":"world","objects":{"red chair":[1.2,0.3,0.7]},"object_estimates":{"red chair":{"position":[1.2,0.3,0.7],"method":"pointcloud_bbox","point_count":18}},"place_position":[0.8,0.2,0.3],"place_estimate":{"method":"robot_observation_pose"}}
```

> VLM Box 和点云标定的精度会影响结果，稀疏点云也可能不足以确定目标深度。
> 物品表面坐标不等于机器人可到达的位置；导航仍需要代价地图、避障及合适的接近距离。

---

## 功能三：场景图的 Web 对话窗口（scene graph web dialog）

一个极简 HTTP 服务（只用标准库 `http.server`，无 FastAPI/uvicorn 依赖），把
`SpatialMemory` 里的场景图内容暴露出来，方便你/网页看“场景里有什么”。

**开关（`SceneGraphServerConfig`）：** `--scene-graph-server.port <n>`

- 默认 `None` → 不启动任何 server（完全不变）。
- 打开后在后台线程起一个 server，端点：
  - `http://127.0.0.1:<n>/`         —— 简单网页
  - `http://127.0.0.1:<n>/scene_graph` —— 场景图（JSON）：机器人位置 + 统计
  - `http://127.0.0.1:<n>/query?q=the+bed` —— 语义查询场景图（JSON 结果）
  - `http://127.0.0.1:<n>/health`   —— 健康检查（`{"ok": true}`）

```bash
# 起一个场景图对话窗口，监听 5556
dimos run unitree-go2-agentic --robot-ip 192.168.123.161 \
  --scene-graph-server.port 5556

# 另一个终端 / 浏览器看：
curl http://127.0.0.1:5556/scene_graph
curl "http://127.0.0.1:5556/query?q=the+bed"
curl http://127.0.0.1:5556/health
# 浏览器打开 http://127.0.0.1:5556/
```

---

## 三个开关一起用（+ 物品级 3D 标注）

```bash
dimos run unitree-go2-agentic --robot-ip 192.168.123.161 \
  --spatialmemory.scene-map-dir ./my_saved_scene \
  --spatialmemory.vlm-url http://10.6.32.16:8000 \
  --spatialmemory.vlm-enable-place-tagging \
  --spatialmemory.vlm-enable-object-tagging \
  --named-person-recognizer-skill-container.gallery-dir ./people \
  --named-person-recognizer-skill-container.threshold 0.6 \
  --scene-graph-server.port 5556
```

---

## 测试（快速，无需重依赖）

```bash
uv run pytest \
  dimos/agents/skills/test_person_recognition.py \
  dimos/agents/skills/test_person_recognition_cv.py \
  dimos/agents/skills/test_scene_graph_server.py \
  dimos/perception/experimental/test_scene_map_config.py
```

- 人识别（reid 后端）：用假模型 / 假检测器 / 假喇叭，覆盖 匹配 / 低于阈值 / 冷却 / 无喇叭 / 全流程 / 加载相册。
- 人识别（**cv 后端**）：用真 `cv2.face.LBPHFaceRecognizer`（本地、无模型下载），覆盖 识别 / 无匹配 / 惰性 / LBPH / 检测器。
- 场景图窗口：起一个真 server（临时端口）+ 假 SpatialMemory，打 `/scene_graph`、`/query`、`/`、`/health`。
- 场景图加载：monkeypatch 掉 chromadb / CLIP / visual memory，断言 `scene_map_dir` 推导出的路径。

---

## 本次验证结果（self_hosted / 本地 / agentic 可行性）

| 项目 | 结果 |
|---|---|
| **本地模型 = 传统 CV（LBPH）** | ✅ `cv2.face.LBPHFaceRecognizer` 在 CPU 上跑通：相册训练 → 预测 → 播报，**无需下载任何模型**（见 `test_person_recognition_cv.py`，6 个用例全过） |
| **CLIP ONNX（场景图/地图）** | ⚠️ 本地有 `./data/models_clip/model.onnx`（605MB，onnxruntime 能加载）；但场景图用的 `ImageEmbeddingProvider` 走 HuggingFace API，本机 HF 缓存 `openai/clip-vit-base-patch32` **不完整**（离线加载报 `AttributeError`），所以“加载已生成场景图”这一路目前被 HF 缓存卡住（与本次新增代码无关，是环境问题） |
| **TorchReIDModel（reid 后端）** | ⚠️ 缺 `osnet_x1_0.pth` 权重（无 `models_torchreid` 目录）→ reid 后端需要联网下载；所以**离线场景请用 `cv` 后端** |
| **agentic 可行性（蓝图）** | ✅ `unitree-go2-agentic` 蓝图正常 import + build，两个新模块（`NamedPersonRecognizerSkillContainer` / `SceneGraphServerModule`）已接入（默认 inert） |
| **全量测试** | ✅ 22 个用例全过（8 reid + 6 cv + 5 场景图窗口 + 3 场景图加载）；mypy / ruff 在 4 个生产文件上全过 |

> **一句话**：要在**完全离线/本地**跑人识别，用 `--named-person-recognizer-skill-container.recognition-backend cv`
> （传统 CV LBPH，零模型下载）；要加载“已生成的场景图”，需要本机 HF 的 CLIP 缓存完整
> （或用本地 ONNX CLIP 替换 `ImageEmbeddingProvider`）。

---

## 改了什么（全部是“增量”，不动原架构）

| 文件 | 改动 | 影响 |
|---|---|---|
| `dimos/perception/experimental/spatial_perception.py` | `SpatialConfig` 加 `scene_map_dir` 字段 + 在 `__init__` 里推导路径 | 默认 `None` → 无副作用 |
| `dimos/perception/experimental/spatial_memory_spec.py` | `SpatialMemorySpec` 加 `get_robot_locations` / `get_stats` | `SpatialMemory` 本来就有这俩方法，纯补充 |
| `dimos/agents/skills/person_recognition.py` | **新增** 模块（功能二），含 `reid` + `cv`(LBPH) 两种后端 | 默认 `gallery_dir=None` → no-op |
| `dimos/agents/skills/scene_graph_server.py` | **新增** 模块（功能三） | 默认 `port=None` → no-op |
| `dimos/robot/unitree/go2/blueprints/agentic/unitree_go2_agentic.py` | 把上面 2 个新模块接进蓝图 | 不开开关就不生效 |

> 设计原则：每个新模块的 `start()` 里第一件事就是“开关没开就 `super().start()` 然后
> 直接返回”，所以默认情况下蓝图的行为、e2e 测试完全不变。`cv` 后端是纯增量（`reid`
> 后端完全保留，`recognition_backend` 默认 `reid`，不改原路径）。

| 文件 | 改动 | 影响 |
|---|---|---|
| `dimos/perception/experimental/vlm_caption_provider.py` | **新增** VLM 调用模块（功能四）；解析 caption/place/items+bbox | 默认未实例化 → no-op |
| `dimos/perception/experimental/spatial_perception.py` | `SpatialConfig` 加 VLM 字段 + 后台进程调用 VLM + 写 JSONL 报告 + `add_robot_location` 自动 `tag_location` + 物品 3D 投影 | 默认 `vlm_url=None` → 无副作用 |
| `dimos/perception/experimental/spatial_vector_db.py` | 新增 `update_metadata` | 支持异步 caption 回写 |
| `dimos/perception/experimental/spatial_memory_spec.py` | 加 `find_robot_location` | Spec 补齐 |
| `dimos/robot/unitree/unitree_skill_container.py` | 新增 `go_to_place` 和 `go_to_object` skills | 按名称导航到地点/物品 |


## Update

python -m pip install -e '.[all,tests]'