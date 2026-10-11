"""Each audit rule is pinned to the real misclassification it was learned from."""

import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from asset_viewer import production
from asset_viewer.app import scan_collection
from asset_viewer.production_audit import audit
from asset_viewer.storage import add_collection


def image(path: Path, seed: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 400), (seed % 255, (seed * 7) % 255, (seed * 13) % 255)).save(path)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.root = Path(self.tmp.name) / "ev"
        names = ["room-current.webp", "room-member-menu.webp", "compare-room.webp",
                 "perform-piano-solfege.webp", "perform-grid-solfege.webp",
                 "sg/contact-sheet.jpg", "mayones/contact-sheet.jpg", "jaguarillo/contact-sheet.jpg",
                 "sg/front.jpg", "mayones/front.jpg", "hall_v1.png", "hall_v2.png"]
        for i, name in enumerate(names):
            image(self.root / name, i + 1)
        add_collection(str(self.root), "Evidence")
        scan_collection("evidence", force=True)

    def tearDown(self):
        os.environ.pop("ASSET_VIEWER_HOME", None)
        os.environ.pop("ASSET_VIEWER_CACHE", None)
        self.tmp.cleanup()

    def make(self, key, views, files):
        production.create_asset("Game", key, key=key, kind="ui", views=views)
        return [production.attach_revision(key, view, "evidence", rel=rel)["revision_id"] for view, rel in files]

    def rules(self, key):
        return [f["rule"] for f in audit(key)["findings"]]

    def test_screen_states_filed_as_versions_are_caught_and_fixed(self):  # TASK-82.2 room screen v1…v3
        self.make("ui", ["room"], [("room", "room-current.webp"), ("room", "room-member-menu.webp"), ("room", "compare-room.webp")])
        findings = audit("ui")["findings"]
        self.assertEqual([f["rule"] for f in findings], ["state-as-revision", "state-as-revision"])
        for finding in findings:
            fix = finding["fixes"][0]
            production.move_revision(fix["revision_id"], view=fix["view"], preview_of=fix["preview_of"])
        self.assertEqual(self.rules("ui"), [])
        room = production.asset_detail("ui")["views"][0]
        self.assertEqual(([r["label"] for r in room["revisions"]], len(room["revisions"][0]["previews"])), (["v1"], 2))

    def test_parallel_layouts_filed_as_versions_are_caught(self):  # piano vs round keys as v2/v3
        self.make("keys", ["perform"], [("perform", "perform-piano-solfege.webp"), ("perform", "perform-grid-solfege.webp")])
        finding = audit("keys")["findings"][0]
        self.assertEqual(finding["rule"], "parallel-as-revision")
        self.assertIn("grid", finding["message"])

    def test_competing_options_filed_as_iterations_are_caught_and_marked(self):  # donors as v1…v3
        ids = self.make("guitar", ["overview"], [("overview", "sg/contact-sheet.jpg"), ("overview", "mayones/contact-sheet.jpg"),
                                                 ("overview", "jaguarillo/contact-sheet.jpg")])
        finding = audit("guitar")["findings"][0]
        self.assertEqual(finding["rule"], "options-as-revisions")
        production.set_revision_kind(finding["fixes"][0]["revision_ids"], "option")
        self.assertEqual(self.rules("guitar"), [])
        labels = [r["label"] for r in production.asset_detail("guitar")["views"][0]["revisions"]]
        self.assertEqual(labels, ["方案 A", "方案 B", "方案 C"])
        self.assertEqual(len(ids), 3)

    def test_the_slot_name_itself_is_not_a_state(self):  # front.jpg in the front view, one per donor
        self.make("bass", ["front"], [("front", "sg/front.jpg"), ("front", "mayones/front.jpg")])
        self.assertEqual(self.rules("bass"), ["options-as-revisions"])

    def test_facts_and_acknowledged_decisions_stay_quiet(self):
        self.make("hall", ["main"], [("main", "hall_v1.png"), ("main", "hall_v2.png")])
        self.assertEqual(self.rules("hall"), [])  # explicit vN is a fact
        self.make("tone", ["phrase"], [("phrase", "sg/contact-sheet.jpg"), ("phrase", "mayones/contact-sheet.jpg")])
        finding = audit("tone")["findings"][0]
        production.acknowledge("tone", finding["rule"], finding["subject"], "TASK-141: Delta follows Baseline")
        self.assertEqual(self.rules("tone"), [])

    def test_a_lock_whose_bytes_changed_is_an_error(self):
        ids = self.make("dup", ["a"], [("a", "hall_v1.png")])
        production.attach_preview("dup", "a", 1, "evidence", rel="hall_v2.png")
        self.assertEqual(self.rules("dup"), [])
        production.lock_design("dup")
        image(self.root / "hall_v1.png", 99)  # bytes changed after the lock
        self.assertEqual(self.rules("dup")[0], "lock-unverified")
        self.assertEqual(len(ids), 1)


if __name__ == "__main__":
    unittest.main()
