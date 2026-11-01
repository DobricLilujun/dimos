import { createVoxelScene } from "../panels/voxelScene.ts";
import {
  heightColors,
  type ObjectMapTag,
  type Pose3d,
  type TaggedVoxelScene,
} from "../panels/voxelView.ts";

interface Snapshot {
  points: Float32Array;
  pose: Pose3d | null;
  tags: ObjectMapTag[];
  version: string | number;
  scene: string;
  stale: boolean;
}

const xyz = (value: unknown): value is [number, number, number] =>
  Array.isArray(value) && value.length === 3 &&
  value.every((v) => typeof v === "number" && Number.isFinite(v));

/** Reject frame mismatches rather than draw tags in an unrelated coordinate system. */
export function parseSnapshot(value: unknown): Snapshot {
  if (typeof value !== "object" || value === null) throw new Error("Invalid map response");
  const data = value as Record<string, unknown>;
  if (data.ok === false) {
    throw new Error(typeof data.error === "string" ? data.error : "World map unavailable");
  }
  if (data.ok !== true || data.frame !== "world" || !Array.isArray(data.points) ||
    !Array.isArray(data.tags) || !["string", "number"].includes(typeof data.map_version)) {
    throw new Error("World map unavailable");
  }
  const points = new Float32Array(data.points.filter(xyz).slice(0, 50000).flat());
  const seen = new Set<string>();
  const tags: ObjectMapTag[] = [];
  for (const item of data.tags) {
    if (typeof item !== "object" || item === null) continue;
    const tag = item as Record<string, unknown>;
    if (typeof tag.id !== "string" || !tag.id || seen.has(tag.id) || !xyz(tag.position)) continue;
    seen.add(tag.id);
    tags.push({
      id: tag.id, position: tag.position,
      name: typeof tag.name === "string" ? tag.name : "Object",
      kind: typeof tag.kind === "string" ? tag.kind : "object",
      description: typeof tag.description === "string" ? tag.description : "",
      metadata: typeof tag.metadata === "object" && tag.metadata !== null
        ? tag.metadata as Record<string, unknown> : undefined,
    });
  }
  const pose = data.pose as Pose3d | null;
  return {
    points, tags,
    pose: pose && [pose.x, pose.y, pose.z, pose.yaw].every((v) =>
      typeof v === "number" && Number.isFinite(v)) ? pose : null,
    version: data.map_version as string | number,
    scene: String(data.scene_id ?? ""),
    stale: data.stale !== false,
  };
}

export interface ObjectMapOptions {
  navigate: (id: string, button: HTMLButtonElement) => void;
  navigationReady: () => boolean;
  createScene?: (canvas: HTMLCanvasElement) => TaggedVoxelScene;
  fetchMap?: (signal: AbortSignal) => Promise<unknown>;
}

export interface ObjectMapController {
  setVisible(visible: boolean): void;
  refresh(): void;
  dispose(): void;
}

