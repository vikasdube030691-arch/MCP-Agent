"""Outgoing email (password resets), sent over SMTP in a worker thread."""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from html import escape
from typing import Any
from urllib.parse import urlencode

from . import config

log = logging.getLogger(__name__)

# Sends run in the background so a request answers at once (and in the same time whether
# or not an email goes out). Kept here so the tasks aren't garbage-collected mid-send.
_pending: set[asyncio.Task] = set()


def reset_link(token: str) -> str:
    return f"{config.PASSWORD_RESET_URL}?{urlencode({'token': token})}"


def send_password_reset(user: dict[str, Any], token: str) -> None:
    """Email the user a link to choose a new password, in the background."""
    link = reset_link(token)
    if not config.SMTP_HOST:
        log.warning("SMTP_HOST is not set, so no email was sent. Password reset link for %s: %s",
                    user["email"], link)
        return

    minutes = config.RESET_TOKEN_TTL_MINUTES
    msg = EmailMessage()
    msg["Subject"] = "Reset your Expense Assistant password"
    msg["From"] = config.SMTP_FROM
    msg["To"] = user["email"]
    msg.set_content(
        f"Hi {user['name']},\n\n"
        f"Someone asked to reset the password for your Expense Assistant account.\n"
        f"To choose a new password, open this link within {minutes} minutes:\n\n{link}\n\n"
        f"If it wasn't you, ignore this email; your password stays the same.\n"
    )
    msg.add_alternative(
        f"<p>Hi {escape(user['name'])},</p>"
        f"<p>Someone asked to reset the password for your Expense Assistant account.</p>"
        f'<p><a href="{escape(link)}" style="display:inline-block;padding:10px 18px;'
        f'background:#0b57d0;color:#fff;border-radius:20px;text-decoration:none">'
        f"Choose a new password</a></p>"
        f"<p>The link works once and expires in {minutes} minutes. If it wasn't you, ignore "
        f"this email; your password stays the same.</p>",
        subtype="html",
    )
    task = asyncio.create_task(_send(msg))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _send(msg: EmailMessage) -> None:
    try:
        await asyncio.to_thread(_send_blocking, msg)
        log.info("Password reset email sent to %s", msg["To"])
    except (OSError, smtplib.SMTPException):
        log.exception("Could not send the password reset email to %s", msg["To"])


def _send_blocking(msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if config.SMTP_SECURITY == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                              context=context, timeout=20)
    else:
        smtp = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
    with smtp:
        if config.SMTP_SECURITY == "starttls":
            smtp.starttls(context=context)
        if config.SMTP_USERNAME:
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD or "")
        smtp.send_message(msg)
