"""SMTP sending for registration verification emails (standard library only).

Credentials come exclusively from the backend .env (SMTP_* fields defined in
``backend/app/core/config.py``) and are never persisted to the database,
written to logs, or echoed in API responses.  The lazy import keeps this
module usable from standalone scripts where the backend settings are absent
(everything then reports "not configured").
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

_SMTP_TIMEOUT_SECONDS = 10


class EmailSendError(RuntimeError):
    """Delivery failure with a user-facing message (no credentials inside)."""


def _settings():
    try:
        from app.core.config import settings  # noqa: PLC0415 — deliberate bridge
        return settings
    except Exception:  # noqa: BLE001 — standalone/scripts fallback
        return None


def smtp_configured() -> bool:
    s = _settings()
    return bool(s and getattr(s, "smtp_host", "") and getattr(s, "smtp_sender", ""))


def smtp_status() -> dict:
    """Non-secret status for the admin page."""
    s = _settings()
    if not s:
        return {"configured": False, "sender": "", "from_name": ""}
    sender = getattr(s, "smtp_sender", "") or getattr(s, "smtp_username", "")
    return {
        "configured": bool(getattr(s, "smtp_host", "") and sender),
        "sender": sender,
        "from_name": getattr(s, "smtp_from_name", "") or "Paper Agent",
    }


def send_email(to: str, subject: str, body: str) -> None:
    """Send one plain-text email; raise EmailSendError on any failure."""
    s = _settings()
    if not s:
        raise EmailSendError("后端未加载 SMTP 配置")
    host = getattr(s, "smtp_host", "")
    sender = getattr(s, "smtp_sender", "") or getattr(s, "smtp_username", "")
    if not host or not sender:
        raise EmailSendError("未配置 SMTP 发信（缺少 SMTP_HOST / SMTP_SENDER）")
    username = getattr(s, "smtp_username", "")
    password = getattr(s, "smtp_password", "")
    port = int(getattr(s, "smtp_port", 465) or 465)
    security = str(getattr(s, "smtp_security", "ssl") or "ssl").lower()
    from_name = getattr(s, "smtp_from_name", "") or "Paper Agent"

    message = EmailMessage()
    message["From"] = formataddr((from_name, sender))
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        if security == "starttls":
            with smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS) as client:
                client.starttls()
                if username:
                    client.login(username, password)
                client.send_message(message)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT_SECONDS) as client:
                if username:
                    client.login(username, password)
                client.send_message(message)
    except smtplib.SMTPAuthenticationError:
        raise EmailSendError("SMTP 账号认证失败，请检查 SMTP_USERNAME / SMTP_PASSWORD") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError(f"邮件服务器连接或发送失败：{type(exc).__name__}") from None


def send_verification_email(to: str, code: str, *, ttl_minutes: int = 10) -> None:
    body = (
        "您正在注册 Paper Agent 账号。\n\n"
        f"验证码：{code}\n"
        f"验证码 {ttl_minutes} 分钟内有效。如非本人操作，请忽略本邮件。\n"
    )
    send_email(to, "Paper Agent 注册验证码", body)


def send_test_email(to: str) -> None:
    body = (
        "这是 Paper Agent 管理页发送的 SMTP 测试邮件。\n"
        "收到本邮件说明验证码邮件可以正常发出。\n"
    )
    send_email(to, "Paper Agent SMTP 测试邮件", body)
