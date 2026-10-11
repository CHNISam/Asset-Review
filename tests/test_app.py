import os
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from asset_viewer.app import _run_isolated_worker, host_is_trusted, image_rows, make_thumbnail, origin_matches_host, serve
from asset_viewer.storage import add_collection


def sleeping_worker(seconds, sender):
    time.sleep(seconds)
    sender.send({"ok": True})
    sender.close()



class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ASSET_VIEWER_HOME"] = str(Path(self.tmp.name) / "data")
        os.environ["ASSET_VIEWER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.images = Path(self.tmp.name) / "images"
        self.images.mkdir()
        Image.new("RGB", (1200, 800), (20, 30, 40)).save(self.images / "sample.png")
        add_collection(str(self.images), "Samples")

    def tearDown(self):
        os.environ.pop("ASSET_VIEWER_HOME", None)
        os.environ.pop("ASSET_VIEWER_CACHE", None)
        self.tmp.cleanup()

    def test_gallery_script_polls_catalog_generation(self):
        script = (Path(__file__).parents[1] / "asset_viewer" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("async function pollCatalog()", script)
        self.assertIn("catalogGeneration", script)

    def test_gallery_uses_smart_selectors_instead_of_action_button_farm(self):
        html = (Path(__file__).parents[1] / "asset_viewer" / "static" / "index.html").read_text(encoding="utf-8")
        js = (Path(__file__).parents[1] / "asset_viewer" / "static" / "app.js").read_text(encoding="utf-8")
        for control_id in ("view", "actions", "selectionAction", "reviewStatus", "assetActions", "familyActions", "annotationMode"):
            self.assertIn(f'id="{control_id}"', html)
        for retired_id in ("selectAll", "reviewComplete", "report", "refresh", "saveComment", "showHistory", "copyPath", "copyReviewLink"):
            self.assertNotIn(f'id="{retired_id}"', html)
        self.assertIn("function selectAllVisible()", js)
        self.assertIn("function handleCollectionAction(value)", js)
        self.assertIn("function handleAssetAction(value)", js)
        self.assertIn("function renderReviewDecision(asset)", js)

    def test_v09_saved_views_and_command_palette_remain_progressive_disclosure(self):
        html = (Path(__file__).parents[1] / "asset_viewer" / "static" / "index.html").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "asset_viewer" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="commandPalette" class="command-palette hidden"', html)
        self.assertNotIn('id="commandPaletteButton"', html)
        self.assertIn("SAVED_VIEWS_KEY", script)
        self.assertIn("function renderViewMenu()", script)
        self.assertIn("已保存视图", script)
        self.assertIn("function openCommandPalette()", script)
        self.assertIn("event.ctrlKey || event.metaKey", script)
        self.assertIn("event.key.toLowerCase() === 'k'", script)

    def test_studio_is_organized_around_production_assets(self):
        root = Path(__file__).parents[1] / "asset_viewer" / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        library = (root / "app.js").read_text(encoding="utf-8")
        studio = (root / "studio.js").read_text(encoding="utf-8")
        css = (root / "app.css").read_text(encoding="utf-8")
        for control_id in ("studioView", "libraryView", "assetNav", "collectionNav", "viewStudio", "projectSwitcher", "assignRow"):
            self.assertIn(f'id="{control_id}"', html)
        # The split 素材 / 记录 / 交付 entry points are gone; the asset page carries all three.
        for retired_id in ("tabGallery", "tabActivity", "tabApproved", "approvedPanel", "activityView"):
            self.assertNotIn(f'id="{retired_id}"', html)
        self.assertNotIn("setWorkspaceTab", library)
        self.assertIn('<script src="/studio.js"></script>', html)
        for function in ("function renderAsset()", "function openViewer(", "function lockDialog()", "function makingOf(", "function renderDecide()"):
            self.assertIn(function, studio)
        for endpoint in ("/api/production/select", "/api/production/lock", "/api/production/stage", "/api/production/upload"):
            self.assertIn(endpoint, studio)
        self.assertIn("object-fit:contain", css)
        self.assertIn(".view-studio", css)
        self.assertIn(':root[data-theme="auto"]', css)

    def test_project_workspace_exposes_facets_and_card_selection_mode(self):
        root = Path(__file__).parents[1] / "asset_viewer" / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "app.css").read_text(encoding="utf-8")
        for control_id in (
            "projectFilter",
            "sceneFilter",
            "useFilter",
            "selectionMode",
            "selectAllVisible",
            "clearSelection",
        ):
            self.assertIn(f'id="{control_id}"', html)
        self.assertIn("selectionMode: false", script)
        self.assertIn("selectionAnchor", script)
        self.assertIn("selectionAnchorKey", script)
        self.assertIn("function collectionTaxonomy", script)
        self.assertIn("function selectRange", script)
        self.assertIn("scope.kind === 'all' ? '__all__'", script)
        self.assertIn("state.selectionMode || count > 0", script)
        self.assertIn("const scopeRequest = state.scopeRequest", script)
        reset_selection = script.split("function resetSelectionMode() {", 1)[1].split("function applyGalleryScope", 1)[0]
        bulk_review = script.split("async function bulkReview(status) {", 1)[1].split("async function undoReview", 1)[0]
        self.assertIn("state.selectionAnchorKey = '';", reset_selection)
        self.assertIn("state.selectionAnchorKey = '';", bulk_review)
        self.assertIn(".facet-bar", css)
        self.assertIn(".selection-mode", css)

    def test_review_happens_in_one_surface(self):
        root = Path(__file__).parents[1] / "asset_viewer" / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "app.css").read_text(encoding="utf-8")
        for control_id in ("startReview", "filmstrip", "reviewStatus", "comment", "modal"):
            self.assertIn(f'id="{control_id}"', html)
        # The former standalone quick-review mode is gone: one review surface.
        self.assertNotIn('id="quickReview"', html)
        self.assertNotIn("quickReview", script)
        self.assertIn("function startReview()", script)
        self.assertIn("function reviewCandidates()", script)
        self.assertIn("function nextPendingIndex(", script)
        self.assertIn("async function undoLastReview()", script)
        self.assertIn("apiPost('/api/review'", script)
        self.assertIn("apiPost('/api/undo'", script)
        for key in ("'1'", "'2'", "'3'", "'0'"):
            self.assertIn(f"event.key === {key}", script)
        self.assertIn("event.key === 'ArrowLeft'", script)
        self.assertIn("event.key === 'ArrowRight'", script)
        self.assertIn("event.key.toLowerCase() === 'z'", script)
        self.assertIn(".filmstrip", css)

    def test_project_taxonomy_comes_from_registered_group_metadata(self):
        script = (Path(__file__).parents[1] / "asset_viewer" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function collectionTaxonomy", script)
        self.assertIn("collection.group", script)
        self.assertIn("parts.slice(2, -1)", script)
        self.assertNotIn("COLLECTION_TAXONOMY", script)
        self.assertNotIn("mirror-garden-concept-art-2026-10-10", script)

    def test_comment_autosave_captures_asset_identity(self):
        script = (Path(__file__).parents[1] / "asset_viewer" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const assetKey = keyFor(asset);", script)
        self.assertIn("saveCommentSnapshot(assetKey, collection, assetId, comment)", script)
        self.assertNotIn("setTimeout(() => saveComment().catch(showError), 650)", script)

    def test_image_discovery(self):
        rows = image_rows("samples")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "sample.png")
        self.assertEqual(rows[0]["width"], 1200)

    def test_thumbnail_generation(self):
        blob = make_thumbnail(self.images / "sample.png")
        self.assertGreater(len(blob), 100)

    def test_isolated_worker_timeout_fails_closed(self):
        payload = _run_isolated_worker(sleeping_worker, (1.0,), 0.05)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "worker timeout")

    def test_host_validation(self):
        trusted = {"127.0.0.1", "localhost", "::1", "gallery.example.com"}
        self.assertTrue(host_is_trusted("127.0.0.1:8160", trusted))
        self.assertTrue(host_is_trusted("gallery.example.com", trusted))
        self.assertFalse(host_is_trusted("attacker.example", trusted))
        self.assertFalse(host_is_trusted(None, trusted))

    def test_origin_validation(self):
        self.assertTrue(origin_matches_host("http://127.0.0.1:8160", "127.0.0.1:8160"))
        self.assertTrue(origin_matches_host("https://gallery.example.com", "gallery.example.com"))
        self.assertFalse(origin_matches_host("https://attacker.example", "gallery.example.com"))
        self.assertTrue(origin_matches_host(None, "gallery.example.com"))
        self.assertFalse(origin_matches_host("null", "gallery.example.com"))

    def test_non_loopback_bind_requires_trusted_host(self):
        with self.assertRaises(ValueError):
            serve("0.0.0.0", 0, [])

    def test_non_loopback_bind_requires_auth_or_explicit_boundary_override(self):
        with self.assertRaisesRegex(ValueError, "require ASSET_VIEWER_PASSWORD"):
            serve("0.0.0.0", 0, ["gallery.example.com"])


if __name__ == "__main__":
    unittest.main()
