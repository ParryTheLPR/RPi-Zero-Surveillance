"""SMTP 邮件发送：支持 SSL(465) 与 STARTTLS(587)，密码走环境变量。"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

log = logging.getLogger(__name__)


class Mailer:
    def __init__(self, cfg):
        m = cfg.section("mail")
        self.enabled = bool(m["enabled"])
        self.host = m["smtp_host"]
        self.port = int(m["smtp_port"])
        self.use_ssl = bool(m["use_ssl"])
        self.use_starttls = bool(m["use_starttls"])
        self.username = m["username"]
        self.password = cfg.env_password(m["password_env"])
        self.sender = m["sender"] or self.username
        self.recipients = list(m["recipients"])
        self.prefix = m["subject_prefix"]

    def send(self, subject: str, body: str) -> bool:
        if not self.enabled:
            log.info("邮件功能已关闭，跳过: %s", subject)
            return False
        if not self.host or not self.recipients:
            log.warning("SMTP 未配置（host/recipients 为空），无法发信")
            return False

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"{self.prefix} {subject}"
        msg["From"] = formataddr(("RPi-Zero 监控", self.sender))
        msg["To"] = ", ".join(self.recipients)
        msg["Date"] = formatdate(localtime=True)

        try:
            context = ssl.create_default_context()
            if self.use_ssl:
                with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=20) as s:
                    if self.username:
                        s.login(self.username, self.password)
                    s.sendmail(self.sender, self.recipients, msg.as_string())
            else:
                with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                    s.ehlo()
                    if self.use_starttls:
                        s.starttls(context=context)
                        s.ehlo()
                    if self.username:
                        s.login(self.username, self.password)
                    s.sendmail(self.sender, self.recipients, msg.as_string())
            log.info("邮件已发送: %s", subject)
            return True
        except Exception as e:  # 网络/认证失败不应崩溃主循环
            log.error("邮件发送失败: %s", e)
            return False
