import base64
import io
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from PIL import Image

from asset_viewer import storage
from asset_viewer.wsgi import AssetViewerWSGI, create_app


class WsgiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("ASSET_VIEWER_HOME")
        self.old_cache = os.environ.get("ASSET_VIEWER_CACHE")
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.images = Path(self.tmp.name) / "images"
        self.images.mkdir()
        Image.new("RGB", (80, 60), (20, 30, 40)).save(self.images / "sample.png")
        storage.add_collection(str(self.images), "Samples")
        self.app = AssetViewerWSGI({"127.0.0.1", "localhost"}, csrf_token="csrf-test")

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("ASSET_VIEWER_HOME", None)
        else:
            os.environ["ASSET_VIEWER_HOME"] = self.old_home
        if self.old_cache is None:
            os.environ.pop("ASSET_VIEWER_CACHE", None)
        else:
            os.environ["ASSET_VIEWER_CACHE"] = self.old_cache
        self.tmp.cleanup()

    def request(self, method, path, *, query="", host="127.0.0.1:8160", origin=None, body=None, auth=None, app=None):
        raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "8160",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(raw),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "HTTP_HOST": host,
            "CONTENT_LENGTH": str(len(raw)),
        }
        if body is not None:
            environ["CONTENT_TYPE"] = "application/json"
        if origin:
            environ["HTTP_ORIGIN"] = origin
        if auth:
            environ["HTTP_AUTHORIZATION"] = auth
        if method == "POST":
            environ["HTTP_X_ASSET_VIEWER_CSRF"] = "csrf-test"
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        result = (app or self.app)(environ, start_response)
        try:
            payload = b"".join(result)
        finally:
            close = getattr(result, "close", None)
            if close:
                close()
        return int(captured["status"].split()[0]), captured["headers"], payload

    def test_health_uses_waitress_application_contract(self):
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["server"], "waitress")

    def test_untrusted_host_is_rejected(self):
        status, _, _ = self.request("GET", "/", host="attacker.example")
        self.assertEqual(status, 421)

    def test_basic_auth_and_session(self):
        protected = AssetViewerWSGI({"127.0.0.1"}, auth_password="secret", csrf_token="csrf-test")
        status, headers, _ = self.request("GET", "/api/session", app=protected)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))
        token = base64.b64encode(b"asset-viewer:secret").decode()
        status, _, body = self.request("GET", "/api/session", auth=f"Basic {token}", app=protected)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["csrf"], "csrf-test")

    def test_gallery_review_and_manifest_round_trip(self):
        status, _, body = self.request("GET", "/api/gallery", query="collection=samples&refresh=1")
        self.assertEqual(status, 200)
        asset = json.loads(body)["images"][0]
        origin = "http://127.0.0.1:8160"
        status, _, _ = self.request(
            "POST", "/api/review", origin=origin,
            body={"collection": "samples", "asset_id": asset["asset_id"], "status": "approved", "comment": "ship"},
        )
        self.assertEqual(status, 200)
        status, _, body = self.request("GET", "/api/reviews", query="collection=samples")
        self.assertEqual(status, 200)
        item = json.loads(body)["items"][0]
        self.assertEqual(item["status"], "approved")
        self.assertEqual(item["comment"], "ship")
        self.assertRegex(item["review_sha256"], r"^[0-9a-f]{64}$")

    def test_spatial_annotation_round_trip(self):
        status, _, body = self.request("GET", "/api/gallery", query="collection=samples&refresh=1")
        self.assertEqual(status, 200)
        asset = json.loads(body)["images"][0]
        origin = "http://127.0.0.1:8160"
        status, _, body = self.request(
            "POST", "/api/annotation", origin=origin,
            body={"collection": "samples", "asset_id": asset["asset_id"], "action": "create", "kind": "point", "x": 0.25, "y": 0.4, "text": "move this"},
        )
        self.assertEqual(status, 201)
        annotation = json.loads(body)["annotation"]
        status, _, body = self.request(
            "GET", "/api/annotations", query=f"collection=samples&asset_id={asset['asset_id']}"
        )
        self.assertEqual(status, 200)
        items = json.loads(body)["annotations"]
        self.assertEqual(items[0]["annotation_id"], annotation["annotation_id"])
        self.assertEqual(items[0]["text"], "move this")

    def test_variant_family_api_round_trip(self):
        Image.new("RGB", (80, 60), (90, 80, 70)).save(self.images / "sample-v2.png")
        status, _, body = self.request("GET", "/api/gallery", query="collection=samples&refresh=1")
        self.assertEqual(status, 200)
        assets = json.loads(body)["images"]
        ids = [asset["asset_id"] for asset in assets]
        status, _, body = self.request(
            "POST", "/api/family", origin="http://127.0.0.1:8160",
            body={"collection": "samples", "action": "create", "name": "Sample variants", "asset_ids": ids},
        )
        self.assertEqual(status, 201)
        family = json.loads(body)["family"]
        status, _, body = self.request(
            "POST", "/api/family", origin="http://127.0.0.1:8160",
            body={"collection": "samples", "action": "prefer", "family_id": family["family_id"], "asset_id": ids[0]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["family"]["preferred_asset_id"], ids[0])
        status, _, body = self.request("GET", "/api/families", query="collection=samples")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["families"][0]["name"], "Sample variants")
        status, _, body = self.request("GET", "/api/gallery", query="collection=samples")
        self.assertEqual(status, 200)
        gallery = json.loads(body)
        self.assertEqual(len(gallery["families"]), 1)
        self.assertTrue(any(asset["family_preferred"] for asset in gallery["images"]))

    def test_v08_metadata_activity_handoff_and_grouped_gallery(self):
        storage.add_collection(str(self.images), "Samples", group="Whetstone / Brand")
        status, _, body = self.request("GET", "/api/gallery", query="collection=samples&refresh=1")
        self.assertEqual(status, 200)
        gallery = json.loads(body)
        self.assertEqual(gallery["collections"][0]["group"], "Whetstone / Brand")
        asset = gallery["images"][0]
        origin = "http://127.0.0.1:8160"
        status, _, body = self.request(
            "POST", "/api/metadata", origin=origin,
            body={"collection": "samples", "asset": asset["asset_id"], "metadata": {"model": "gpt-image", "run_id": "v08-1"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["metadata"]["model"], "gpt-image")
        self.request(
            "POST", "/api/review", origin=origin,
            body={"collection": "samples", "asset_id": asset["asset_id"], "status": "approved", "comment": "winner"},
        )
        status, _, body = self.request("GET", "/api/activity", query="collection=samples")
        self.assertEqual(status, 200)
        summaries = [event["summary"] for event in json.loads(body)["events"]]
        self.assertTrue(any("Provenance updated" in summary for summary in summaries))
        status, _, body = self.request("GET", "/api/handoff", query="collection=samples")
        self.assertEqual(status, 200)
        handoff = json.loads(body)
        self.assertEqual(handoff["count"], 1)
        self.assertNotIn("source_root", handoff)
        self.assertNotIn("source_path", handoff["items"][0])
        self.assertNotIn(str(self.images.resolve()), body.decode())
        self.assertEqual(handoff["items"][0]["provenance"]["run_id"], "v08-1")

    def test_cross_origin_post_is_rejected(self):
        status, _, _ = self.request(
            "POST", "/api/review", origin="https://attacker.example",
            body={"collection": "samples", "rel": "sample.png", "status": "approved"},
        )
        self.assertEqual(status, 403)

    def test_preview_and_original_headers(self):
        self.request("GET", "/api/gallery", query="collection=samples&refresh=1")
        status, headers, preview = self.request("GET", "/asset/preview/samples/sample.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(headers["Cache-Control"], "private, max-age=86400")
        self.assertGreater(len(preview), 100)
        status, headers, original = self.request("GET", "/asset/file/samples/sample.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertGreater(len(original), 20)

    def test_waitress_latin1_path_info_preserves_utf8_asset_names(self):
        name = "示例花园.png"
        Image.new("RGB", (20, 12), (7, 8, 9)).save(self.images / name)
        self.request("GET", "/api/gallery", query="collection=samples&refresh=1")
        request_path = f"/asset/thumb/samples/{urllib.parse.quote(name)}"
        waitress_path_info = urllib.parse.unquote_to_bytes(request_path).decode("latin-1")
        status, headers, body = self.request("GET", waitress_path_info)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertGreater(len(body), 100)

    def test_human_review_report_route(self):
        self.request("GET", "/api/gallery", query="collection=samples&refresh=1")
        storage.set_review("samples", "sample.png", "approved", comment="ready")
        status, headers, body = self.request("GET", "/report", query="collection=samples&present=1&images=0")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("style-src 'unsafe-inline'", headers["Content-Security-Policy"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn(b"Asset Viewer Review Report", body)
        self.assertIn(b"ready", body)

    def test_unsupported_method_is_405(self):
        status, headers, _ = self.request("PUT", "/api/review")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET, POST")

    def test_create_app_can_share_csrf_token_across_processes(self):
        old = os.environ.get("ASSET_VIEWER_CSRF_TOKEN")
        os.environ["ASSET_VIEWER_CSRF_TOKEN"] = "shared-worker-token"
        try:
            app = create_app("127.0.0.1")
            self.assertEqual(app.csrf_token, "shared-worker-token")
        finally:
            if old is None:
                os.environ.pop("ASSET_VIEWER_CSRF_TOKEN", None)
            else:
                os.environ["ASSET_VIEWER_CSRF_TOKEN"] = old

    def test_non_loopback_app_requires_trusted_host_and_auth(self):
        with self.assertRaises(ValueError):
            create_app("0.0.0.0", [])
        with self.assertRaisesRegex(ValueError, "require ASSET_VIEWER_PASSWORD"):
            create_app("0.0.0.0", ["gallery.example.com"])
        app = create_app("0.0.0.0", ["gallery.example.com"], auth_password="secret")
        self.assertEqual(app.trusted_hosts, {"gallery.example.com"})


if __name__ == "__main__":
    unittest.main()
