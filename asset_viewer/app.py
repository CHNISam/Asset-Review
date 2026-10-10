from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import mimetypes
import multiprocessing
import os
import secrets
import shutil
import time
import threading
import urllib.parse
import warnings
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from . import __version__
from .preview_worker import inspect_images_worker, render_preview_worker, render_video_poster_worker
from .activity import activity_feed
from .handoff import approved_handoff
from .storage import (
    ASSET_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    asset_rel,
    asset_metadata,
    set_asset_metadata,
    annotations_for_asset,
    create_annotation,
    update_annotation,
    delete_annotation,
    catalog_records,
    catalog_state,
    collection_review_state,
    collections,
    complete_collection_review,
    create_family,
    add_family_members,
    remove_family_members,
    set_family_preferred,
    rename_family,
    delete_family,
    families_for_collection,
    file_sha256,
    mark_seen,
    pending_summary,
    prune_preview_cache,
    reconcile_catalog,
    reopen_collection_review,
    review_events_since,
    review_history,
    review_manifest,
    root_for,
    safe_file,
    set_comment,
    set_review,
    set_reviews_batch,
    thumb_path,
    preview_path,
    undo_last_review,
)

STATIC = Path(__file__).resolve().parent / "static"
LOGGER = logging.getLogger("asset_viewer")
BASE_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


MAX_SCAN_FILES = _env_int("ASSET_VIEWER_MAX_SCAN_FILES", 50_000)
MAX_SCAN_SECONDS = _env_int("ASSET_VIEWER_MAX_SCAN_SECONDS", 10)
MAX_REQUEST_BODY_BYTES = 256 * 1024
MAX_REJECTED_BODY_DRAIN_BYTES = MAX_REQUEST_BODY_BYTES + 64 * 1024
SCAN_TTL_SECONDS = _env_int("ASSET_VIEWER_SCAN_TTL_SECONDS", 60)
MAX_THUMBNAIL_SOURCE_BYTES = _env_int("ASSET_VIEWER_MAX_THUMBNAIL_BYTES", 250 * 1024 * 1024)
MAX_VIDEO_PREVIEW_SOURCE_BYTES = _env_int("ASSET_VIEWER_MAX_VIDEO_PREVIEW_BYTES", 2 * 1024 * 1024 * 1024)
MAX_IMAGE_PIXELS = _env_int("ASSET_VIEWER_MAX_IMAGE_PIXELS", 50_000_000)
MAX_THUMBNAIL_CONCURRENCY = _env_int("ASSET_VIEWER_THUMBNAIL_WORKERS", 2)
PREVIEW_PROCESS_TIMEOUT_SECONDS = _env_int("ASSET_VIEWER_PREVIEW_TIMEOUT_SECONDS", 15)
PREVIEW_WORKER_MEMORY_BYTES = _env_int("ASSET_VIEWER_PREVIEW_MEMORY_MB", 768) * 1024 * 1024
METADATA_BATCH_SIZE = _env_int("ASSET_VIEWER_METADATA_BATCH_SIZE", 512)
PREVIEW_CACHE_MAX_BYTES = _env_int("ASSET_VIEWER_CACHE_MAX_MB", 2048) * 1024 * 1024
PREVIEW_CACHE_MAX_AGE_SECONDS = _env_int("ASSET_VIEWER_CACHE_MAX_AGE_DAYS", 30) * 86400
CACHE_PRUNE_INTERVAL_SECONDS = _env_int("ASSET_VIEWER_CACHE_PRUNE_INTERVAL_SECONDS", 300)
THUMBNAIL_SEMAPHORE = threading.BoundedSemaphore(MAX_THUMBNAIL_CONCURRENCY)
_CACHE_PRUNE_LOCK = threading.Lock()
_LAST_CACHE_PRUNE = 0.0
PROCESS_CONTEXT = multiprocessing.get_context("spawn")
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def capability_document() -> dict[str, Any]:
    return {
        "app": "asset-viewer",
        "app_version": __version__,
        "protocol_version": 1,
        "review_statuses": ["approved", "maybe", "rejected"],
        "features": {
            "stable_asset_ids": True,
            "review_fingerprints": True,
            "catalog": True,
            "review_comments": True,
            "review_history": True,
            "undo": True,
            "batch_review": True,
            "compare": True,
            "compare_modes": ["side", "overlay", "difference"],
            "explicit_completion": True,
            "event_feed": True,
            "filesystem_lifecycle_events": True,
            "filesystem_watcher": True,
            "review_report": True,
            "mcp_adapter": True,
            "wait_for_review_cli": True,
            "asset_deep_links": True,
            "spatial_annotations": True,
            "annotation_kinds": ["point", "region"],
            "annotation_events": True,
            "tombstones": True,
            "bounded_review_previews": True,
            "process_isolated_previews": True,
            "production_wsgi_server": True,
            "variant_families": True,
            "family_preferred_member": True,
            "family_events": True,
            "asset_provenance": True,
            "activity_view": True,
            "approved_handoff": True,
            "collection_groups": True,
            "smart_action_menus": True,
            "video_assets": True,
            "video_poster_thumbnails": True,
        },
        "limits": {
            "max_scan_files": MAX_SCAN_FILES,
            "max_scan_seconds": MAX_SCAN_SECONDS,
            "scan_ttl_seconds": SCAN_TTL_SECONDS,
            "max_thumbnail_source_bytes": MAX_THUMBNAIL_SOURCE_BYTES,
            "max_video_preview_source_bytes": MAX_VIDEO_PREVIEW_SOURCE_BYTES,
            "max_image_pixels": MAX_IMAGE_PIXELS,
            "thumbnail_concurrency": MAX_THUMBNAIL_CONCURRENCY,
            "preview_process_timeout_seconds": PREVIEW_PROCESS_TIMEOUT_SECONDS,
            "preview_worker_memory_bytes": PREVIEW_WORKER_MEMORY_BYTES,
            "preview_cache_max_bytes": PREVIEW_CACHE_MAX_BYTES,
            "preview_cache_max_age_seconds": PREVIEW_CACHE_MAX_AGE_SECONDS,
            "max_batch_review": 500,
            "max_comment_chars": 10000,
            "max_annotation_chars": 4000,
        },
    }


