"""运行时上下文：持有各组件，提供“安全项热重载”，结构项标记需重启。

分级原则：
- 安全项（阈值、灵敏度、冷却、邮件/SMB 可选参数等）直接改内存并即时生效；
- 结构项（摄像头分辨率/帧率、录制模式、SMB 主机/共享、电池后端、端口等）
  只写进配置文件，提示重启服务后生效。
"""
from __future__ import annotations

import time


# 结构项：改动后必须重启服务
RESTART_KEYS = {
    "camera.main_width", "camera.main_height", "camera.fps",
    "camera.lores_width", "camera.lores_height", "camera.vflip",
    "camera.hflip", "camera.bitrate",
    "camera.burn_timestamp", "camera.timestamp_format",
    "recorder.mode", "recorder.local_dir", "recorder.snapshot_dir",
    "smb.host", "smb.port", "smb.share", "smb.username",
    "smb.password_env", "smb.domain", "smb.remote_dir",
    "battery.backend", "battery.command", "battery.mcp3008",
    "battery.ina219",
    "web.host", "web.port", "web.username", "web.password_env",
}


class AppContext:
    def __init__(self, cfg, camera, motion, recorder, battery, mailer, uploader):
        self.cfg = cfg
        self.camera = camera
        self.motion = motion
        self.recorder = recorder
        self.battery = battery
        self.mailer = mailer
        self.uploader = uploader
        self._started = time.time()

    # ---------- 状态 ----------
    def status(self) -> dict:
        r = self.cfg.section("recorder")
        return {
            "uptime_sec": int(time.time() - self._started),
            "mode": r["mode"],
            "recording": self.camera.is_recording,
            "recording_file": self.camera.recording_path.name if self.camera.recording_path else "",
            "battery_enabled": self.battery.enabled,
            "battery_voltage": self.battery.last_voltage,
            "battery_low": self.cfg.section("battery")["low_threshold"],
            "battery_critical": self.cfg.section("battery")["critical_threshold"],
            "mail_enabled": self.mailer.enabled,
            "smb_enabled": self.uploader.enabled,
            "smb_pending": self.uploader.pending_count(),
            "web_port": self.cfg.section("web")["port"],
        }

    # ---------- 热重载 ----------
    def apply_safe(self, flat: dict[str, str]) -> tuple[list[str], list[str]]:
        """flat: dotted key -> 字符串值。返回 (已生效项, 需重启项)。"""
        applied: list[str] = []
        need_restart: list[str] = []
        raw = self.cfg._raw

        for key, raw_val in flat.items():
            section, _, field = key.partition(".")
            if key in RESTART_KEYS:
                # 结构项：不热生效，但仍写回配置文件，重启后生效
                if section in raw and field in raw[section]:
                    raw[section][field] = _coerce(raw[section][field], raw_val)
                need_restart.append(key)
                continue
            if section not in raw or field not in raw[section]:
                continue
            new_val = _coerce(raw[section][field], raw_val)
            raw[section][field] = new_val
            self._mutate(section, field, new_val)
            applied.append(key)

        return applied, need_restart

    def _mutate(self, section: str, field: str, value) -> None:
        if section == "motion":
            m = self.motion
            if field == "enabled":
                m.enabled = bool(value)
            elif field == "sensitivity":
                m.threshold = int(value)
            elif field == "min_area":
                m.min_area = float(value)
            elif field == "blur_px":
                m.blur_px = int(value)
            elif field == "confirm_frames":
                m.confirm_frames = max(1, int(value))
            elif field == "draw_boxes":
                m.draw_boxes = bool(value)
            elif field == "box_thickness":
                m.box_thickness = int(value)
            elif field == "snapshot_on_motion":
                self.recorder.snapshot_on_motion = bool(value)
        elif section == "recorder":
            r = self.recorder
            if field == "post_motion_seconds":
                r.post_motion_seconds = float(value)
            elif field == "segment_seconds":
                r.segment_seconds = float(value)
            elif field == "max_local_gb":
                r.max_local_bytes = float(value) * 1024 ** 3 if float(value) else 0
            elif field == "keep_local_after_upload":
                r.keep_local = bool(value)
                self.uploader.keep_local = bool(value)
        elif section == "battery":
            b = self.battery
            if field == "enabled":
                b.enabled = bool(value)
            elif field == "low_threshold":
                b.low_threshold = float(value)
            elif field == "critical_threshold":
                b.critical_threshold = float(value)
            elif field == "poll_interval":
                b.poll_interval = float(value)
            elif field == "alert_cooldown":
                b.cooldown = float(value)
        elif section == "mail":
            ml = self.mailer
            if field == "enabled":
                ml.enabled = bool(value)
            elif field == "smtp_host":
                ml.host = value
            elif field == "smtp_port":
                ml.port = int(value)
            elif field == "use_ssl":
                ml.use_ssl = bool(value)
            elif field == "use_starttls":
                ml.use_starttls = bool(value)
            elif field == "username":
                ml.username = value
            elif field == "sender":
                ml.sender = value
            elif field == "recipients":
                ml.recipients = [x.strip() for x in str(value).split(",") if x.strip()]
            elif field == "subject_prefix":
                ml.prefix = value
        elif section == "smb":
            if field == "retry_interval":
                self.uploader.retry_interval = int(value)
            elif field == "upload_snapshots":
                self.uploader.upload_snapshots = bool(value)
        elif section == "web":
            w = self.cfg.section("web")
            if field in ("stream_width", "stream_fps", "jpeg_quality"):
                pass  # 读取时实时取 cfg，无需额外动作


def _coerce(current, raw_val: str):
    """按当前值类型把表单字符串转成对应类型。"""
    if isinstance(current, bool):
        return raw_val in ("on", "true", "1", "yes", "True")
    if isinstance(current, int):
        try:
            return int(raw_val)
        except ValueError:
            return current
    if isinstance(current, float):
        try:
            return float(raw_val)
        except ValueError:
            return current
    return raw_val
