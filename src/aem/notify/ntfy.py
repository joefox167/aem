from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


def send(url: str, title: str, message: str, click: str | None = None,
         priority: str | None = None, tags: str | None = None) -> bool:
    """Post a notification to an ntfy topic URL. Returns True on success.
    Header values must stay ASCII; put anything fancy in the body."""
    if not url:
        log.debug("ntfy: no URL configured, skipping: %s", title)
        return False
    headers = {"Title": title.encode("ascii", "ignore").decode()}
    if click:
        headers["Click"] = click
    if priority:
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = tags
    try:
        resp = httpx.post(url, content=message.encode("utf-8"), headers=headers, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.error("ntfy send failed: %s", exc)
        return False
