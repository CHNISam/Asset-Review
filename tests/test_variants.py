"""Variants vs. revisions vs. previews, with the performance key layout UI as the case.

The piano keyboard layout and the round key layout ship side by side: they are
two variants of one asset, each with its own revisions and its own previews
(interaction states, notation), never v1 and v2 of each other.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from asset_viewer import production
from asset_viewer.app import scan_collection
from asset_viewer.storage import _connection, add_collection, annotations_for_asset, create_annotation, set_comment


def image(path: Path, color, size=(640, 400)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


class VariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.root = Path(self.tmp.name) / "ui"
        self.keys = self.root / "perform-keys" / "design"
        image(self.keys / "piano" / "v1" / "piano.png", (20, 30, 40))
        image(self.keys / "piano" / "v1" / "piano_receded.png", (21, 30, 40))
        image(self.keys / "round" / "v1" / "round.png", (200, 120, 60))
        image(self.keys / "round" / "v1" / "round_jianpu.png", (201, 120, 60))
        image(self.keys / "round" / "round-new-idea.png", (90, 90, 90))   # no version: a fact we do not have
        image(self.keys / "grid" / "grid_v1.png", (10, 200, 10))          # undeclared slot
        add_collection(str(self.root), "UI", "Game / Hall / UI / Design")
        scan_collection("ui", force=True)
        production.create_asset(
            "Game", "演奏键位 UI", key="perform-keys", kind="ui", source_collection="ui", source_prefix="perform-keys",
            views=[{"key": "piano", "label": "钢琴布局", "kind": "variant"}, {"key": "round", "label": "数字圆形布局", "kind": "variant"}],
        )
        production.discover("perform-keys")

    def tearDown(self):
        os.environ.pop("ASSET_VIEWER_HOME", None)
        os.environ.pop("ASSET_VIEWER_CACHE", None)
        self.tmp.cleanup()

    def slot(self, detail, key):
        return next(v for v in detail["views"] if v["view"] == key)

    def test_two_layouts_of_the_same_version_are_variants_not_v1_and_v2(self):
        detail = production.asset_detail("perform-keys")
        piano, round_ = self.slot(detail, "piano"), self.slot(detail, "round")
        self.assertEqual((piano["kind"], round_["kind"]), ("variant", "variant"))
        self.assertEqual([r["label"] for r in piano["revisions"]], ["v1"])
        self.assertEqual([r["label"] for r in round_["revisions"]], ["v1"])
        self.assertEqual([p["label"] for p in piano["revisions"][0]["previews"]], ["receded"])
        self.assertEqual([p["label"] for p in round_["revisions"][0]["previews"]], ["jianpu"])
        # Nothing to choose between yet: both single takes ride along with the lock.
        self.assertEqual([d["type"] for d in detail["decisions"]], ["lock"])

    def test_files_without_facts_wait_in_unsorted_and_are_never_cleaned(self):
        unsorted = {item["rel"].split("/", 2)[-1]: item["reason"] for item in production.unsorted_files("perform-keys")}
        self.assertIn("round/round-new-idea.png", unsorted)
        self.assertIn("没有版本号", unsorted["round/round-new-idea.png"])
        self.assertIn("grid", unsorted["grid/grid_v1.png"])
        self.assertEqual(production.discover("perform-keys")["added"], [])
        self.assertEqual(production.cleanup_plan("perform-keys")["items"], [])

    def test_agent_gets_every_layout_and_every_file_from_one_lock(self):
        production.lock_design("perform-keys", "两种布局同时上线")
        lock = production.design_lock("perform-keys")
        self.assertTrue(lock["verified"])
        by_slot = {v["view"]: v for v in lock["views"]}
        self.assertEqual(set(by_slot), {"piano", "round"})
        self.assertEqual({v["kind"] for v in lock["views"]}, {"variant"})
        self.assertEqual([f["role"] for f in by_slot["piano"]["files"]], ["primary", "preview"])
        self.assertTrue(all(Path(f["path"]).is_file() for v in lock["views"] for f in v["files"]))
        self.assertEqual(by_slot["round"]["files"][1]["label"], "jianpu")

    def test_one_layout_iterates_and_a_partial_update_relocks_the_set(self):
        production.lock_design("perform-keys")
        image(self.keys / "piano" / "v2" / "piano.png", (30, 40, 50))
        image(self.keys / "piano" / "v2" / "piano_receded.png", (31, 40, 50))
        image(self.keys / "piano" / "v2" / "piano_more.png", (32, 40, 50))
        scan_collection("ui", force=True)
        production.discover("perform-keys")
        detail = production.asset_detail("perform-keys")
        piano = self.slot(detail, "piano")
        self.assertEqual([r["label"] for r in piano["revisions"]], ["v1", "v2"])
        self.assertEqual(len(piano["revisions"][1]["previews"]), 2)
        self.assertEqual([r["label"] for r in self.slot(detail, "round")["revisions"]], ["v1"])
        self.assertEqual(piano["status"], "updated")
        production.select_revision("perform-keys", "piano", piano["revisions"][1]["revision_id"])
        detail = production.lock_design("perform-keys", "钢琴布局 v2")
        lock = production.design_lock("perform-keys")
        self.assertEqual((lock["lock"]["name"], {v["view"]: v["revision"] for v in lock["views"]}), ("D2", {"piano": "v2", "round": "v1"}))
        self.assertEqual(len(next(v for v in lock["views"] if v["view"] == "piano")["files"]), 3)
        self.assertEqual([l["superseded_at"] is None for l in detail["locks"]], [False, True])

    def test_more_shots_of_a_revision_never_become_versions(self):
        extra = image(self.keys / "piano" / "shots" / "piano-hover.png", (40, 40, 40))
        scan_collection("ui", force=True)
        rel = extra.relative_to(self.root).as_posix()
        production.attach_preview("perform-keys", "piano", 1, "ui", rel=rel, label="hover")
        piano = self.slot(production.asset_detail("perform-keys"), "piano")
        self.assertEqual([r["label"] for r in piano["revisions"]], ["v1"])
        self.assertEqual([p["label"] for p in piano["revisions"][0]["previews"]], ["receded", "hover"])
        with self.assertRaises(ValueError):
            production.attach_revision("perform-keys", "piano", "ui", rel=rel)  # a file belongs to one place
        with self.assertRaises(ValueError):
            production.attach_preview("perform-keys", "piano", 9, "ui", rel="perform-keys/design/round/round-new-idea.png")

    def test_previews_added_after_a_lock_need_a_relock_and_old_locks_still_read(self):
        production.lock_design("perform-keys")
        with _connection() as conn:  # a lock written before previews/kind existed
            row = conn.execute("SELECT lock_id, views FROM prod_locks WHERE superseded_at IS NULL").fetchone()
            legacy = [{k: v for k, v in view.items() if k not in {"previews", "kind"}} for view in json.loads(row["views"])]
            conn.execute("UPDATE prod_locks SET views=? WHERE lock_id=?", (json.dumps(legacy), row["lock_id"]))
        lock = production.design_lock("perform-keys")
        self.assertTrue(lock["verified"])
        self.assertEqual({v["kind"] for v in lock["views"]}, {"view"})
        self.assertEqual([len(v["files"]) for v in lock["views"]], [1, 1])
        detail = production.lock_design("perform-keys", "补上预览")
        self.assertEqual(detail["lock"]["name"], "D2")


class LegacyCorrectionTests(unittest.TestCase):
    """Data created by the old rule (every file a new version) is fixed without losing anything."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.root = Path(self.tmp.name) / "shots"
        for name, color in (("perform-piano.webp", (1, 2, 3)), ("perform-grid.webp", (200, 2, 3)),
                            ("perform-receded.webp", (200, 3, 3)), ("map.webp", (5, 5, 5))):
            image(self.root / name, color)
        add_collection(str(self.root), "Shots")
        scan_collection("shots", force=True)
        production.create_asset("Game", "音乐厅界面", key="hall-ui", kind="ui",
                                views=[{"key": "perform", "label": "演奏"}, {"key": "map", "label": "地图"}])
        self.wrong = [production.attach_revision("hall-ui", "perform", "shots", rel=name)["revision_id"]
                      for name in ("perform-piano.webp", "perform-grid.webp", "perform-receded.webp")]
        production.attach_revision("hall-ui", "map", "shots", rel="map.webp")
        file_id = production.asset_detail("hall-ui")["views"][0]["revisions"][1]["file_id"]
        create_annotation("shots", file_id, "point", 0.5, 0.5, text="键位太挤")
        set_comment("shots", "perform-grid.webp", "圆形键位留白多一点")

    def tearDown(self):
        os.environ.pop("ASSET_VIEWER_HOME", None)
        os.environ.pop("ASSET_VIEWER_CACHE", None)
        self.tmp.cleanup()

    def test_misfiled_versions_become_variants_and_previews_keeping_files_notes_and_history(self):
        before = {r["rel"]: r for r in production.asset_detail("hall-ui")["views"][0]["revisions"]}
        self.assertEqual([r["label"] for r in before.values()], ["v1", "v2", "v3"])  # the bug, reproduced
        production.add_view("hall-ui", "perform-piano", "演奏 · 钢琴布局", kind="variant")
        production.add_view("hall-ui", "perform-round", "演奏 · 圆形布局", kind="variant")
        production.move_revision(self.wrong[0], view="perform-piano")
        production.move_revision(self.wrong[1], view="perform-round")
        production.move_revision(self.wrong[2], view="perform-round", preview_of=1)
        production.update_view("hall-ui", "perform", required=False)
        detail = production.asset_detail("hall-ui")
        slots = {v["view"]: v for v in detail["views"]}
        self.assertEqual([r["label"] for r in slots["perform-piano"]["revisions"]], ["v1"])
        round_v1 = slots["perform-round"]["revisions"][0]
        self.assertEqual((round_v1["label"], round_v1["rel"], round_v1["sha256"]), ("v1", "perform-grid.webp", before["perform-grid.webp"]["sha256"]))
        self.assertEqual(round_v1["comment"], "圆形键位留白多一点")
        self.assertEqual([p["rel"] for p in round_v1["previews"]], ["perform-receded.webp"])
        self.assertEqual(len(annotations_for_asset("shots", round_v1["file_id"])), 1)
        self.assertEqual(slots["perform"]["revisions"], [])
        self.assertEqual(sum(1 for e in detail["events"] if e["action"] == "revision_reclassified"), 3)
        self.assertTrue(all((self.root / name).is_file() for name in before))
        production.lock_design("hall-ui")
        lock = production.design_lock("hall-ui")
        self.assertEqual({v["view"] for v in lock["views"]}, {"perform-piano", "perform-round", "map"})

    def test_decided_revisions_are_never_rewritten_and_their_lock_stays_valid(self):
        production.select_revision("hall-ui", "perform", self.wrong[1])
        with self.assertRaises(ValueError):
            production.move_revision(self.wrong[1], view="map", preview_of=1)
        with self.assertRaises(ValueError):
            production.detach_revision(self.wrong[1])
        production.lock_design("hall-ui")
        self.assertTrue(production.design_lock("hall-ui")["verified"])
        production.detach_revision(self.wrong[2])  # undecided: unlinked, file untouched
        self.assertTrue((self.root / "perform-receded.webp").is_file())

    def test_old_databases_gain_the_slot_kind_without_losing_rows(self):
        db = Path(os.environ["ASSET_VIEWER_HOME"]) / "asset-viewer.db"
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE prod_views DROP COLUMN kind")
        conn.execute("DROP TABLE prod_previews")
        conn.commit()
        conn.close()
        detail = production.asset_detail("hall-ui")  # reconnecting runs the additive migration
        self.assertEqual({v["kind"] for v in detail["views"]}, {"view"})
        self.assertEqual(len(detail["views"][0]["revisions"]), 3)

    def test_agent_cli_is_explicit_and_compact(self):
        env = os.environ | {"PYTHONUTF8": "1"}
        run = lambda *a: subprocess.run([sys.executable, "-m", "asset_viewer", "prod", *a], env=env, capture_output=True, text=True, encoding="utf-8")
        result = run("create", "--project", "Game", "--name", "键位", "--key", "keys", "--kind", "ui",
                     "--variant", "piano:钢琴布局", "--variant", "round:圆形布局")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(run("attach", "keys", "--slot", "piano", "--collection", "shots", "--rel", "perform-piano.webp").returncode, 2)  # already linked elsewhere: refused
        shown = run("show", "keys", "--json")
        payload = json.loads(shown.stdout)
        self.assertEqual([(s["slot"], s["kind"]) for s in payload["slots"]], [("piano", "variant"), ("round", "variant")])
        self.assertLess(len(shown.stdout), 1200)
        self.assertNotIn("/asset/thumb", shown.stdout)


if __name__ == "__main__":
    unittest.main()
