"""Browser check: two key layouts are variants, not versions.

    python tests/e2e_variants.py ./shots
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_smoke import free_port, wait_for_server  # noqa: E402


def keyboard(path: Path, layout: str, *, tint=(36, 40, 52), state: str = "") -> None:
    """Code-drawn performance HUD: a piano keyboard or a grid of round keys."""
    w, h = 1280, 720
    image = Image.new("RGB", (w, h), (24, 28, 38))
    draw = ImageDraw.Draw(image)
    for y in range(h):
        draw.line((0, y, w, y), fill=(24 + y // 40, 30 + y // 30, 44 + y // 25))
    top = 430 if state != "receded" else 560
    draw.rounded_rectangle((120, top - 30, w - 120, h - 40), radius=28, fill=tint)
    if layout == "piano":
        keys = 14
        kw = (w - 300) / keys
        for i in range(keys):
            x = 150 + i * kw
            draw.rounded_rectangle((x, top, x + kw - 6, h - 70), radius=8, fill=(240, 238, 232))
        for i in (0, 1, 3, 4, 5, 7, 8, 10, 11, 12):
            x = 150 + (i + 1) * kw - kw * 0.3
            draw.rounded_rectangle((x, top, x + kw * 0.6, top + (h - 70 - top) * 0.6), radius=6, fill=(30, 30, 34))
    else:
        for row in range(2):
            for col in range(8):
                cx, cy = 230 + col * 118, top + 60 + row * 110
                draw.ellipse((cx - 44, cy - 44, cx + 44, cy + 44), fill=(240, 196, 92) if (row + col) % 3 == 0 else (236, 234, 228))
                draw.text((cx - 6, cy - 8), str((row * 8 + col) % 7 + 1), fill=(30, 30, 34))
    if state == "more":
        draw.rounded_rectangle((w - 360, 80, w - 80, 330), radius=20, fill=(48, 54, 70))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def cli(env: dict, *args: str) -> str:
    result = subprocess.run([sys.executable, "-m", "asset_viewer", *args], env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"{' '.join(args)}\n{result.stdout}\n{result.stderr}"
    return result.stdout


def main() -> None:
    shots = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="variants-shots-")).resolve()
    shots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="variants-e2e-") as temp_dir:
        root = Path(temp_dir)
        env = os.environ | {"ASSET_VIEWER_HOME": str(root / "data"), "ASSET_VIEWER_CACHE": str(root / "cache"), "PYTHONUTF8": "1"}
        art = root / "ui"
        design = art / "perform-keys" / "design"
        keyboard(design / "piano" / "v1" / "piano.png", "piano")
        keyboard(design / "piano" / "v1" / "piano_receded.png", "piano", state="receded")
        keyboard(design / "round" / "v1" / "round.png", "round")
        keyboard(design / "round" / "v1" / "round_more.png", "round", state="more")
        keyboard(design / "round" / "round-alt.png", "round", tint=(60, 44, 52))  # no version → 待归类
        cli(env, "add", str(art), "UI", "--group", "Game / Hall / UI / Design")
        cli(env, "prod", "create", "--project", "Game", "--name", "演奏键位 UI", "--key", "perform-keys", "--kind", "ui",
            "--variant", "piano:钢琴布局", "--variant", "round:数字圆形布局", "--collection", "ui", "--prefix", "perform-keys")
        found = cli(env, "prod", "discover", "perform-keys")
        assert "revision piano v1" in found and "revision round v1" in found and "待归类 1" in found, found
        assert "v2" not in found, found  # two different layouts are never v1 → v2

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen([sys.executable, "-m", "asset_viewer", "serve", "--host", "127.0.0.1", "--port", str(port), "--no-watch"],
                                  env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            wait_for_server(base, server)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

                # Asset → 配置变体: piano / round, each v1 with its own previews; one unsorted file.
                page.goto(f"{base}/#/a/perform-keys")
                expect(page.locator(".slot-group").first).to_contain_text("配置变体")
                expect(page.locator('[data-view-card="piano"] .tag.variant')).to_be_visible()
                expect(page.locator('[data-view-card="round"] .ver')).to_have_count(1)
                expect(page.locator(".unsorted-item")).to_have_count(1)
                page.evaluate("document.querySelectorAll('img[loading=lazy]').forEach(i => i.loading = 'eager')")
                page.wait_for_timeout(800)
                page.screenshot(path=str(shots / "v1-asset.png"), full_page=True)

                # Viewer: switching layout is a configuration switch; versions belong to one layout.
                page.locator('[data-view-card="piano"]').click()
                expect(page.locator(".config-switch button")).to_have_count(2)
                expect(page.locator(".vs-ver:not(.add)")).to_have_count(1)
                page.locator('[data-shot="1"]').click()
                expect(page.locator(".shot-tag")).to_contain_text("receded")
                page.screenshot(path=str(shots / "v2-piano-preview.png"))
                page.locator('[data-switch-slot="round"]').click()
                expect(page).to_have_url(f"{base}/#/a/perform-keys/v/round")
                expect(page.locator(".vs-title strong")).to_have_text("数字圆形布局")
                expect(page.locator(".vs-ver:not(.add)")).to_have_count(1)
                page.keyboard.press("Escape")

                # The unsorted file is classified by a person: a preview of round v1, not a version.
                with page.expect_response(lambda r: "/api/production/preview" in r.url and r.ok):
                    page.locator("[data-classify]").select_option(label="数字圆形布局 v1 · 添加为预览")
                expect(page.locator(".unsorted-item")).to_have_count(0)
                expect(page.locator('[data-view-card="round"] .tag').last).to_contain_text("+2")

                # Lock the set: both layouts with all their files.
                page.locator("[data-lock]").first.click()
                expect(page.locator(".lock-preview figure")).to_have_count(2)
                with page.expect_response(lambda r: "/api/production/lock" in r.url and r.ok):
                    page.locator('#dialog [type="submit"]').click()
                lock = json.loads(cli(env, "prod", "lock", "perform-keys"))
                assert lock["verified"], lock
                files = {v["view"]: [f["rel"].split("/")[-1] for f in v["files"]] for v in lock["views"]}
                assert files == {"piano": ["piano.png", "piano_receded.png"], "round": ["round.png", "round_more.png", "round-alt.png"]}, files

                # Piano layout iterates alone; the next lock keeps round v1.
                keyboard(design / "piano" / "v2" / "piano.png", "piano", tint=(52, 44, 70))
                keyboard(design / "piano" / "v2" / "piano_receded.png", "piano", tint=(52, 44, 70), state="receded")
                cli(env, "prod", "discover", "perform-keys")
                page.goto(f"{base}/#/a/perform-keys/v/piano")
                expect(page.locator(".vs-ver:not(.add)")).to_have_count(2)
                page.locator('#viewStudio [data-mode="side"]').click()
                expect(page.locator(".vs-side figcaption")).to_have_text([re.compile("^v1"), re.compile("^v2")])  # history, oldest first
                page.screenshot(path=str(shots / "v3-piano-history-compare.png"))
                page.locator('#viewStudio [data-mode="single"]').click()
                page.keyboard.press("ArrowRight")  # the viewer opens on the selected v1
                expect(page.locator(".vs-ver-big")).to_have_text("v2")
                with page.expect_response(lambda r: "/api/production/select" in r.url and r.ok):
                    page.keyboard.press("Enter")
                page.keyboard.press("Escape")
                page.locator("[data-lock]").first.click()
                with page.expect_response(lambda r: "/api/production/lock" in r.url and r.ok):
                    page.locator('#dialog [type="submit"]').click()
                lock = json.loads(cli(env, "prod", "lock", "perform-keys"))
                assert lock["lock"]["name"] == "D2" and {v["view"]: v["revision"] for v in lock["views"]} == {"piano": "v2", "round": "v1"}, lock
                page.goto(f"{base}/#/a/perform-keys")
                page.evaluate("document.querySelectorAll('img[loading=lazy]').forEach(i => i.loading = 'eager')")
                page.wait_for_timeout(800)
                page.screenshot(path=str(shots / "v4-asset-locked.png"))
                shown = cli(env, "prod", "show", "perform-keys")
                print(shown.strip())
                assert not errors, errors
                browser.close()
            print(f"variants e2e passed; screenshots in {shots}")
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
