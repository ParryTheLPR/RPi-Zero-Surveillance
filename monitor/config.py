"""配置加载：读取 config.yaml，解析相对路径与环境变量密码。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULTS: Dict[str, Any] = {
    "camera": {
        "main_width": 1280,
        "main_height": 720,
        "fps": 15,
        "lores_width": 320,
        "lores_height": 240,
        "vflip": False,
        "hflip": False,
        "bitrate": 3_000_000,
        "burn_timestamp": True,
        "timestamp_format": "%Y-%m-%d %H:%M:%S",
    },
    "motion": {
        "enabled": True,
        "sensitivity": 28,
        "min_area": 400,
        "blur_px": 5,
        "close_width": 15,
        "close_height": 7,
        "merge_gap": 60,
        "confirm_frames": 2,
        "draw_boxes": True,
        "box_color": [0, 255, 0],
        "box_thickness": 2,
        "snapshot_on_motion": True,
    },
    "recorder": {
        "mode": "motion",
        "segment_seconds": 300,
        "post_motion_seconds": 12,
        "local_dir": "./recordings",
        "snapshot_dir": "./snapshots",
        "max_local_gb": 5,
        "keep_local_after_upload": True,
    },
    "smb": {
        "enabled": True,
        "host": "",
        "port": 445,
        "share": "",
        "username": "guest",
        "password_env": "SMB_PASSWORD",
        "domain": "",
        "remote_dir": "/",
        "retry_interval": 60,
        "upload_snapshots": True,
    },
    "battery": {
        "enabled": True,
        "backend": "command",
        "poll_interval": 30,
        "low_threshold": 3.5,
        "critical_threshold": 3.3,
        "alert_cooldown": 1800,
        "command": "",
        "mcp3008": {"bus": 0, "device": 0, "channel": 0, "vref": 3.3, "divider_ratio": 2.0},
        "ina219": {"i2c_bus": 1, "address": "0x40"},
    },
    "mail": {
        "enabled": True,
        "smtp_host": "",
        "smtp_port": 465,
        "use_ssl": True,
        "use_starttls": False,
        "username": "",
        "password_env": "MAIL_PASSWORD",
        "sender": "",
        "recipients": [],
        "subject_prefix": "[RPi-Zero 监控]",
    },
    "web": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8080,
        "username": "admin",
        "password_env": "WEB_PASSWORD",
        "stream_width": 480,
        "stream_fps": 10,
        "jpeg_quality": 60,
    },
    "system": {"log_level": "INFO", "queue_file": "./upload_queue.json"},
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """带分区访问、相对路径解析的配置对象。"""

    def __init__(self, raw: Dict[str, Any], base_dir: Path):
        self._raw = raw
        self.base_dir = base_dir

    def section(self, name: str) -> Dict[str, Any]:
        return self._raw.get(name, {}) or {}

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.base_dir / p)

    def env_password(self, env_key: str) -> str:
        return os.environ.get(env_key, "")


def load_config(config_path: str | os.PathLike) -> Config:
    path = Path(config_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        user_raw = yaml.safe_load(f) or {}
    merged = _deep_merge(DEFAULTS, user_raw)
    cfg = Config(merged, path.parent)

    # 建本地目录
    for rel in (cfg.section("recorder")["local_dir"], cfg.section("recorder")["snapshot_dir"]):
        cfg.path(rel).mkdir(parents=True, exist_ok=True)
    return cfg
