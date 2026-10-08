# Copyright 2025-2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Spatial Memory module for creating a semantic map of the environment.
"""

from collections.abc import Mapping
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any
import uuid

import numpy as np
from pydantic import AliasChoices, Field
from reactivex import Observable, interval, operators as ops
from reactivex.disposable import Disposable
from scipy.spatial import ConvexHull, QhullError

from dimos.constants import DIMOS_PROJECT_ROOT
from dimos.core.core import rpc
from dimos.core.global_config import global_config
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Transform import Transform
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.CameraInfo import CameraInfo
from dimos.msgs.sensor_msgs.Image import Image, ImageFormat
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.msgs.tf2_msgs.TFMessage import TFMessage
from dimos.navigation.go2.loop_closure.pgo import PoseGraph
from dimos.perception.experimental.image_embedding import ImageEmbeddingProvider
from dimos.perception.experimental.object_segmentation import (
    ObjectSegmentationProvider,
    VlmBboxSegmenter,
    create_segmenter,
    mask_centroid,
)
from dimos.perception.experimental.spatial_vector_db import SpatialVectorDB
from dimos.perception.experimental.tag_view import match_tag_view
from dimos.perception.experimental.visual_memory import VisualMemory
from dimos.perception.experimental.vlm_caption_provider import (
    DEFAULT_VLM_PROMPT,
    VlmCaptionProvider,
)
from dimos.protocol.tf.tf import MultiTBuffer
from dimos.types.robot_location import RobotLocation
from dimos.utils.logging_config import setup_logger

_OUTPUT_DIR = DIMOS_PROJECT_ROOT / "assets" / "output"
_MEMORY_DIR = _OUTPUT_DIR / "memory"
_SPATIAL_MEMORY_DIR = _MEMORY_DIR / "spatial_memory"
_DB_PATH = _SPATIAL_MEMORY_DIR / "chromadb_data"
_VISUAL_MEMORY_PATH = _SPATIAL_MEMORY_DIR / "visual_memory.pkl"

logger = setup_logger()


class SpatialConfig(ModuleConfig):
    collection_name: str = "spatial_memory"
    embedding_model: str = "clip"
    embedding_dimensions: int = 512
    min_distance_threshold: float = 0.01  # Min distance in meters to store a new frame
    min_time_threshold: float = 1.0  # Min time in seconds to record a new frame
    db_path: str | None = str(_DB_PATH)  # Path for ChromaDB persistence
    visual_memory_path: str | None = str(
        _VISUAL_MEMORY_PATH
    )  # Path for saving/loading visual memory
    new_memory: bool = True  # Whether to create a new memory from scratch
    # Load a previously-generated scene map: one directory containing a saved
    # ChromaDB (`chromadb_data/`) and `visual_memory.pkl`. When set, this
    # overrides db_path/visual_memory_path and forces new_memory=False so the
    # existing map is loaded instead of rebuilt. This is the "native scene map
    # that DimOS generated in real time" knob for `dimos run <blueprint> --spatial-memory.scene-map-dir <path>`.
    scene_map_dir: str | None = None
    output_dir: str | None = str(_SPATIAL_MEMORY_DIR)  # Directory for storing visual memory data
    chroma_client: Any = None  # Optional ChromaDB client for persistence
    visual_memory: VisualMemory | None = None  # Optional VisualMemory instance for storing images

    # --- VLM-based automatic scene annotation (opt-in) ----------------------
    # When ``vlm_url`` is set, SpatialMemory will call the OpenAI-compatible
    # vision endpoint for each stored keyframe and save the caption in the
    # frame metadata.  If ``vlm_enable_place_tagging`` is true, a place name
    # extracted from the caption is also registered as a named robot location.
    vlm_url: str | None = None
    vlm_model: str = "Inferact/Qwen3.8-27B-NVFP4"
    vlm_prompt: str = DEFAULT_VLM_PROMPT
    vlm_enable_place_tagging: bool = False
    vlm_timeout: float = 120.0
    vlm_max_tokens: int = 512
    vlm_distance_m: float = 1.0  # Minimum travelled distance between VLM captions
    vlm_api_key: str | None = Field(
        default_factory=lambda: global_config.openai_api_key,
        validation_alias=AliasChoices("vlm_api_key", "openai_api_key"),
    )

    # --- Object-level 3D annotation (opt-in) --------------------------------
    # When ``vlm_enable_object_tagging`` is true and a VLM endpoint is set,
    # the VLM is asked to also return a list of objects with 2D bboxes. Each
    # object is projected into 3D world coordinates using camera intrinsics,
    # the current TF, and the lidar pointcloud.
    vlm_enable_object_tagging: bool = False
    object_default_distance_m: float = 2.0  # Fallback distance if lidar has no ray hit
    object_max_distance_m: float = 10.0  # Ignore lidar hits beyond this
    object_sensor_time_tolerance_s: float = 1.0
    object_segmenter: str = "auto"  # "auto" | "vlm" | "yolo"


def _vlm_worker(
    config: dict[str, Any],
    task_queue: "queue.Queue[dict[str, Any] | None]",
    result_queue: "queue.Queue[dict[str, Any]]",
) -> None:
    """Background worker that captions frames via VLM and emits results.

    Runs in a background thread so that slow VLM calls do not block the main
    spatial-memory frame loop. Each result is placed on ``result_queue``; the
    parent process consumes it asynchronously.
    """
    import cv2

    from dimos.perception.experimental.vlm_caption_provider import VlmCaptionProvider

    provider = VlmCaptionProvider(
        base_url=config["base_url"],
        model=config["model"],
        prompt=config["prompt"],
        max_tokens=config["max_tokens"],
        timeout=config["timeout"],
        api_key=config.get("api_key"),
    )

    while True:
        task = task_queue.get()
        if task is None:
            break

        try:
            frame = cv2.imdecode(task["frame_array"], cv2.IMREAD_COLOR)
            if frame is None:
                result_queue.put(
                    {
                        "frame_id": task["frame_id"],
                        "generation": task.get("generation", 0),
                        "error": "decode_failed",
                    }
                )
                continue

            result = provider.analyze(frame)
            if result is None:
                result_queue.put(
                    {
                        "frame_id": task["frame_id"],
                        "generation": task.get("generation", 0),
                        "error": "vlm_failed",
                    }
                )
                continue

            result_queue.put(
                {
                    "frame_id": task["frame_id"],
                    "generation": task.get("generation", 0),
                    "caption": result.get("caption"),
                    "place": result.get("place"),
                    "items": result.get("items", []),
                    "place_bbox": result.get("place_bbox"),
                    "projection_context": task.get("projection_context"),
                    "frame_bgr": frame,
                    "position": task.get("position"),
                    "rotation": task.get("rotation"),
                    "timestamp": task.get("timestamp"),
                }
            )
        except Exception as e:
            result_queue.put(
                {
                    "frame_id": task["frame_id"],
                    "generation": task.get("generation", 0),
                    "error": str(e),
                }
            )


class SpatialMemory(Module):
    """
    A Dimos module for building and querying Robot spatial memory.

    This module processes video frames and odometry data from LCM streams,
    associates them with XY locations, and stores them in a vector database
    for later retrieval via RPC calls. It also maintains a list of named
    robot locations that can be queried by name.
    """

    config: SpatialConfig
    dedicated_worker = True

    # LCM inputs
    color_image: In[Image]
    tf: In[TFMessage]
    pgo_raw_tf: In[TFMessage]
    pgo_raw_lidar: In[PointCloud2]
    lidar: In[PointCloud2]
    global_map: In[PointCloud2]
    camera_info: In[CameraInfo]

    def __init__(self, **kwargs: Any) -> None:
        """
        Initialize the spatial perception system.

        Args:
            collection_name: Name of the vector database collection
            embedding_model: Model to use for image embeddings ("clip", "resnet", etc.)
            embedding_dimensions: Dimensions of the embedding vectors
            min_distance_threshold: Minimum distance in meters to record a new frame
            min_time_threshold: Minimum time in seconds to record a new frame
            chroma_client: Optional ChromaDB client for persistent storage
            visual_memory: Optional VisualMemory instance for storing images
            output_dir: Directory for storing visual memory data if visual_memory is not provided
        """
        super().__init__(**kwargs)

        self.collection_name = self.config.collection_name
        self.embedding_model = self.config.embedding_model
        self.embedding_dimensions = self.config.embedding_dimensions
        self.min_distance_threshold = self.config.min_distance_threshold
        self.min_time_threshold = self.config.min_time_threshold
        # A single scene_map_dir loads a saved native scene map: derive the
        # ChromaDB + visual-memory paths from it and load (do not rebuild).
        if self.config.scene_map_dir:
            scene_dir = Path(self.config.scene_map_dir)
            self.config.db_path = str(scene_dir / "chromadb_data")
            self.config.visual_memory_path = str(scene_dir / "visual_memory.pkl")
            self.config.new_memory = False
            logger.info(f"Loading scene map from {scene_dir}")
        self.db_path = self.config.db_path
        self.visual_memory_path = self.config.visual_memory_path

        # Setup ChromaDB client if not provided
        self._chroma_client = self.config.chroma_client
        if self._chroma_client is None and self.db_path is not None:
            # Create db directory if needed
            os.makedirs(self.db_path, exist_ok=True)

            # Clean up existing DB if creating new memory
            if self.config.new_memory and os.path.exists(self.db_path):
                try:
                    # Try to delete any existing database files
                    import shutil

                    for item in os.listdir(self.db_path):
                        item_path = os.path.join(self.db_path, item)
                        if os.path.isfile(item_path):
                            os.unlink(item_path)
                        elif os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                    logger.info(f"Removed existing ChromaDB files from {self.db_path}")
                except Exception as e:
                    logger.error(f"Error clearing ChromaDB directory: {e}")

            import chromadb
            from chromadb.config import Settings

            self._chroma_client = chromadb.PersistentClient(
                path=self.db_path, settings=Settings(anonymized_telemetry=False)
            )

        # Initialize or load visual memory
        self._visual_memory = self.config.visual_memory
        if self._visual_memory is None:
            if self.config.new_memory or not os.path.exists(self.visual_memory_path or ""):
                self._visual_memory = VisualMemory(output_dir=self.config.output_dir)
            else:
                try:
                    self._visual_memory = VisualMemory.load(
                        self.visual_memory_path,  # type: ignore[arg-type]
                        output_dir=self.config.output_dir,
                    )
                except Exception as e:
                    logger.error(f"Error loading visual memory: {e}")
                    self._visual_memory = VisualMemory(output_dir=self.config.output_dir)

        self.embedding_provider = ImageEmbeddingProvider(
            model_name=self.embedding_model, dimensions=self.embedding_dimensions
        )

        self.vector_db: SpatialVectorDB = SpatialVectorDB(
            collection_name=self.collection_name,
            chroma_client=self._chroma_client,
            visual_memory=self._visual_memory,
            embedding_provider=self.embedding_provider,
        )

        self.last_position: Vector3 | None = None
        self.last_record_time: float | None = None

        self.frame_count: int = 0
        self.stored_frame_count: int = 0

        # List to store robot locations
        self.robot_locations: list[RobotLocation] = []

        # Optional VLM captioner for automatic scene annotation
        self._vlm: VlmCaptionProvider | None = None
        self._vlm_thread: threading.Thread | None = None
        self._vlm_task_queue: queue.Queue[dict[str, Any] | None] | None = None
        self._vlm_result_queue: queue.Queue[dict[str, Any]] | None = None
        self._vlm_report: list[dict[str, Any]] = []
        self._auto_tagging_enabled = True
        self._tagging_generation = 0
        self._tagging_lock = threading.RLock()
        self._vlm_report_path: Path | None = None
        # Track robot position at which the last VLM task was enqueued so we
        # can throttle VLM calls by travelled distance (e.g. every 1m).
        self._vlm_last_position: Vector3 | None = None
        if self.config.vlm_url:
            self._vlm = VlmCaptionProvider(
                base_url=self.config.vlm_url,
                model=self.config.vlm_model,
                prompt=self.config.vlm_prompt,
                max_tokens=self.config.vlm_max_tokens,
                timeout=self.config.vlm_timeout,
                api_key=self.config.vlm_api_key,
            )
            self._vlm_task_queue = queue.Queue()
            self._vlm_result_queue = queue.Queue()
            # If a scene map directory is being used, keep the VLM report next
            # to the map so it travels with the saved scene data.
            if self.config.scene_map_dir:
                report_dir = Path(self.config.scene_map_dir)
            else:
                report_dir = Path(self.config.output_dir or _SPATIAL_MEMORY_DIR)
            report_dir.mkdir(parents=True, exist_ok=True)
            self._vlm_report_path = (
                report_dir / f"vlm_tags_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
            )
            self._vlm_thread = threading.Thread(
                target=_vlm_worker,
                args=(
                    {
                        "base_url": self.config.vlm_url,
                        "model": self.config.vlm_model,
                        "prompt": self.config.vlm_prompt,
                        "max_tokens": self.config.vlm_max_tokens,
                        "timeout": self.config.vlm_timeout,
                        "api_key": self.config.vlm_api_key,
                    },
                    self._vlm_task_queue,
                    self._vlm_result_queue,
                ),
                daemon=True,
            )
            self._vlm_thread.start()
            logger.info(f"Started VLM tagging worker; report -> {self._vlm_report_path}")

        # Latest sensor data for object-level 3D projection
        self._latest_pointcloud: PointCloud2 | None = None
        self._map_points: np.ndarray | None = None
        self._pgo_graph: PoseGraph | None = None
        self._pgo_session: str | None = None
        self._pgo_sync_error: str | None = None
        self._pgo_raw_buffer = MultiTBuffer()
        self._pgo_raw_cloud: PointCloud2 | None = None
        self._latest_camera_info: CameraInfo | None = None
        self._latest_video_frame_bgr: np.ndarray | None = None
        self._latest_observation: tuple[np.ndarray, dict[str, Any] | None, float] | None = None

        # Optional torch-based segmenter for refining VLM bboxes.
        self._object_segmenter: ObjectSegmentationProvider | None = None
        if self.config.vlm_enable_object_tagging:
            if self.config.object_segmenter == "vlm":
                self._object_segmenter = VlmBboxSegmenter()
            elif self.config.object_segmenter == "yolo":
                from dimos.perception.experimental.object_segmentation import YoloSegSegmenter

                self._object_segmenter = YoloSegSegmenter()
            else:
                self._object_segmenter = create_segmenter(prefer_torch=True)

        # Track latest data for processing
        self._latest_video_frame: np.ndarray | None = None
        self._process_interval = 1

    def _drain_vlm_results(self) -> None:
        """Consume any VLM results available without blocking."""
        if self._vlm_result_queue is None:
            return
        import queue

        while True:
            try:
                result = self._vlm_result_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_vlm_result(result)

    @rpc
    def automatic_tagging_status(self) -> dict[str, Any]:
        with self._tagging_lock:
            configured = self._vlm is not None and (
                self.config.vlm_enable_place_tagging or self.config.vlm_enable_object_tagging
            )
            return {
                "configured": configured,
                "enabled": configured and self._auto_tagging_enabled,
                "place_tagging": self.config.vlm_enable_place_tagging,
                "object_tagging": self.config.vlm_enable_object_tagging,
            }

    @rpc
    def update_pgo_graph(
        self, graph: PoseGraph, session_id: str, map_cloud: PointCloud2 | None = None
    ) -> None:
        """Synchronously persist this run's coordinates in the fixed saved-map world."""
        with self._tagging_lock:
            self._ensure_pgo_ready()
            if self._pgo_session is not None and self._pgo_session != session_id:
                raise RuntimeError("SpatialMemory cannot mix two live PGO sessions")
            self._pgo_session = session_id
            try:
                self._rewrite_pgo_records(graph, session_id)
            except Exception as error:
                self._pgo_sync_error = str(error)
                self.set_automatic_tagging(False)
                logger.exception("PGO memory synchronization failed; queries and tagging blocked")
                raise
            self._pgo_graph = graph
            if map_cloud is not None:
                self._map_points = map_cloud.points_f32().copy()

    def _ensure_pgo_ready(self) -> None:
        if self._pgo_sync_error is not None:
            raise RuntimeError(
                f"PGO synchronization failed; restart required: {self._pgo_sync_error}"
            )

    def _rewrite_pgo_records(self, graph: PoseGraph, session_id: str) -> None:
        for collection in (self.vector_db.location_collection, self.vector_db.image_collection):
            records = collection.get(where={"pgo_session": session_id}, include=["metadatas"])
            ids, metadatas = records["ids"], records["metadatas"]
            updated: list[Mapping[str, Any]] = []
            for metadata in metadatas:
                raw = Transform.from_matrix(
                    np.asarray(json.loads(metadata["pgo_raw_pose"])),
                    frame_id="world",
                    child_frame_id="base_link",
                )
                pose = (
                    graph.world_correction(metadata["pgo_timestamp"]) + raw
                    if graph.keyframes
                    else raw
                )
                angles = pose.rotation.to_euler()
                updated.append(
                    {
                        **metadata,
                        "pos_x": float(pose.translation.x),
                        "pos_y": float(pose.translation.y),
                        "pos_z": float(pose.translation.z),
                        "rot_x": float(angles.x),
                        "rot_y": float(angles.y),
                        "rot_z": float(angles.z),
                    }
                )
            if ids:
                collection.update(ids=ids, metadatas=updated)

    def _pgo_metadata(
        self, metadata: dict[str, Any], timestamp: float, graph: PoseGraph | None
    ) -> dict[str, Any]:
        self._ensure_pgo_ready()
        if self._pgo_session is None:
            return metadata
        pose = Transform(
            translation=Vector3([metadata[f"pos_{axis}"] for axis in "xyz"]),
            rotation=Quaternion.from_euler(Vector3([metadata[f"rot_{axis}"] for axis in "xyz"])),
            frame_id="world",
            child_frame_id="base_link",
        )
        raw = (
            graph.world_correction(timestamp).inverse() + pose
            if graph and graph.keyframes
            else pose
        )
        corrected = (
            self._pgo_graph.world_correction(timestamp) + raw
            if self._pgo_graph and self._pgo_graph.keyframes
            else raw
        )
        angles = corrected.rotation.to_euler()
        return {
            **metadata,
            "pgo_session": self._pgo_session,
            "pgo_timestamp": timestamp,
            "pgo_raw_pose": json.dumps(raw.to_matrix().tolist()),
            "pos_x": float(corrected.translation.x),
            "pos_y": float(corrected.translation.y),
            "pos_z": float(corrected.translation.z),
            "rot_x": float(angles.x),
            "rot_y": float(angles.y),
            "rot_z": float(angles.z),
        }

    @rpc
    def set_automatic_tagging(self, enabled: bool) -> dict[str, Any]:
        """Pause/resume configured automatic tags, leaving manual tagging available."""
        with self._tagging_lock:
            if enabled:
                self._ensure_pgo_ready()
            if enabled and not self.automatic_tagging_status()["configured"]:
                raise RuntimeError(
                    "Configure a VLM and automatic tagging in Settings, then restart."
                )
            if enabled != self._auto_tagging_enabled:
                self._auto_tagging_enabled = enabled
                self._tagging_generation += 1
                self._vlm_last_position = None
                if self._vlm_task_queue is not None:
                    while True:
                        try:
                            task = self._vlm_task_queue.get_nowait()
                        except queue.Empty:
                            break
                        if task is None:
                            self._vlm_task_queue.put(None)
                            break
            logger.info("Automatic tagging changed", enabled=enabled)
            return self.automatic_tagging_status()

    def _enqueue_vlm_task(self, task: dict[str, Any], position: Vector3) -> None:
        with self._tagging_lock:
            if self._auto_tagging_enabled and self._vlm_task_queue is not None:
                task["generation"] = self._tagging_generation
                self._vlm_task_queue.put(task)
                self._vlm_last_position = position

    def _handle_vlm_result(self, result: dict[str, Any]) -> None:
        with self._tagging_lock:
            if (
                not self._auto_tagging_enabled
                or result.get("generation", 0) != self._tagging_generation
            ):
                return
            self._apply_vlm_result(result)

    def _apply_vlm_result(self, result: dict[str, Any]) -> None:
        """Apply a VLM caption/place result and record it for the report."""
        frame_id = result.get("frame_id")
        error = result.get("error")
        if error:
            logger.warning(f"VLM tagging failed for {frame_id}: {error}")
            self._vlm_report.append({"frame_id": frame_id, "error": error})
            return

        caption = result.get("caption")
        place = result.get("place")
        position = result.get("position")
        rotation = result.get("rotation")
        timestamp = result.get("timestamp")
        context = result.get("projection_context")
        frame_bgr = result.get("frame_bgr")
        place_position = position

        # Update the vector DB metadata if the frame is still present.
        if frame_id is not None and caption:
            try:
                self.vector_db.update_metadata(frame_id, {"caption": caption})
            except Exception as e:
                logger.warning(f"Could not update caption metadata for {frame_id}: {e}")

        # Add named location if a place was inferred and it is new.
        if self.config.vlm_enable_place_tagging:
            if place and self.find_robot_location(place) is None:
                self.add_named_location(
                    name=place,
                    position=place_position,
                    rotation=rotation,
                    description=caption,
                    observation=context if self._pgo_session else None,
                    reference_image=frame_bgr,
                )
            elif caption and not place:
                # Even if no place name was inferred, record the current
                # position with the caption as a fallback tag so the robot
                # still has a named target for this view.
                self.add_named_location(
                    name=caption[:40].strip(),
                    position=position,
                    rotation=rotation,
                    description=caption,
                    observation=context if self._pgo_session else None,
                    reference_image=frame_bgr,
                )

        # Add 3D object tags from VLM-detected items.
        object_positions: dict[str, list[float]] = {}
        object_estimates: dict[str, dict[str, Any]] = {}
        if self.config.vlm_enable_object_tagging:
            for item in result.get("items") or []:
                item_name = item.get("name")
                bbox = item.get("bbox")
                if not item_name or not bbox:
                    continue
                if context is None or frame_bgr is None:
                    continue
                estimate = self._estimate_target(
                    bbox, item_name, frame_bgr, context, require_depth=True
                )
                if estimate is None:
                    continue
                world_pos = estimate["position"]
                self._tag_object_location(
                    item_name,
                    world_pos,
                    rotation,
                    f"Object '{item_name}' seen in {place or 'scene'}: {caption}",
                    reference_image=self._tag_crop(frame_bgr, bbox),
                    **(
                        {"pgo_metadata": estimate["pgo_metadata"]}
                        if "pgo_metadata" in estimate
                        else {}
                    ),
                )
                object_positions[item_name] = world_pos
                object_estimates[item_name] = estimate

        report_entry: dict[str, Any] = {
            "frame_id": frame_id,
            "caption": caption,
            "place": place,
            "position": position,
            "rotation": rotation,
            "timestamp": timestamp,
            "coordinate_frame": "world",
            "place_position": place_position,
            "place_estimate": {"method": "robot_observation_pose"},
            "detections": result.get("items") or [],
        }
        if object_positions:
            report_entry["objects"] = object_positions
            report_entry["object_estimates"] = object_estimates
        self._vlm_report.append(report_entry)
        self._append_vlm_report_entry(report_entry)

    def _append_vlm_report_entry(self, entry: dict[str, Any]) -> None:
        """Append a single VLM tagging entry to the JSONL report immediately."""
        if self._vlm_report_path is None:
            return
        try:
            with open(self._vlm_report_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
        except Exception as e:
            logger.error(f"Failed to append VLM report entry: {e}")

    def _write_vlm_report(self) -> Path | None:
        """Write the accumulated VLM tagging report to disk.

        Deprecated by _append_vlm_report_entry; kept for compatibility and to
        ensure the final file exists.
        """
        if self._vlm_report_path is None or not self._vlm_report:
            return None
        try:
            with open(self._vlm_report_path, "w", encoding="utf-8") as f:
                for entry in self._vlm_report:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.info(f"Wrote VLM tagging report to {self._vlm_report_path}")
            return self._vlm_report_path
        except Exception as e:
            logger.error(f"Failed to write VLM report: {e}")
            return None

    def _estimate_object_position(
        self,
        bbox: list[int],
        item_name: str | None = None,
        frame_bgr: np.ndarray | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[float] | None:
        """Project an object using captured geometry rather than the robot's live pose."""
        if context is None:
            return None
        estimate = self._estimate_target(bbox, item_name or "object", frame_bgr, context)
        return estimate["position"] if estimate else None

    @staticmethod
    def _project_bbox_position(
        bbox: list[int],
        context: dict[str, Any],
        max_distance: float,
        default_distance: float,
    ) -> dict[str, Any] | None:
        """Estimate a box target in world coordinates from a captured sensor snapshot."""
        if len(bbox) != 4:
            return None
        width, height = context["image_size"]
        bounds = np.asarray(bbox, dtype=float)
        if not np.isfinite(bounds).all():
            return None
        left, top, right, bottom = bounds
        left, right = np.clip([left, right], 0, width)
        top, bottom = np.clip([top, bottom], 0, height)
        if right <= left or bottom <= top:
            return None
        intrinsics = np.asarray(context["intrinsics"], dtype=float).reshape(3, 3)
        if not np.isfinite(intrinsics).all() or intrinsics[0, 0] <= 0 or intrinsics[1, 1] <= 0:
            return None
        origin = np.asarray(context["camera_origin"], dtype=float)
        rotation = np.asarray(context["camera_rotation"], dtype=float)
        points = np.asarray(context["world_points"], dtype=float).reshape(-1, 3)
        camera_points = (points - origin) @ rotation
        distances = np.linalg.norm(camera_points, axis=1)
        valid = (
            np.isfinite(camera_points).all(axis=1)
            & (camera_points[:, 2] > 0)
            & (distances > 0.2)
            & (distances <= max_distance)
        )
        camera_points = camera_points[valid]
        pixels = camera_points @ intrinsics.T
        pixels = pixels[:, :2] / pixels[:, 2:3]
        inside = (
            (pixels[:, 0] >= left)
            & (pixels[:, 0] < right)
            & (pixels[:, 1] >= top)
            & (pixels[:, 1] < bottom)
        )
        mask = context.get("mask")
        if mask is not None:
            indices = np.flatnonzero(inside)
            image_pixels = pixels[indices].astype(int)
            inside[indices] &= mask[image_pixels[:, 1], image_pixels[:, 0]].astype(bool)
        candidates = camera_points[inside]
        center = np.array([(left + right) / 2, (top + bottom) / 2, 1.0])
        ray = np.linalg.solve(intrinsics, center)
        method = "default_distance"
        point_count = 0
        if len(candidates):
            front_depth = float(np.quantile(candidates[:, 2], 0.25, method="nearest"))
            foreground = candidates[
                np.abs(candidates[:, 2] - front_depth) <= max(0.3, front_depth * 0.1)
            ]
            camera_target = np.median(foreground, axis=0)
            method = "pointcloud_bbox"
            point_count = len(foreground)
        else:
            camera_target = ray / np.linalg.norm(ray) * min(default_distance, max_distance)
        position = origin + rotation @ camera_target
        return {
            "position": position.tolist(),
            "method": method,
            "point_count": point_count,
        }

    def _estimate_target(
        self,
        bbox: list[int],
        name: str,
        frame: np.ndarray | None,
        context: dict[str, Any],
        *,
        require_depth: bool = False,
    ) -> dict[str, Any] | None:
        """Refine a captured image region and estimate its world position."""
        if self._object_segmenter is not None and frame is not None:
            try:
                mask = self._object_segmenter.segment(frame, name, bbox=bbox)
                if mask is not None and mask.shape == frame.shape[:2] and np.any(mask):
                    context = {**context, "mask": mask}
            except Exception as error:
                logger.warning(f"Segmentation failed for '{name}': {error}")
        estimate = self._project_bbox_position(
            bbox, context, self.config.object_max_distance_m, self.config.object_default_distance_m
        )
        if estimate is not None:
            estimate = self._constrain_object_to_map(estimate, context)
        if require_depth and (estimate is None or estimate["point_count"] == 0):
            logger.warning(
                "Object tag skipped: no valid lidar points inside detected region", name=name
            )
            return None
        if estimate is not None and self._pgo_session is not None:
            metadata = {
                **{
                    f"pos_{axis}": value
                    for axis, value in zip("xyz", estimate["position"], strict=True)
                },
                "rot_x": 0.0,
                "rot_y": 0.0,
                "rot_z": 0.0,
            }
            with self._tagging_lock:
                metadata = self._pgo_metadata(
                    metadata, context["timestamp"], context.get("pgo_graph")
                )
            estimate = {
                **estimate,
                "position": [metadata[f"pos_{axis}"] for axis in "xyz"],
                "pgo_metadata": metadata,
            }
        return estimate

    @staticmethod
    def _constrain_object_to_map(
        estimate: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Clamp out-of-map XY estimates to a measured boundary, inset toward the robot."""
        inset_m = 0.3
        raw_points = context.get("map_points", context["world_points"])
        points = np.asarray(raw_points, dtype=float).reshape(-1, 3)
        points = points[np.isfinite(points).all(axis=1)]
        target = np.asarray(estimate["position"], dtype=float)
        origin = np.asarray(context.get("robot_origin", context["camera_origin"]), dtype=float)
        if not np.isfinite(target).all() or not np.isfinite(origin).all() or len(points) < 3:
            logger.warning("Object tag skipped: insufficient finite pointcloud/map geometry")
            return None
        try:
            hull = ConvexHull(points[:, :2])
        except QhullError:
            logger.warning("Object tag skipped: pointcloud has no usable XY map boundary")
            return None
        normals, offsets = hull.equations[:, :2], hull.equations[:, 2]
        if np.all(normals @ target[:2] + offsets <= 1e-8):
            return estimate
        if np.any(normals @ origin[:2] + offsets > 1e-8):
            logger.warning("Object tag skipped: robot lies outside the pointcloud map boundary")
            return None
        delta = target[:2] - origin[:2]
        distance = float(np.linalg.norm(delta))
        if distance <= inset_m:
            logger.warning("Object tag skipped: cannot inset map boundary by 30 cm")
            return None
        direction = delta / distance
        outward = normals @ direction
        exits = outward > 1e-10
        exit_distance = float(
            np.min(-(normals[exits] @ origin[:2] + offsets[exits]) / outward[exits])
        )
        boundary_xy = origin[:2] + direction * exit_distance
        # Require real point support near the ray exit, not an invented hull-edge coordinate.
        near_exit = np.linalg.norm(points[:, :2] - boundary_xy, axis=1)
        candidates = points[near_exit <= 0.2]
        if not len(candidates):
            logger.warning("Object tag skipped: no measured point near the sight-line map boundary")
            return None
        order = np.lexsort(
            (
                np.abs(candidates[:, 2] - target[2]),
                np.linalg.norm(candidates[:, :2] - boundary_xy, axis=1),
            )
        )
        boundary = candidates[order[0]]
        to_robot = origin[:2] - boundary[:2]
        length = float(np.linalg.norm(to_robot))
        if length <= inset_m + 1e-8:
            logger.warning("Object tag skipped: boundary is within 30 cm of the robot")
            return None
        position = boundary.copy()
        position[:2] += to_robot / length * inset_m
        logger.info(
            "Object estimate clamped to pointcloud boundary",
            original=target.tolist(),
            position=position.tolist(),
        )
        return {
            **estimate,
            "position": position.tolist(),
            "method": "pointcloud_map_boundary_inset",
            "point_count": len(candidates),
            "estimated_position": target.tolist(),
            "boundary_position": boundary.tolist(),
            "inset_m": inset_m,
        }

    @rpc
    def capture_object_observation(self) -> tuple[Image, dict[str, Any]]:
        """Capture an image and its aligned geometry before slow object detection."""
        observation = self._latest_observation
        if observation is None or observation[1] is None:
            raise RuntimeError(
                "No image with aligned camera geometry; check map alignment and sensors"
            )
        frame, context, timestamp = observation
        assert context is not None
        if len(context["world_points"]) == 0:
            raise RuntimeError("No timestamp-aligned lidar available for object tagging")
        return Image.from_numpy(frame.copy(), format=ImageFormat.BGR, ts=timestamp), context

    @rpc
    def tag_object_from_observation(
        self, name: str, bbox: list[int], image: Image, context: dict[str, Any]
    ) -> str:
        """Store the object's lidar-derived world position, never the robot position."""
        estimate = self._estimate_target(bbox, name, image.to_opencv(), context, require_depth=True)
        if estimate is None:
            raise RuntimeError(
                f"Cannot locate '{name}': no valid object depth. Move to a better view and retry."
            )
        position = estimate["position"]
        if not self._tag_object_location(
            name,
            position,
            [0.0, 0.0, 0.0],
            f"Object '{name}': lidar-derived surface position from image at {image.ts}",
            reference_image=self._tag_crop(image.to_opencv(), bbox),
            **({"pgo_metadata": estimate["pgo_metadata"]} if "pgo_metadata" in estimate else {}),
        ):
            raise RuntimeError(f"Failed to save object tag '{name}'")
        return json.dumps({"name": name, "frame": "world", **estimate}, ensure_ascii=False)

    @rpc
    def locate_in_observation(
        self, name: str, bbox: list[int], image: Image, context: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Estimate a box's lidar-derived world position without storing a tag.

        Returns ``None`` when the region has no valid lidar depth, so callers
        never fall back to the robot's position or a fixed distance.
        """
        estimate = self._estimate_target(bbox, name, image.to_opencv(), context, require_depth=True)
        if estimate is None:
            return None
        return {
            "position": [float(value) for value in estimate["position"]],
            "method": estimate["method"],
            "point_count": int(estimate["point_count"]),
        }

    @rpc
    def update_robot_location(self, location_id: str, position: list[float]) -> bool:
        """Move an existing tag to a new world position and stamp it with the current time.

        Used for tags of things that move, such as people. Refused while a PGO
        session is active, because corrected tag poses are managed there.
        """
        if len(position) != 3 or not all(math.isfinite(value) for value in position):
            raise ValueError("position must be three finite numbers")
        with self._tagging_lock:
            self._ensure_pgo_ready()
            if self._pgo_session is not None:
                logger.warning("Tag refresh refused during a PGO session", location_id=location_id)
                return False
            location = next(
                (item for item in self.get_robot_locations() if item.location_id == location_id),
                None,
            )
            if location is None:
                return False
            location.position = (float(position[0]), float(position[1]), float(position[2]))
            location.timestamp = time.time()
            self.vector_db.location_collection.update(
                ids=[location_id], metadatas=[location.to_vector_metadata()]
            )
        return True

    def _capture_projection_context(
        self, frame: np.ndarray, timestamp: float
    ) -> dict[str, Any] | None:
        """Capture timestamp-aligned geometry before asynchronous VLM processing."""
        with self._tagging_lock:
            return self._capture_projection_context_locked(frame, timestamp)

    def _capture_projection_context_locked(
        self, frame: np.ndarray, timestamp: float
    ) -> dict[str, Any] | None:
        info = self._latest_camera_info
        if info is None:
            return None
        tolerance = self.config.object_sensor_time_tolerance_s
        buffer = self._pgo_raw_buffer if self._pgo_session is not None else self.tfbuffer
        graph = self._pgo_graph
        camera_tf = buffer.get(
            "world",
            info.frame_id or "camera_optical",
            time_point=timestamp,
            time_tolerance=tolerance,
            warn=False,
        )
        if camera_tf is None:
            return None
        if graph is not None and graph.keyframes:
            camera_tf = graph.world_correction(timestamp) + camera_tf
        height, width = frame.shape[:2]
        if (width, height) != (info.width, info.height):
            return None
        world_points = np.empty((0, 3))
        cloud = self._pgo_raw_cloud if self._pgo_session is not None else self._latest_pointcloud
        if cloud is not None and abs(cloud.ts - timestamp) <= tolerance:
            points = np.asarray(cloud.pointcloud.points).copy()
            if cloud.frame_id == "world":
                world_points = points
            elif cloud.frame_id:
                cloud_tf = buffer.get(
                    "world",
                    cloud.frame_id,
                    time_point=cloud.ts,
                    time_tolerance=tolerance,
                    warn=False,
                )
                if cloud_tf is not None:
                    world_points = points @ cloud_tf.rotation.to_rotation_matrix().T + np.array(
                        [cloud_tf.translation.x, cloud_tf.translation.y, cloud_tf.translation.z]
                    )
            if graph is not None and graph.keyframes:
                correction = graph.world_correction(cloud.ts)
                world_points = (
                    world_points @ correction.rotation.to_rotation_matrix().T
                    + correction.translation.to_numpy()
                )
        robot_tf = buffer.get(
            "world", "base_link", time_point=timestamp, time_tolerance=tolerance, warn=False
        )
        if robot_tf is not None and graph is not None and graph.keyframes:
            robot_tf = graph.world_correction(timestamp) + robot_tf
        return {
            "image_size": (width, height),
            "intrinsics": np.asarray(info.K).reshape(3, 3).copy(),
            "camera_origin": np.array(
                [camera_tf.translation.x, camera_tf.translation.y, camera_tf.translation.z]
            ),
            "camera_rotation": camera_tf.rotation.to_rotation_matrix().copy(),
            "world_points": world_points,
            "pgo_graph": graph,
            "robot_tf": robot_tf,
            "timestamp": timestamp,
            "map_points": self._map_points if self._map_points is not None else world_points,
            "robot_origin": np.array(
                [robot_tf.translation.x, robot_tf.translation.y, robot_tf.translation.z]
                if robot_tf is not None
                else [camera_tf.translation.x, camera_tf.translation.y, camera_tf.translation.z]
            ),
        }

    def _object_pixel_center(
        self,
        bbox: list[int],
        item_name: str | None,
        frame_bgr: np.ndarray | None,
    ) -> tuple[float | None, float | None]:
        """Return the pixel center to use for 3D projection.

        If a torch segmenter is available and ``frame_bgr`` is provided, use the
        mask centroid; otherwise fall back to the bbox center.
        """
        # Clamp bbox to image bounds first.
        x1, y1, x2, y2 = bbox
        width = int(self._latest_camera_info.width) if self._latest_camera_info else 0
        height = int(self._latest_camera_info.height) if self._latest_camera_info else 0
        if width <= 0 or height <= 0:
            return None, None
        x1, x2 = max(0, min(x1, width)), max(0, min(x2, width))
        y1, y2 = max(0, min(y1, height)), max(0, min(y2, height))
        if x2 <= x1 or y2 <= y1:
            return None, None

        if (
            self._object_segmenter is not None
            and frame_bgr is not None
            and frame_bgr.shape[0] == height
            and frame_bgr.shape[1] == width
        ):
            try:
                mask = self._object_segmenter.segment(
                    frame_bgr, item_name or "object", bbox=[x1, y1, x2, y2]
                )
                if mask is not None and mask.sum() > 0:
                    cx, cy = mask_centroid(mask)
                    logger.info(f"Segmenter refined '{item_name}' center to ({cx:.1f}, {cy:.1f})")
                    return cx, cy
            except Exception as e:
                logger.warning(f"Object segmenter failed for '{item_name}': {e}")

        return (x1 + x2) / 2.0, (y1 + y2) / 2.0

    def _query_pointcloud_distance(self, origin: Vector3, ray: np.ndarray) -> float | None:
        """Return distance to the nearest lidar point roughly along ``ray``.

        Searches the latest pointcloud for points within an angular cone of the
        ray and returns the closest one within ``object_max_distance_m``.
        """
        if self._latest_pointcloud is None:
            return None
        try:
            points = np.asarray(self._latest_pointcloud.pointcloud.points)
        except Exception as e:
            logger.debug(f"Could not read pointcloud points: {e}")
            return None
        if len(points) == 0:
            return None

        origin_arr = np.array([origin.x, origin.y, origin.z], dtype=np.float64)
        deltas = points - origin_arr
        distances = np.linalg.norm(deltas, axis=1)
        valid = (distances > 0.2) & (distances < self.config.object_max_distance_m)
        if not np.any(valid):
            return None

        directions = deltas[valid] / distances[valid][:, None]
        cos_angles = directions @ ray
        # Accept points within ~15 degrees of the ray.
        cone_valid = cos_angles > 0.9659
        if not np.any(cone_valid):
            return None

        nearest_idx = np.argmin(distances[valid][cone_valid])
        return float(distances[valid][cone_valid][nearest_idx])

    def _shutdown_vlm_worker(self) -> None:
        """Signal the VLM worker to stop and wait for it to finish."""
        if self._vlm_thread is None or self._vlm_task_queue is None:
            return
        try:
            self._vlm_task_queue.put(None, timeout=2.0)
        except Exception:
            pass
        self._vlm_thread.join(timeout=10.0)
        if self._vlm_thread.is_alive():
            logger.warning("VLM worker thread did not terminate gracefully")

    @rpc
    def start(self) -> None:
        super().start()

        # Subscribe to LCM streams
        def set_video(image_msg: Image) -> None:
            # Convert Image message to numpy array
            if hasattr(image_msg, "data"):
                frame = image_msg.to_opencv().copy()
                self._latest_video_frame = frame
                self._latest_video_frame_bgr = frame.copy()
                context = self._capture_projection_context(frame, image_msg.ts)
                self._latest_observation = (frame, context, image_msg.ts)
            else:
                logger.warning("Received image message without data attribute")

        self.register_disposable(Disposable(self.color_image.subscribe(set_video)))

        def set_lidar(pc: PointCloud2) -> None:
            self._latest_pointcloud = pc

        self.register_disposable(Disposable(self.lidar.subscribe(set_lidar)))
        self.register_disposable(
            Disposable(self.pgo_raw_tf.subscribe(self._pgo_raw_buffer.receive_tfmessage))
        )

        def set_raw_lidar(pc: PointCloud2) -> None:
            self._pgo_raw_cloud = pc

        self.register_disposable(Disposable(self.pgo_raw_lidar.subscribe(set_raw_lidar)))

        def set_map(pc: PointCloud2) -> None:
            if self._pgo_session is not None:
                return
            if pc.frame_id != "world":
                logger.warning("Ignoring object-tag map outside world frame", frame_id=pc.frame_id)
                return
            self._map_points = np.asarray(pc.pointcloud.points).copy()

        self.register_disposable(Disposable(self.global_map.subscribe(set_map)))

        def set_camera_info(info: CameraInfo) -> None:
            self._latest_camera_info = info

        self.register_disposable(Disposable(self.camera_info.subscribe(set_camera_info)))

        # Start periodic processing using interval
        self.register_disposable(
            interval(self._process_interval).subscribe(lambda _: self._process_frame())
        )

    @rpc
    def stop(self) -> None:
        # Drain any remaining VLM results before shutdown.
        if self._vlm is not None or self._pgo_session is not None:
            self._drain_vlm_results()
            self._shutdown_vlm_worker()
            self._write_vlm_report()

        # Save data before shutdown
        self.save()

        if self._visual_memory:
            self._visual_memory.clear()

        super().stop()

    def _process_frame(self) -> None:
        """Process the latest frame with pose data if available."""
        self._drain_vlm_results()
        observation = self._latest_observation
        if self._vlm is not None:
            if observation is None:
                return
            frame, projection_context, image_timestamp = observation
            if self._pgo_session is not None:
                tf = projection_context.get("robot_tf") if projection_context is not None else None
            else:
                tf = self.tfbuffer.get(
                    "world",
                    "base_link",
                    time_point=image_timestamp,
                    time_tolerance=self.config.object_sensor_time_tolerance_s,
                    warn=False,
                )
        else:
            if self._latest_video_frame is None:
                return
            frame = self._latest_video_frame
            projection_context = None
            image_timestamp = time.time()
            tf = self.tfbuffer.get("world", "base_link", warn=False)

        if tf is None:
            return

        if self._latest_video_frame is None:
            return

        # Create Pose object with position and orientation
        current_pose = tf.to_pose()

        # Process the frame directly
        try:
            self.frame_count += 1

            # Check distance constraint
            if self.last_position is not None:
                distance_moved = np.linalg.norm(
                    [
                        current_pose.position.x - self.last_position.x,
                        current_pose.position.y - self.last_position.y,
                        current_pose.position.z - self.last_position.z,
                    ]
                )
                if distance_moved < self.min_distance_threshold:
                    return

            # Check time constraint
            if self.last_record_time is not None:
                time_elapsed = time.time() - self.last_record_time
                if time_elapsed < self.min_time_threshold:
                    return

            current_time = time.time()

            # Get embedding for the frame
            frame_embedding = self.embedding_provider.get_embedding(frame)

            frame_id = f"frame_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            # Get euler angles from quaternion orientation for metadata
            euler = tf.rotation.to_euler()

            # Create metadata dictionary with primitive types only
            metadata: dict[str, Any] = {
                "pos_x": float(current_pose.position.x),
                "pos_y": float(current_pose.position.y),
                "pos_z": float(current_pose.position.z),
                "rot_x": float(euler.x),
                "rot_y": float(euler.y),
                "rot_z": float(euler.z),
                "timestamp": current_time,
                "frame_id": frame_id,
            }

            # Optional VLM caption for automatic scene annotation.
            # Enqueue a new VLM task once the robot has moved at least 1m since
            # the previous VLM task (or on the very first stored frame).  The
            # queue is unbounded so slow VLM calls do not block frame storage.
            should_vlm = self._vlm is not None and self._auto_tagging_enabled
            if should_vlm and self._vlm_last_position is not None:
                distance_since_last_vlm = np.linalg.norm(
                    [
                        current_pose.position.x - self._vlm_last_position.x,
                        current_pose.position.y - self._vlm_last_position.y,
                        current_pose.position.z - self._vlm_last_position.z,
                    ]
                )
                if distance_since_last_vlm < self.config.vlm_distance_m:
                    should_vlm = False

            if should_vlm and self._vlm_task_queue is not None:
                self._drain_vlm_results()
                import cv2

                success, encoded = cv2.imencode(".jpg", frame)
                if success:
                    self._enqueue_vlm_task(
                        {
                            "frame_id": frame_id,
                            "frame_array": encoded,
                            "projection_context": projection_context,
                            "position": [
                                float(current_pose.position.x),
                                float(current_pose.position.y),
                                float(current_pose.position.z),
                            ],
                            "rotation": [
                                float(euler.x),
                                float(euler.y),
                                float(euler.z),
                            ],
                            "timestamp": image_timestamp,
                        },
                        current_pose.position,
                    )
                else:
                    logger.warning(f"Failed to encode frame for VLM tagging: {frame_id}")

            # Store in vector database
            with self._tagging_lock:
                metadata = self._pgo_metadata(
                    metadata,
                    image_timestamp,
                    projection_context.get("pgo_graph") if projection_context else self._pgo_graph,
                )
                self.vector_db.add_image_vector(
                    vector_id=frame_id,
                    image=frame,
                    embedding=frame_embedding,
                    metadata=metadata,
                )

            # Update tracking variables
            self.last_position = current_pose.position
            self.last_record_time = current_time
            self.stored_frame_count += 1

            logger.info(
                f"Stored frame at position ({current_pose.position.x:.2f}, {current_pose.position.y:.2f}, {current_pose.position.z:.2f}), "
                f"rotation ({euler.x:.2f}, {euler.y:.2f}, {euler.z:.2f}) "
                f"stored {self.stored_frame_count}/{self.frame_count} frames"
            )

            # Periodically save visual memory to disk
            if self._visual_memory is not None and self.visual_memory_path is not None:
                if self.stored_frame_count % 100 == 0:
                    self.save()

        except Exception as e:
            logger.error(f"Error processing frame: {e}")

    @rpc
    def save(self) -> bool:
        """
        Save the visual memory component to disk.

        Returns:
            True if memory was saved successfully, False otherwise
        """
        if self._visual_memory is not None and self.visual_memory_path is not None:
            try:
                saved_path = self._visual_memory.save(self.visual_memory_path)
                logger.info(f"Saved {self._visual_memory.count()} images to {saved_path}")
                return True
            except Exception as e:
                logger.error(f"Failed to save visual memory: {e}")
        return False

    def process_stream(self, combined_stream: Observable) -> Observable:  # type: ignore[type-arg]
        """
        Process a combined stream of video frames and positions.

        This method handles a stream where each item already contains both the frame and position,
        such as the stream created by combining video and transform streams with the
        with_latest_from operator.

        Args:
            combined_stream: Observable stream of dictionaries containing 'frame' and 'position'

        Returns:
            Observable of processing results, including the stored frame and its metadata
        """

        def process_combined_data(data):  # type: ignore[no-untyped-def]
            self.frame_count += 1

            frame = data.get("frame")
            position_vec = data.get("position")  # Use .get() for consistency
            rotation_vec = data.get("rotation")  # Get rotation data if available

            if position_vec is None or rotation_vec is None:
                logger.info("No position or rotation data available, skipping frame")
                return None

            # position_vec is already a Vector3, no need to recreate it
            position_v3 = position_vec

            if self.last_position is not None:
                distance_moved = np.linalg.norm(
                    [
                        position_v3.x - self.last_position.x,
                        position_v3.y - self.last_position.y,
                        position_v3.z - self.last_position.z,
                    ]
                )
                if distance_moved < self.min_distance_threshold:
                    logger.debug("Position has not moved, skipping frame")
                    return None

            if (
                self.last_record_time is not None
                and (time.time() - self.last_record_time) < self.min_time_threshold
            ):
                logger.debug("Time since last record too short, skipping frame")
                return None

            current_time = time.time()

            frame_embedding = self.embedding_provider.get_embedding(frame)

            frame_id = f"frame_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

            # Create metadata dictionary with primitive types only
            metadata = {
                "pos_x": float(position_v3.x),
                "pos_y": float(position_v3.y),
                "pos_z": float(position_v3.z),
                "rot_x": float(rotation_vec.x),
                "rot_y": float(rotation_vec.y),
                "rot_z": float(rotation_vec.z),
                "timestamp": current_time,
                "frame_id": frame_id,
            }

            self.vector_db.add_image_vector(
                vector_id=frame_id, image=frame, embedding=frame_embedding, metadata=metadata
            )

            self.last_position = position_v3
            self.last_record_time = current_time
            self.stored_frame_count += 1

            logger.info(
                f"Stored frame at position ({position_v3.x:.2f}, {position_v3.y:.2f}, {position_v3.z:.2f}), "
                f"rotation ({rotation_vec.x:.2f}, {rotation_vec.y:.2f}, {rotation_vec.z:.2f}) "
                f"stored {self.stored_frame_count}/{self.frame_count} frames"
            )

            # Create return dictionary with primitive-compatible values
            return {
                "frame": frame,
                "position": (position_v3.x, position_v3.y, position_v3.z),
                "rotation": (rotation_vec.x, rotation_vec.y, rotation_vec.z),
                "frame_id": frame_id,
                "timestamp": current_time,
            }

        return combined_stream.pipe(
            ops.map(process_combined_data), ops.filter(lambda result: result is not None)
        )

    @rpc
    def query_by_image(self, image: np.ndarray, limit: int = 5) -> list[dict]:  # type: ignore[type-arg]
        """
        Query the vector database for images similar to the provided image.

        Args:
            image: Query image
            limit: Maximum number of results to return

        Returns:
            List of results, each containing the image and its metadata
        """
        embedding = self.embedding_provider.get_embedding(image)
        return self.vector_db.query_by_embedding(embedding, limit)

    @rpc
    def query_by_text(self, text: str, limit: int = 5) -> list[dict]:  # type: ignore[type-arg]
        """
        Query the vector database for images matching the provided text description.

        This method uses CLIP's text-to-image matching capability to find images
        that semantically match the text query (e.g., "where is the kitchen").

        Args:
            text: Text query to search for
            limit: Maximum number of results to return

        Returns:
            List of results, each containing the image, its metadata, and similarity score
        """
        logger.info(f"Querying spatial memory with text: '{text}'")
        with self._tagging_lock:
            self._ensure_pgo_ready()
            return self.vector_db.query_by_text(text, limit)

    @rpc
    def add_robot_location(self, location: RobotLocation) -> bool:
        """
        Add a named robot location to spatial memory.

        Args:
            location: The RobotLocation object to add

        Returns:
            True if successfully added, False otherwise
        """
        try:
            # Also persist it to the vector DB so it can be queried by name/text.
            if not self.tag_location(location):
                return False
            self.robot_locations.append(location)
            logger.info(f"Added robot location '{location.name}' at position {location.position}")
            return True

        except Exception as e:
            logger.error(f"Error adding robot location: {e}")
            return False

    def _tag_object_location(
        self,
        name: str,
        position: list[float],
        rotation: list[float] | None,
        description: str,
        *,
        pgo_metadata: dict[str, Any] | None = None,
        reference_image: np.ndarray | None = None,
    ) -> bool:
        for existing in self.get_robot_locations():
            if (
                existing.name.casefold() == name.casefold()
                and np.linalg.norm(np.asarray(existing.position) - position) <= 1.0
            ):
                if reference_image is not None and existing.frame_id is None:
                    with self._tagging_lock:
                        self._ensure_pgo_ready()
                        self._attach_tag_image(existing, reference_image)
                        self.vector_db.location_collection.update(
                            ids=[existing.location_id], metadatas=[existing.to_vector_metadata()]
                        )
                return True
        if pgo_metadata is not None:
            location = RobotLocation.from_vector_metadata(
                {
                    **pgo_metadata,
                    "location_name": name,
                    "description": description,
                    "kind": "object",
                    "timestamp": pgo_metadata["pgo_timestamp"],
                }
            )
            self._attach_tag_image(location, reference_image)
            return self.add_robot_location(location)
        return self.add_named_location(
            name,
            position,
            rotation,
            description,
            kind="object",
            reference_image=reference_image,
        )

    @rpc
    def add_named_location(
        self,
        name: str,
        position: list[float] | None = None,
        rotation: list[float] | None = None,
        description: str | None = None,
        kind: str = "location",
        observation: dict[str, Any] | None = None,
        reference_image: np.ndarray | None = None,
    ) -> bool:
        """
        Add a named robot location to spatial memory using current or specified position.

        Args:
            name: Name of the location
            position: Optional position [x, y, z], uses current position if None
            rotation: Optional rotation [roll, pitch, yaw], uses current rotation if None
            description: Optional description of the location
            kind: Tag source category, such as object or location

        Returns:
            True if successfully added, False otherwise
        """
        if position is None or rotation is None:
            buffer = self._pgo_raw_buffer if self._pgo_session is not None else self.tfbuffer
            tf = buffer.get("world", "base_link")
            if tf is None:
                logger.error("No position available for robot location")
                return False
            with self._tagging_lock:
                if self._pgo_graph is not None and self._pgo_graph.keyframes:
                    tf = self._pgo_graph.world_correction(tf.ts) + tf
                if observation is None and self._pgo_session is not None:
                    observation = {"timestamp": tf.ts, "pgo_graph": self._pgo_graph}
            if position is None:
                position = [
                    float(tf.translation.x),
                    float(tf.translation.y),
                    float(tf.translation.z),
                ]
            if rotation is None:
                euler = tf.rotation.to_euler()
                rotation = [float(euler.x), float(euler.y), float(euler.z)]
        location = RobotLocation(
            name=name,
            position=(float(position[0]), float(position[1]), float(position[2])),
            rotation=(float(rotation[0]), float(rotation[1]), float(rotation[2])),
            timestamp=time.time(),
            metadata={"description": description or f"Location: {name}", "kind": kind},
        )
        if observation is not None and self._pgo_session is not None:
            with self._tagging_lock:
                metadata = self._pgo_metadata(
                    location.to_vector_metadata(),
                    observation["timestamp"],
                    observation.get("pgo_graph"),
                )
                location = RobotLocation.from_vector_metadata(metadata)

        if reference_image is not None:
            self._attach_tag_image(location, reference_image)
        return self.add_robot_location(location)

    @rpc
    def get_robot_locations(self) -> list[RobotLocation]:
        """
        Get all stored robot locations.

        Returns:
            List of RobotLocation objects
        """
        with self._tagging_lock:
            self._ensure_pgo_ready()
            return self.vector_db.get_robot_locations()

    @rpc
    def find_robot_location(self, name: str) -> RobotLocation | None:
        """
        Find a robot location by name.

        Args:
            name: Name of the location to find

        Returns:
            RobotLocation object if found, None otherwise
        """
        # Simple search through our list of locations
        for location in self.get_robot_locations():
            if location.name.lower() == name.lower():
                return location

        return None

    @rpc
    def get_stats(self) -> dict[str, int]:
        """Get statistics about the spatial memory module.

        Returns:
            Dictionary containing:
                - frame_count: Total number of frames processed
                - stored_frame_count: Number of frames actually stored
        """
        return {"frame_count": self.frame_count, "stored_frame_count": self.stored_frame_count}

    @rpc
    def tag_location(self, robot_location: RobotLocation) -> bool:
        try:
            with self._tagging_lock:
                self._ensure_pgo_ready()
                if robot_location.frame_id is None:
                    self._attach_tag_image(robot_location, self._latest_video_frame_bgr)
                if self._pgo_session is not None and "pgo_raw_pose" not in robot_location.metadata:
                    metadata = self._pgo_metadata(
                        robot_location.to_vector_metadata(),
                        robot_location.timestamp,
                        self._pgo_graph,
                    )
                    robot_location = RobotLocation.from_vector_metadata(metadata)
                elif (
                    self._pgo_session is not None and self._pgo_graph and self._pgo_graph.keyframes
                ):
                    metadata = robot_location.to_vector_metadata()
                    raw = Transform.from_matrix(
                        np.asarray(json.loads(metadata["pgo_raw_pose"])),
                        frame_id="world",
                        child_frame_id="base_link",
                    )
                    corrected = self._pgo_graph.world_correction(metadata["pgo_timestamp"]) + raw
                    angles = corrected.rotation.to_euler()
                    robot_location.position = (
                        float(corrected.translation.x),
                        float(corrected.translation.y),
                        float(corrected.translation.z),
                    )
                    robot_location.rotation = (float(angles.x), float(angles.y), float(angles.z))
                self.vector_db.tag_location(robot_location)
        except Exception:
            logger.exception("Failed to persist location tag", name=robot_location.name)
            return False
        return True

    @staticmethod
    def _tag_crop(frame: np.ndarray, bbox: list[int]) -> np.ndarray:
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = (int(value) for value in bbox)
        crop = frame[max(0, y1) : min(height, y2), max(0, x1) : min(width, x2)]
        if crop.size == 0:
            raise ValueError("Cannot save an empty tag image")
        return crop.copy()

    def _attach_tag_image(self, location: RobotLocation, frame: np.ndarray | None) -> None:
        if frame is None:
            logger.warning("Tag has no reference image", location_id=location.location_id)
            return
        with self._tagging_lock:
            assert self._visual_memory is not None
            image_id = f"tag_{location.location_id}"
            self._visual_memory.add(image_id, frame)
            if self._visual_memory.get(image_id) is None:
                raise RuntimeError("Failed to store tag reference image")
            location.frame_id = image_id
            if self.visual_memory_path is not None:
                self._visual_memory.save(self.visual_memory_path)

    @rpc
    def verify_tag_view(self, location_id: str) -> dict[str, Any]:
        """Compare this tag's saved view against a fresh camera frame, locally."""
        with self._tagging_lock:
            self._ensure_pgo_ready()
            location = next(
                (item for item in self.get_robot_locations() if item.location_id == location_id),
                None,
            )
            if location is None:
                raise ValueError(f"Tag {location_id!r} no longer exists")
            if location.frame_id is None or self._visual_memory is None:
                raise ValueError("Tag has no reference image; re-tag it before visual search")
            reference = self._visual_memory.get(location.frame_id)
            if reference is None:
                raise ValueError("Tag reference image is missing; re-tag it before visual search")
            observation = self._latest_observation
            if observation is None or not 0 <= time.time() - observation[2] <= 3:
                raise RuntimeError("No fresh camera frame for visual search")
            frame, _, timestamp = observation
            result = match_tag_view(reference, frame.copy())
            return {**result, "image_ts": timestamp, "location_id": location_id}

    @rpc
    def query_tagged_location(self, query: str) -> RobotLocation | None:
        with self._tagging_lock:
            self._ensure_pgo_ready()
            location, semantic_distance = self.vector_db.query_tagged_location(query)
        if semantic_distance < 0.3:
            return location
        return None
