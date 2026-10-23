"""移动检测：低分辨率灰度帧差 + 轮廓筛选，输出主画面坐标系的目标框。

只在快照上画框，视频本体保持干净（见 camera.py 说明）。
"""
from __future__ import annotations

from datetime import datetime

import cv2
import numpy as np


class MotionDetector:
    def __init__(self, cfg):
        m = cfg.section("motion")
        self.enabled = bool(m["enabled"])
        self.threshold = int(m["sensitivity"])
        self.min_area = float(m["min_area"])
        self.blur_px = int(m["blur_px"])
        # 闭运算核尺寸（lores 像素）：桥接同一目标“旧位残影+新位”两条变化带，避免成对细条框
        self.close_w = int(m.get("close_width", 15))
        self.close_h = int(m.get("close_height", 7))
        # 轮廓合并间距（lores 像素）：水平间距小于它的相邻框合并成一个，消除同一目标的残影成对框
        self.merge_gap = int(m.get("merge_gap", 60))
        self.confirm_frames = max(1, int(m["confirm_frames"]))
        self.draw_boxes = bool(m["draw_boxes"])
        self.box_color = tuple(int(v) for v in m["box_color"])
        self.box_thickness = int(m["box_thickness"])

        self._prev = None
        self._hits = 0

    def reset(self) -> None:
        self._prev = None
        self._hits = 0

    def detect(self, gray_lores: np.ndarray, scale: tuple[float, float]) -> tuple[bool, list[tuple[int, int, int, int]]]:
        """返回 (是否判定移动, 主画面坐标系的 [(x,y,w,h), ...])。"""
        if not self.enabled:
            return False, []

        k = self.blur_px if self.blur_px % 2 == 1 else self.blur_px + 1
        blurred = cv2.GaussianBlur(gray_lores, (k, k), 0)

        boxes: list[tuple[int, int, int, int]] = []
        if self._prev is None:
            self._prev = blurred
            return False, []

        diff = cv2.absdiff(blurred, self._prev)
        self._prev = blurred

        _, binary = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)
        # 闭运算先把同一目标新旧位置的两条变化带桥接成一块，再轻微膨胀填充
        close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (self.close_w, self.close_h))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, close_kernel)
        binary = cv2.dilate(binary, np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        sx, sy = scale
        # 先在 lores 坐标收集合格轮廓框
        lores_rects: list[tuple[int, int, int, int]] = []
        for cnt in contours:
            if cv2.contourArea(cnt) < self.min_area:
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            lores_rects.append((x, y, w, h))

        # 合并水平间距小于 merge_gap 的相邻框（同一目标的残影带）
        lores_rects = self._merge_rects(lores_rects, self.merge_gap)

        raw_boxes: list[tuple[int, int, int, int]] = []
        for x, y, w, h in lores_rects:
            raw_boxes.append((int(x * sx), int(y * sy), int(w * sx), int(h * sy)))

        # 连续命中计数，避免单帧噪声触发
        if raw_boxes:
            self._hits = min(self._hits + 1, self.confirm_frames)
        else:
            self._hits = max(0, self._hits - 1)

        triggered = self._hits >= self.confirm_frames
        if triggered:
            boxes = raw_boxes
        return triggered, boxes

    @staticmethod
    def _merge_rects(rects: list[tuple[int, int, int, int]], gap: int):
        """按 x 排序，把水平间距 <= gap 且纵向重叠的相邻框合并为外接矩形。"""
        rects = sorted(rects, key=lambda r: r[0])
        merged: list[list[int]] = []
        for x, y, w, h in rects:
            if merged and x <= merged[-1][0] + merged[-1][2] + gap:
                ox, oy, ow, oh = merged[-1]
                nx = min(ox, x)
                ny = min(oy, y)
                nx2 = max(ox + ow, x + w)
                ny2 = max(oy + oh, y + h)
                merged[-1] = [nx, ny, nx2 - nx, ny2 - ny]
            else:
                merged.append([x, y, w, h])
        return [tuple(r) for r in merged]

    def annotate(self, bgr: np.ndarray, boxes: list[tuple[int, int, int, int]], event_label: str) -> np.ndarray:
        """在快照 BGR 图上画框、时间戳与事件标签，返回新图。"""
        out = bgr.copy()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.draw_boxes:
            for x, y, w, h in boxes:
                cv2.rectangle(out, (x, y), (x + w, y + h), self.box_color, self.box_thickness)
        cv2.putText(out, f"{now}  {event_label}", (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2, cv2.LINE_AA)
        return out
