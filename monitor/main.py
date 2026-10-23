"""主程序：编排摄像头、移动检测、录制、SMB 上传、电池监测与邮件。

用法：
    python3 -m monitor.main --config config.yaml
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .battery import BatteryMonitor
from .camera import Camera
from .config import load_config
from .mailer import Mailer
from .motion import MotionDetector
from .recorder import Recorder
from .runtime import AppContext
from .smb_uploader import SMBUploader
from .web import WebServer

log = logging.getLogger("surveillance")
_stop = False


def _setup_logging(base_dir: Path, level: str) -> None:
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 按天滚动：每天午夜新建一个日志文件，旧文件按日期后缀归档，保留 14 天
    file_handler = TimedRotatingFileHandler(
        log_dir / "surveillance.log",
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
        utc=False,
    )
    file_handler.suffix = "%Y%m%d"
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def _handle_signal(signum, frame):
    global _stop
    _stop = True
    log.info("收到信号 %s，准备退出…", signum)


def main() -> int:
    parser = argparse.ArgumentParser(description="RPi Zero W OV5647 监控")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--dev", action="store_true", help="本机模拟模式：合成画面，跳过硬件录制/上传")
    args = parser.parse_args()

    cfg = load_config(args.config)
    _setup_logging(cfg.base_dir, cfg.section("system")["log_level"])

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # dev 模式：关闭真实上传/电池，网页给默认密码便于本机点验
    if args.dev:
        import os
        cfg._raw["smb"]["enabled"] = False
        cfg._raw["battery"]["enabled"] = False
        web_env = cfg.section("web")["password_env"]
        if not os.environ.get(web_env):
            os.environ[web_env] = "admin"

    mailer = Mailer(cfg)
    uploader = SMBUploader(cfg, keep_local_after_upload=cfg.section("recorder")["keep_local_after_upload"])
    battery = BatteryMonitor(cfg, mailer)
    motion = MotionDetector(cfg)

    if args.dev:
        from .dev_camera import DevCamera
        camera = DevCamera(cfg)
    else:
        camera = Camera(cfg)
    recorder = Recorder(cfg, camera, motion, uploader)

    # 摄像头可能晚于服务就绪，带退避重试（dev 模式直接就绪）
    cam_ready = False
    while not cam_ready and not _stop:
        try:
            camera.start()
            cam_ready = True
        except Exception as e:
            if args.dev:
                log.error("dev 摄像头启动失败: %s", e)
                return 1
            log.error("摄像头启动失败，10 秒后重试: %s", e)
            for _ in range(100):
                if _stop:
                    break
                time.sleep(0.1)

    if not cam_ready:
        log.error("摄像头不可用，退出")
        return 1

    ctx = AppContext(cfg, camera, motion, recorder, battery, mailer, uploader)
    web = WebServer(cfg, ctx, config_path=args.config)

    try:
        battery.start()
        uploader.start()
        recorder.start()
        web.start()
        log.info("监控已启动（模式=%s）", cfg.section("recorder")["mode"])

        frame_interval = 1.0 / max(1, cfg.section("camera")["fps"])
        while not _stop:
            loop_start = time.time()
            try:
                gray = camera.lores_gray()
                active, boxes = motion.detect(gray, camera.scale)
                recorder.update(loop_start, active, boxes)
            except Exception as e:
                log.warning("主循环单帧异常: %s", e)

            elapsed = time.time() - loop_start
            remain = frame_interval - elapsed
            if remain > 0:
                time.sleep(remain)
    finally:
        log.info("停止各模块…")
        web.stop()
        recorder.stop()
        uploader.stop()
        battery.stop()
        camera.stop()
        log.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
