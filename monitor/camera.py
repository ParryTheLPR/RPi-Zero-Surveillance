"""CSI OV5647 摄像头封装（libcamera / picamera2）。

设计要点（面向 Zero W 性能）：
- 主通道输出 RGB888 用于抓快照；低分辨率 lores 通道专供移动检测。
- 录像走硬件 H.264 + ffmpeg mux 成 mp4，不在 CPU 上逐帧叠加标记。
- 翻转在 ISP 变换层完成，不额外消耗 CPU。
"""
from __future__ import annotations

import threading
from pathlib import Path

import cv2

from .overlay import draw_timestamp

try:  # picamera2 只在树莓派上存在，Windows 开发环境导入失败不影响语法检查
    from picamera2 import Picamera2
    from picamera2.encoders import FfmpegEncoder
    from libcamera import Transform
except ImportError:  # pragma: no cover - 非树莓派环境
    Picamera2 = None  # type: ignore
    FfmpegEncoder = None  # type: ignore
    Transform = None  # type: ignore


class Camera:
    def __init__(self, cfg):
        c = cfg.section("camera")
        self.main_size = (int(c["main_width"]), int(c["main_height"]))
        self.lores_size = (int(c["lores_width"]), int(c["lores_height"]))
        self.fps = int(c["fps"])
        self.bitrate = int(c["bitrate"])
        self.vflip = bool(c["vflip"])
        self.hflip = bool(c["hflip"])
        self.burn_timestamp = bool(c.get("burn_timestamp", True))
        self.timestamp_format = c.get("timestamp_format", "%Y-%m-%d %H:%M:%S")
        self._picam = None
        self._encoder = None
        self._recording_path: Path | None = None
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if Picamera2 is None:
            raise RuntimeError("picamera2 不可用：请在树莓派上安装 python3-picamera2")
        self._picam = Picamera2()
        controls = {"FrameRate": self.fps}
        kwargs = {
            "main": {"size": self.main_size, "format": "RGB888"},
            "lores": {"size": self.lores_size, "format": "RGB888"},
            "controls": controls,
        }
        if Transform is not None:
            kwargs["transform"] = Transform(hflip=self.hflip, vflip=self.vflip)
        self._picam.configure(self._picam.create_video_configuration(**kwargs))
        # 烧录时间戳：在每帧送编码器前原地叠加（轻量文字绘制，不改变编码路径）
        if self.burn_timestamp:
            fmt = self.timestamp_format
            self._picam.pre_callback = lambda array: draw_timestamp(array, fmt=fmt)
        self._picam.start()

    def stop(self) -> None:
        self.stop_recording()
        if self._picam is not None:
            try:
                self._picam.stop()
                self._picam.close()
            finally:
                self._picam = None

    # ---------- 取帧 ----------
    def lores_gray(self):
        """返回低分辨率灰度图（numpy uint8），供移动检测。"""
        arr = self._picam.capture_array("lores")
        return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    def main_bgr(self):
        """返回主通道 BGR 图（numpy），供快照绘制。"""
        arr = self._picam.capture_array("main")
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

    def stream_bgr(self, width: int | None = None):
        """供 MJPEG 预览：直接取 lores 通道并按需缩放，避免解码主通道浪费 CPU。"""
        arr = self._picam.capture_array("lores")
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        if width:
            h, w = bgr.shape[:2]
            new_h = max(1, int(h * width / w))
            bgr = cv2.resize(bgr, (int(width), new_h), interpolation=cv2.INTER_LINEAR)
        return bgr

    @property
    def scale(self) -> tuple[float, float]:
        """lores -> main 的缩放比，用于把检测框换算回主画面。"""
        sx = self.main_size[0] / self.lores_size[0]
        sy = self.main_size[1] / self.lores_size[1]
        return sx, sy

    # ---------- 录像 ----------
    @property
    def recording_path(self) -> Path | None:
        return self._recording_path

    @property
    def is_recording(self) -> bool:
        return self._recording_path is not None

    def start_recording(self, path: str | Path) -> bool:
        with self._lock:
            if self._recording_path is not None:
                return False
            self._encoder = FfmpegEncoder(container="mp4", bitrate=self.bitrate, audio=False)
            self._picam.start_recording(self._encoder, str(path))
            self._recording_path = Path(path)
            return True

    def stop_recording(self) -> Path | None:
        with self._lock:
            if self._recording_path is None:
                return None
            self._picam.stop_recording()
            self._encoder = None
            path = self._recording_path
            self._recording_path = None
            return path
