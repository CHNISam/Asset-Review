import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

from asset_viewer.app import AssetViewerHandler, ThreadingHTTPServer
from asset_viewer.storage import add_collection


class HttpSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.images = Path(self.tmp.name) / "images"
        self.images.mkdir()
        Image.new("RGB", (32, 32), (1, 2, 3)).save(self.images / "sample.png")
        Image.new("RGB", (32, 32), (4, 5, 6)).save(self.images / "second.png")
        (self.images / ".env").write_text("TOP_SECRET=1")
        add_collection(str(self.images), "Samples")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AssetViewerHandler)
        self.server.trusted_hosts = {"127.0.0.1", "localhost", "::1"}
        self.server.csrf_token = "test-csrf-token"
        self.server.auth_password = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        os.environ.pop("ASSET_VIEWER_HOME", None)
        os.environ.pop("ASSET_VIEWER_CACHE", None)
        self.tmp.cleanup()

    def request(self, method, path, *, host=None, origin=None, body=None, csrf=None, authorization=None, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin is not None:
            headers["Origin"] = origin
        if body is not None:
            headers["Content-Type"] = content_type
        if csrf is not None:
            headers["X-Asset-Viewer-CSRF"] = csrf
        if authorization is not None:
            headers["Authorization"] = authorization
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        headers_out = dict(response.getheaders())
        conn.close()
        return response.status, headers_out, data

    def same_origin(self):
        return f"http://127.0.0.1:{self.port}"

    def test_encoded_traversal_is_rejected(self):
        outside = Path(self.tmp.name) / "outside.png"
        Image.new("RGB", (12, 12), (9, 9, 9)).save(outside)
        status, _, _ = self.request("GET", "/asset/file/samples/%2e%2e/outside.png")
        self.assertEqual(status, 404)

    def test_malformed_image_gets_inert_preview_instead_of_crashing(self):
        bad = self.images / "broken.png"
        bad.write_bytes(b"not really a png")
        self.request("GET", "/api/gallery?collection=samples&refresh=1")
        status, headers, body = self.request("GET", "/asset/preview/samples/broken.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")
        import io
        with Image.open(io.BytesIO(body)) as preview:
            self.assertEqual(preview.format, "JPEG")

    def test_non_image_cannot_be_fetched_by_guessed_url(self):
        status, _, body = self.request("GET", "/asset/file/samples/.env")
        self.assertEqual(status, 404)
        self.assertNotIn(b"TOP_SECRET", body)

    def test_host_header_is_enforced(self):
        status, _, _ = self.request("GET", "/api/health", host="attacker.example")
        self.assertEqual(status, 421)

    def test_review_rejects_cross_origin(self):
        payload = json.dumps({"collection": "samples", "rel": "sample.png", "status": "approved"})
        status, _, _ = self.request("POST", "/api/review", origin="https://attacker.example", body=payload, csrf="test-csrf-token")
        self.assertEqual(status, 403)

    def test_review_accepts_same_origin(self):
        payload = json.dumps({"collection": "samples", "rel": "sample.png", "status": "approved", "comment": "ship it"})
        status, _, body = self.request("POST", "/api/review", origin=self.same_origin(), body=payload, csrf="test-csrf-token")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_review_rejects_missing_csrf(self):
        payload = json.dumps({"collection": "samples", "rel": "sample.png", "status": "approved"})
        status, _, _ = self.request("POST", "/api/review", origin=self.same_origin(), body=payload)
        self.assertEqual(status, 403)

    def test_review_rejects_oversized_request(self):
        origin = f"http://127.0.0.1:{self.port}"
        oversized = b"{" + (b" " * (256 * 1024 + 1)) + b"}"
        status, _, _ = self.request(
            "POST", "/api/review", origin=origin, body=oversized, csrf="test-csrf-token"
        )
        self.assertEqual(status, 400)

    def test_review_rejects_wrong_content_type(self):
        payload = json.dumps({"collection": "samples", "rel": "sample.png", "status": "approved"})
        status, _, _ = self.request("POST", "/api/review", origin=self.same_origin(), body=payload, csrf="test-csrf-token", content_type="text/plain")
        self.assertEqual(status, 400)

    def test_batch_review_and_manifest_api(self):
        self.request("GET", "/api/gallery?collection=samples")
        payload = json.dumps({"collection": "samples", "rels": ["sample.png", "second.png"], "status": "maybe"})
        status, _, body = self.request("POST", "/api/review", origin=self.same_origin(), body=payload, csrf="test-csrf-token")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["changed"], 2)
        status, _, body = self.request("GET", "/api/reviews?collection=samples&status=maybe")
        manifest = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(manifest["counts"]["total"], 2)

    def test_seen_endpoint_changes_new_state(self):
        status, _, body = self.request("GET", "/api/gallery?collection=samples")
        self.assertEqual(status, 200)
        gallery = json.loads(body)
        self.assertTrue(all(item["is_new"] for item in gallery["images"]))
        payload = json.dumps({"collection": "samples", "rel": "sample.png"})
        status, _, _ = self.request("POST", "/api/seen", origin=self.same_origin(), body=payload, csrf="test-csrf-token")
        self.assertEqual(status, 200)
        _, _, body = self.request("GET", "/api/gallery?collection=samples")
        gallery = json.loads(body)
        sample = next(item for item in gallery["images"] if item["rel"] == "sample.png")
        self.assertFalse(sample["is_new"])

    def test_session_exposes_csrf_only_after_auth_guard(self):
        status, _, body = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["csrf"], "test-csrf-token")

    def test_optional_basic_auth(self):
        self.server.auth_password = "secret"
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))
        token = base64.b64encode(b"asset-viewer:secret").decode()
        status, _, _ = self.request("GET", "/", authorization=f"Basic {token}")
        self.assertEqual(status, 200)

    def test_stable_collection_url_serves_app(self):
        status, _, body = self.request("GET", "/c/samples")
        self.assertEqual(status, 200)
        self.assertIn(b"Asset Review", body)

    def test_svg_is_not_rendered_inline_and_thumb_is_raster(self):
        (self.images / "active.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
        status, headers, body = self.request("GET", "/asset/file/samples/active.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/octet-stream")
        self.assertTrue(headers.get("Content-Disposition", "").startswith("attachment;"))
        self.assertIn(b"<script>", body)
        status, headers, body = self.request("GET", "/asset/thumb/samples/active.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")
        self.assertNotIn(b"<script>", body)
        self.assertTrue(body.startswith(b"\xff\xd8"))

    def test_capabilities_endpoint(self):
        status, _, body = self.request("GET", "/api/capabilities")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["protocol_version"], 1)
        self.assertTrue(payload["features"]["asset_deep_links"])

    def test_gallery_includes_collection_review_state(self):
        status, _, body = self.request("GET", "/api/gallery?collection=samples")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["review_state"]["pending"])
        self.assertEqual(payload["review_state"]["unreviewed"], 2)

    def test_complete_reopen_and_pending_endpoints(self):
        self.request("GET", "/api/gallery?collection=samples")
        payload = json.dumps({"collection": "samples"})
        status, _, body = self.request("POST", "/api/complete", origin=self.same_origin(), body=payload, csrf="test-csrf-token")
        self.assertEqual(status, 400)
        self.assertIn("unreviewed", json.loads(body)["error"])
        batch = json.dumps({"collection": "samples", "rels": ["sample.png", "second.png"], "status": "approved"})
        status, _, _ = self.request("POST", "/api/review", origin=self.same_origin(), body=batch, csrf="test-csrf-token")
        self.assertEqual(status, 200)
        status, _, body = self.request("POST", "/api/complete", origin=self.same_origin(), body=payload, csrf="test-csrf-token")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["review_state"]["complete"])
        status, _, body = self.request("GET", "/api/pending?collection=samples")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["pending"])
        status, _, body = self.request("POST", "/api/reopen", origin=self.same_origin(), body=payload, csrf="test-csrf-token")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["review_state"]["pending"])

    def test_review_history_and_undo_endpoints(self):
        self.request("GET", "/api/gallery?collection=samples")
        first = json.dumps({"collection": "samples", "rel": "sample.png", "status": "maybe", "comment": "first"})
        second = json.dumps({"collection": "samples", "rel": "sample.png", "status": "approved", "comment": "ship"})
        self.assertEqual(self.request("POST", "/api/review", origin=self.same_origin(), body=first, csrf="test-csrf-token")[0], 200)
        self.assertEqual(self.request("POST", "/api/review", origin=self.same_origin(), body=second, csrf="test-csrf-token")[0], 200)
        status, _, body = self.request("GET", "/api/review-history?collection=samples&rel=sample.png")
        self.assertEqual(status, 200)
        history = json.loads(body)["events"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["new_status"], "approved")
        undo = json.dumps({"collection": "samples", "rel": "sample.png"})
        status, _, body = self.request("POST", "/api/undo", origin=self.same_origin(), body=undo, csrf="test-csrf-token")
        self.assertEqual(status, 200)
        result = json.loads(body)["result"]
        self.assertEqual(result["status"], "maybe")
        self.assertEqual(result["comment"], "first")

    def test_review_can_target_stable_asset_id_and_events_are_readable(self):
        status, _, body = self.request("GET", "/api/gallery?collection=samples&refresh=1")
        self.assertEqual(status, 200)
        gallery = json.loads(body)
        sample = next(item for item in gallery["images"] if item["rel"] == "sample.png")
        payload = json.dumps({"collection": "samples", "asset_id": sample["asset_id"], "status": "approved", "comment": "stable"})
        status, _, _ = self.request("POST", "/api/review", origin=self.same_origin(), body=payload, csrf="test-csrf-token")
        self.assertEqual(status, 200)
        status, _, body = self.request("GET", "/api/events?collection=samples&after=0")
        self.assertEqual(status, 200)
        events = json.loads(body)["events"]
        self.assertTrue(any(event["asset_id"] == sample["asset_id"] and event["action"] == "review" for event in events))

    def test_spatial_annotation_api_and_manifest(self):
        status, _, body = self.request("GET", "/api/gallery?collection=samples&refresh=1")
        self.assertEqual(status, 200)
        gallery = json.loads(body)
        sample = next(item for item in gallery["images"] if item["rel"] == "sample.png")
        create = json.dumps({
            "collection": "samples", "asset_id": sample["asset_id"], "action": "create",
            "kind": "region", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "text": "Tighten this area",
        })
        status, _, body = self.request(
            "POST", "/api/annotation", origin=self.same_origin(), body=create, csrf="test-csrf-token"
        )
        self.assertEqual(status, 201)
        annotation = json.loads(body)["annotation"]
        self.assertEqual(annotation["kind"], "region")
        self.assertEqual(annotation["text"], "Tighten this area")

        status, _, body = self.request(
            "GET", f"/api/annotations?collection=samples&asset_id={sample['asset_id']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["annotations"]), 1)

        update = json.dumps({
            "collection": "samples", "action": "update",
            "annotation_id": annotation["annotation_id"], "resolved": True, "text": "Fixed",
        })
        status, _, body = self.request(
            "POST", "/api/annotation", origin=self.same_origin(), body=update, csrf="test-csrf-token"
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["annotation"]["resolved"])

        status, _, body = self.request("GET", "/api/reviews?collection=samples")
        self.assertEqual(status, 200)
        item = next(item for item in json.loads(body)["items"] if item["asset_id"] == sample["asset_id"])
        self.assertEqual(item["annotations"][0]["annotation_id"], annotation["annotation_id"])

        delete = json.dumps({
            "collection": "samples", "action": "delete", "annotation_id": annotation["annotation_id"],
        })
        status, _, _ = self.request(
            "POST", "/api/annotation", origin=self.same_origin(), body=delete, csrf="test-csrf-token"
        )
        self.assertEqual(status, 200)

    def test_annotation_api_rejects_invalid_geometry_and_missing_csrf(self):
        _, _, body = self.request("GET", "/api/gallery?collection=samples&refresh=1")
        sample = next(item for item in json.loads(body)["images"] if item["rel"] == "sample.png")
        payload = json.dumps({
            "collection": "samples", "asset_id": sample["asset_id"], "action": "create",
            "kind": "region", "x": 0.9, "y": 0.9, "w": 0.3, "h": 0.3,
        })
        status, _, _ = self.request("POST", "/api/annotation", origin=self.same_origin(), body=payload)
        self.assertEqual(status, 403)
        status, _, body = self.request(
            "POST", "/api/annotation", origin=self.same_origin(), body=payload, csrf="test-csrf-token"
        )
        self.assertEqual(status, 400)
        self.assertIn("outside", json.loads(body)["error"])

    def test_review_preview_is_bounded_and_private(self):
        large = self.images / "large.png"
        Image.new("RGB", (3000, 2200), (4, 5, 6)).save(large)
        self.request("GET", "/api/gallery?collection=samples&refresh=1")
        status, headers, body = self.request("GET", "/asset/preview/samples/large.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")
        self.assertEqual(headers.get("Cache-Control"), "private, max-age=86400")
        import io
        with Image.open(io.BytesIO(body)) as preview:
            self.assertLessEqual(max(preview.size), 2048)

    def test_thumbnail_response_is_private_cache(self):
        self.request("GET", "/api/gallery?collection=samples&refresh=1")
        status, headers, _ = self.request("GET", "/asset/thumb/samples/sample.png")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Cache-Control", "").startswith("private"))

    def test_security_headers_present(self):
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""))
        self.assertEqual(headers.get("Cross-Origin-Opener-Policy"), "same-origin")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")


if __name__ == "__main__":
    unittest.main()
