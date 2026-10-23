"""分段录制状态机：支持移动触发(motion)与全天候连续(continuous)两种模式。

- motion：移动上升沿开录，静止 post_motion_seconds 后收尾；同时保存带框快照。
- continuous：持续录像，每 segment_seconds 切一个新分段。
- 收尾的视频/快照交给 SMBUploader 队列；本地按容量上限清理最旧文件。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

import cv2

log = logging.getLogger(__name__)


class Recorder:
    SNAPSHOT_INTERVAL = 5.0  # 持续移动期间重复抓快照的间隔（秒）

    def __init__(self, cfg, camera, motion, uploader):
        r = cfg.section("recorder")
        self.mode = r["mode"]
        self.segment_seconds = float(r["segment_seconds"])
        self.post_motion_seconds = float(r["post_motion_seconds"])
        self.local_dir = cfg.path(r["local_dir"])
        self.snapshot_dir = cfg.path(r["snapshot_dir"])
        self.max_local_bytes = float(r["max_local_gb"]) * 1024 ** 3 if r["max_local_gb"] else 0

        self.camera = camera
        self.motion = motion
        self.uploader = uploader
        self.snapshot_on_motion = bool(cfg.section("motion").get("snapshot_on_motion", True))

        self._active_path: Path | None = None
        self._seg_started = 0.0
        self._last_motion = 0.0
        self._was_active = False
        self._last_snap = 0.0

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self.mode == "continuous":
            self._open_segment()

    def stop(self) -> None:
        self._close_segment()

    # ---------- 主循环每帧调用 ----------
    def update(self, now: float, motion_active: bool, boxes: list[tuple[int, int, int, int]]) -> None:
        rising = motion_active and not self._was_active
        if self.mode == "continuous":
            self._continuous_tick(now)
        else:
            self._motion_tick(now, motion_active, boxes)
        # 两种模式下，移动时都保存带框快照
        if motion_active:
            self._maybe_snapshot(now, boxes, rising)
        self._was_active = motion_active

    # ---------- motion 模式 ----------
    def _motion_tick(self, now: float, active: bool, boxes) -> None:
        if active:
            self._last_motion = now
            if self._active_path is None:
                self._open_segment()
        else:
            if self._active_path is not None and now - self._last_motion >= self.post_motion_seconds:
                self._close_segment()

    # ---------- continuous 模式 ----------
    def _continuous_tick(self, now: float) -> None:
        if self._active_path is None:
            self._open_segment()
        elif now - self._seg_started >= self.segment_seconds:
            self._close_segment()
            self._open_segment()

    # ---------- 分段开关 ----------
    def _unique_path(self, directory: Path, stem: str, suffix: str) -> Path:
        candidate = directory / f"{stem}{suffix}"
        n = 1
        while candidate.exists():
            candidate = directory / f"{stem}_{n}{suffix}"
            n += 1
        return candidate

    def _open_segment(self) -> None:
        # 录像命名：Video + 开始时间 YYYYMMDD-HHMMSS.mp4
        start_dt = datetime.now()
        stem = f"Video{start_dt:%Y%m%d-%H%M%S}"
        path = self._unique_path(self.local_dir, stem, ".mp4")
        if self.camera.start_recording(path):
            self._active_path = path
            self._seg_started = time.time()
            log.info("开始录像: %s", path.name)

    def _close_segment(self) -> None:
        closed = self.camera.stop_recording()
        if closed is None:
            return
        self._active_path = None
        log.info("结束录像: %s", closed.name)
        self.uploader.enqueue(closed, "video")
        self._enforce_retention()

    # ---------- 快照 ----------
    def _maybe_snapshot(self, now: float, boxes, rising: bool) -> None:
        if not self.motion.enabled or not self.snapshot_on_motion:
            return
        if not rising and now - self._last_snap < self.SNAPSHOT_INTERVAL:
            return
        self._last_snap = now
        try:
            bgr = self.camera.main_bgr()
        except Exception as e:
            log.warning("抓快照失败: %s", e)
            return
        label = "MOTION"
        annotated = self.motion.annotate(bgr, boxes, label)
        # 快照命名：YYYYMMDD-HHMMSS_毫秒（同一秒内多张也天然唯一，无需序号后缀）
        now_dt = datetime.now()
        ms = now_dt.microsecond // 1000
        stem = now_dt.strftime("%Y%m%d-%H%M%S") + f"_{ms:03d}"
        snap = self._unique_path(self.snapshot_dir, f"{stem}_snap", ".jpg")
        ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            snap.write_bytes(buf.tobytes())
            log.info("保存快照: %s", snap.name)
            self.uploader.enqueue(snap, "snapshot")
        else:
            log.warning("快照编码失败: %s", snap.name)

    # ---------- 本地容量清理 ----------
    def _enforce_retention(self) -> None:
        if not self.max_local_bytes:
            return
        files: list[Path] = []
        for d in (self.local_dir, self.snapshot_dir):
            if d.exists():
                files.extend(p for p in d.rglob("*") if p.is_file())
        active = self._active_path
        total = sum(p.stat().st_size for p in files)
        # 按修改时间从旧到新删除，跳过正在录制的文件
        for p in sorted(files, key=lambda x: x.stat().st_mtime):
            if total <= self.max_local_bytes:
                break
            if active and p.resolve() == active.resolve():
                continue
            size = p.stat().st_size
            try:
                p.unlink()
                total -= size
                log.info("本地容量清理，删除旧文件: %s", p.name)
            except Exception as e:
                log.warning("删除旧文件失败 %s: %s", p.name, e)
