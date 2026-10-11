from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from PIL import Image
from playwright.sync_api import sync_playwright


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until(description: str, condition: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except Exception as exc:  # transient browser/navigation state
            last_error = exc
        time.sleep(0.1)
    detail = f"; last error: {last_error}" if last_error else ""
    raise AssertionError(f"Timed out waiting for {description}{detail}")


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30.0) as response:
        assert response.status == 200, f"GET {url} returned {response.status}"
        payload = json.load(response)
    assert isinstance(payload, dict), f"GET {url} did not return a JSON object"
    return payload


def wait_for_server(base_url: str, process: subprocess.Popen[str], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"Asset Viewer exited before startup (code {process.returncode}):\n{output}")
        try:
            with urllib.request.urlopen(f"{base_url}/api/session", timeout=1.0) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    raise RuntimeError("Asset Viewer did not become ready before the browser smoke-test timeout")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="asset-viewer-browser-smoke-") as temp_dir:
        root = Path(temp_dir)
        env = os.environ.copy()
        env["ASSET_VIEWER_HOME"] = str(root / "data")
        env["ASSET_VIEWER_CACHE"] = str(root / "cache")

        # Run the product outside the repository checkout. In release CI this
        # prevents the source tree from shadowing the cleanly installed wheel.
        subprocess.run(
            [sys.executable, "-m", "asset_viewer", "demo", "--path", str(root / "demo-assets")],
            check=True,
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
        )

        # A second, independent project proves the shared workstation can
        # organize existing folders without a separate service or database.
        genshin_assets = root / "genshin-theater-reference"
        genshin_assets.mkdir()
        Image.new("RGB", (320, 180), (54, 76, 112)).save(genshin_assets / "theater-reference.png")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "asset_viewer",
                "add",
                str(genshin_assets),
                "Second Project — Theme reference",
                "--group",
                "Second Project / Theme / Reference",
            ],
            check=True,
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
        )

        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "asset_viewer",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-watch",
                "--log-level",
                "WARNING",
            ],
            env=env,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        try:
            wait_for_server(base_url, process)

            # Prove the documented demo -> serve path populates the catalog on
            # the first normal gallery request before exercising the browser UI.
            gallery = get_json(f"{base_url}/api/gallery")
            demo_slug = str(gallery.get("active") or "")
            assert demo_slug.startswith("northline-coffee-summer-launch"), gallery
            assert len(gallery.get("images") or []) == 6, gallery
            assert not (gallery.get("scan") or {}).get("truncated"), gallery.get("scan")

            page_errors: list[str] = []
            console_errors: list[str] = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: console_errors.append(message.text) if message.type == "error" else None,
                )

                def switch_project(name: str) -> None:
                    page.locator("#projectSwitcher").click()
                    page.locator(f'[data-switch-project="{name}"]').click()

                response = page.goto(base_url, wait_until="domcontentloaded")
                assert response is not None and response.ok, "initial gallery request failed"
                page.locator("#summary").wait_for(state="visible")
                try:
                    wait_until(
                        "six demo images to render",
                        lambda: page.locator("#grid .card").count() == 6,
                    )
                except AssertionError as exc:
                    summary = page.locator("#summary").text_content()
                    raise AssertionError(
                        f"{exc}; summary={summary!r}; page_errors={page_errors!r}; console_errors={console_errors!r}"
                    ) from exc

                assert page.title() == "Asset Review" or page.title().endswith("· Asset Review")
                assert page.locator("#collection").input_value() == demo_slug
                assert "Northline Coffee" in page.locator("#collectionTitle").inner_text()
                assert page.locator("#collectionRail").is_visible()
                assert page.locator("#tabGallery").is_visible()
                assert page.locator("#tabActivity").is_visible()
                assert page.locator("#tabApproved").is_visible()
                assert page.locator("#projectSwitcher").is_visible()
                assert page.locator("#useFilter").is_visible()
                assert page.locator("#selectionMode").is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                summary = (page.locator("#summary").text_content() or "").strip()
                assert summary and summary.lower() != "loading…", summary

                # A project filter must open another registered project without
                # replacing the demo collection or starting another service.
                switch_project("Second Project")
                wait_until(
                    "second project gallery to load",
                    lambda: page.locator("#grid .card").count() == 1,
                )
                assert page.locator("#grid .card").filter(has_text="theater-reference.png").count() == 1

                # The sidebar shows one project at a time; switch back the way
                # a reviewer does, then open the collection from the sidebar.
                switch_project("未分组")
                page.locator(f'[data-collection="{demo_slug}"]').click()
                wait_until("demo gallery to return", lambda: page.locator("#grid .card").count() == 6)

                # "All projects" remains an explicit scope rather than
                # silently falling back to the active collection.  Selecting
                # across collections keeps native review mutations disabled.
                switch_project("__all__")
                wait_until("all project assets to load", lambda: page.locator("#grid .card").count() == 7)
                assert page.locator("#projectFilter").input_value() == "__all__"
                assert page.locator("#sceneFilter").is_disabled()
                page.locator("#selectionMode").click()
                all_cards = page.locator("#grid .card")
                all_cards.nth(0).click()
                all_cards.nth(6).click()
                wait_until(
                    "two cross-project cards selected",
                    lambda: (page.locator("#selectedCount").text_content() or "").startswith("2"),
                )
                assert not page.locator('#selectionAction option[value="approve"]').is_enabled()
                page.locator("#clearSelection").click()
                page.locator("#selectionMode").click()
                switch_project("未分组")
                page.locator(f'[data-collection="{demo_slug}"]').click()
                wait_until("demo gallery after all-project scope", lambda: page.locator("#grid .card").count() == 6)

                # In selection mode, an entire card selects rather than opens a
                # preview. Shift selects a contiguous range and Ctrl toggles.
                page.locator("#selectionMode").click()
                cards = page.locator("#grid .card")
                cards.nth(0).click()
                cards.nth(2).click(modifiers=["Shift"])
                wait_until(
                    "three cards selected by range",
                    lambda: (page.locator("#selectedCount").text_content() or "").startswith("3"),
                )
                assert page.locator("#modal").is_hidden()
                cards.nth(1).click(modifiers=["Control"])
                wait_until(
                    "Ctrl toggles one selected card",
                    lambda: (page.locator("#selectedCount").text_content() or "").startswith("2"),
                )
                page.locator("#selectAllVisible").click()
                wait_until(
                    "all filtered cards selected",
                    lambda: (page.locator("#selectedCount").text_content() or "").startswith("6"),
                )
                page.locator("#clearSelection").click()
                page.locator("#bulkbar").wait_for(state="visible")
                assert page.locator("#selectAllVisible").is_enabled()
                page.locator("#selectionMode").click()
                page.locator("#bulkbar").wait_for(state="hidden")

                page.locator("#tabApproved").click()
                wait_until("one seeded approved asset", lambda: page.locator("#grid .card").count() == 1)
                assert page.locator("#approvedPanel").is_visible()
                page.locator("#tabGallery").click()
                wait_until("gallery restored", lambda: page.locator("#grid .card").count() == 6)

                target_card = page.locator("#grid .card").filter(has_text="06-homepage-banner.png")
                assert target_card.count() == 1, "expected exactly one unreviewed demo banner card"
                target_card.click()
                page.locator("#modal").wait_for(state="visible")
                assert page.locator("#filename").inner_text().strip() == "06-homepage-banner.png"

                with page.expect_response(
                    lambda response: urllib.parse.urlsplit(response.url).path == "/api/review"
                    and response.request.method == "POST",
                    timeout=15_000,
                ) as review_response_info:
                    page.locator('#reviewStatus [data-status="approved"]').click()
                review_response = review_response_info.value
                assert review_response.ok, f"review mutation returned HTTP {review_response.status}"
                wait_until(
                    "approved decision button",
                    lambda: page.locator("#reviewStatus").get_attribute("data-value") == "approved",
                )

                reviews = get_json(f"{base_url}/api/reviews?collection={demo_slug}&present=1")
                banner_review = next(
                    (item for item in reviews.get("items", []) if item.get("rel") == "06-homepage-banner.png"),
                    None,
                )
                assert banner_review and banner_review.get("status") == "approved", reviews

                page.locator("#close").click()
                page.locator("#modal").wait_for(state="hidden")

                # Fast culling is a thin view over the same native review
                # endpoints: mouse decisions, keyboard decisions, undo,
                # comments, native details, and comparison all stay local.
                page.locator("#quickReviewLaunch").click()
                page.locator("#quickReview").wait_for(state="visible")
                wait_until(
                    "quick-review filmstrip",
                    lambda: page.locator("#quickReviewFilmstrip .quick-review-frame").count() == 6,
                )
                first_quick_title = page.locator("#quickReviewTitle").inner_text()
                with page.expect_response(
                    lambda response: urllib.parse.urlsplit(response.url).path == "/api/review"
                    and response.request.method == "POST",
                    timeout=15_000,
                ) as quick_mouse_review:
                    page.locator("#quickReviewApprove").click()
                assert quick_mouse_review.value.ok
                wait_until(
                    "quick-review auto-advance",
                    lambda: page.locator("#quickReviewTitle").inner_text() != first_quick_title,
                )
                with page.expect_response(
                    lambda response: urllib.parse.urlsplit(response.url).path == "/api/review"
                    and response.request.method == "POST",
                    timeout=15_000,
                ) as quick_keyboard_review:
                    page.keyboard.press("2")
                assert quick_keyboard_review.value.ok
                with page.expect_response(
                    lambda response: urllib.parse.urlsplit(response.url).path == "/api/undo"
                    and response.request.method == "POST",
                    timeout=15_000,
                ) as quick_undo:
                    page.keyboard.press("Control+Z")
                assert quick_undo.value.ok
                with page.expect_response(
                    lambda response: urllib.parse.urlsplit(response.url).path == "/api/comment"
                    and response.request.method == "POST",
                    timeout=15_000,
                ) as quick_comment:
                    page.keyboard.press("n")
                    page.locator("#quickReviewComment").fill("Increase contrast before the next pass.")
                assert quick_comment.value.ok
                page.locator("#quickReviewCompare").click()
                page.locator("#compareModal").wait_for(state="visible")
                page.locator("#closeCompare").click()
                page.locator("#compareModal").wait_for(state="hidden")
                page.locator("#quickReviewDetailsButton").click()
                page.locator("#modal").wait_for(state="visible")
                page.locator("#close").click()
                page.locator("#quickReview").wait_for(state="visible")
                page.locator("#closeQuickReview").click()
                page.locator("#quickReview").wait_for(state="hidden")

                page.locator("#tabApproved").click()
                wait_until("quick-review approval appears in Approved", lambda: page.locator("#grid .card").count() == 3)
                assert page.locator("#approvedPanel").is_visible()

                page.locator("#view").select_option("activity")
                page.locator("#activityView").wait_for(state="visible")
                wait_until(
                    "review event to appear in Activity",
                    lambda: page.locator("#activityList .activity-event").count() > 0,
                )

                page.set_viewport_size({"width": 390, "height": 844})
                page.locator("#tabGallery").click()
                wait_until("full mobile gallery restored after Approved and Activity", lambda: page.locator("#grid .card").count() == 6)
                assert page.locator("#filter").input_value() == "all"
                assert not page.locator("#collectionRail").is_visible()
                assert page.locator(".mobile-collection").is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

                assert not page_errors, f"browser page errors: {page_errors}"
                assert not console_errors, f"browser console errors: {console_errors}"
                browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
