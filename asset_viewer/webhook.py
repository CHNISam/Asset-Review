from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .storage import data_dir, recent_review_events, review_events_since, utc_now


def validate_webhook_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("webhook URL must be absolute http:// or https://")
    return url


def _endpoint_key(url: str, collection: str | None) -> str:
    raw = f"{collection or '*'}\n{url}".encode()
    return hashlib.sha256(raw).hexdigest()


def webhook_state_path() -> Path:
    return data_dir() / "webhook-cursors.json"


def _read_state() -> dict[str, Any]:
    path = webhook_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _write_state(payload: dict[str, Any]) -> None:
    path = webhook_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def webhook_cursor(url: str, collection: str | None) -> int | None:
    key = _endpoint_key(validate_webhook_url(url), collection)
    row = _read_state().get(key)
    if not isinstance(row, dict) or not isinstance(row.get("last_event_id"), int):
        return None
    return int(row["last_event_id"])


def save_webhook_cursor(url: str, collection: str | None, last_event_id: int, *, last_error: str = "") -> None:
    url = validate_webhook_url(url)
    state = _read_state()
    state[_endpoint_key(url, collection)] = {
        "collection": collection or "*",
        "host": urlparse(url).hostname or "",
        "last_event_id": int(last_event_id),
        "updated_at": utc_now(),
        "last_error": str(last_error)[:500],
    }
    _write_state(state)


def _body(collection: str | None, events: list[dict[str, Any]]) -> bytes:
    return (json.dumps({
        "version": 1,
        "source": "asset-viewer",
        "collection": collection,
        "events": events,
    }, separators=(",", ":"), sort_keys=True) + "\n").encode()


def deliver_webhook(
    url: str,
    collection: str | None,
    events: list[dict[str, Any]],
    *,
    secret: str = "",
    timeout: float = 10.0,
) -> dict[str, Any]:
    url = validate_webhook_url(url)
    payload = _body(collection, events)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Asset-Viewer-Webhook/1",
    }
    if secret:
        digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        headers["X-Asset-Viewer-Signature"] = f"sha256={digest}"
    request = Request(url, data=payload, headers=headers, method="POST")
    try:
        # URL is normalized by validate_webhook_url() above and restricted to http/https.
        with urlopen(request, timeout=max(0.1, timeout)) as response:  # nosec B310
            status = int(getattr(response, "status", response.getcode()))
            if status < 200 or status >= 300:
                raise RuntimeError(f"webhook returned HTTP {status}")
    except HTTPError as exc:
        raise RuntimeError(f"webhook returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"webhook delivery failed: {exc.reason}") from exc
    return {"delivered": len(events), "status": status}


def _latest_event_id(collection: str | None) -> int:
    payload = recent_review_events(collection, 1)
    return int(payload.get("last_event_id") or 0)


def run_webhook(
    url: str,
    *,
    collection: str | None = None,
    secret: str = "",
    once: bool = False,
    replay: bool = False,
    after_id: int | None = None,
    interval: float = 2.0,
    batch_size: int = 100,
    timeout: float = 10.0,
) -> dict[str, Any]:
    url = validate_webhook_url(url)
    saved = webhook_cursor(url, collection)
    initialized = False
    if after_id is not None:
        cursor = max(0, int(after_id))
    elif saved is not None:
        cursor = saved
    elif replay:
        cursor = 0
    else:
        cursor = _latest_event_id(collection)
        save_webhook_cursor(url, collection, cursor)
        initialized = True

    delivered = 0
    while True:
        feed = review_events_since(collection, cursor, batch_size)
        events = feed["events"]
        if events:
            try:
                deliver_webhook(url, collection, events, secret=secret, timeout=timeout)
            except RuntimeError as exc:
                save_webhook_cursor(url, collection, cursor, last_error=str(exc))
                if once:
                    raise
            else:
                cursor = int(events[-1]["id"])
                delivered += len(events)
                save_webhook_cursor(url, collection, cursor)
        if once:
            return {"initialized": initialized, "delivered": delivered, "last_event_id": cursor}
        time.sleep(max(0.25, interval))


def webhook_status() -> dict[str, Any]:
    state = _read_state()
    endpoints = []
    for key, row in state.items():
        if not isinstance(row, dict):
            continue
        endpoints.append({
            "endpoint_id": key[:12],
            "collection": str(row.get("collection") or "*"),
            "host": str(row.get("host") or ""),
            "last_event_id": int(row.get("last_event_id") or 0),
            "updated_at": row.get("updated_at"),
            "last_error": str(row.get("last_error") or ""),
        })
    endpoints.sort(key=lambda item: (item["host"], item["collection"], item["endpoint_id"]))
    return {"path": str(webhook_state_path()), "endpoints": endpoints}
