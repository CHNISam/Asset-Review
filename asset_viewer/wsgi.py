from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import mimetypes
import os
import secrets
import urllib.parse
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Iterable

from waitress import serve as waitress_serve

from . import __version__
from .activity import activity_feed
from .handoff import approved_handoff
from .audio import AUDIO_EXTS, peaks_for
from . import production_api
from .app import (
    BASE_CSP,
    LOOPBACK_HOSTS,
    byte_range,
    STATIC,
    capability_document,
    host_is_trusted,
    make_review_preview,
    make_thumbnail,
    origin_matches_host,
    scan_collection,
)
from .storage import (
    asset_rel,
    asset_metadata,
    set_asset_metadata,
    annotations_for_asset,
    create_annotation,
    delete_annotation,
    update_annotation,
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
    mark_seen,
    pending_summary,
    reopen_collection_review,
    review_events_since,
    review_history,
    review_manifest,
    root_for,
    safe_file,
    set_comment,
    set_review,
    set_reviews_batch,
    undo_last_review,
)

LOGGER = logging.getLogger("asset_viewer")
StartResponse = Callable[[str, list[tuple[str, str]]], Any]


def security_headers() -> list[tuple[str, str]]:
    return [
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Frame-Options", "DENY"),
        ("Content-Security-Policy", BASE_CSP),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ]


def _status_line(status: int) -> str:
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "Unknown"
    return f"{status} {phrase}"


def _normalize_wsgi_path(path: str) -> str:
    """Recover UTF-8 URL bytes that WSGI servers expose as Latin-1 text."""
    try:
        return path.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return path


def _header(environ: dict[str, Any], name: str) -> str:
    key = "HTTP_" + name.upper().replace("-", "_")
    if name.lower() == "content-type":
        return str(environ.get("CONTENT_TYPE") or "")
    if name.lower() == "content-length":
        return str(environ.get("CONTENT_LENGTH") or "")
    return str(environ.get(key) or "")


def _bytes_response(
    start_response: StartResponse,
    status: int,
    content_type: str,
    body: str | bytes,
    extra: dict[str, str] | None = None,
) -> list[bytes]:
    data = body if isinstance(body, bytes) else body.encode("utf-8")
    headers = {"Content-Type": content_type, "Content-Length": str(len(data))}
    headers.update(dict(security_headers()))
    headers.update(extra or {})
    start_response(_status_line(status), list(headers.items()))
    return [data]


def _json_response(
    start_response: StartResponse,
    status: int,
    payload: Any,
    extra: dict[str, str] | None = None,
) -> list[bytes]:
    headers = {"Cache-Control": "no-store"}
    headers.update(extra or {})
    return _bytes_response(
        start_response,
        status,
        "application/json; charset=utf-8",
        json.dumps(payload, separators=(",", ":")),
        headers,
    )


def _file_response(
    environ: dict[str, Any],
    start_response: StartResponse,
    path: Path,
    content_type: str,
    extra: dict[str, str] | None = None,
) -> Iterable[bytes]:
    headers = [("Content-Type", content_type), ("Content-Length", str(path.stat().st_size)), *security_headers()]
    headers.extend((extra or {}).items())
    start_response("200 OK", headers)
    handle = path.open("rb")
    wrapper = environ.get("wsgi.file_wrapper")
    if wrapper:
        return wrapper(handle, 1024 * 1024)

    def chunks() -> Iterable[bytes]:
        try:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            handle.close()

    return chunks()


