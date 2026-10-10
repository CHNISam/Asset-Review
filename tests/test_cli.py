import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from asset_viewer import storage


class CliWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("ASSET_VIEWER_HOME")
        self.old_cache = os.environ.get("ASSET_VIEWER_CACHE")
        self.env = os.environ.copy()
        self.env["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        self.env["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        os.environ["ASSET_VIEWER_HOME"] = self.env["ASSET_VIEWER_HOME"]
        os.environ["ASSET_VIEWER_CACHE"] = self.env["ASSET_VIEWER_CACHE"]
        self.images = Path(self.tmp.name) / "images"
        self.images.mkdir()
        Image.new("RGB", (24, 24), (12, 34, 56)).save(self.images / "a.png")
        Image.new("RGB", (24, 24), (65, 43, 21)).save(self.images / "b.png")
        storage.add_collection(str(self.images), "Images")

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

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "asset_viewer", *args],
            cwd=Path(__file__).resolve().parents[1],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_pending_exit_code_and_completion(self):
        pending = self.run_cli("pending", "--collection", "images", "--json")
        self.assertEqual(pending.returncode, 2)
        payload = json.loads(pending.stdout)
        self.assertTrue(payload["pending"])
        storage.set_reviews_batch("images", ["a.png", "b.png"], "approved")
        complete = self.run_cli("complete", "images")
        self.assertEqual(complete.returncode, 0, complete.stderr)
        pending = self.run_cli("pending", "--collection", "images", "--json", "--no-scan")
        self.assertEqual(pending.returncode, 0)
        self.assertFalse(json.loads(pending.stdout)["pending"])

    def test_capabilities_json(self):
        result = self.run_cli("capabilities", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["protocol_version"], 1)
        self.assertTrue(payload["features"]["stable_asset_ids"])
        self.assertTrue(payload["features"]["event_feed"])

    def test_collection_url(self):
        result = self.run_cli("collection-url", "images", "--base-url", "https://viewer.example.test/base/")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "https://viewer.example.test/base/c/images")

    def test_asset_url_uses_stable_asset_id(self):
        from asset_viewer.app import scan_collection
        scan_collection("images", force=True)
        asset = storage.catalog_records("images")[0]
        result = self.run_cli("asset-url", "images", asset["rel"], "--base-url", "https://viewer.example.test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"https://viewer.example.test/c/images?asset={asset['asset_id']}")

    def test_spatial_annotation_cli(self):
        from asset_viewer.app import scan_collection
        scan_collection("images", force=True)
        asset = next(row for row in storage.catalog_records("images") if row["rel"] == "a.png")
        created = self.run_cli(
            "annotate", "images", asset["asset_id"], "region", "0.1", "0.2", "tighten",
            "--width", "0.3", "--height", "0.4", "--json",
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        annotation = json.loads(created.stdout)
        self.assertEqual(annotation["kind"], "region")
        listed = self.run_cli("annotations", "images", asset["asset_id"], "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["annotations"][0]["text"], "tighten")
        updated = self.run_cli("annotation-update", "images", annotation["annotation_id"], "--resolve", "--json")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertTrue(json.loads(updated.stdout)["resolved"])
        deleted = self.run_cli("annotation-delete", "images", annotation["annotation_id"])
        self.assertEqual(deleted.returncode, 0, deleted.stderr)

    def test_variant_family_cli_round_trip(self):
        from asset_viewer.app import scan_collection
        scan_collection("images", force=True)
        created = self.run_cli("family-create", "images", "Concept family", "a.png", "b.png", "--json")
        self.assertEqual(created.returncode, 0, created.stderr)
        family = json.loads(created.stdout)
        self.assertEqual(len(family["members"]), 2)
        preferred = self.run_cli("family-prefer", "images", family["family_id"], "b.png", "--json")
        self.assertEqual(preferred.returncode, 0, preferred.stderr)
        self.assertEqual(json.loads(preferred.stdout)["preferred_asset_id"], next(row["asset_id"] for row in storage.catalog_records("images") if row["rel"] == "b.png"))
        listed = self.run_cli("families", "images", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["families"][0]["name"], "Concept family")

    def test_events_and_wait_for_review_cli(self):
        storage.set_review("images", "a.png", "approved")
        events = self.run_cli("events", "--collection", "images", "--json")
        self.assertEqual(events.returncode, 0, events.stderr)
        payload = json.loads(events.stdout)
        self.assertEqual(payload["events"][0]["action"], "review")
        pending = self.run_cli("wait-for-review", "--collection", "images", "--timeout", "0", "--no-scan", "--json")
        self.assertEqual(pending.returncode, 2)
        storage.set_review("images", "b.png", "approved")
        storage.complete_collection_review("images")
        done = self.run_cli("wait-for-review", "--collection", "images", "--timeout", "0", "--no-scan", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(json.loads(done.stdout)["pending"])

    def test_cache_status_and_prune_cli(self):
        cache = storage.cache_dir()
        (cache / "thumb-one.jpg").write_bytes(b"x" * 10)
        status = self.run_cli("cache", "status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["files"], 1)
        pruned = self.run_cli("cache", "prune", "--max-mb", "0", "--max-age-days", "0", "--json")
        self.assertEqual(pruned.returncode, 0, pruned.stderr)
        self.assertEqual(json.loads(pruned.stdout)["files"], 1)

    def test_human_report_cli(self):
        from asset_viewer.app import scan_collection
        scan_collection("images", force=True)
        storage.set_review("images", "a.png", "approved", comment="ship")
        output = Path(self.tmp.name) / "review.md"
        result = self.run_cli("report", "--collection", "images", "--format", "markdown", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = output.read_text(encoding="utf-8")
        self.assertIn("Asset Viewer Review Report", text)
        self.assertIn("a.png — Approved", text)

    def test_history_and_undo_cli(self):
        storage.set_review("images", "a.png", "maybe", comment="first")
        storage.set_review("images", "a.png", "approved", comment="second")
        history = self.run_cli("history", "images", "a.png", "--json")
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertEqual(len(json.loads(history.stdout)["events"]), 2)
        undo = self.run_cli("undo", "images", "a.png")
        self.assertEqual(undo.returncode, 0, undo.stderr)
        item = storage.review_manifest("images")["items"][0]
        self.assertEqual(item["status"], "maybe")
        self.assertEqual(item["comment"], "first")


    def test_v08_metadata_activity_and_handoff_cli(self):
        from asset_viewer.app import scan_collection
        scan_collection("images", force=True)
        metadata = self.run_cli(
            "metadata", "images", "a.png", "--set", "source_project=Whetstone",
            "--set", "model=gpt-image", "--set", "run_id=cli-08", "--json",
        )
        self.assertEqual(metadata.returncode, 0, metadata.stderr)
        self.assertEqual(json.loads(metadata.stdout)["run_id"], "cli-08")
        storage.set_review("images", "a.png", "approved", comment="ship")
        activity = self.run_cli("activity", "--collection", "images", "--json")
        self.assertEqual(activity.returncode, 0, activity.stderr)
        self.assertTrue(any("Provenance updated" in event["summary"] for event in json.loads(activity.stdout)["events"]))
        output = Path(self.tmp.name) / "handoff.json"
        handoff = self.run_cli("handoff", "images", "--output", str(output))
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["provenance"]["source_project"], "Whetstone")


if __name__ == "__main__":
    unittest.main()