/** Console-only adapter: same renderer as Cockpit, no relay or React required. */
export function mountObjectMap(root: HTMLElement, options: ObjectMapOptions): ObjectMapController {
  const doc = root.ownerDocument;
  const canvas = doc.createElement("canvas");
  canvas.className = "object-map-canvas";
  canvas.setAttribute("aria-label", "World map. Drag to orbit, scroll to zoom; use the object list to inspect or navigate.");
  const toolbar = doc.createElement("div");
  toolbar.className = "object-map-toolbar";
  const fit = doc.createElement("button");
  fit.textContent = "Fit world map";
  fit.type = "button";
  const status = doc.createElement("span");
  status.setAttribute("role", "status");
  toolbar.append(fit, status);
  const tooltip = doc.createElement("div");
  tooltip.className = "object-map-tooltip";
  tooltip.id = "object-map-properties";
  tooltip.setAttribute("role", "tooltip");
  tooltip.hidden = true;
  const list = doc.createElement("details");
  list.className = "object-map-list";
  const summary = doc.createElement("summary");
  const entries = doc.createElement("div");
  list.append(summary, entries);
  root.replaceChildren(canvas, toolbar, tooltip, list);
  let scene: TaggedVoxelScene | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let pulseTimer: ReturnType<typeof setTimeout> | undefined;
  let request: AbortController | null = null;
  let visible = false;
  let disposed = false;
  let stale = true;
  let sceneId: string | undefined;
  let version: string | number | undefined;
  let tagSignature = "";
  let generation = 0;
  let pointerStart: [number, number] | null = null;
  let pointerDragged = false;
  let fitted = false;

  const canNavigate = (): boolean => !stale && options.navigationReady();
  const details = (tag: ObjectMapTag): string => [
    tag.name, `Category: ${tag.kind}`, tag.description,
    `World XYZ: ${tag.position.map((v) => v.toFixed(2)).join(", ")} m`,
    `ID: ${tag.id}`,
    ...(tag.metadata ? Object.entries(tag.metadata).map(([key, value]) =>
      `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`) : []),
    canNavigate() ? "Click to confirm navigation nearby." :
      stale ? "Navigation disabled: map is stale." : "Navigation disabled: alignment / robot readiness required.",
  ].filter(Boolean).join("\n");
  const show = (tag: ObjectMapTag, x?: number, y?: number): void => {
    tooltip.textContent = details(tag);
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(8, Math.min(x ?? 12, root.clientWidth - 290))}px`;
    tooltip.style.top = `${Math.max(46, Math.min(y ?? 50, root.clientHeight - 210))}px`;
  };
  const select = (tag: ObjectMapTag, button: HTMLButtonElement): void => {
    if (canNavigate()) options.navigate(tag.id, button);
  };
  const resize = (): void => {
    if (!visible || !scene || !root.clientWidth || !root.clientHeight) return;
    scene.resize(root.clientWidth, root.clientHeight, Math.min(doc.defaultView?.devicePixelRatio ?? 1, 2));
    scene.render();
  };
  const observer = new ResizeObserver(resize);
  observer.observe(root);
  const stop = (): void => {
    generation++;
    clearTimeout(timer);
    clearTimeout(pulseTimer);
    pulseTimer = undefined;
    timer = undefined;
    request?.abort();
    request = null;
    scene?.dispose();
    scene = null;
    sceneId = undefined;
    version = undefined;
    tagSignature = "";
    fitted = false;
    stale = true;
    pointerStart = null;
    tooltip.hidden = true;
  };
  const pulse = (): void => {
    if (!visible || disposed || !scene) return;
    scene.pulseTags(performance.now());
    scene.render();
    pulseTimer = setTimeout(pulse, 100);
  };
  const fetchMap = options.fetchMap ?? (async (signal: AbortSignal): Promise<unknown> => {
    const response = await fetch("/api/object-map", { signal, cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  });
  const poll = async (): Promise<void> => {
    if (!visible || disposed || request || !scene) return;
    const current = generation;
    const controller = new AbortController();
    request = controller;
    // Bound hung requests without adding an interval or overlapping polls.
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const snapshot = parseSnapshot(await fetchMap(controller.signal));
      if (current !== generation || !visible || !scene) return;
      if (sceneId !== snapshot.scene) {
        scene.setTags([]);
        version = undefined;
        fitted = false;
        tagSignature = "";
        tooltip.hidden = true;
      }
      sceneId = snapshot.scene;
      if (version !== snapshot.version) {
        scene.setVoxels(snapshot.points, heightColors(snapshot.points), 0.08);
        version = snapshot.version;
      }
      scene.setPose(snapshot.pose);
      stale = snapshot.stale;
      const signature = JSON.stringify(snapshot.tags);
      if (signature !== tagSignature) {
        scene.setTags(snapshot.tags);
        tooltip.hidden = true;
        entries.replaceChildren();
        for (const tag of snapshot.tags) {
          const button = doc.createElement("button");
          button.type = "button";
          button.textContent = `${tag.name} · ${tag.id}`;
          button.setAttribute("aria-describedby", tooltip.id);
          // aria-disabled preserves focus/hover when physical movement is unavailable.
          button.setAttribute("aria-disabled", String(!canNavigate()));
          button.onfocus = () => show(tag);
          button.onblur = () => { tooltip.hidden = true; };
          button.onpointerenter = () => show(tag);
          button.onpointerleave = () => { tooltip.hidden = true; };
          button.onclick = () => select(tag, button);
          entries.append(button);
        }
        tagSignature = signature;
      }
      for (const button of entries.querySelectorAll("button")) {
        button.setAttribute("aria-disabled", String(!canNavigate()));
      }
      summary.textContent = `Objects (${snapshot.tags.length}) · inspect / navigate`;
      if (!snapshot.tags.length) {
        clearTimeout(pulseTimer);
        pulseTimer = undefined;
      } else if (pulseTimer === undefined &&
        !doc.defaultView?.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        pulse();
      }
      status.textContent = [
        stale ? "Stale map — navigation unavailable" : "World map · live",
        !snapshot.points.length ? "No map points yet" : `${snapshot.points.length / 3} points`,
        !snapshot.tags.length ? "No stored objects" : `${snapshot.tags.length} stored objects`,
        !snapshot.pose ? "Robot pose unavailable" : "",
      ].filter(Boolean).join(" · ");
      status.dataset.stale = String(stale);
      if (!fitted && (snapshot.points.length || snapshot.tags.length)) {
        resize();
        scene.fit();
        fitted = true;
      }
      scene.render();
    } catch (error) {
      if (current !== generation || !visible) return;
      stale = true;
      tooltip.hidden = true;
      status.textContent = `Stale / disconnected — ${error instanceof Error ? error.message : "map unavailable"}. Retrying…`;
      status.dataset.stale = "true";
      for (const button of entries.querySelectorAll("button")) button.setAttribute("aria-disabled", "true");
    } finally {
      clearTimeout(timeout);
      if (current === generation) {
        request = null;
        if (visible && !disposed) timer = setTimeout(() => void poll(), 2000);
      }
    }
  };
  fit.onclick = () => { scene?.fit(); scene?.render(); };
  canvas.onpointerdown = (event) => {
    pointerStart = [event.clientX, event.clientY];
    pointerDragged = false;
    tooltip.hidden = true;
  };
  canvas.onpointermove = (event) => {
    if (pointerStart && Math.hypot(event.clientX - pointerStart[0], event.clientY - pointerStart[1]) > 5) pointerDragged = true;
    if (pointerStart) return;
    const hits = scene?.pickTags(event.clientX, event.clientY) ?? [];
    const rect = root.getBoundingClientRect();
    if (hits.length) {
      show(hits[0], event.clientX - rect.left + 12, event.clientY - rect.top + 12);
      if (hits.length > 1) tooltip.textContent += `\n${hits.length} overlapping objects — use Objects list to choose.`;
    } else tooltip.hidden = true;
    canvas.style.cursor = hits.length ? "pointer" : "grab";
  };
  canvas.onpointerup = (event) => {
    const start = pointerStart;
    pointerStart = null;
    if (!start || pointerDragged || event.button !== 0 ||
      Math.hypot(event.clientX - start[0], event.clientY - start[1]) > 5) return;
    const hits = scene?.pickTags(event.clientX, event.clientY) ?? [];
    if (hits.length > 1) {
      list.open = true;
      entries.querySelector("button")?.focus();
    } else if (hits.length) {
      // Reuse the existing dialog/confirmation flow, never navigate to arbitrary XYZ.
      const button = Array.from(entries.querySelectorAll("button")).find((b) => b.textContent === `${hits[0].name} · ${hits[0].id}`);
      if (button) select(hits[0], button);
    }
  };
  canvas.onpointerleave = canvas.onpointercancel = () => {
    pointerStart = null;
    tooltip.hidden = true;
  };
  return {
    setVisible(on) {
      if (disposed || visible === on) return;
      visible = on;
      root.hidden = !on;
      if (!on) { stop(); return; }
      try {
        scene = (options.createScene ?? createVoxelScene)(canvas);
        status.textContent = "Loading world map and stored objects…";
        resize();
        void poll();
      } catch (error) {
        status.textContent = `3D unavailable: ${error instanceof Error ? error.message : "WebGL could not start"}. Rerun is still available.`;
        status.dataset.stale = "true";
      }
    },
    refresh() {
      if (!visible || disposed || request) return;
      clearTimeout(timer);
      version = undefined;
      void poll();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      visible = false;
      stop();
      observer.disconnect();
      root.replaceChildren();
    },
  };
}
