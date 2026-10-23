"""内置网页服务：Basic Auth 登录、MJPEG 实时预览、状态面板、分级热重载配置表单。

仅用标准库 http.server，不引入 Flask，降低 Zero W 依赖。
"""
from __future__ import annotations

import base64
import json
import logging
import shutil
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import yaml

log = logging.getLogger(__name__)

CHECKBOXES = {
    "motion.enabled", "motion.draw_boxes", "motion.snapshot_on_motion",
    "recorder.keep_local_after_upload",
    "battery.enabled",
    "mail.enabled", "mail.use_ssl", "mail.use_starttls",
    "smb.upload_snapshots",
}


class WebServer:
    def __init__(self, cfg, ctx, config_path: str):
        w = cfg.section("web")
        self.enabled = bool(w["enabled"])
        self.host = w["host"]
        self.port = int(w["port"])
        self.username = w["username"]
        self.password = cfg.env_password(w["password_env"])
        self.stream_width = int(w["stream_width"])
        self.fps = int(w["stream_fps"])
        self.quality = int(w["jpeg_quality"])
        self.ctx = ctx
        self.config_path = config_path
        self._http: ThreadingHTTPServer | None = None

    def start(self) -> None:
        if not self.enabled:
            log.info("Web 服务已关闭")
            return
        if not self.password:
            log.warning("未设置 WEB_PASSWORD，网页将拒绝访问")
        handler = lambda request, client_address, server: Handler(request, client_address, server, self)  # noqa: E731
        self._http = ThreadingHTTPServer((self.host, self.port), handler)
        threading.Thread(target=self._http.serve_forever, daemon=True, name="web").start()
        log.info("Web 面板监听 http://%s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._http:
            try:
                self._http.shutdown()
                self._http.server_close()
            except Exception:
                pass

    def backup_config(self, keep: int = 3) -> Path | None:
        """保存前把当前 config.yaml 备份为 config-bak-YYYYMMDD-HHMMSS_毫秒.yaml，只保留最新 keep 份。"""
        src = Path(self.config_path)
        if not src.exists():
            return None
        now = datetime.now()
        ms = now.microsecond // 1000
        bak = src.parent / f"config-bak-{now:%Y%m%d-%H%M%S}_{ms:03d}.yaml"
        shutil.copy2(src, bak)
        backups = sorted(src.parent.glob("config-bak-*.yaml"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            try:
                old.unlink()
            except Exception as e:
                log.warning("清理旧备份失败 %s: %s", old.name, e)
        return bak


class Handler(BaseHTTPRequestHandler):
    server_version = "RPiZeroSurv/1.0"

    def __init__(self, request, client_address, server, web: WebServer):
        self.web = web
        super().__init__(request, client_address, server)

    # ---------- 鉴权 ----------
    def _auth_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pwd = decoded.partition(":")
        except Exception:
            return False
        return user == self.web.username and pwd == self.web.password and bool(self.web.password)

    def _require_auth(self) -> bool:
        if self._auth_ok():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="rpi-zero"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def log_message(self, fmt, *args):
        log.debug("web: " + fmt, *args)

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self._require_auth():
            return
        if parsed.path in ("/", "/index.html"):
            self._dashboard()
        elif parsed.path == "/stream":
            self._mjpeg()
        elif parsed.path == "/status":
            self._json(self.web.ctx.status())
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self._require_auth():
            return
        if parsed.path == "/config":
            self._save_config()
        elif parsed.path == "/restart":
            self._restart_service()
        else:
            self.send_error(404)

    # ---------- 一键重启服务 ----------
    def _restart_service(self) -> None:
        import subprocess

        # 树莓派正式服务：systemctl 重启（需 sudo 免密，见 README）
        try:
            r = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "surveillance"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                # 进程已被 systemd 拉起新实例，无需返回响应，浏览器稍后刷新即可
                return
            detail = (r.stderr or r.stdout or "").strip()
        except FileNotFoundError:
            detail = "当前环境没有 systemctl"
        except subprocess.TimeoutExpired:
            detail = "systemctl 超时"
        except Exception as e:
            detail = str(e)

        self._html_message(
            f"未能通过 systemctl 重启（{detail}）。"
            "正式部署在树莓派上请配置 sudo 免密；本机预览环境无需重启。"
        )

    def _html_message(self, text: str) -> None:
        body = f"<html><body><h3>{text}</h3></body></html>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- MJPEG ----------
    def _mjpeg(self):
        ctx = self.web.ctx
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        interval = 1.0 / max(1, self.web.fps)
        try:
            while True:
                try:
                    frame = ctx.camera.stream_bgr(self.web.stream_width)
                except Exception:
                    frame = _placeholder()
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.web.quality])
                if ok:
                    data = buf.tobytes()
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(
                        f"Content-Type: image/jpeg\r\nContent-Length: {len(data)}\r\n\r\n".encode()
                    )
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.debug("MJPEG 结束: %s", e)

    # ---------- 状态 ----------
    def _json(self, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 配置页 ----------
    def _dashboard(self, message: str = "", applied: list[str] | None = None,
                   restart: list[str] | None = None):
        html = render_dashboard(self.web.ctx.cfg, message, applied or [], restart or [])
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _save_config(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        # 复选框归一化
        for key in CHECKBOXES:
            form[key] = "on" if form.get(key) else "off"

        applied, restart = self.web.ctx.apply_safe(form)

        # 保存前先备份当前文件（只留最新 3 份），再把整份配置落盘
        msg = "配置已保存。"
        try:
            bak = self.web.backup_config(keep=3)
            if bak is not None:
                msg += f" 已备份: {bak.name}"
            with open(self.web.config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(self.web.ctx.cfg._raw, f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            msg = f"配置写盘失败: {e}"

        self._dashboard(msg, applied, restart)


def _placeholder():
    img = __import__("numpy").zeros((240, 320, 3), dtype="uint8")
    cv2.putText(img, "no camera", (90, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    return img


# ---------------- 页面渲染 ----------------
def _val(cfg, section, field, default=""):
    v = cfg.section(section).get(field, default)
    return "" if v is None else v


def _cb(cfg, section, field) -> str:
    return "checked" if bool(cfg.section(section).get(field, False)) else ""


def render_dashboard(cfg, message: str, applied: list[str], restart: list[str]) -> str:
    m, r, b, ml, s, w = (cfg.section(x) for x in ("motion", "recorder", "battery", "mail", "smb", "web"))
    applied_html = "".join(f"<li><span class='ok'>已生效</span> {k}</li>" for k in applied)
    restart_html = "".join(f"<li><span class='warn'>需重启服务</span> {k}</li>" for k in restart)
    msg_html = f"<div class='msg'>{message}</div>" if message else ""
    lists = ""
    if applied_html or restart_html:
        lists = f"<ul class='chg'>{applied_html}{restart_html}</ul>"

    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RPi Zero 监控面板</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;background:#f2f4f7;color:#1f2937}}
.wrap{{max-width:900px;margin:0 auto;padding:16px}}
h1{{font-size:20px}} h2{{font-size:15px;margin:18px 0 8px}}
.card{{background:#fff;border-radius:10px;padding:14px;margin:10px 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
img.stream{{width:100%;border-radius:8px;background:#000}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
td{{padding:6px 8px;vertical-align:middle}}
td:first-child{{width:42%;color:#4b5563}}
input[type=number],input[type=text],select{{width:100%;padding:6px;border:1px solid #d1d5db;border-radius:6px}}
button{{background:#2563eb;color:#fff;border:0;padding:10px 18px;border-radius:8px;font-size:15px;cursor:pointer}}
.msg{{background:#eef2ff;padding:10px;border-radius:8px;margin:8px 0}}
.chg{{font-size:13px}} .ok{{color:#16a34a;font-weight:600}} .warn{{color:#d97706;font-weight:600}}
.pill{{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px}}
.rec{{background:#dcfce7;color:#166534}} .idle{{background:#f3f4f6;color:#6b7280}}
</style></head><body><div class="wrap">
<h1>树莓派 Zero W 监控面板</h1>
<div class="card">
  <img class="stream" src="/stream" alt="preview">
  <div id="status" class="chg">加载中…</div>
</div>
<div class="card">
  <form method="post" action="/restart" onsubmit="return confirm('确定重启监控服务？当前连接会短暂断开。')">
    <button type="submit" style="background:#dc2626">一键重启服务</button>
    <span class="chg">应用“需重启”的配置项；正式环境走 systemctl，本机回退为进程自重启。</span>
  </form>
</div>
<form method="post" action="/config">
<div class="card"><h2>移动检测</h2><table>
<tr><td>启用移动检测</td><td><input type="checkbox" name="motion.enabled" {_cb(cfg,"motion","enabled")}></td></tr>
<tr><td>灵敏度阈值(0-255，越大越不灵敏)</td><td><input type="number" name="motion.sensitivity" value="{m.get('sensitivity')}"></td></tr>
<tr><td>最小目标面积(lores像素)</td><td><input type="number" name="motion.min_area" value="{m.get('min_area')}"></td></tr>
<tr><td>模糊核(奇数)</td><td><input type="number" name="motion.blur_px" value="{m.get('blur_px')}"></td></tr>
<tr><td>连续确认帧数(防抖)</td><td><input type="number" name="motion.confirm_frames" value="{m.get('confirm_frames')}"></td></tr>
<tr><td>快照上画移动框</td><td><input type="checkbox" name="motion.draw_boxes" {_cb(cfg,"motion","draw_boxes")}></td></tr>
<tr><td>移动时保存快照</td><td><input type="checkbox" name="motion.snapshot_on_motion" {_cb(cfg,"motion","snapshot_on_motion")}></td></tr>
</table></div>
<div class="card"><h2>录制</h2><table>
<tr><td>录制模式(改后需重启)</td><td><select name="recorder.mode">
<option value="motion" {r.get('mode')=='motion' and 'selected' or ''}>motion 移动触发</option>
<option value="continuous" {r.get('mode')=='continuous' and 'selected' or ''}>continuous 连续</option></select></td></tr>
<tr><td>静止后继续录制(秒)</td><td><input type="number" name="recorder.post_motion_seconds" value="{r.get('post_motion_seconds')}"></td></tr>
<tr><td>连续模式分段时长(秒)</td><td><input type="number" name="recorder.segment_seconds" value="{r.get('segment_seconds')}"></td></tr>
<tr><td>本地保留上限(GB，0不限)</td><td><input type="number" name="recorder.max_local_gb" value="{r.get('max_local_gb')}"></td></tr>
<tr><td>上传成功后保留本地</td><td><input type="checkbox" name="recorder.keep_local_after_upload" {_cb(cfg,"recorder","keep_local_after_upload")}></td></tr>
</table></div>
<div class="card"><h2>电池告警</h2><table>
<tr><td>启用电池监测</td><td><input type="checkbox" name="battery.enabled" {_cb(cfg,"battery","enabled")}></td></tr>
<tr><td>低电压阈值(V)</td><td><input type="number" step="0.01" name="battery.low_threshold" value="{b.get('low_threshold')}"></td></tr>
<tr><td>严重低压阈值(V)</td><td><input type="number" step="0.01" name="battery.critical_threshold" value="{b.get('critical_threshold')}"></td></tr>
<tr><td>轮询间隔(秒)</td><td><input type="number" name="battery.poll_interval" value="{b.get('poll_interval')}"></td></tr>
<tr><td>告警冷却(秒)</td><td><input type="number" name="battery.alert_cooldown" value="{b.get('alert_cooldown')}"></td></tr>
</table></div>
<div class="card"><h2>邮件 SMTP</h2><table>
<tr><td>启用邮件</td><td><input type="checkbox" name="mail.enabled" {_cb(cfg,"mail","enabled")}></td></tr>
<tr><td>SMTP 服务器</td><td><input type="text" name="mail.smtp_host" value="{ml.get('smtp_host')}"></td></tr>
<tr><td>端口</td><td><input type="number" name="mail.smtp_port" value="{ml.get('smtp_port')}"></td></tr>
<tr><td>SSL(465)</td><td><input type="checkbox" name="mail.use_ssl" {_cb(cfg,"mail","use_ssl")}></td></tr>
<tr><td>STARTTLS(587)</td><td><input type="checkbox" name="mail.use_starttls" {_cb(cfg,"mail","use_starttls")}></td></tr>
<tr><td>账号</td><td><input type="text" name="mail.username" value="{ml.get('username')}"></td></tr>
<tr><td>发件人</td><td><input type="text" name="mail.sender" value="{ml.get('sender')}"></td></tr>
<tr><td>收件人(逗号分隔)</td><td><input type="text" name="mail.recipients" value="{','.join(ml.get('recipients', []))}"></td></tr>
<tr><td>主题前缀</td><td><input type="text" name="mail.subject_prefix" value="{ml.get('subject_prefix')}"></td></tr>
</table></div>
<div class="card"><h2>SMB 上传</h2><table>
<tr><td>服务器(改后需重启)</td><td><input type="text" name="smb.host" value="{s.get('host')}"></td></tr>
<tr><td>共享名(改后需重启)</td><td><input type="text" name="smb.share" value="{s.get('share')}"></td></tr>
<tr><td>用户名(改后需重启)</td><td><input type="text" name="smb.username" value="{s.get('username')}"></td></tr>
<tr><td>远端目录(改后需重启)</td><td><input type="text" name="smb.remote_dir" value="{s.get('remote_dir')}"></td></tr>
<tr><td>重试间隔(秒)</td><td><input type="number" name="smb.retry_interval" value="{s.get('retry_interval')}"></td></tr>
<tr><td>上传快照</td><td><input type="checkbox" name="smb.upload_snapshots" {_cb(cfg,"smb","upload_snapshots")}></td></tr>
</table></div>
<div class="card"><h2>预览流</h2><table>
<tr><td>流宽度(px)</td><td><input type="number" name="web.stream_width" value="{w.get('stream_width')}"></td></tr>
<tr><td>帧率(fps)</td><td><input type="number" name="web.stream_fps" value="{w.get('stream_fps')}"></td></tr>
<tr><td>JPEG 质量(1-95)</td><td><input type="number" name="web.jpeg_quality" value="{w.get('jpeg_quality')}"></td></tr>
</table></div>
{msg_html}{lists}
<button type="submit">保存配置</button>
</form>
<p class="chg">提示：标注“改后需重启”的项保存后写入文件，重启服务生效；其余项即时生效。</p>
</div>
<script>
async function refresh(){{
  try{{
    const r=await fetch('/status'); const d=await r.json();
    const rec=d.recording?'<span class="pill rec">录制中</span>':'<span class="pill idle">空闲</span>';
    const bat=d.battery_voltage!=null?d.battery_voltage.toFixed(3)+' V':'—';
    document.getElementById('status').innerHTML=
      rec+' 模式:'+d.mode+' | 电池:'+bat+' | SMB待传:'+d.smb_pending+
      ' | 邮件:'+(d.mail_enabled?'开':'关')+' | 已运行:'+Math.floor(d.uptime_sec/60)+'分钟';
  }}catch(e){{}}
}}
refresh(); setInterval(refresh,2000);
</script>
</body></html>"""
