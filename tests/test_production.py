import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from asset_viewer import production
from asset_viewer.app import scan_collection
from asset_viewer.storage import add_collection, set_review


def image(path: Path, color, size=(640, 420)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.root = Path(self.tmp.name) / "art"
        self.hall = self.root / "music-hall"
        image(self.hall / "design" / "front" / "front_v1.png", (200, 80, 60))
        image(self.hall / "design" / "front" / "front_v2.png", (190, 120, 70))
        image(self.hall / "design" / "front" / "front_v3.png", (170, 150, 90))
        image(self.hall / "design" / "side_v1.png", (60, 90, 160))
        image(self.hall / "design" / "top" / "top_v1.png", (90, 140, 80))
        image(self.hall / "design" / "top" / "top_v2_tmp.png", (90, 140, 81))
        image(self.hall / "design" / "top" / "top_thumb_v3.png", (10, 10, 10), size=(120, 90))
        add_collection(str(self.root), "Art", "Nameless Reach / Music Hall / Design")
        scan_collection("art", force=True)
        self.asset = production.create_asset(
            "Nameless Reach", "音乐厅", key="music-hall", scene="Music Hall", kind="environment",
            views=[{"key": "front"}, {"key": "side"}, {"key": "top"}, {"key": "detail", "required": False}],
            source_collection="art", source_prefix="music-hall",
        )

    def tearDown(self):
        os.environ.pop("ASSET_VIEWER_HOME", None)
        os.environ.pop("ASSET_VIEWER_CACHE", None)
        self.tmp.cleanup()

    def revisions(self, detail, view):
        return next(item for item in detail["views"] if item["view"] == view)["revisions"]

    def test_discovery_numbers_revisions_and_culls_obvious_drafts(self):
        production.discover("music-hall")
        detail = production.asset_detail("music-hall")
        self.assertEqual([r["label"] for r in self.revisions(detail, "front")], ["v1", "v2", "v3"])
        self.assertEqual([r["label"] for r in self.revisions(detail, "side")], ["v1"])
        top = {r["label"]: r for r in self.revisions(detail, "top")}
        self.assertEqual(top["v1"]["state"], "candidate")
        self.assertEqual(top["v2"]["state"], "culled")
        self.assertEqual(top["v2"]["reason"], "临时草稿")
        self.assertEqual(top["v3"]["state"], "culled")
        self.assertIn("分辨率过低", top["v3"]["reason"])
        self.assertEqual(production.discover("music-hall")["added"], [])
        self.assertEqual([d["type"] for d in detail["decisions"]], ["select", "select", "select"])
        self.assertTrue(detail["next"]["owner"])
        self.assertEqual(detail["gates"]["visual"]["state"], "progress")

    def test_select_lock_and_agent_reads_verified_design(self):
        production.discover("music-hall")
        detail = production.asset_detail("music-hall")
        with self.assertRaises(ValueError):
            production.lock_design("music-hall")
        for view, label in (("front", "v3"), ("side", "v1"), ("top", "v1")):
            revision = next(r for r in self.revisions(detail, view) if r["label"] == label)
            detail = production.select_revision("music-hall", view, revision["revision_id"])
        self.assertEqual(detail["decisions"][-1]["type"], "lock")
        detail = production.lock_design("music-hall", "first full set")
        self.assertEqual(detail["lock"]["name"], "D1")
        self.assertEqual(detail["gates"]["visual"]["state"], "approved")
        self.assertEqual(detail["decisions"], [])
        lock = production.design_lock("music-hall")
        self.assertTrue(lock["locked"])
        self.assertTrue(lock["verified"])
        self.assertEqual({v["view"]: v["revision"] for v in lock["views"]}, {"front": "v3", "side": "v1", "top": "v1"})
        self.assertTrue(Path(lock["views"][0]["path"]).is_file())
        json.dumps(lock)
        with self.assertRaises(ValueError):
            production.lock_design("music-hall")
        # Changing bytes under a lock is reported, never silently trusted.
        image(self.hall / "design" / "side_v1.png", (1, 2, 3))
        stale = production.design_lock("music-hall")
        self.assertFalse(stale["verified"])
        self.assertIn("side: bytes changed since lock", stale["problems"])

    def test_new_revision_after_selection_is_surfaced_and_relock_supersedes(self):
        production.discover("music-hall")
        detail = production.asset_detail("music-hall")
        for view in ("front", "side", "top"):
            first = self.revisions(detail, view)[0]
            detail = production.select_revision("music-hall", view, first["revision_id"])
        production.lock_design("music-hall")
        image(self.hall / "design" / "side" / "side_v2.png", (70, 100, 170))
        scan_collection("art", force=True)
        production.discover("music-hall")
        detail = production.asset_detail("music-hall")
        side = next(v for v in detail["views"] if v["view"] == "side")
        self.assertEqual(side["status"], "updated")
        self.assertIn("v2", detail["next"]["text"])
        newest = self.revisions(detail, "side")[-1]
        production.select_revision("music-hall", "side", newest["revision_id"])
        detail = production.lock_design("music-hall")
        self.assertEqual(detail["lock"]["name"], "D2")
        self.assertEqual([lock["superseded_at"] is None for lock in detail["locks"]], [False, True])

    def test_stage_gates_keep_progress_and_acceptance_separate(self):
        image(self.hall / "model" / "clay.png", (128, 128, 128))
        image(self.hall / "engine" / "godot_capture.png", (40, 50, 70))
        scan_collection("art", force=True)
        production.discover("music-hall")
        detail = production.asset_detail("music-hall")
        self.assertEqual(next(s for s in detail["stages"] if s["stage"] == "model")["status"], "wip")
        with self.assertRaises(ValueError):
            production.set_stage("music-hall", "model", status="accepted", actor="agent")
        with self.assertRaises(ValueError):
            production.set_stage("music-hall", "design", status="ready", actor="agent")
        with self.assertRaises(ValueError):
            production.set_stage("music-hall", "uv", status="accepted", actor="owner")  # no evidence yet
        detail = production.set_stage("music-hall", "model", status="ready", summary="12k tris", next_step="等待验收", actor="agent")
        self.assertIn("验收「建模」", [d["text"] for d in detail["decisions"]])
        detail = production.set_stage("music-hall", "model", status="accepted", actor="owner")
        self.assertEqual(next(s for s in detail["stages"] if s["stage"] == "model")["status"], "accepted")
        with self.assertRaises(ValueError):
            production.set_stage("music-hall", "model", status="wip", actor="agent")
        self.assertEqual(detail["gates"]["tech"]["state"], "progress")
        production.set_stage("music-hall", "engine", status="ready", actor="agent")
        detail = production.asset_detail("music-hall")
        self.assertEqual(detail["gates"]["runtime"]["state"], "review")
        self.assertEqual(detail["cover"]["stage"], "engine")
        chapter = {c["stage"]: c for c in detail["making_of"]}
        self.assertTrue(chapter["uv"]["missing"])
        self.assertFalse(chapter["engine"]["missing"])
        production.attach_evidence("music-hall", "texture", source="Substance file: hall_wood.spp", caption="木纹材质源文件")
        detail = production.asset_detail("music-hall")
        self.assertEqual(next(c for c in detail["making_of"] if c["stage"] == "texture")["items"][0]["kind"], "source")

    def test_cleanup_parks_only_unprotected_revisions_and_can_restore(self):
        production.discover("music-hall")
        detail = production.asset_detail("music-hall")
        front = self.revisions(detail, "front")
        for view in ("front", "side", "top"):
            pick = self.revisions(detail, view)[-1] if view == "front" else self.revisions(detail, view)[0]
            detail = production.select_revision("music-hall", view, pick["revision_id"])
        set_review("art", front[0]["rel"], "approved")  # an approved file is never cleaned
        production.lock_design("music-hall")
        plan = production.cleanup_plan("music-hall")
        rels = {item["rel"] for item in plan["items"]}
        self.assertIn("music-hall/design/front/front_v2.png", rels)
        self.assertNotIn("music-hall/design/front/front_v1.png", rels)
        self.assertNotIn("music-hall/design/front/front_v3.png", rels)
        self.assertIn("music-hall/design/top/top_v2_tmp.png", rels)
        target = next(item for item in plan["items"] if item["rel"].endswith("front_v2.png"))
        result = production.cleanup_execute([target["revision_id"]])
        self.assertFalse((self.hall / "design" / "front" / "front_v2.png").exists())
        self.assertTrue((self.root / result["moved"][0]["trash_rel"]).is_file())
        scan_collection("art", force=True)
        self.assertEqual(production.discover("music-hall")["added"], [])
        with self.assertRaises(ValueError):
            production.cleanup_execute([next(r for r in front if r["label"] == "v3")["revision_id"]])
        detail = production.restore_revision(target["revision_id"])
        self.assertTrue((self.hall / "design" / "front" / "front_v2.png").is_file())
        self.assertEqual(next(r for r in self.revisions(detail, "front") if r["label"] == "v2")["state"], "candidate")

    def test_culling_a_selected_revision_is_refused(self):
        production.discover("music-hall")
        detail = production.asset_detail("music-hall")
        first = self.revisions(detail, "front")[0]
        production.select_revision("music-hall", "front", first["revision_id"])
        with self.assertRaises(ValueError):
            production.cull_revision(first["revision_id"], "bad")

    def test_upload_writes_next_version_without_overwriting(self):
        stored = production.store_upload("music-hall", filename="anything.png", data=(self.hall / "design" / "side_v1.png").read_bytes(), view="side")
        self.assertEqual(stored["rel"], "music-hall/design/side/side_v1.png")
        production.discover("music-hall")
        again = production.store_upload("music-hall", filename="x.png", data=b"\x89PNG", view="side")
        self.assertNotEqual(again["rel"], stored["rel"])
        with self.assertRaises(ValueError):
            production.store_upload("music-hall", filename="evil.exe", data=b"MZ", view="side")


    def test_asset_fields_views_and_pipeline_are_editable_per_asset(self):
        detail = production.update_asset("music-hall", name="中央音乐厅", stages=["model", "engine"], brief="拱顶")
        self.assertEqual(detail["asset"]["name"], "中央音乐厅")
        self.assertEqual(detail["asset"]["stages"], ["design", "model", "engine"])
        with self.assertRaises(ValueError):
            production.update_asset("music-hall", kind="spaceship")
        detail = production.add_view("music-hall", "门厅细节", "门厅细节", required=False)
        self.assertIn("门厅细节", [v["label"] for v in detail["views"]])
        production.discover("music-hall")
        with self.assertRaises(ValueError):
            production.remove_view("music-hall", "front")  # has revisions: make it optional instead
        detail = production.update_view("music-hall", "front", required=False)
        self.assertFalse(next(v for v in detail["views"] if v["view"] == "front")["required"])

if __name__ == "__main__":
    unittest.main()