def _host_name(value: str) -> str | None:
    try:
        return urllib.parse.urlsplit("//" + value).hostname
    except ValueError:
        return None


def host_is_trusted(value: str | None, trusted_hosts: set[str]) -> bool:
    if not value:
        return False
    host = _host_name(value)
    return bool(host and host.lower().rstrip(".") in trusted_hosts)


def origin_matches_host(origin: str | None, host: str | None) -> bool:
    if not origin:
        return True
    if not host:
        return False
    try:
        return urllib.parse.urlsplit(origin).netloc.lower() == host.lower()
    except ValueError:
        return False


def _run_isolated_worker(target: Any, args: tuple[Any, ...], timeout: float) -> dict[str, Any]:
    receiver, sender = PROCESS_CONTEXT.Pipe(duplex=False)
    process = PROCESS_CONTEXT.Process(target=target, args=(*args, sender), daemon=True)
    try:
        process.start()
        sender.close()
        if receiver.poll(timeout):
            try:
                payload = receiver.recv()
            except EOFError:
                payload = {"ok": False, "error": "worker exited without a result"}
        else:
            payload = {"ok": False, "error": "worker timeout"}
            process.terminate()
        process.join(timeout=1.0)
        if process.is_alive():
            process.kill()
            process.join(timeout=1.0)
        return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid worker response"}
    finally:
        try:
            sender.close()
        except OSError:
            pass
        receiver.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=1.0)


def _inspect_dimensions_isolated(paths: list[Path]) -> dict[str, tuple[int, int, str | None]]:
    results: dict[str, tuple[int, int, str | None]] = {}
    for offset in range(0, len(paths), METADATA_BATCH_SIZE):
        batch = paths[offset:offset + METADATA_BATCH_SIZE]
        payload = _run_isolated_worker(
            inspect_images_worker,
            ([str(path) for path in batch], MAX_THUMBNAIL_SOURCE_BYTES, MAX_IMAGE_PIXELS, PREVIEW_WORKER_MEMORY_BYTES),
            PREVIEW_PROCESS_TIMEOUT_SECONDS,
        )
        worker_results = payload.get("results", {}) if isinstance(payload, dict) else {}
        for path in batch:
            item = worker_results.get(str(path)) if isinstance(worker_results, dict) else None
            if item:
                error = item.get("error")
                if path.suffix.lower() == ".svg" and not error:
                    error = "SVG preview is intentionally raster-placeholder only"
                results[str(path)] = (int(item.get("width") or 0), int(item.get("height") or 0), error)
            else:
                results[str(path)] = (0, 0, "preview metadata unavailable")
        if not payload.get("ok"):
            LOGGER.warning("isolated metadata worker failed: %s", payload.get("error", "unknown error"))
    return results


def _catalog_fresh(state: dict[str, Any]) -> bool:
    value = state.get("last_scan_at")
    if not value:
        return False
    try:
        scanned = datetime.fromisoformat(str(value))
        if scanned.tzinfo is None:
            scanned = scanned.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - scanned).total_seconds()) < SCAN_TTL_SECONDS
    except (TypeError, ValueError):
        return False


