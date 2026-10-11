"""End-to-end production check in a real browser.

Concert hall asset with four design views; the front view went through three
revisions. The owner compares, selects and locks the design in the browser;
an agent reads the verified lock from the CLI, delivers model, UV, texture and
Godot evidence, and reports progress; the owner accepts each stage and cleans
up the unchosen revisions. Screenshots land in the directory given as argv[1].

    python tests/e2e_production.py ./shots
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_smoke import free_port, get_json, wait_for_server  # noqa: E402


def cli(env: dict, *args: str) -> str:
    result = subprocess.run([sys.executable, "-m", "asset_viewer", *args], env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"asset-viewer {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout


def shoot(page: Page, shots: Path, name: str, *, full: bool = False) -> None:
    # Lazy images below the fold would otherwise be blank in a full-page capture.
    page.evaluate("document.querySelectorAll('img[loading=lazy]').forEach(img => img.loading = 'eager')")
    page.wait_for_timeout(700)
    page.screenshot(path=str(shots / f"{name}.png"), full_page=full)


def main() -> None:
    shots = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="asset-review-e2e-shots-")).resolve()
    shots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="asset-review-e2e-") as temp_dir:
        root = Path(temp_dir)
        env = os.environ | {"ASSET_VIEWER_HOME": str(root / "data"), "ASSET_VIEWER_CACHE": str(root / "cache"), "PYTHONUTF8": "1"}
        cli(env, "demo", "--path", str(root / "demo"))
        studio_root = root / "demo-studio"
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [sys.executable, "-m", "asset_viewer", "serve", "--host", "127.0.0.1", "--port", str(port)],
            env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            wait_for_server(base, server)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

                # 1. Overview: the owner sees what needs a decision.
                page.goto(f"{base}/#/")
                expect(page.locator(".work-card")).to_have_count(1)
                expect(page.locator(".decision-card").first).to_contain_text("音乐厅")
                shoot(page, shots, "01-overview")

                # 2. Asset page before any decision.
                page.locator(".work-card").click()
                expect(page.locator(".hero-info h1")).to_have_text("音乐厅")
                expect(page.locator(".view-card")).to_have_count(4)
                expect(page.locator('[data-view-card="front"] .ver')).to_have_count(3)
                shoot(page, shots, "02-asset-before", full=True)

                # 3. Front view: three revisions, compare v2 and v3, annotate v2, select v3.
                page.locator('[data-view-card="front"]').click()
                expect(page.locator(".vs-ver:not(.add)")).to_have_count(3)
                page.locator('.vs-versions [data-rev]').nth(1).click()
                expect(page.locator(".vs-ver-big")).to_have_text("v2")
                with page.expect_response(lambda r: "/api/comment" in r.url and r.ok):
                    page.locator("[data-comment]").fill("拱顶太平，玫瑰窗要更突出")
                page.locator("[data-pin-mode]").click()
                page.locator(".vs-figure img").click(position={"x": 520, "y": 160})
                page.locator(".pin-form input").fill("这里改成拱顶")
                with page.expect_response(lambda r: "/api/annotation" in r.url and r.ok):
                    page.locator(".pin-form button").click()
                expect(page.locator(".vs-pins .pin")).to_have_count(1)
                page.keyboard.press("ArrowRight")
                expect(page.locator(".vs-ver-big")).to_have_text("v3")
                page.locator('#viewStudio [data-mode="wipe"]').click()
                expect(page.locator(".vs-wipe")).to_be_visible()
                page.locator('#viewStudio [data-mode="side"]').click()
                shoot(page, shots, "03-front-compare")
                page.locator('#viewStudio [data-mode="single"]').click()
                for view in ("front", "side", "top"):
                    expect(page).to_have_url(f"{base}/#/a/music-hall/v/{view}")
                    with page.expect_response(lambda r: "/api/production/select" in r.url and r.ok):
                        page.keyboard.press("Enter")
                    page.wait_for_timeout(700)
                page.keyboard.press("Escape")
                # The optional detail view is not queued as a decision, but can still be chosen.
                page.locator('[data-view-card="detail"]').click()
                expect(page.locator("#viewStudio .vs-title strong")).to_have_text("局部细节")
                with page.expect_response(lambda r: "/api/production/select" in r.url and r.ok):
                    page.keyboard.press("Enter")
                page.keyboard.press("Escape")

                # 4. Lock the full design.
                expect(page.locator(".next-card")).to_contain_text("确认整套设计")
                page.locator(".next-card [data-lock]").click()
                expect(page.locator(".lock-preview figure")).to_have_count(4)
                shoot(page, shots, "04-lock-dialog")
                page.locator('#dialog input[name="note"]').fill("采用拱顶方案 v3")
                with page.expect_response(lambda r: "/api/production/lock" in r.url and r.ok):
                    page.locator('#dialog [type="submit"]').click()
                expect(page.locator(".gate.approved")).to_contain_text("视觉批准")

                # 5. Agent reads the exact, verified design lock.
                lock_path = root / "lock.json"
                cli(env, "prod", "lock", "music-hall", "--output", str(lock_path))
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                assert lock["locked"] and lock["verified"], lock
                assert lock["lock"]["name"] == "D1", lock["lock"]
                revisions = {view["view"]: view["revision"] for view in lock["views"]}
                assert revisions == {"front": "v3", "side": "v1", "top": "v1", "detail": "v1"}, revisions
                assert all(Path(view["path"]).is_file() for view in lock["views"])
                v2_notes = get_json(f"{base}/api/production/asset?key=music-hall")
                front = next(v for v in v2_notes["views"] if v["view"] == "front")
                assert next(r for r in front["revisions"] if r["label"] == "v2")["comment"] == "拱顶太平，玫瑰窗要更突出"

                # 6. Agent delivers later stages into the asset folder and reports progress.
                from asset_viewer.demo_production import create_stage_set
                create_stage_set(studio_root)
                cli(env, "prod", "discover", "music-hall")
                cli(env, "prod", "stage", "music-hall", "model", "--status", "ready", "--summary", "拱顶与立面完成，18.4k 三角面")
                cli(env, "prod", "stage", "music-hall", "uv", "--status", "ready", "--summary", "单套 UV，2048 贴图密度一致")
                cli(env, "prod", "evidence", "music-hall", "--stage", "texture", "--source", "art/music-hall/hall_materials.spp", "--caption", "Substance 材质源文件")
                cli(env, "prod", "stage", "music-hall", "texture", "--status", "ready", "--summary", "石材 + 铜绿屋面")
                cli(env, "prod", "stage", "music-hall", "engine", "--status", "ready", "--summary", "Godot 4.4 白天/黄昏两套光照", "--next", "等待实机验收")
                denied = subprocess.run([sys.executable, "-m", "asset_viewer", "prod", "stage", "music-hall", "engine", "--status", "accepted"], env=env, capture_output=True, text=True)
                assert denied.returncode != 0, "agents must not accept stages"

                # 7. Owner accepts each stage on the same asset page.
                page.reload()
                expect(page.locator(".next-card")).to_contain_text("验收")
                shoot(page, shots, "05-stages-ready", full=True)
                for stage in ("model", "uv", "texture", "engine"):
                    with page.expect_response(lambda r: "/api/production/stage" in r.url and r.ok):
                        page.locator(f'[data-accept="{stage}"]').click()
                    page.wait_for_timeout(300)
                expect(page.locator(".next-card")).to_contain_text("全部完成")
                detail = get_json(f"{base}/api/production/asset?key=music-hall")
                assert {k: g["state"] for k, g in detail["gates"].items()} == {"visual": "approved", "tech": "approved", "runtime": "approved"}, detail["gates"]
                assert detail["cover"]["stage"] == "engine"
                assert not any(chapter["missing"] for chapter in detail["making_of"])
                page.evaluate("window.scrollTo(0, 0)")
                shoot(page, shots, "06-asset-done")
                page.locator("#making").scroll_into_view_if_needed()
                shoot(page, shots, "07-asset-full", full=True)

                # 8. Clean up unchosen revisions: parked, never deleted, lock still verified.
                page.evaluate("window.scrollTo(0, 0)")
                page.locator("[data-cleanup]").click()
                expect(page.locator(".cleanup-list li")).to_have_count(3)
                shoot(page, shots, "08-cleanup")
                with page.expect_response(lambda r: "/api/production/cleanup" in r.url and r.ok):
                    page.locator('#dialog [type="submit"]').click()
                trash = list((studio_root / ".asset-review-trash").rglob("*.png"))
                assert {p.name for p in trash} == {"front_v1.png", "front_v2.png", "top_v2_tmp.png"}, trash
                assert json.loads(cli(env, "prod", "lock", "music-hall"))["verified"]

                # 9. Declare another asset from the UI: a character with its own views.
                page.goto(f"{base}/#/")
                page.locator("[data-new-asset]").first.click()
                page.locator('#dialog input[name="name"]').fill("指挥家")
                page.locator('#dialog [data-kind="character"]').click()
                page.locator('#dialog input[name="customView"]').fill("指挥棒细节")
                page.locator("#dialog [data-add-custom-view]").click()
                shoot(page, shots, "09-new-asset")
                with page.expect_response(lambda r: "/api/production/asset" in r.url and r.request.method == "POST" and r.ok):
                    page.locator('#dialog [type="submit"]').click()
                expect(page.locator(".hero-info h1")).to_have_text("指挥家")
                expect(page.locator(".view-card")).to_have_count(4)
                expect(page.locator('[data-view-card] .view-title strong').last).to_have_text("指挥棒细节")
                expect(page.locator(".stepper li")).to_have_count(6)

                # 10. Overview shows both assets; the library knows where files are used.
                page.goto(f"{base}/#/")
                expect(page.locator(".work-next.done")).to_be_visible()
                expect(page.locator(".work-card")).to_have_count(2)
                shoot(page, shots, "10-overview")
                expect(page.locator("#libraryBody")).to_be_hidden()  # raw folders stay folded in the studio
                page.locator("#libraryToggle").click()
                page.locator(".collection-link").first.click()
                expect(page.locator("#libraryView")).to_be_visible()
                page.locator(".card").first.click()
                expect(page.locator("#assignRow")).to_be_visible()
                shoot(page, shots, "11-library-file")

                assert not errors, errors
                browser.close()
            print(f"production e2e passed; screenshots in {shots}")
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
