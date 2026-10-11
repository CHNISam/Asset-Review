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
                    page.locator(f'[data-project-pick="{name}"]').click()
                    page.locator("#studioView .page-head, #studioView .empty-state").first.wait_for()
                    page.wait_for_timeout(300)

                def open_library(selector: str) -> None:
                    if page.locator("#libraryBody").is_hidden():
                        page.locator("#libraryToggle").click()
                    page.locator(f"#collectionNav {selector}").first.click()
                    page.locator("#libraryView").wait_for(state="visible")

                # A collection link opens the file library inside the studio shell.
                response = page.goto(f"{base_url}/c/{demo_slug}", wait_until="domcontentloaded")
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

                assert page.title().endswith("素材库") or "Asset Review" in page.title(), page.title()
                assert page.locator("#collection").input_value() == demo_slug
                assert "Northline Coffee" in page.locator("#collectionTitle").inner_text()
                assert page.locator("#libraryView").is_visible()
                assert page.locator("#studioView").is_hidden()
                assert page.locator("#tabApproved").count() == 0
                assert page.locator("#projectSwitcher").is_visible()
                assert page.locator("#useFilter").is_visible()
                assert page.locator("#selectionMode").is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                summary = (page.locator("#summary").text_content() or "").strip()
                assert summary and summary.lower() != "loading…", summary

                # Switching project re-scopes the studio and the library together.
                switch_project("Second Project")
                open_library('[data-project="Second Project"]')
                wait_until(
                    "second project gallery to load",
                    lambda: page.locator("#grid .card").count() == 1,
                )
                assert page.locator("#grid .card").filter(has_text="theater-reference.png").count() == 1

                switch_project("未分组")
                open_library(f'[data-collection="{demo_slug}"]')
                wait_until("demo gallery to return", lambda: page.locator("#grid .card").count() == 6)

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

                page.locator('#statusChips [data-status="approved"]').click()
                wait_until("one seeded approved asset", lambda: page.locator("#grid .card").count() == 1)
                page.locator('#statusChips [data-status="all"]').click()
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
                # 通过 saves and moves straight to the next pending asset.
                wait_until(
                    "auto-advance to the next pending asset",
                    lambda: page.locator("#filename").inner_text().strip() != "06-homepage-banner.png",
                )

                reviews = get_json(f"{base_url}/api/reviews?collection={demo_slug}&present=1")
                banner_review = next(
                    (item for item in reviews.get("items", []) if item.get("rel") == "06-homepage-banner.png"),
                    None,
                )
                assert banner_review and banner_review.get("status") == "approved", reviews

                # Review has one surface: the same view keeps going with
                # keyboard decisions, undo, comments and the filmstrip.
                wait_until("filmstrip", lambda: page.locator("#filmstrip .film-frame").count() == 6)
                with page.expect_response(
                    lambda response: urllib.parse.urlsplit(response.url).path == "/api/review"
                    and response.request.method == "POST",
                    timeout=15_000,
                ) as keyboard_review:
                    page.keyboard.press("1")
                assert keyboard_review.value.ok
                with page.expect_response(
                    lambda response: urllib.parse.urlsplit(response.url).path == "/api/undo"
                    and response.request.method == "POST",
                    timeout=15_000,
                ) as keyboard_undo:
                    page.keyboard.press("Control+Z")
                assert keyboard_undo.value.ok
                with page.expect_response(
                    lambda response: urllib.parse.urlsplit(response.url).path == "/api/review"
                    and response.request.method == "POST",
                    timeout=15_000,
                ) as keyboard_review_again:
                    page.keyboard.press("1")
                assert keyboard_review_again.value.ok
                with page.expect_response(
                    lambda response: urllib.parse.urlsplit(response.url).path == "/api/comment"
                    and response.request.method == "POST",
                    timeout=15_000,
                ) as keyboard_comment:
                    page.keyboard.press("n")
                    page.locator("#comment").fill("Increase contrast before the next pass.")
                assert keyboard_comment.value.ok
                page.locator("#comment").press("Escape")
                page.locator("#close").click()
                page.locator("#modal").wait_for(state="hidden")

                # 审核 opens the same view at the first candidate.
                page.locator("#startReview").click()
                page.locator("#modal").wait_for(state="visible")
                page.locator("#close").click()
                page.locator("#modal").wait_for(state="hidden")

                # Comparison stays available from a multi-selection.
                page.locator("#selectionMode").click()
                page.locator("#grid .card").nth(0).click()
                page.locator("#grid .card").nth(1).click()
                page.locator("#selectionAction").select_option("compare")
                page.locator("#compareModal").wait_for(state="visible")
                page.locator("#closeCompare").click()
                page.locator("#compareModal").wait_for(state="hidden")
                page.locator("#selectionMode").click()

                page.locator('#statusChips [data-status="approved"]').click()
                wait_until("approvals appear under 通过", lambda: page.locator("#grid .card").count() == 3)

                page.set_viewport_size({"width": 390, "height": 844})
                page.locator('#statusChips [data-status="all"]').click()
                wait_until("full mobile gallery restored", lambda: page.locator("#grid .card").count() == 6)
                assert page.locator("#filter").input_value() == "all"
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
