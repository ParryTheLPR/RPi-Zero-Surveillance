"""电池亏电监测：可插拔后端（command / mcp3008 / ina219），轮询电压并分级发信。

后端抽象：新增硬件只需实现 read_voltage() -> float（伏特）。
告警分级：critical <= critical_threshold < low <= low_threshold，其余正常；
同级别告警受 alert_cooldown 冷却约束，避免频繁发信。
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class BatteryBackend(ABC):
    @abstractmethod
    def read_voltage(self) -> float:
        """返回电池电压（V）。读不到抛异常。"""

    def close(self) -> None:
        pass


class CommandBackend(BatteryBackend):
    """执行自定义命令，从 stdout 解析第一个浮点数值作为电压(V)。"""

    def __init__(self, command: str):
        self.command = command

    def read_voltage(self) -> float:
        out = subprocess.run(
            self.command, shell=True, capture_output=True, text=True, timeout=15, check=False
        )
        text = (out.stdout or "") + " " + (out.stderr or "")
        m = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        if not m:
            raise RuntimeError(f"命令未输出可解析电压: {text.strip()!r}")
        return float(m.group(0))


class Mcp3008Backend(BatteryBackend):
    """SPI ADC(MCP3008) + 分压电阻读电池电压。"""

    def __init__(self, cfg: dict):
        self.bus = int(cfg["bus"])
        self.device = int(cfg["device"])
        self.channel = int(cfg["channel"])
        self.vref = float(cfg["vref"])
        self.divider_ratio = float(cfg["divider_ratio"])
        self._spi = None
        try:
            import spidev  # type: ignore
            self._spi = spidev.SpiDev()
            self._spi.open(self.bus, self.device)
            self._spi.max_speed_hz = 1_000_000
        except Exception as e:
            raise RuntimeError(f"MCP3008 初始化失败: {e}") from e

    def read_voltage(self) -> float:
        # MCP3008 单端通道读取
        r = self._spi.xfer2([1, (8 + self.channel) << 4, 0])
        raw = ((r[1] & 3) << 8) + r[2]
        return raw / 1023.0 * self.vref * self.divider_ratio

    def close(self) -> None:
        if self._spi:
            self._spi.close()


class Ina219Backend(BatteryBackend):
    """INA219 I2C 传感器，读 VBUS 寄存器(0x02)，LSB=4mV。"""

    def __init__(self, cfg: dict):
        self.bus_no = int(cfg["i2c_bus"])
        self.address = int(str(cfg["address"]), 16) if isinstance(cfg["address"], str) else int(cfg["address"])
        self._bus = None
        try:
            from smbus2 import SMBus  # type: ignore
            self._bus = SMBus(self.bus_no)
        except Exception as e:
            raise RuntimeError(f"INA219 初始化失败: {e}") from e

    def read_voltage(self) -> float:
        raw = self._bus.read_word_data(self.address, 0x02)
        raw = ((raw & 0xFF) << 8) | (raw >> 8)  # 大小端交换
        return (raw >> 3) * 0.004  # 12bit, 4mV/LSB

    def close(self) -> None:
        if self._bus:
            self._bus.close()


def build_backend(cfg) -> BatteryBackend:
    b = cfg.section("battery")
    kind = b["backend"]
    if kind == "mcp3008":
        return Mcp3008Backend(b["mcp3008"])
    if kind == "ina219":
        return Ina219Backend(b["ina219"])
    if not b.get("command"):
        raise ValueError("backend=command 时必须配置 battery.command")
    return CommandBackend(b["command"])


class BatteryMonitor:
    def __init__(self, cfg, mailer):
        self._cfg = cfg
        b = cfg.section("battery")
        self.enabled = bool(b["enabled"])
        self.poll_interval = float(b["poll_interval"])
        self.low_threshold = float(b["low_threshold"])
        self.critical_threshold = float(b["critical_threshold"])
        self.cooldown = float(b["alert_cooldown"])
        self.mailer = mailer
        self.backend: BatteryBackend | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_alert: dict[str, float] = {}
        self._last_level = "normal"
        self._last_voltage: float | None = None

    @property
    def last_voltage(self) -> float | None:
        return self._last_voltage

    def start(self) -> None:
        if not self.enabled:
            log.info("电池监测已关闭")
            return
        self.backend = build_backend(self._cfg)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="battery-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self.backend:
            self.backend.close()

    def _level(self, v: float) -> str:
        if v <= self.critical_threshold:
            return "critical"
        if v <= self.low_threshold:
            return "low"
        return "normal"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                v = self.backend.read_voltage()
                self._last_voltage = v
                level = self._level(v)
                self._evaluate(level, v)
                self._last_level = level
            except Exception as e:
                log.warning("电池电压读取失败: %s", e)
            self._stop.wait(self.poll_interval)

    def _evaluate(self, level: str, v: float) -> None:
        if level == "normal":
            self._last_alert.pop("low", None)
            self._last_alert.pop("critical", None)
            return
        now = time.time()
        last = self._last_alert.get(level, 0.0)
        transition = level != self._last_level
        if transition or now - last >= self.cooldown:
            self._last_alert[level] = now
            self._send(level, v)

    def _send(self, level: str, v: float) -> None:
        if level == "critical":
            subject = "电池严重低压，建议立即断电保护"
            body = (
                f"电池电压 {v:.3f} V，已低于严重阈值 {self.critical_threshold:.3f} V。\n"
                f"低电压阈值 {self.low_threshold:.3f} V。请立即外接电源或安全关机，避免过放。\n"
            )
        else:
            subject = "电池电压偏低提醒"
            body = (
                f"电池电压 {v:.3f} V，已低于低电压阈值 {self.low_threshold:.3f} V。\n"
                f"严重阈值 {self.critical_threshold:.3f} V，请及时充电或接外部电源。\n"
            )
        self.mailer.send(subject, body)
