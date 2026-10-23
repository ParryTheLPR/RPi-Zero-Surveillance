"""本机 dev 模拟摄像头：生成一个来回移动的色块，替代 CSI 摄像头。

仅用于没有树莓派时在 PC 上点验网页面板、移动检测与“录制效果”。
与真实硬件不同：这里用 OpenCV VideoWriter 写一个 mp4v 的 mp4 供回放查看，
不走树莓派硬件 H.264；SMB 上传、电池监测仍在 dev 下关闭。
"""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from .overlay import draw_timestamp


class DevCamera:
    def __init__(self, cfg):
        c = cfg.section("camera")
        self.main_size = (int(c["main_width"]), int(c["main_height"]))
        self.lores_size = (int(c["lores_width"]), int(c["lores_height"]))
        self.fps = int(c["fps"])
        self.burn_timestamp = bool(c.get("burn_timestamp", True))
        self.timestamp_format = c.get("timestamp_format", "%Y-%m-%d %H:%M:%S")
        self._t = 0
        self._active_path: Path | None = None
        self._writer: cv2.VideoWriter | None = None

    # ---------- 生命周期（空实现） ----------
    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stop_recording()

    # ---------- 合成画面 ----------
    def _render(self, size: tuple[int, int]) -> np.ndarray:
        w, h = size
        frame = np.full((h, w, 3), 40, np.uint8)
        # 淡背景网格，便于看出画面
        for x in range(0, w, 40):
            cv2.line(frame, (x, 0), (x, h), (60, 60, 60), 1)
        for y in range(0, h, 40):
            cv2.line(frame, (0, y), (w, y), (60, 60, 60), 1)
        # 来回快速移动的亮蓝色方块（模拟移动目标，变化面积需超过检测阈值）
        period = 40
        cx = int((math.sin(self._t / period * 2 * math.pi) * 0.5 + 0.5) * (w - 60))
        cy = h // 2 - 30
        cv2.rectangle(frame, (cx, cy), (cx + 60, cy + 60), (255, 120, 0), -1)
        cv2.putText(frame, "DEV SIMULATION", (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        return frame

    def lores_gray(self) -> np.ndarray:
        # 主循环每帧调用：顺手把主画面写入录像器
        main = self._render(self.main_size)
        self._t += 1
        if self.burn_timestamp:
            draw_timestamp(main, fmt=self.timestamp_format)
        if self._writer is not None:
            self._writer.write(main)
        small = cv2.resize(main, self.lores_size)
        return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    def main_bgr(self) -> np.ndarray:
        frame = self._render(self.main_size)
        self._t += 1
        return frame

    def stream_bgr(self, width: int | None = None) -> np.ndarray:
        frame = self._render(self.lores_size)
        self._t += 1
        if width:
            h, w = frame.shape[:2]
            new_h = max(1, int(h * width / w))
            frame = cv2.resize(frame, (int(width), new_h), interpolation=cv2.INTER_LINEAR)
        return frame

    @property
    def scale(self) -> tuple[float, float]:
        return (self.main_size[0] / self.lores_size[0],
                self.main_size[1] / self.lores_size[1])

    # ---------- 录像（dev：用 mp4v 写一个可回放的 mp4） ----------
    @property
    def recording_path(self) -> Path | None:
        return self._active_path

    @property
    def is_recording(self) -> bool:
        return self._writer is not None

    def start_recording(self, path) -> bool:
        if self._writer is not None:
            return False
        writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, self.main_size
        )
        if not writer.isOpened():
            return False
        self._writer = writer
        self._active_path = Path(path)
        return True

    def stop_recording(self) -> Path | None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        p = self._active_path
        self._active_path = None
        return p
