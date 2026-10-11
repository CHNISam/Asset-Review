import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from asset_viewer import production
from asset_viewer.app import image_rows, scan_collection
from asset_viewer.storage import _connection, add_collection, safe_file


class ArtScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for key, folder in (("ASSET_VIEWER_HOME", "data"), ("ASSET_VIEWER_CACHE", "cache")):
            previous = os.environ.get(key)
            os.environ[key] = str(root / folder)
            self.addCleanup(self.restore_env, key, previous)
        self.media = root / "media"
        self.media.mkdir()
        Image.new("RGB", (300, 300)).save(self.media / "design.png")
        self.collection = add_collection(str(self.media), "Art")

    @staticmethod
    def restore_env(key, previous):
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous

    def test_audio_is_ignored_and_cannot_be_served_but_originals_remain(self):
        for ext in ("wav", "mp3", "ogg", "oga", "flac", "m4a", "aac", "opus"):
            path = self.media / f"clip.{ext}"
            path.write_bytes(b"audio candidate")
            self.assertIsNone(safe_file("art", path.name), ext)
        rows, _ = scan_collection("art", force=True)
        self.assertEqual([row["name"] for row in rows], ["design.png"])
        self.assertEqual(len(list(self.media.glob("clip.*"))), 8)

    def test_audio_production_kinds_and_evidence_are_rejected(self):
        for kind in ("instrument", "music", "sfx"):
            with self.assertRaises(ValueError):
                production.create_asset("Game", "Audio", key=kind, kind=kind)
        production.create_asset("Game", "Art", key="art", kind="2d")
        with self.assertRaises(ValueError):
            production.attach_evidence("art", "engine", source="clip.wav", kind="audio")

    def test_audio_upload_is_rejected_before_writing(self):
        production.create_asset("Game", "Art", key="art", kind="2d", source_collection="art")
        with self.assertRaises(ValueError):
            production.store_upload("art", filename="clip.wav", data=b"candidate", view="main")
        self.assertFalse((self.media / "clip.wav").exists())

    def test_legacy_audio_records_are_hidden_without_erasing_history(self):
        # Emulate a pre-upgrade database, without invoking removed audio APIs.
        scan_collection("art", force=True)
        production.create_asset("Game", "Legacy", key="legacy", kind="2d")
        with _connection() as conn:
            conn.execute("UPDATE assets SET rel='clip.wav', status='approved', comment='keep' WHERE collection='art'")
            conn.execute("UPDATE prod_assets SET kind='music' WHERE asset_key='legacy'")
        (self.media / "design.png").rename(self.media / "clip.wav")
        self.assertEqual(image_rows("art"), [])
        self.assertEqual(production.asset_keys("Game"), [])
        self.assertEqual(production.assign_targets(), [])
        self.assertEqual(production.project_overview("Game")["decisions"], [])
        with self.assertRaises(ValueError):
            production.asset_detail("legacy")
        scan_collection("art", force=True)
        with _connection() as conn:
            old = conn.execute("SELECT status, comment FROM assets WHERE rel='clip.wav'").fetchone()
            self.assertEqual(tuple(old), ("approved", "keep"))
            self.assertEqual(conn.execute("SELECT kind FROM prod_assets WHERE asset_key='legacy'").fetchone()[0], "music")
