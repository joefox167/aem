from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import getaddresses

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _parse_recipients(raw: str) -> list[str]:
    normalized = raw.replace(";", ",")
    return [addr for _, addr in getaddresses([normalized]) if addr]


def send_html(user: str, app_password: str, to_addrs: str, subject: str, html: str) -> bool:
    recipients = _parse_recipients(to_addrs)
    if not (user and app_password and recipients):
        log.debug("email: credentials/recipient not configured, skipping: %s", subject)
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, app_password)
            smtp.sendmail(user, recipients, msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001 -- a failed send is reported, never raised
        log.error("email send failed: %s", exc)
        return False
