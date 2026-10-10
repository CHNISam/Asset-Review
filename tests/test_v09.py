import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import unittest
from pathlib import Path

from PIL import Image, PngImagePlugin

from asset_viewer import storage
from asset_viewer.metadata_adapters import import_collection_metadata
from asset_viewer.project_config import load_project_config
from asset_viewer.project_sync import sync_project
from asset_viewer.webhook import run_webhook, webhook_cursor, webhook_status


class V09WebhookValidationTests(unittest.TestCase):
    def test_webhook_rejects_non_http_schemes(self):
        from asset_viewer.webhook import validate_webhook_url

        for value in ("file:///etc/passwd", "ftp://example.com/events", "javascript:alert(1)"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "absolute http:// or https://"):
                    validate_webhook_url(value)


class V09ProjectIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("ASSET_VIEWER_HOME")
        self.old_cache = os.environ.get("ASSET_VIEWER_CACHE")
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.project = Path(self.tmp.name) / "project"
        self.renders = self.project / "artifacts" / "renders"
        self.renders.mkdir(parents=True)
        self.config = self.project / ".asset-viewer.toml"
        self.config.write_text(
            '''version = 1
project = "Demo Project"
group = "Demo"

[metadata]
agent = "test-agent"

[[collections]]
path = "artifacts/renders"
label = "Generated Renders"
adapters = ["png_text", "sidecar"]
'''
        )

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
    def test_project_config_is_strict_and_project_relative(self):
        payload = load_project_config(self.project)
        self.assertEqual(payload["project"], "Demo Project")
        self.assertEqual(payload["collections"][0]["relative_path"], "artifacts/renders")
        self.assertEqual(payload["collections"][0]["group"], "Demo")
        self.assertEqual(payload["collections"][0]["metadata"]["source_project"], "Demo Project")
        self.assertTrue(payload["collections"][0]["available"])

        bad = self.project / "bad"
        bad.mkdir()
        (bad / ".asset-viewer.toml").write_text(
            'version = 1\nproject = "Bad"\n[[collections]]\npath = "../escape"\n'
        )
        with self.assertRaisesRegex(ValueError, "project-relative"):
            load_project_config(bad)

    def test_project_sync_registers_folder_and_imports_sidecar_metadata(self):
        image = self.renders / "concept.png"
        Image.new("RGB", (20, 20), (1, 2, 3)).save(image)
        sidecar = image.with_name(image.name + ".asset-viewer.json")
        sidecar.write_text(json.dumps({
            "version": 1,
            "provenance": {"tool": "imagegen", "model": "gpt-image", "run_id": "run-9"},
            "metadata": {"brief": "hero", "temperature": 0.7},
        }))

        payload = sync_project(self.project)
        row = payload["collections"][0]
        self.assertTrue(row["registered"])
        self.assertEqual(row["collection"], "generated-renders")
        self.assertEqual(row["metadata"]["imported"], 1)
        collection = storage.collections()[0]
        self.assertEqual(collection["group"], "Demo")
        asset = storage.catalog_records("generated-renders")[0]
        provenance = asset["provenance"]
        self.assertEqual(provenance["source_project"], "Demo Project")
        self.assertEqual(provenance["agent"], "test-agent")
        self.assertEqual(provenance["model"], "gpt-image")
        self.assertEqual(provenance["extra"]["brief"], "hero")
    def test_png_text_adapter_imports_common_generation_metadata(self):
        image = self.renders / "a1111.png"
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text(
            "parameters",
            "A blue fox in a snow field\nNegative prompt: blurry\nSteps: 28, Sampler: Euler, Seed: 4242, Model: fox-xl",
        )
        Image.new("RGB", (24, 24), (4, 5, 6)).save(image, pnginfo=pnginfo)
        storage.add_collection(str(self.renders), "Renders")
        from asset_viewer.app import scan_collection
        scan_collection("renders", force=True)

        result = import_collection_metadata("renders", ["png_text"], {"source_project": "Demo"})
        self.assertEqual(result["imported"], 1)
        asset = storage.catalog_records("renders")[0]
        provenance = asset["provenance"]
        self.assertEqual(provenance["tool"], "automatic1111")
        self.assertEqual(provenance["prompt"], "A blue fox in a snow field")
        self.assertEqual(provenance["seed"], "4242")
        self.assertEqual(provenance["model"], "fox-xl")
        self.assertIn("Steps: 28", provenance["extra"]["automatic1111_parameters"])

    def test_config_discovery_and_resync_are_idempotent(self):
        image = self.renders / "concept.png"
        Image.new("RGB", (20, 20), (1, 2, 3)).save(image)
        nested = self.renders / "nested"
        nested.mkdir()
        first = sync_project(nested)
        second = sync_project(nested)
        self.assertEqual(first["collections"][0]["collection"], second["collections"][0]["collection"])
        self.assertEqual(len(storage.collections()), 1)

    def test_project_sync_cli_round_trip(self):
        image = self.renders / "cli.png"
        Image.new("RGB", (20, 20), (7, 8, 9)).save(image)
        env = os.environ.copy()
        result = subprocess.run(
            [sys.executable, "-m", "asset_viewer", "project-sync", str(self.project), "--json"],
            cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["project"], "Demo Project")
        self.assertTrue(payload["collections"][0]["registered"])

    def test_invalid_sidecar_is_reported_without_aborting_collection(self):
        image = self.renders / "bad.png"
        Image.new("RGB", (20, 20), (1, 1, 1)).save(image)
        image.with_name(image.name + ".asset-viewer.json").write_text("not json")
        storage.add_collection(str(self.renders), "Renders")
        from asset_viewer.app import scan_collection
        scan_collection("renders", force=True)
        result = import_collection_metadata("renders", ["sidecar"], {"source_project": "Demo"})
        self.assertEqual(result["imported"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("invalid metadata sidecar", result["errors"][0]["error"])

    def test_webhook_cursor_advances_only_after_successful_delivery(self):
        image = self.renders / "hook.png"
        Image.new("RGB", (20, 20), (2, 3, 4)).save(image)
        storage.add_collection(str(self.renders), "Hooks")
        from asset_viewer.app import scan_collection
        scan_collection("hooks", force=True)
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append((self.headers.get("X-Asset-Viewer-Signature"), json.loads(self.rfile.read(length))))
                self.send_response(204)
                self.end_headers()
            def log_message(self, format, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/events"
        try:
            first = run_webhook(url, collection="hooks", once=True, secret="secret")
            self.assertTrue(first["initialized"])
            self.assertEqual(first["delivered"], 0)
            storage.set_review("hooks", "hook.png", "approved")
            second = run_webhook(url, collection="hooks", once=True, secret="secret")
            self.assertEqual(second["delivered"], 1)
            self.assertEqual(webhook_cursor(url, "hooks"), second["last_event_id"])
            status = webhook_status()["endpoints"][0]
            self.assertEqual(status["host"], "127.0.0.1")
            self.assertNotIn(str(server.server_port), status["host"])
            self.assertEqual(status["last_error"], "")
            self.assertEqual(received[0][1]["events"][0]["action"], "review")
            self.assertTrue(received[0][0].startswith("sha256="))
        finally:
            server.shutdown()
            server.server_close()

    def test_webhook_failure_does_not_advance_cursor(self):
        image = self.renders / "fail.png"
        Image.new("RGB", (20, 20), (5, 6, 7)).save(image)
        storage.add_collection(str(self.renders), "Failures")
        from asset_viewer.app import scan_collection
        scan_collection("failures", force=True)
        storage.set_review("failures", "fail.png", "maybe")
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(500)
                self.end_headers()
            def log_message(self, format, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/events"
        try:
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                run_webhook(url, collection="failures", once=True, replay=True)
            self.assertEqual(webhook_cursor(url, "failures"), 0)
            failed = next(item for item in webhook_status()["endpoints"] if item["collection"] == "failures")
            self.assertIn("HTTP 500", failed["last_error"])
            self.assertNotIn(url, json.dumps(webhook_status()))
        finally:
            server.shutdown()
            server.server_close()

    def test_project_config_rejects_unknown_contract_keys(self):
        self.config.write_text(
            'version = 1\nproject = "Demo"\nmystery = true\n[[collections]]\npath = "artifacts/renders"\n'
        )
        with self.assertRaisesRegex(ValueError, "unsupported project config key"):
            load_project_config(self.project)

    def test_sidecar_symlink_is_rejected(self):
        image = self.renders / "linked.png"
        Image.new("RGB", (20, 20), (9, 9, 9)).save(image)
        outside = self.project / "outside.json"
        outside.write_text(json.dumps({"version": 1, "provenance": {"model": "secret"}}))
        try:
            image.with_name(image.name + ".asset-viewer.json").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        storage.add_collection(str(self.renders), "Linked")
        from asset_viewer.app import scan_collection
        scan_collection("linked", force=True)
        result = import_collection_metadata("linked", ["sidecar"])
        self.assertEqual(result["imported"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("must not be a symlink", result["errors"][0]["error"])

    def test_project_sync_dry_run_never_registers(self):
        payload = sync_project(self.project, dry_run=True)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["collections"][0]["registered"])
        self.assertEqual(storage.collections(), [])


if __name__ == "__main__":
    unittest.main()