def _gallery_rows(slug: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        rel = record["rel"]
        quoted = urllib.parse.quote(rel, safe="/")
        mtime_ns = int(record.get("mtime_ns") or 0)
        media_type = "video" if Path(rel).suffix.lower() in VIDEO_EXTS else "image"
        rows.append({
            "asset_id": record.get("asset_id"),
            "collection": slug,
            "name": Path(rel).name,
            "rel": rel,
            "media_type": media_type,
            "size": int(record.get("size") or 0),
            "mtime": mtime_ns / 1_000_000_000 if mtime_ns else 0,
            "width": int(record.get("width") or 0),
            "height": int(record.get("height") or 0),
            "status": record.get("status") or "",
            "comment": record.get("comment") or "",
            "is_new": record.get("seen_at") is None,
            "first_seen_at": record.get("first_seen_at"),
            "seen_at": record.get("seen_at"),
            "updated_at": record.get("updated_at"),
            "preview_error": record.get("preview_error"),
            "present": bool(record.get("present", 1)),
            "annotation_count": int(record.get("annotation_count") or 0),
            "family_id": record.get("family_id"),
            "family_name": record.get("family_name"),
            "family_preferred": bool(record.get("family_preferred")),
            "provenance": record.get("provenance") or {},
            "thumb": f"/asset/thumb/{slug}/{quoted}",
            "preview": f"/asset/preview/{slug}/{quoted}",
            "file": f"/asset/file/{slug}/{quoted}",
        })
    return rows


def scan_collection(slug: str, force: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = root_for(slug)
    if not root:
        configured = any(row["slug"] == slug for row in collections())
        if not configured:
            return [], {"truncated": False, "reason": None, "elapsed_ms": 0, "generation": 0, "last_scan_at": None, "cached": True}
        state = reconcile_catalog(slug, [], truncated=True, reason="collection_unavailable", elapsed_ms=0)
        return _gallery_rows(slug, catalog_records(slug, present_only=True)), state | {"cached": True}

    current_state = catalog_state(slug)
    if current_state["generation"] > 0 and not force and _catalog_fresh(current_state):
        return _gallery_rows(slug, catalog_records(slug, present_only=True)), current_state | {"cached": True}

    started = time.monotonic()
    existing = catalog_records(slug, present_only=False)
    by_rel = {row["rel"]: row for row in existing}
    by_identity = {
        (int(row["device"]), int(row["inode"])): row
        for row in existing
        if row.get("device") and row.get("inode")
    }
    discoveries: list[dict[str, Any]] = []
    pending_metadata: list[Path] = []
    truncated = False
    reason = None
    visited = 0
    def walk_error(exc: OSError) -> None:
        LOGGER.warning("collection walk error slug=%s error=%s", slug, exc)

    try:
        stop = False
        for directory, _, filenames in os.walk(root, followlinks=False, onerror=walk_error):
            for filename in filenames:
                if time.monotonic() - started > MAX_SCAN_SECONDS:
                    truncated, reason, stop = True, "scan_time_limit", True
                    break
                path = Path(directory) / filename
                try:
                    if path.suffix.lower() not in ASSET_EXTS:
                        continue
                    resolved = path.resolve(strict=True)
                    if resolved == root or root not in resolved.parents or not resolved.is_file():
                        LOGGER.warning("skipping asset that resolves outside collection root: %s", path)
                        continue
                    visited += 1
                    if visited > MAX_SCAN_FILES:
                        truncated, reason, stop = True, "scan_file_limit", True
                        break
                    rel = path.relative_to(root).as_posix()
                    stat = resolved.stat()
                    prior = by_rel.get(rel) or by_identity.get((int(stat.st_dev), int(stat.st_ino)))
                    review_hash_mismatch = False
                    annotation_hash_mismatch = False
                    if prior and prior.get("size") == stat.st_size and prior.get("mtime_ns") == stat.st_mtime_ns:
                        width = prior.get("width") or 0
                        height = prior.get("height") or 0
                        preview_error = prior.get("preview_error")
                        expected_review = prior.get("review_sha256") if prior.get("status") else None
                        expected_annotation = prior.get("annotation_sha256")
                        if expected_review or expected_annotation:
                            observed_hash = file_sha256(resolved)
                            review_hash_mismatch = bool(expected_review and observed_hash != expected_review)
                            annotation_hash_mismatch = bool(expected_annotation and observed_hash != expected_annotation)
                    else:
                        width, height, preview_error = 0, 0, None
                        if path.suffix.lower() in IMAGE_EXTS:
                            pending_metadata.append(resolved)
                    discoveries.append({
                        "rel": rel,
                        "device": int(stat.st_dev),
                        "inode": int(stat.st_ino),
                        "size": int(stat.st_size),
                        "mtime_ns": int(stat.st_mtime_ns),
                        "width": int(width or 0),
                        "height": int(height or 0),
                        "preview_error": preview_error,
                        "review_hash_mismatch": review_hash_mismatch,
                        "annotation_hash_mismatch": annotation_hash_mismatch,
                        "_source": str(resolved),
                    })
                except OSError as exc:
                    LOGGER.warning("skipping unreadable asset %s: %s", path, exc)
            if stop:
                break
    except OSError as exc:
        LOGGER.warning("collection scan failed for %s: %s", slug, exc)
        truncated, reason = True, "scan_error"

    if pending_metadata:
        dimensions = _inspect_dimensions_isolated(pending_metadata)
        for item in discoveries:
            source_text = item.pop("_source", None)
            if source_text and source_text in dimensions and not (item.get("width") or item.get("height")):
                item["width"], item["height"], item["preview_error"] = dimensions[source_text]
    else:
        for item in discoveries:
            item.pop("_source", None)

    elapsed_ms = round((time.monotonic() - started) * 1000)
    state = reconcile_catalog(slug, discoveries, truncated=truncated, reason=reason, elapsed_ms=elapsed_ms)
    rows = _gallery_rows(slug, catalog_records(slug, present_only=True))
    if truncated:
        LOGGER.warning("collection scan truncated slug=%s reason=%s assets=%s elapsed_ms=%s", slug, reason, len(rows), elapsed_ms)
    return rows, state | {"cached": False}


def image_rows(slug: str, force: bool = False) -> list[dict[str, Any]]:
    return scan_collection(slug, force=force)[0]


def _placeholder_preview(path: Path, reason: str, size: tuple[int, int], destination: Path) -> bytes:
    if destination.exists():
        return destination.read_bytes()
    width, height = size
    image = Image.new("RGB", size, (18, 20, 24))
    draw = ImageDraw.Draw(image)
    label = "SVG" if path.suffix.lower() == ".svg" else "PREVIEW"
    inset = max(28, min(width, height) // 12)
    draw.rounded_rectangle((inset, inset, width - inset, height - inset), radius=max(16, inset // 2), outline=(78, 85, 98), width=3)
    draw.text((inset + 28, inset + 28), label, fill=(235, 238, 243))
    draw.text((inset + 28, height // 2 - 14), path.name[:72], fill=(235, 238, 243))
    draw.text((inset + 28, height // 2 + 22), reason[:88], fill=(145, 151, 163))
    image.save(destination, "JPEG", quality=82, optimize=True)
    return destination.read_bytes()


def _maybe_prune_preview_cache() -> None:
    global _LAST_CACHE_PRUNE
    now = time.monotonic()
    if now - _LAST_CACHE_PRUNE < CACHE_PRUNE_INTERVAL_SECONDS:
        return
    if not _CACHE_PRUNE_LOCK.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if now - _LAST_CACHE_PRUNE < CACHE_PRUNE_INTERVAL_SECONDS:
            return
        result = prune_preview_cache(PREVIEW_CACHE_MAX_BYTES, PREVIEW_CACHE_MAX_AGE_SECONDS)
        _LAST_CACHE_PRUNE = now
        if result.get("removed_files"):
            LOGGER.info(
                "preview cache pruned files=%s bytes=%s remaining_bytes=%s",
                result["removed_files"], result["removed_bytes"], result["bytes"],
            )
    finally:
        _CACHE_PRUNE_LOCK.release()


def _render_preview(path: Path, destination: Path, max_size: tuple[int, int]) -> bytes:
    _maybe_prune_preview_cache()
    if destination.exists():
        return destination.read_bytes()
    placeholder_size = (640, 480) if max(max_size) <= 640 else (1280, 900)
    if path.suffix.lower() == ".svg":
        return _placeholder_preview(path, "Safe placeholder — open original downloads the SVG", placeholder_size, destination)
    with THUMBNAIL_SEMAPHORE:
        if destination.exists():
            return destination.read_bytes()
        # Source-byte validation belongs inside the disposable preview worker.
        # Avoid a second parent-process filesystem touch after safe_file() has
        # already established collection containment.
        if path.suffix.lower() in VIDEO_EXTS:
            payload = _run_isolated_worker(
                render_video_poster_worker,
                (str(path), str(destination), max_size, MAX_VIDEO_PREVIEW_SOURCE_BYTES, PREVIEW_WORKER_MEMORY_BYTES),
                max(PREVIEW_PROCESS_TIMEOUT_SECONDS, 25),
            )
        else:
            payload = _run_isolated_worker(
                render_preview_worker,
                (str(path), str(destination), max_size, MAX_THUMBNAIL_SOURCE_BYTES, MAX_IMAGE_PIXELS, PREVIEW_WORKER_MEMORY_BYTES),
                PREVIEW_PROCESS_TIMEOUT_SECONDS,
            )
        if payload.get("ok") and destination.exists():
            return destination.read_bytes()
        LOGGER.warning("isolated preview worker failed for %s: %s", path, payload.get("error", "unknown error"))
        return _placeholder_preview(path, "Preview unavailable: isolated decoder rejected or timed out", placeholder_size, destination)


def make_thumbnail(path: Path) -> bytes:
    return _render_preview(path, thumb_path(path), (640, 480))


def make_review_preview(path: Path) -> bytes:
    return _render_preview(path, preview_path(path), (2048, 2048))


class AssetViewerHandler(BaseHTTPRequestHandler):
    server_version = f"AssetViewer/{__version__}"
    sys_version = ""

    def _trusted_request(self) -> bool:
        trusted = getattr(self.server, "trusted_hosts", LOOPBACK_HOSTS)
        if host_is_trusted(self.headers.get("Host"), trusted):
            return True
        if self.command == "POST":
            self._discard_rejected_body()
        self._send(421, "text/plain; charset=utf-8", "Untrusted Host header")
        return False

    def _authenticated_request(self) -> bool:
        password = getattr(self.server, "auth_password", None)
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
                username, supplied = decoded.split(":", 1)
                if hmac.compare_digest(username, "asset-viewer") and hmac.compare_digest(supplied, password):
                    return True
            except (ValueError, UnicodeDecodeError, binascii.Error):
                pass
        if self.command == "POST":
            self._discard_rejected_body()
        self._send(401, "text/plain; charset=utf-8", "Authentication required", {"WWW-Authenticate": 'Basic realm="Asset Viewer", charset="UTF-8"'})
        return False

    def _guard(self) -> bool:
        return self._trusted_request() and self._authenticated_request()

    def _csrf_valid(self) -> bool:
        token = self.headers.get("X-Asset-Viewer-CSRF", "")
        expected = getattr(self.server, "csrf_token", "")
        if not token or not hmac.compare_digest(token, expected):
            return False
        return origin_matches_host(self.headers.get("Origin"), self.headers.get("Host"))

    def _security_headers(self) -> dict[str, str]:
        return {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": BASE_CSP,
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }

    def _send(self, status: int, content_type: str, body: str | bytes, extra: dict[str, str] | None = None) -> None:
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        headers = self._security_headers()
        headers.update(extra or {})
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        headers = {"Cache-Control": "no-store"}
        headers.update(extra or {})
        self._send(status, "application/json; charset=utf-8", json.dumps(payload, separators=(",", ":")), headers)

    def _send_path(self, status: int, content_type: str, path: Path, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        headers = self._security_headers()
        headers.update(extra or {})
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            # Callers pass packaged static files or a `safe_file()`-validated asset.
            with path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _discard_rejected_body(self, length: int | None = None) -> None:
        """Drain a small rejected POST body so the HTTP response is delivered reliably."""
        try:
            length = int(self.headers.get("Content-Length", "0")) if length is None else length
        except ValueError:
            self.close_connection = True
            return
        if length <= 0:
            return
        if length > MAX_REJECTED_BODY_DRAIN_BYTES:
            self.close_connection = True
            return
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _static(self, name: str, content_type: str) -> None:
        path = STATIC / name
        self._send_path(200, content_type, path, {"Cache-Control": "public, max-age=300"}) if path.is_file() else self._send(404, "text/plain; charset=utf-8", "Not found")

    def _read_json(self, maximum: int = MAX_REQUEST_BODY_BYTES) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > maximum:
            self._discard_rejected_body(length)
            raise ValueError("request too large")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._discard_rejected_body(length)
            raise ValueError("content type must be application/json")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def do_GET(self) -> None:
        if not self._trusted_request():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            return self._send_json(200, {"ok": True, "service": "asset-viewer", "version": __version__})
        if not self._authenticated_request():
            return
        if path in ("/", "/index.html") or path.startswith("/c/"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/app.css":
            return self._static("app.css", "text/css; charset=utf-8")
        if path == "/app.js":
            return self._static("app.js", "application/javascript; charset=utf-8")
        if path == "/api/session":
            return self._send_json(200, {"csrf": getattr(self.server, "csrf_token", ""), "version": __version__})
        if path == "/api/capabilities":
            return self._send_json(200, capability_document())
        if path == "/api/gallery":
            rows = collections()
            query = urllib.parse.parse_qs(parsed.query)
            active = (query.get("collection") or [rows[0]["slug"] if rows else ""])[0]
            if active and not any(row["slug"] == active for row in rows):
                return self._send_json(404, {"error": "collection not found"})
            public_rows = [{"slug": row["slug"], "label": row["label"], "group": row.get("group", ""), "available": bool(row.get("available", True))} for row in rows]
            force_scan = (query.get("refresh") or [""])[0].lower() in {"1", "true", "yes"}
            images, scan = scan_collection(active, force=force_scan) if active else ([], {"truncated": False, "reason": None, "elapsed_ms": 0, "cached": True})
            review_state = collection_review_state(active) if active else None
            families = families_for_collection(active, include_missing=False) if active else []
            return self._send_json(200, {"collections": public_rows, "active": active, "images": images, "families": families, "scan": scan, "review_state": review_state})
        if path == "/report":
            query = urllib.parse.parse_qs(parsed.query)
            collection = (query.get("collection") or [None])[0]
            status = (query.get("status") or [None])[0]
            present_only = (query.get("present") or ["1"])[0].lower() in {"1", "true", "yes"}
            embed_images = (query.get("images") or ["1"])[0].lower() not in {"0", "false", "no"}
            force_scan = (query.get("refresh") or [""])[0].lower() in {"1", "true", "yes"}
            if collection and not any(row["slug"] == collection for row in collections()):
                return self._send_json(404, {"error": "collection not found"})
            try:
                if force_scan:
                    if collection:
                        scan_collection(collection, force=True)
                    else:
                        for row in collections():
                            scan_collection(row["slug"], force=True)
                from .report import render_review_report
                report = render_review_report(
                    review_manifest(collection, status, include_missing=not present_only),
                    format="html", embed_images=embed_images,
                )
                return self._send(200, "text/html; charset=utf-8", report, {"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
        if path == "/api/reviews":
            query = urllib.parse.parse_qs(parsed.query)
            collection = (query.get("collection") or [None])[0]
            status = (query.get("status") or [None])[0]
            present_only = (query.get("present") or [""])[0].lower() in {"1", "true", "yes"}
            try:
                return self._send_json(200, review_manifest(collection, status, include_missing=not present_only))
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
        if path == "/api/activity":
            query = urllib.parse.parse_qs(parsed.query)
            collection = (query.get("collection") or [None])[0]
            try:
                after_id = int((query.get("after") or ["0"])[0])
                limit = int((query.get("limit") or ["200"])[0])
                return self._send_json(200, activity_feed(collection, after_id, limit))
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
        if path == "/api/handoff":
            query = urllib.parse.parse_qs(parsed.query)
            collection = (query.get("collection") or [""])[0]
            if not collection:
                return self._send_json(400, {"error": "collection is required"})
            try:
                return self._send_json(200, approved_handoff(collection))
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
        if path == "/api/metadata":
            query = urllib.parse.parse_qs(parsed.query)
            collection = (query.get("collection") or [""])[0]
            asset = (query.get("asset") or query.get("asset_id") or [""])[0]
            if not collection or not asset:
                return self._send_json(400, {"error": "collection and asset are required"})
            try:
                return self._send_json(200, asset_metadata(collection, asset))
            except ValueError as exc:
                return self._send_json(404, {"error": str(exc)})
        if path == "/api/events":
            query = urllib.parse.parse_qs(parsed.query)
            collection = (query.get("collection") or [None])[0]
            try:
                after_id = int((query.get("after") or ["0"])[0])
                limit = int((query.get("limit") or ["100"])[0])
                return self._send_json(200, review_events_since(collection, after_id, limit))
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
        if path == "/api/pending":
            query = urllib.parse.parse_qs(parsed.query)
            collection = (query.get("collection") or [None])[0]
            try:
                return self._send_json(200, pending_summary(collection))
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
        if path == "/api/families":
            query = urllib.parse.parse_qs(parsed.query)
            collection = (query.get("collection") or [""])[0]
            if not collection:
                return self._send_json(400, {"error": "collection is required"})
            if not any(row["slug"] == collection for row in collections()):
                return self._send_json(404, {"error": "collection not found"})
            include_missing = (query.get("present") or [""])[0].lower() not in {"1", "true", "yes"}
            return self._send_json(200, {"version": 1, "collection": collection, "families": families_for_collection(collection, include_missing=include_missing)})
        if path == "/api/annotations":
            query = urllib.parse.parse_qs(parsed.query)
            collection = (query.get("collection") or [""])[0]
            asset_id = (query.get("asset_id") or [""])[0]
            include_stale = (query.get("stale") or ["1"])[0].lower() not in {"0", "false", "no"}
            if not collection or not asset_id:
                return self._send_json(400, {"error": "collection and asset_id are required"})
            if not any(row["slug"] == collection for row in collections()):
                return self._send_json(404, {"error": "collection not found"})
            return self._send_json(200, {
                "version": 1, "collection": collection, "asset_id": asset_id,
                "annotations": annotations_for_asset(collection, asset_id, include_stale=include_stale),
            })
        if path == "/api/review-history":
            query = urllib.parse.parse_qs(parsed.query)
            collection = (query.get("collection") or [""])[0]
            rel = (query.get("rel") or [""])[0]
            asset_id = (query.get("asset_id") or [""])[0]
            if asset_id and collection and not rel:
                rel = asset_rel(collection, asset_id) or ""
            if not root_for(collection) or not rel:
                return self._send_json(400, {"error": "collection plus rel or asset_id are required"})
            return self._send_json(200, {"collection": collection, "rel": rel, "events": review_history(collection, rel)})
        if path.startswith("/asset/file/") or path.startswith("/asset/thumb/") or path.startswith("/asset/preview/"):
            parts = path.split("/", 4)
            if len(parts) != 5:
                return self._send(404, "text/plain; charset=utf-8", "Not found")
            kind, slug, relative = parts[2], parts[3], urllib.parse.unquote(parts[4])
            source = safe_file(slug, relative)
            if not source:
                return self._send(404, "text/plain; charset=utf-8", "Not found")
            if kind == "thumb":
                return self._send(200, "image/jpeg", make_thumbnail(source), {"Cache-Control": "private, max-age=86400"})
            if kind == "preview":
                return self._send(200, "image/jpeg", make_review_preview(source), {"Cache-Control": "private, max-age=86400"})
            if source.suffix.lower() == ".svg":
                filename = urllib.parse.quote(source.name, safe="")
                return self._send_path(200, "application/octet-stream", source, {"Cache-Control": "private, max-age=3600", "Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})
            content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            return self._send_path(200, content_type, source, {"Cache-Control": "private, max-age=3600"})
        return self._send(404, "text/plain; charset=utf-8", "Not found")

    def _payload_rel(self, slug: str, payload: dict[str, Any]) -> str:
        rel = str(payload.get("rel", ""))
        if not rel and payload.get("asset_id"):
            rel = asset_rel(slug, str(payload.get("asset_id"))) or ""
        return rel

    def do_POST(self) -> None:
        if not self._guard():
            return
        if not self._csrf_valid():
            self._discard_rejected_body()
            return self._send_json(403, {"error": "CSRF validation failed"})
        path = urllib.parse.urlparse(self.path).path
        try:
            payload = self._read_json()
            slug = str(payload.get("collection", ""))
            if not root_for(slug):
                raise ValueError("collection not found")
            if path == "/api/review":
                if isinstance(payload.get("rels"), list):
                    rels = [str(rel) for rel in payload["rels"]]
                    if not rels or len(rels) > 500:
                        raise ValueError("batch must contain 1-500 assets")
                    if any(not safe_file(slug, rel) for rel in rels):
                        raise ValueError("one or more assets are invalid")
                    status = str(payload.get("status", ""))
                    changed = set_reviews_batch(slug, rels, status)
                    return self._send_json(200, {"ok": True, "changed": changed, "status": status})
                rel = self._payload_rel(slug, payload)
                if not safe_file(slug, rel):
                    raise ValueError("asset not found")
                status = str(payload.get("status", ""))
                comment = payload.get("comment")
                if comment is not None and not isinstance(comment, str):
                    raise ValueError("comment must be a string")
                set_review(slug, rel, status, comment)
                return self._send_json(200, {"ok": True, "status": status})
            if path == "/api/metadata":
                asset = str(payload.get("asset_id") or payload.get("asset") or payload.get("rel") or "")
                metadata = payload.get("metadata")
                if not asset or not isinstance(metadata, dict):
                    raise ValueError("asset and metadata object are required")
                return self._send_json(200, {"ok": True, "metadata": set_asset_metadata(slug, asset, metadata)})
            if path == "/api/comment":
                rel = self._payload_rel(slug, payload)
                comment = payload.get("comment", "")
                if not safe_file(slug, rel) or not isinstance(comment, str):
                    raise ValueError("invalid comment request")
                set_comment(slug, rel, comment)
                return self._send_json(200, {"ok": True})
            if path == "/api/seen":
                rels = payload.get("rels")
                if not isinstance(rels, list):
                    rels = [payload.get("rel")]
                rels = [str(rel) for rel in rels if rel]
                if not rels or len(rels) > 500 or any(not safe_file(slug, rel) for rel in rels):
                    raise ValueError("invalid seen request")
                changed = mark_seen(slug, rels)
                return self._send_json(200, {"ok": True, "changed": changed})
            if path == "/api/family":
                action = str(payload.get("action", "create"))
                family_id = str(payload.get("family_id", ""))
                asset_ids = payload.get("asset_ids")
                if asset_ids is None and payload.get("asset_id"):
                    asset_ids = [payload.get("asset_id")]
                if asset_ids is not None and (not isinstance(asset_ids, list) or len(asset_ids) > 500):
                    raise ValueError("asset_ids must be a list of at most 500 IDs")
                ids = [str(asset_id) for asset_id in (asset_ids or []) if asset_id]
                if action == "create":
                    family = create_family(slug, str(payload.get("name", "")), ids, str(payload.get("preferred_asset_id")) if payload.get("preferred_asset_id") else None)
                    return self._send_json(201, {"ok": True, "family": family})
                if not family_id:
                    raise ValueError("family_id is required")
                if action == "add":
                    family = add_family_members(slug, family_id, ids)
                elif action == "remove":
                    family = remove_family_members(slug, family_id, ids)
                elif action == "prefer":
                    preferred = payload.get("asset_id")
                    family = set_family_preferred(slug, family_id, str(preferred) if preferred else None)
                elif action == "rename":
                    family = rename_family(slug, family_id, str(payload.get("name", "")))
                elif action == "delete":
                    family = delete_family(slug, family_id)
                    return self._send_json(200, {"ok": True, "deleted": family})
                else:
                    raise ValueError("family action must be create, add, remove, prefer, rename, or delete")
                return self._send_json(200, {"ok": True, "family": family})
            if path == "/api/annotation":
                asset_id = str(payload.get("asset_id", ""))
                action = str(payload.get("action", "create"))
                if action == "create":
                    rel = asset_rel(slug, asset_id)
                    if not rel or not safe_file(slug, rel):
                        raise ValueError("asset not found")
                    annotation = create_annotation(
                        slug, asset_id, str(payload.get("kind", "point")),
                        payload.get("x"), payload.get("y"),
                        w=payload.get("w", 0), h=payload.get("h", 0), text=str(payload.get("text", "")),
                    )
                    return self._send_json(201, {"ok": True, "annotation": annotation})
                annotation_id = str(payload.get("annotation_id", ""))
                if not annotation_id:
                    raise ValueError("annotation_id is required")
                if action == "update":
                    text = payload.get("text")
                    if text is not None and not isinstance(text, str):
                        raise ValueError("annotation text must be a string")
                    resolved = payload.get("resolved")
                    if resolved is not None and not isinstance(resolved, bool):
                        raise ValueError("resolved must be boolean")
                    annotation = update_annotation(slug, annotation_id, text=text, resolved=resolved)
                    return self._send_json(200, {"ok": True, "annotation": annotation})
                if action == "delete":
                    annotation = delete_annotation(slug, annotation_id)
                    return self._send_json(200, {"ok": True, "annotation": annotation})
                raise ValueError("annotation action must be create, update, or delete")
            if path == "/api/complete":
                state = complete_collection_review(slug)
                return self._send_json(200, {"ok": True, "review_state": state})
            if path == "/api/reopen":
                state = reopen_collection_review(slug)
                return self._send_json(200, {"ok": True, "review_state": state})
            if path == "/api/undo":
                rel = self._payload_rel(slug, payload)
                if not safe_file(slug, rel):
                    raise ValueError("asset not found")
                result = undo_last_review(slug, rel)
                if not result:
                    raise ValueError("no review change to undo")
                return self._send_json(200, {"ok": True, "result": result})
            return self._send_json(404, {"error": "not found"})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            LOGGER.warning("bad request path=%s client=%s error=%s", path, self.client_address[0], exc)
            return self._send_json(400, {"ok": False, "error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.client_address[0], fmt % args)


def serve(
    host: str = "127.0.0.1",
    port: int = 8160,
    trusted_hosts: list[str] | None = None,
    auth_password: str | None = None,
    log_level: str = "INFO",
    allow_unauthenticated_remote: bool = False,
    watch: bool = True,
    watch_debounce: float = 0.75,
    watch_reconcile_interval: float = 60.0,
) -> None:
    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    normalized = {h.lower().rstrip(".") for h in (trusted_hosts or []) if h}
    effective_password = auth_password or os.environ.get("ASSET_VIEWER_PASSWORD")
    if host in LOOPBACK_HOSTS:
        normalized |= LOOPBACK_HOSTS
    else:
        if not normalized:
            raise ValueError("non-loopback binds require at least one --trusted-host")
        if not effective_password and not allow_unauthenticated_remote:
            raise ValueError(
                "non-loopback binds require ASSET_VIEWER_PASSWORD/--auth-password equivalent; "
                "use --allow-unauthenticated-remote only behind a trusted private/authenticated boundary"
            )
    server = ThreadingHTTPServer((host, port), AssetViewerHandler)
    server.trusted_hosts = normalized
    server.csrf_token = secrets.token_urlsafe(32)
    server.auth_password = effective_password
    watcher = None
    if watch:
        from .watcher import start_collection_watcher
        watcher = start_collection_watcher(
            scan_collection, debounce_seconds=watch_debounce, reconcile_interval=watch_reconcile_interval
        )
    LOGGER.info(
        "Asset Viewer %s running at http://%s:%s auth=%s watcher=%s",
        __version__, host, port, "enabled" if server.auth_password else "disabled", "enabled" if watcher else "disabled",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if watcher:
            watcher.stop()
        server.server_close()
