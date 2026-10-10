import os
import tempfile
import threading
import unittest
from pathlib import Path

from asset_viewer import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("ASSET_VIEWER_HOME")
        self.old_cache = os.environ.get("ASSET_VIEWER_CACHE")
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.images = Path(self.tmp.name) / "images"
        self.images.mkdir()

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

    def test_add_list_remove_collection(self):
        row = storage.add_collection(str(self.images), "My Images")
        self.assertEqual(row["slug"], "my-images")
        self.assertEqual(len(storage.collections()), 1)
        removed = storage.remove_collection("my-images")
        self.assertEqual(removed["label"], "My Images")
        self.assertEqual(storage.collections(), [])

    def test_safe_file_blocks_traversal_symlink_and_non_images(self):
        storage.add_collection(str(self.images), "Images")
        inside = self.images / "inside.png"
        inside.write_bytes(b"not-an-image")
        secret = self.images / ".env"
        secret.write_text("secret")
        outside = Path(self.tmp.name) / "outside.png"
        outside.write_bytes(b"outside")
        link = self.images / "outside-link.png"
        try:
            link.symlink_to(outside)
        except OSError:
            link = None
        self.assertEqual(storage.safe_file("images", "inside.png"), inside.resolve())
        self.assertIsNone(storage.safe_file("images", ".env"))
        self.assertIsNone(storage.safe_file("images", "../outside.png"))
        if link is not None:
            self.assertIsNone(storage.safe_file("images", "outside-link.png"))

    def test_review_round_trip_with_comment(self):
        storage.add_collection(str(self.images), "Images")
        storage.set_review("images", "frame.png", "approved", comment="Ship it")
        self.assertEqual(storage.reviews()["images"]["frame.png"], "approved")
        manifest = storage.review_manifest("images")
        self.assertEqual(manifest["items"][0]["comment"], "Ship it")
        self.assertEqual(manifest["counts"]["approved"], 1)
        self.assertIsNotNone(manifest["items"][0]["seen_at"])
        storage.set_review("images", "frame.png", "")
        self.assertNotIn("frame.png", storage.reviews()["images"])

    def test_batch_review_and_new_tracking(self):
        storage.add_collection(str(self.images), "Images")
        storage.ensure_assets("images", ["a.png", "b.png", "c.png"])
        manifest = storage.review_manifest("images")
        self.assertEqual(manifest["counts"]["new"], 3)
        storage.mark_seen("images", ["a.png"])
        storage.set_reviews_batch("images", ["b.png", "c.png"], "maybe")
        manifest = storage.review_manifest("images")
        self.assertEqual(manifest["counts"]["maybe"], 2)
        self.assertEqual(manifest["counts"]["new"], 0)
        new_only = storage.review_manifest("images", "new")
        self.assertEqual(new_only["counts"]["total"], 0)

    def test_comment_is_limited_and_status_filter_works(self):
        storage.add_collection(str(self.images), "Images")
        storage.set_review("images", "approved.png", "approved", comment="x" * 12000)
        storage.set_review("images", "rejected.png", "rejected")
        approved = storage.review_manifest("images", "approved")
        self.assertEqual(approved["counts"]["total"], 1)
        self.assertEqual(len(approved["items"][0]["comment"]), 10000)

    def test_concurrent_review_writes_do_not_lose_rows(self):
        storage.add_collection(str(self.images), "Images")
        errors = []

        def write(index):
            try:
                storage.set_review("images", f"frame-{index}.png", "approved", comment=str(index))
            except Exception as exc:  # pragma: no cover - failure detail only
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        manifest = storage.review_manifest("images")
        self.assertEqual(manifest["counts"]["approved"], 20)

    def test_legacy_reviews_migrate_to_sqlite(self):
        storage.add_collection(str(self.images), "Images")
        legacy = storage.legacy_reviews_path()
        legacy.write_text('{"images":{"old.png":"maybe"}}')
        self.assertEqual(storage.reviews()["images"]["old.png"], "maybe")
        self.assertTrue(storage.database_path().exists())
        self.assertTrue(legacy.with_name("reviews.json.migrated").exists())

    def test_review_history_and_undo(self):
        storage.add_collection(str(self.images), "Images")
        storage.set_review("images", "frame.png", "maybe", comment="first")
        storage.set_review("images", "frame.png", "approved", comment="ship it")
        history = storage.review_history("images", "frame.png")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["new_status"], "approved")
        undone = storage.undo_last_review("images", "frame.png")
        self.assertEqual(undone["status"], "maybe")
        manifest = storage.review_manifest("images")
        self.assertEqual(manifest["items"][0]["status"], "maybe")
        self.assertEqual(manifest["items"][0]["comment"], "first")
        self.assertIsNotNone(storage.review_history("images", "frame.png")[0]["undone_at"])

    def test_collection_completion_and_pending_summary(self):
        storage.add_collection(str(self.images), "Images")
        (self.images / "a.png").write_bytes(b"asset-a")
        (self.images / "b.png").write_bytes(b"asset-b")
        storage.ensure_assets("images", ["a.png", "b.png"])
        state = storage.collection_review_state("images")
        self.assertTrue(state["pending"])
        with self.assertRaises(ValueError):
            storage.complete_collection_review("images")
        storage.set_reviews_batch("images", ["a.png", "b.png"], "approved")
        completed = storage.complete_collection_review("images")
        self.assertTrue(completed["complete"])
        self.assertFalse(storage.pending_summary("images")["pending"])
        reopened = storage.reopen_collection_review("images")
        self.assertTrue(reopened["pending"])
        self.assertFalse(reopened["complete"])

    def test_completed_collection_becomes_stale_when_new_asset_is_discovered(self):
        storage.add_collection(str(self.images), "Images")
        (self.images / "a.png").write_bytes(b"asset-a")
        storage.ensure_assets("images", ["a.png"])
        storage.set_review("images", "a.png", "approved")
        storage.complete_collection_review("images")
        # ISO timestamps use second precision; force an older completion time so
        # the new discovery deterministically lands after it.
        with storage._connection() as conn:
            conn.execute("UPDATE collection_state SET completed_at='2000-01-01T00:00:00+00:00' WHERE collection='images'")
        storage.ensure_assets("images", ["b.png"])
        state = storage.collection_review_state("images")
        self.assertTrue(state["stale"])
        self.assertTrue(state["pending"])

    def test_preview_cache_budget_prunes_oldest_files(self):
        cache = storage.cache_dir()
        old = cache / "thumb-old.jpg"
        new = cache / "thumb-new.jpg"
        old.write_bytes(b"a" * 100)
        new.write_bytes(b"b" * 100)
        os.utime(old, (1, 1))
        os.utime(new, (2, 2))
        status = storage.preview_cache_status()
        self.assertEqual(status["files"], 2)
        result = storage.prune_preview_cache(100, 0)
        self.assertEqual(result["files"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(result["removed_files"], 1)

    def test_state_files_are_private(self):
        if os.name == "nt":
            self.skipTest("POSIX permission bits are not reported on Windows")
        storage.add_collection(str(self.images), "Images")
        storage.set_review("images", "frame.png", "approved")
        self.assertEqual(storage.data_dir().stat().st_mode & 0o077, 0)
        self.assertEqual(storage.registry_path().stat().st_mode & 0o077, 0)
        self.assertEqual(storage.database_path().stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
