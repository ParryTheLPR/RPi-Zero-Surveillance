"""SMB 上传：后台线程 + 持久化队列，断网重启后继续补传。

使用 pysmb（纯 Python），不依赖系统 smbclient。
队列落盘到 system.queue_file，记录待传文件、类型与重试次数。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

try:
    from smb.SMBConnection import SMBConnection
except ImportError:  # pragma: no cover
    SMBConnection = None  # type: ignore


class SMBUploader:
    def __init__(self, cfg, keep_local_after_upload: bool):
        s = cfg.section("smb")
        self.enabled = bool(s["enabled"])
        self.host = s["host"]
        self.port = int(s["port"])
        self.share = s["share"]
        self.username = s["username"]
        self.password = cfg.env_password(s["password_env"])
        self.domain = s["domain"]
        self.remote_dir = (s["remote_dir"] or "/").strip("/")
        self.retry_interval = int(s["retry_interval"])
        self.upload_snapshots = bool(s["upload_snapshots"])
        self.keep_local = bool(keep_local_after_upload)

        self.queue_file = cfg.path(cfg.section("system")["queue_file"])
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    # ---------- 队列持久化 ----------
    def _load(self) -> None:
        if self.queue_file.exists():
            try:
                self._queue = json.loads(self.queue_file.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("上传队列读取失败，重建空队列: %s", e)
                self._queue = []

    def _save(self) -> None:
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        self.queue_file.write_text(json.dumps(self._queue, ensure_ascii=False, indent=2), encoding="utf-8")

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for q in self._queue if q["status"] == "pending")

    def enqueue(self, path: Path, kind: str) -> None:
        if not self.enabled:
            return
        if kind == "snapshot" and not self.upload_snapshots:
            return
        with self._lock:
            if any(item["path"] == str(path) and item["status"] == "pending" for item in self._queue):
                return
            self._queue.append({
                "path": str(path),
                "kind": kind,
                "status": "pending",
                "attempts": 0,
                "enqueued_at": datetime.now().isoformat(timespec="seconds"),
            })
            self._save()

    # ---------- 线程 ----------
    def start(self) -> None:
        if not self.enabled:
            log.info("SMB 上传已关闭")
            return
        if SMBConnection is None:
            log.warning("pysmb 未安装，上传功能不可用")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="smb-uploader")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            pending = self._pending()
            if not pending:
                self._stop.wait(10)
                continue
            ok_all = True
            conn = self._connect()
            if conn is None:
                ok_all = False
            else:
                try:
                    for item in pending:
                        if self._upload_one(conn, item):
                            self._mark_done(item)
                        else:
                            ok_all = False
                            self._bump_attempt(item)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._stop.wait(self.retry_interval if not ok_all else 5)

    def _pending(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._queue if i["status"] == "pending"]

    def _connect(self):
        try:
            conn = SMBConnection(
                self.username, self.password, "rpi-zero-w", self.host,
                domain=self.domain, use_ntlm_v2=True,
            )
            if conn.connect(self.host, self.port, timeout=15):
                return conn
            log.error("SMB 连接失败: %s:%s", self.host, self.port)
        except Exception as e:
            log.error("SMB 连接异常: %s", e)
        return None

    def _ensure_dir(self, conn, rel_dir: str) -> None:
        """逐级创建远端目录（已存在则忽略）。"""
        parts = [p for p in rel_dir.split("/") if p]
        cur = ""
        for p in parts:
            cur = f"{cur}/{p}" if cur else p
            try:
                conn.createDirectory(self.share, cur)
            except Exception:
                pass  # 目录已存在

    def _upload_one(self, conn, item: dict) -> bool:
        path = Path(item["path"])
        if not path.exists():
            log.warning("待传文件已消失，移出队列: %s", path)
            self._mark_done(item)  # 文件没了，无需再传
            return True
        day = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")
        sub = "snapshots" if item["kind"] == "snapshot" else "videos"
        rel = f"{self.remote_dir}/{sub}/{day}/{path.name}".strip("/")
        self._ensure_dir(conn, str(Path(rel).parent))
        try:
            with path.open("rb") as f:
                conn.storeFile(self.share, rel, f)
            log.info("已上传 SMB: %s -> %s", path.name, rel)
            if not self.keep_local:
                path.unlink(missing_ok=True)
            return True
        except Exception as e:
            log.error("上传失败 %s: %s", path.name, e)
            return False

    def _mark_done(self, item: dict) -> None:
        with self._lock:
            for i, q in enumerate(self._queue):
                if q["path"] == item["path"] and q["status"] == "pending":
                    self._queue[i]["status"] = "done"
                    break
            self._queue = [q for q in self._queue if q["status"] == "pending"]
            self._save()

    def _bump_attempt(self, item: dict) -> None:
        with self._lock:
            for q in self._queue:
                if q["path"] == item["path"]:
                    q["attempts"] = int(q.get("attempts", 0)) + 1
            self._save()
