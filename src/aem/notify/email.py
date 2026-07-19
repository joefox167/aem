from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def send_html(user: str, app_password: str, to_addr: str, subject: str, html: str) -> bool:
    if not (user and app_password and to_addr):
        log.debug("email: credentials/recipient not configured, skipping: %s", subject)
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, app_password)
            smtp.sendmail(user, [to_addr], msg.as_string())
        return True
    except Exception as exc:
        log.error("email send failed: %s", exc)
        return False
