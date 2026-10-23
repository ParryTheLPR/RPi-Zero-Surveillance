"""画面叠加：在帧上烧录时间戳（视频/快照通用，白色文字+半透明暗条保证可读）。"""
from __future__ import annotations

from datetime import datetime

import cv2
import numpy as np


def draw_timestamp(frame: np.ndarray, text: str | None = None,
                   fmt: str = "%Y-%m-%d %H:%M:%S", xy: tuple[int, int] = (12, 30)) -> np.ndarray:
    """在帧左上角绘制时间戳，原地修改并返回帧。frame 为 HxWx3 的 BGR/RGB 数组均可（用白色）。"""
    if text is None:
        text = datetime.now().strftime(fmt)
    (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
    x, y = xy
    # 背景暗条
    cv2.rectangle(frame, (x - 6, y - th - 8), (x + tw + 6, y + baseline + 6), (0, 0, 0), -1)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (255, 255, 255), 2, cv2.LINE_AA)
    return frame