def _media_response(environ: dict[str, Any], start_response: StartResponse, path: Path, content_type: str, extra: dict[str, str]) -> Iterable[bytes]:
    """Originals with Range support so audio/video can seek and A/B at the same time code."""
    size = path.stat().st_size
    span = byte_range(_header(environ, "Range") or None, size)
    if span is False:
        return _bytes_response(start_response, 416, "text/plain; charset=utf-8", "Range not satisfiable", {"Content-Range": f"bytes */{size}"})
    if span is None:
        return _file_response(environ, start_response, path, content_type, extra | {"Accept-Ranges": "bytes"})
    start, end = span
    headers = [("Content-Type", content_type), ("Content-Length", str(end - start + 1)), *security_headers()]
    headers.extend((extra | {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{size}"}).items())
    start_response("206 Partial Content", headers)

    def chunks() -> Iterable[bytes]:
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return chunks()


class AssetViewerWSGI:
    """WSGI application used by the production Waitress serving path."""

    def __init__(
        self,
        trusted_hosts: set[str],
        auth_password: str | None = None,
        csrf_token: str | None = None,
    ) -> None:
        self.trusted_hosts = {host.lower().rstrip(".") for host in trusted_hosts}
        self.auth_password = auth_password
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)

    def _trusted(self, environ: dict[str, Any]) -> bool:
        return host_is_trusted(_header(environ, "Host"), self.trusted_hosts)

    def _authenticated(self, environ: dict[str, Any]) -> bool:
        if not self.auth_password:
            return True
        header = _header(environ, "Authorization")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            username, supplied = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        return hmac.compare_digest(username, "asset-viewer") and hmac.compare_digest(supplied, self.auth_password)

    def _csrf_valid(self, environ: dict[str, Any]) -> bool:
        token = _header(environ, "X-Asset-Viewer-CSRF")
        if not token or not hmac.compare_digest(token, self.csrf_token):
            return False
        return origin_matches_host(_header(environ, "Origin"), _header(environ, "Host"))

    @staticmethod
    def _read_json(environ: dict[str, Any], maximum: int = 256 * 1024) -> dict[str, Any]:
        raw_length = str(environ.get("CONTENT_LENGTH") or "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > maximum:
            raise ValueError("request too large")
        content_type = _header(environ, "Content-Type").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("content type must be application/json")
        stream = environ.get("wsgi.input")
        raw = stream.read(length) if stream is not None else b""
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    @staticmethod
    def _payload_rel(slug: str, payload: dict[str, Any]) -> str:
        rel = str(payload.get("rel", ""))
        if not rel and payload.get("asset_id"):
            rel = asset_rel(slug, str(payload.get("asset_id"))) or ""
        return rel

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        if not self._trusted(environ):
            return _bytes_response(start_response, 421, "text/plain; charset=utf-8", "Untrusted Host header")

        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = _normalize_wsgi_path(str(environ.get("PATH_INFO") or "/"))
        query = urllib.parse.parse_qs(str(environ.get("QUERY_STRING") or ""))

        if method == "GET" and path == "/api/health":
            return _json_response(start_response, 200, {"ok": True, "service": "asset-viewer", "version": __version__, "server": "waitress"})

        if not self._authenticated(environ):
            return _bytes_response(
                start_response,
                401,
                "text/plain; charset=utf-8",
                "Authentication required",
                {"WWW-Authenticate": 'Basic realm="Asset Viewer", charset="UTF-8"'},
            )

        if method == "GET":
            return self._get(environ, start_response, path, query)
        if method == "POST":
            if not self._csrf_valid(environ):
                return _json_response(start_response, 403, {"error": "CSRF validation failed"})
            return self._post(environ, start_response, path)
        return _bytes_response(start_response, 405, "text/plain; charset=utf-8", "Method not allowed", {"Allow": "GET, POST"})

    def _get(
        self,
        environ: dict[str, Any],
        start_response: StartResponse,
        path: str,
        query: dict[str, list[str]],
    ) -> Iterable[bytes]:
        if path in ("/", "/index.html") or path.startswith("/c/"):
            return self._static(environ, start_response, "index.html", "text/html; charset=utf-8")
        if path == "/app.css":
            return self._static(environ, start_response, "app.css", "text/css; charset=utf-8")
        if path == "/app.js":
            return self._static(environ, start_response, "app.js", "application/javascript; charset=utf-8")
        if path == "/studio.js":
            return self._static(environ, start_response, "studio.js", "application/javascript; charset=utf-8")
        if path == "/audio.js":
            return self._static(environ, start_response, "audio.js", "application/javascript; charset=utf-8")
        production = production_api.handle_get(path, str(environ.get("QUERY_STRING") or ""))
        if production is not None:
            return _json_response(start_response, *production)
        if path == "/api/session":
            return _json_response(start_response, 200, {"csrf": self.csrf_token, "version": __version__})
        if path == "/api/capabilities":
            payload = capability_document() | {"production_server": "waitress"}
            return _json_response(start_response, 200, payload)
        if path == "/api/gallery":
            rows = collections()
            active = (query.get("collection") or [rows[0]["slug"] if rows else ""])[0]
            if active and not any(row["slug"] == active for row in rows):
                return _json_response(start_response, 404, {"error": "collection not found"})
            public_rows = [{"slug": row["slug"], "label": row["label"], "group": row.get("group", ""), "available": bool(row.get("available", True))} for row in rows]
            force_scan = (query.get("refresh") or [""])[0].lower() in {"1", "true", "yes"}
            images, scan = scan_collection(active, force=force_scan) if active else ([], {"truncated": False, "reason": None, "elapsed_ms": 0, "cached": True})
            review_state = collection_review_state(active) if active else None
            families = families_for_collection(active, include_missing=False) if active else []
            return _json_response(start_response, 200, {"collections": public_rows, "active": active, "images": images, "families": families, "scan": scan, "review_state": review_state})
        if path == "/report":
            collection = (query.get("collection") or [None])[0]
            status = (query.get("status") or [None])[0]
            present_only = (query.get("present") or ["1"])[0].lower() in {"1", "true", "yes"}
            embed_images = (query.get("images") or ["1"])[0].lower() not in {"0", "false", "no"}
            force_scan = (query.get("refresh") or [""])[0].lower() in {"1", "true", "yes"}
            if collection and not any(row["slug"] == collection for row in collections()):
                return _json_response(start_response, 404, {"error": "collection not found"})
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
                return _bytes_response(
                    start_response, 200, "text/html; charset=utf-8", report, {"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"}
                )
            except ValueError as exc:
                return _json_response(start_response, 400, {"error": str(exc)})
        if path == "/api/reviews":
            collection = (query.get("collection") or [None])[0]
            status = (query.get("status") or [None])[0]
            present_only = (query.get("present") or [""])[0].lower() in {"1", "true", "yes"}
            try:
                return _json_response(start_response, 200, review_manifest(collection, status, include_missing=not present_only))
            except ValueError as exc:
                return _json_response(start_response, 400, {"error": str(exc)})
        if path == "/api/activity":
            collection = (query.get("collection") or [None])[0]
            try:
                after_id = int((query.get("after") or ["0"])[0])
                limit = int((query.get("limit") or ["200"])[0])
                return _json_response(start_response, 200, activity_feed(collection, after_id, limit))
            except ValueError as exc:
                return _json_response(start_response, 400, {"error": str(exc)})
        if path == "/api/handoff":
            collection = (query.get("collection") or [""])[0]
            if not collection:
                return _json_response(start_response, 400, {"error": "collection is required"})
            try:
                return _json_response(start_response, 200, approved_handoff(collection))
            except ValueError as exc:
                return _json_response(start_response, 400, {"error": str(exc)})
        if path == "/api/metadata":
            collection = (query.get("collection") or [""])[0]
            asset = (query.get("asset") or query.get("asset_id") or [""])[0]
            if not collection or not asset:
                return _json_response(start_response, 400, {"error": "collection and asset are required"})
            try:
                return _json_response(start_response, 200, asset_metadata(collection, asset))
            except ValueError as exc:
                return _json_response(start_response, 404, {"error": str(exc)})
        if path == "/api/events":
            collection = (query.get("collection") or [None])[0]
            try:
                after_id = int((query.get("after") or ["0"])[0])
                limit = int((query.get("limit") or ["100"])[0])
                return _json_response(start_response, 200, review_events_since(collection, after_id, limit))
            except ValueError as exc:
                return _json_response(start_response, 400, {"error": str(exc)})
        if path == "/api/pending":
            collection = (query.get("collection") or [None])[0]
            try:
                return _json_response(start_response, 200, pending_summary(collection))
            except ValueError as exc:
                return _json_response(start_response, 400, {"error": str(exc)})
        if path == "/api/families":
            collection = (query.get("collection") or [""])[0]
            if not collection:
                return _json_response(start_response, 400, {"error": "collection is required"})
            if not any(row["slug"] == collection for row in collections()):
                return _json_response(start_response, 404, {"error": "collection not found"})
            include_missing = (query.get("present") or [""])[0].lower() not in {"1", "true", "yes"}
            return _json_response(start_response, 200, {"version": 1, "collection": collection, "families": families_for_collection(collection, include_missing=include_missing)})
        if path == "/api/annotations":
            collection = (query.get("collection") or [""])[0]
            asset_id = (query.get("asset_id") or [""])[0]
            include_stale = (query.get("stale") or ["1"])[0].lower() not in {"0", "false", "no"}
            if not collection or not asset_id:
                return _json_response(start_response, 400, {"error": "collection and asset_id are required"})
            if not any(row["slug"] == collection for row in collections()):
                return _json_response(start_response, 404, {"error": "collection not found"})
            return _json_response(start_response, 200, {
                "version": 1, "collection": collection, "asset_id": asset_id,
                "annotations": annotations_for_asset(collection, asset_id, include_stale=include_stale),
            })
        if path == "/api/review-history":
            collection = (query.get("collection") or [""])[0]
            rel = (query.get("rel") or [""])[0]
            asset_id = (query.get("asset_id") or [""])[0]
            if asset_id and collection and not rel:
                rel = asset_rel(collection, asset_id) or ""
            if not root_for(collection) or not rel:
                return _json_response(start_response, 400, {"error": "collection plus rel or asset_id are required"})
            return _json_response(start_response, 200, {"collection": collection, "rel": rel, "events": review_history(collection, rel)})
        if path.startswith(("/asset/file/", "/asset/thumb/", "/asset/preview/", "/asset/peaks/")):
            parts = path.split("/", 4)
            if len(parts) != 5:
                return _bytes_response(start_response, 404, "text/plain; charset=utf-8", "Not found")
            kind, slug, relative = parts[2], parts[3], urllib.parse.unquote(parts[4])
            source = safe_file(slug, relative)
            if not source:
                return _bytes_response(start_response, 404, "text/plain; charset=utf-8", "Not found")
            if kind == "peaks":
                if source.suffix.lower() not in AUDIO_EXTS:
                    return _json_response(start_response, 404, {"error": "not audio"})
                return _json_response(start_response, 200, peaks_for(source), {"Cache-Control": "private, max-age=86400"})
            if kind == "thumb":
                return _bytes_response(start_response, 200, "image/jpeg", make_thumbnail(source), {"Cache-Control": "private, max-age=86400"})
            if kind == "preview":
                return _bytes_response(start_response, 200, "image/jpeg", make_review_preview(source), {"Cache-Control": "private, max-age=86400"})
            if source.suffix.lower() == ".svg":
                filename = urllib.parse.quote(source.name, safe="")
                return _file_response(environ, start_response, source, "application/octet-stream", {"Cache-Control": "private, max-age=3600", "Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})
            content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            return _media_response(environ, start_response, source, content_type, {"Cache-Control": "private, max-age=3600"})
        return _bytes_response(start_response, 404, "text/plain; charset=utf-8", "Not found")

    def _static(self, environ: dict[str, Any], start_response: StartResponse, name: str, content_type: str) -> Iterable[bytes]:
        path = STATIC / name
        if not path.is_file():
            return _bytes_response(start_response, 404, "text/plain; charset=utf-8", "Not found")
        return _file_response(environ, start_response, path, content_type, {"Cache-Control": "public, max-age=300"})

    def _post(self, environ: dict[str, Any], start_response: StartResponse, path: str) -> Iterable[bytes]:
        if path == production_api.PREFIX + "upload":
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > production_api.production.MAX_UPLOAD_BYTES:
                return _json_response(start_response, 400, {"ok": False, "error": "upload size is invalid or too large"})
            data = environ["wsgi.input"].read(length)
            result = production_api.handle_upload(path, str(environ.get("QUERY_STRING") or ""), _header(environ, "X-Filename"), data)
            return _json_response(start_response, *result)
        try:
            payload = self._read_json(environ)
            production = production_api.handle_post(path, payload)
            if production is not None:
                return _json_response(start_response, *production)
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
                    return _json_response(start_response, 200, {"ok": True, "changed": changed, "status": status})
                rel = self._payload_rel(slug, payload)
                if not safe_file(slug, rel):
                    raise ValueError("asset not found")
                status = str(payload.get("status", ""))
                comment = payload.get("comment")
                if comment is not None and not isinstance(comment, str):
                    raise ValueError("comment must be a string")
                set_review(slug, rel, status, comment)
                return _json_response(start_response, 200, {"ok": True, "status": status})
            if path == "/api/metadata":
                asset = str(payload.get("asset_id") or payload.get("asset") or payload.get("rel") or "")
                metadata = payload.get("metadata")
                if not asset or not isinstance(metadata, dict):
                    raise ValueError("asset and metadata object are required")
                return _json_response(start_response, 200, {"ok": True, "metadata": set_asset_metadata(slug, asset, metadata)})
            if path == "/api/comment":
                rel = self._payload_rel(slug, payload)
                comment = payload.get("comment", "")
                if not safe_file(slug, rel) or not isinstance(comment, str):
                    raise ValueError("invalid comment request")
                set_comment(slug, rel, comment)
                return _json_response(start_response, 200, {"ok": True})
            if path == "/api/seen":
                rels = payload.get("rels")
                if not isinstance(rels, list):
                    rels = [payload.get("rel")]
                rels = [str(rel) for rel in rels if rel]
                if not rels or len(rels) > 500 or any(not safe_file(slug, rel) for rel in rels):
                    raise ValueError("invalid seen request")
                changed = mark_seen(slug, rels)
                return _json_response(start_response, 200, {"ok": True, "changed": changed})
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
                    return _json_response(start_response, 201, {"ok": True, "family": family})
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
                    return _json_response(start_response, 200, {"ok": True, "deleted": family})
                else:
                    raise ValueError("family action must be create, add, remove, prefer, rename, or delete")
                return _json_response(start_response, 200, {"ok": True, "family": family})
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
                    return _json_response(start_response, 201, {"ok": True, "annotation": annotation})
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
                    return _json_response(start_response, 200, {"ok": True, "annotation": annotation})
                if action == "delete":
                    annotation = delete_annotation(slug, annotation_id)
                    return _json_response(start_response, 200, {"ok": True, "annotation": annotation})
                raise ValueError("annotation action must be create, update, or delete")
            if path == "/api/complete":
                state = complete_collection_review(slug)
                return _json_response(start_response, 200, {"ok": True, "review_state": state})
            if path == "/api/reopen":
                state = reopen_collection_review(slug)
                return _json_response(start_response, 200, {"ok": True, "review_state": state})
            if path == "/api/undo":
                rel = self._payload_rel(slug, payload)
                if not safe_file(slug, rel):
                    raise ValueError("asset not found")
                result = undo_last_review(slug, rel)
                if not result:
                    raise ValueError("no review change to undo")
                return _json_response(start_response, 200, {"ok": True, "result": result})
            return _json_response(start_response, 404, {"error": "not found"})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            LOGGER.warning("bad WSGI request path=%s error=%s", path, exc)
            return _json_response(start_response, 400, {"ok": False, "error": str(exc)})


def _server_config(
    host: str,
    trusted_hosts: list[str] | None,
    auth_password: str | None,
    allow_unauthenticated_remote: bool,
) -> tuple[set[str], str | None]:
    normalized = {h.lower().rstrip(".") for h in (trusted_hosts or []) if h}
    effective_password = auth_password or os.environ.get("ASSET_VIEWER_PASSWORD")
    if host in LOOPBACK_HOSTS:
        normalized |= LOOPBACK_HOSTS
    else:
        if not normalized:
            raise ValueError("non-loopback binds require at least one --trusted-host")
        if not effective_password and not allow_unauthenticated_remote:
            raise ValueError(
                "non-loopback binds require ASSET_VIEWER_PASSWORD; use --allow-unauthenticated-remote "
                "only behind a trusted private/authenticated boundary"
            )
    return normalized, effective_password


def create_app(
    host: str = "127.0.0.1",
    trusted_hosts: list[str] | None = None,
    auth_password: str | None = None,
    allow_unauthenticated_remote: bool = False,
    csrf_token: str | None = None,
) -> AssetViewerWSGI:
    normalized, effective_password = _server_config(host, trusted_hosts, auth_password, allow_unauthenticated_remote)
    shared_csrf_token = csrf_token or os.environ.get("ASSET_VIEWER_CSRF_TOKEN")
    return AssetViewerWSGI(normalized, effective_password, shared_csrf_token)


def serve(
    host: str = "127.0.0.1",
    port: int = 8160,
    trusted_hosts: list[str] | None = None,
    auth_password: str | None = None,
    log_level: str = "INFO",
    allow_unauthenticated_remote: bool = False,
    threads: int = 6,
    watch: bool = True,
    watch_debounce: float = 0.75,
    watch_reconcile_interval: float = 60.0,
) -> None:
    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = create_app(host, trusted_hosts, auth_password, allow_unauthenticated_remote)
    watcher = None
    if watch:
        from .watcher import start_collection_watcher
        watcher = start_collection_watcher(
            scan_collection, debounce_seconds=watch_debounce, reconcile_interval=watch_reconcile_interval
        )
    LOGGER.info(
        "Asset Viewer %s running with Waitress at http://%s:%s auth=%s watcher=%s",
        __version__, host, port, "enabled" if app.auth_password else "disabled", "enabled" if watcher else "disabled",
    )
    try:
        waitress_serve(
            app,
            host=host,
            port=port,
            threads=max(1, min(int(threads), 64)),
            clear_untrusted_proxy_headers=True,
            expose_tracebacks=False,
        )
    finally:
        if watcher:
            watcher.stop()
