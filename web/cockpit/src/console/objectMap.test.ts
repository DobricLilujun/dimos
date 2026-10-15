// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TaggedVoxelScene } from "../panels/voxelView.ts";
import { mountObjectMap, parseSnapshot, type ObjectMapController } from "./objectMap.ts";

const tag = {
  id: "saved-stable-id", name: "<img src=x onerror=alert(1)>", kind: "object",
  description: "A chair", position: [12, -4, 2],
  metadata: { observations: 4 },
};
const snapshot = (overrides = {}) => ({
  ok: true, frame: "world", points: [[1, 2, 3], [4, 5, 6]],
  pose: { x: 1, y: 2, z: 3, yaw: 0.5 },
  tags: [tag], map_version: 1, scene_id: "scene-a", stale: false,
  ...overrides,
});

const flush = async () => { await vi.advanceTimersByTimeAsync(0); };

describe("object map", () => {
  let root: HTMLDivElement;
  let controller: ObjectMapController | undefined;
  let scenes: TaggedVoxelScene[];
  let createScene: ReturnType<typeof vi.fn>;
  let navigate: ReturnType<typeof vi.fn>;
  let fetchMap: ReturnType<typeof vi.fn>;
  let navigationReady: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      disconnect() {}
    });
    root = document.createElement("div");
    document.body.append(root);
    scenes = [];
    createScene = vi.fn(() => {
      const scene: TaggedVoxelScene = {
        setVoxels: vi.fn(), setPose: vi.fn(), setTags: vi.fn(),
        pulseTags: vi.fn(),
        pickTags: vi.fn(() => [tag as unknown as Parameters<TaggedVoxelScene["setTags"]>[0][number]]),
        fit: vi.fn(), setFollow: vi.fn(), resize: vi.fn(), render: vi.fn(), dispose: vi.fn(),
      };
      scenes.push(scene);
      return scene;
    });
    fetchMap = vi.fn().mockResolvedValue(snapshot());
    navigationReady = vi.fn(() => true);
    navigate = vi.fn();
  });

  afterEach(() => {
    controller?.dispose();
    root.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  const start = () => {
    controller = mountObjectMap(root, { createScene, fetchMap, navigate, navigationReady });
    controller.setVisible(true);
  };

  it("draws world XYZ, robot pose and all stored objects without a memory query", async () => {
    start();
    await flush();
    expect(scenes[0].setVoxels).toHaveBeenCalledWith(
      new Float32Array([1, 2, 3, 4, 5, 6]), expect.any(Uint8Array), 0.08,
    );
    expect(scenes[0].setPose).toHaveBeenCalledWith(snapshot().pose);
    expect(scenes[0].setTags).toHaveBeenCalledWith([tag]);
    expect(root.textContent).toContain("1 stored objects");
    expect(fetchMap).toHaveBeenCalledOnce();
  });

  it("renders untrusted properties as text, preserves hover/focus without navigation", async () => {
    navigationReady.mockReturnValue(false);
    start();
    await flush();
    const button = root.querySelector(".object-map-list button") as HTMLButtonElement;
    button.focus();
    const tip = root.querySelector('[role="tooltip"]') as HTMLElement;
    expect(tip.hidden).toBe(false);
    expect(tip.textContent).toContain(tag.name);
    expect(tip.textContent).toContain("12.00, -4.00, 2.00");
    expect(tip.textContent).toContain("saved-stable-id");
    expect(tip.textContent).toContain("observations: 4");
    expect(tip.textContent).toContain("Navigation disabled");
    expect(root.querySelector("img")).toBeNull();
    expect(button.disabled).toBe(false);
    expect(button.getAttribute("aria-disabled")).toBe("true");
    button.click();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("passes the stable ID into the existing confirmed navigation callback", async () => {
    start();
    await flush();
    const button = root.querySelector(".object-map-list button") as HTMLButtonElement;
    button.click();
    expect(navigate).toHaveBeenCalledWith("saved-stable-id", button);
  });

  it("polls at two seconds only while visible and disposes the scene on hide", async () => {
    start();
    await flush();
    await vi.advanceTimersByTimeAsync(1999);
    expect(fetchMap).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchMap).toHaveBeenCalledTimes(2);
    controller!.setVisible(false);
    expect(scenes[0].dispose).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMap).toHaveBeenCalledTimes(2);
    controller!.setVisible(true);
    await flush();
    expect(createScene).toHaveBeenCalledTimes(2);
    expect(fetchMap).toHaveBeenCalledTimes(3);
  });

  it("pulses saved markers only while visible and stops when there are no tags", async () => {
    start();
    await flush();
    expect(scenes[0].pulseTags).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(300);
    expect(scenes[0].pulseTags).toHaveBeenCalledTimes(4);
    controller!.setVisible(false);
    await vi.advanceTimersByTimeAsync(300);
    expect(scenes[0].pulseTags).toHaveBeenCalledTimes(4);
    fetchMap.mockResolvedValue(snapshot({ tags: [] }));
    controller!.setVisible(true);
    await flush();
    await vi.advanceTimersByTimeAsync(300);
    expect(scenes[1].pulseTags).not.toHaveBeenCalled();
  });

  it("keeps markers static when reduced motion is requested", async () => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    Object.defineProperty(reducedMotion, "matches", { value: true });
    vi.spyOn(window, "matchMedia").mockReturnValue(reducedMotion);
    start();
    await flush();
    await vi.advanceTimersByTimeAsync(300);
    expect(scenes[0].pulseTags).not.toHaveBeenCalled();
  });

  it("retains a visibly stale map on disconnect but disables movement, then reconciles", async () => {
    start();
    await flush();
    fetchMap.mockRejectedValueOnce(new Error("offline"));
    await vi.advanceTimersByTimeAsync(2000);
    expect(root.textContent).toContain("Stale / disconnected — offline");
    (root.querySelector(".object-map-list button") as HTMLButtonElement).click();
    expect(navigate).not.toHaveBeenCalled();
    fetchMap.mockResolvedValue(snapshot({ tags: [], points: [], map_version: 2 }));
    await vi.advanceTimersByTimeAsync(2000);
    expect(scenes[0].setTags).toHaveBeenLastCalledWith([]);
    expect(root.textContent).toContain("No stored objects");
    expect(root.textContent).toContain("No map points yet");
    expect(root.querySelectorAll(".object-map-list button")).toHaveLength(0);
  });

  it("shows a map even when there are no saved tags and refits after a scene change", async () => {
    fetchMap.mockResolvedValue(snapshot({ tags: [] }));
    start();
    await flush();
    expect(scenes[0].setVoxels).toHaveBeenCalledOnce();
    expect(root.textContent).toContain("No stored objects");
    expect(scenes[0].fit).toHaveBeenCalledTimes(1);
    fetchMap.mockResolvedValue(snapshot({ scene_id: "scene-b", map_version: 1 }));
    await vi.advanceTimersByTimeAsync(2000);
    expect(scenes[0].setVoxels).toHaveBeenCalledTimes(2);
    expect(scenes[0].fit).toHaveBeenCalledTimes(2);
  });

  it("reconciles the map and objects on reconnect without resetting the orbit view", async () => {
    start();
    await flush();
    controller!.refresh();
    await flush();
    expect(scenes[0].dispose).not.toHaveBeenCalled();
    expect(scenes[0].setVoxels).toHaveBeenCalledTimes(2);
    expect(scenes[0].setTags).toHaveBeenCalledWith([tag]);
    expect(fetchMap).toHaveBeenCalledTimes(2);
  });

  it("distinguishes orbit dragging from tag clicks and opens a chooser for overlaps", async () => {
    start();
    await flush();
    const canvas = root.querySelector("canvas")!;
    canvas.dispatchEvent(new PointerEvent("pointerdown", { clientX: 10, clientY: 10 }));
    canvas.dispatchEvent(new PointerEvent("pointermove", { clientX: 40, clientY: 10 }));
    canvas.dispatchEvent(new PointerEvent("pointerup", { clientX: 40, clientY: 10 }));
    expect(navigate).not.toHaveBeenCalled();
    canvas.dispatchEvent(new PointerEvent("pointerdown", { clientX: 10, clientY: 10 }));
    canvas.dispatchEvent(new PointerEvent("pointerup", { clientX: 10, clientY: 10 }));
    expect(navigate).toHaveBeenCalledWith("saved-stable-id", expect.any(HTMLButtonElement));
    navigate.mockClear();
    vi.mocked(scenes[0].pickTags).mockReturnValue([parseSnapshot(snapshot()).tags[0], parseSnapshot(snapshot()).tags[0]]);
    canvas.dispatchEvent(new PointerEvent("pointerdown", { clientX: 10, clientY: 10 }));
    canvas.dispatchEvent(new PointerEvent("pointerup", { clientX: 10, clientY: 10 }));
    expect(navigate).not.toHaveBeenCalled();
    expect(root.querySelector("details")!.open).toBe(true);
  });

  it("reports WebGL startup failure without starting polls", async () => {
    createScene.mockImplementation(() => { throw new Error("WebGL disabled"); });
    start();
    await flush();
    expect(root.textContent).toContain("3D unavailable: WebGL disabled");
    expect(fetchMap).not.toHaveBeenCalled();
  });

  it("aborts pending requests when hidden and ignores late results", async () => {
    let resolve: (value: unknown) => void = () => {};
    fetchMap.mockImplementation(() => new Promise((done) => { resolve = done; }));
    start();
    const signal = fetchMap.mock.calls[0][0] as AbortSignal;
    controller!.setVisible(false);
    expect(signal.aborted).toBe(true);
    resolve(snapshot());
    await flush();
    expect(scenes[0].setTags).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMap).toHaveBeenCalledOnce();
  });

  it("rejects other frames and discards malformed coordinates and duplicate IDs", () => {
    expect(() => parseSnapshot({ ok: false, error: "Memory offline" })).toThrow("Memory offline");
    expect(() => parseSnapshot(snapshot({ frame: "robot" }))).toThrow("World map unavailable");
    const parsed = parseSnapshot(snapshot({
      points: [[0, 1, 2], [0, 1, Infinity], ["1", 2, 3]],
      tags: [tag, tag, { ...tag, id: "bad", position: [1, 2, NaN] }],
      pose: { x: 1, y: 2, z: NaN, yaw: 0 },
    }));
    expect(Array.from(parsed.points)).toEqual([0, 1, 2]);
    expect(parsed.tags).toEqual([tag]);
    expect(parsed.pose).toBeNull();
  });
});
