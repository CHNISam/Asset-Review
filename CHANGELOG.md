# Changelog

All notable changes to Asset Viewer will be documented here.

## Unreleased

- **Production assets.** The studio is now organized around what is being made. An asset declares the design views it needs (any subset of front/side/back/top/detail, or custom views, per asset type). Each view keeps its own revisions (v1, v2, v3) that point at catalog files in place. The owner selects one per view and confirms the set as a design lock (D1, D2), which is re-verified by sha256 on every read and is the only thing `asset-viewer prod lock` / `get_design_lock` hand to a coding agent.
- Stages per asset (设计, 建模, UV, 材质, 绑定动画, 实机) with evidence (images, video, timelapse, source references). Agents report `todo`/`wip`/`ready`; only the owner accepts or sends back, and acceptance needs evidence. 视觉批准, 技术就绪 and 实机验收 are separate gates.
- Files dropped into `<asset>/design/<view>/` or `<asset>/<stage>/` are discovered automatically. Obvious drafts (tmp/draft/wip/草稿 names, under 256 px, byte-identical repeats) are culled with a reason and stay restorable. Uploading writes the next `<view>_vN` and never overwrites.
- Owner cleanup parks culled and unchosen revisions in `<collection>/.asset-review-trash/<time>/`, keeping their record and a one-click restore. Locked, selected, approved and evidence files are refused.
- New UI. The sidebar shows the project switcher, 总览, 待决定 (owner decisions only), assets by scene with progress, and the file 素材库. Each asset page shows the current result, the three gates, one next step, design views with their version chips, a stage track with evidence and acceptance, and a making-of that shows real media per stage and states plainly when a stage has none. The view workspace offers versions, side-by-side or wipe compare, ← → / Enter keys, comments and pin annotations. The 素材 / 记录 / 交付 tabs and their code are removed: the asset page carries the history and the lock replaces the handoff tab.
- Visual redesign: warm white paper with ink primary buttons, orange reserved for owner decisions, artwork-first cards and large hero media.
- `asset-viewer prod …` CLI and six MCP tools for the agent side; `/api/production/*` routes in both servers; `asset-viewer demo` also seeds a code-drawn concert hall; `tests/e2e_production.py` runs the full owner and agent loop in a real browser.

- Merge quick review into the detail view: one review surface. 审核 opens it at the first pending item; `1` 通过 and `2` 拒绝 save and jump to the next pending item, `3` 修改 stays and focuses 意见 (`Ctrl+Enter` saves and moves on), `Ctrl+Z` undoes the last decision, and the filmstrip and progress live in the view. The standalone quick-review mode is removed.
- Rename the third decision to 修改 (needs changes, with a comment); order is 通过 / 拒绝 / 修改 everywhere. Drop the 新 chip and dot: "unseen" is a viewing state, not a decision, and new or changed files are already 待审.
- Light theme by default (dark and follow-system are opt-in); review stages follow the theme. Sidebar without colour tags, topics fold like folders, counts only where work is waiting, shared leading words trimmed from sibling names, quiet scrollbar. Chinese text uses regular weight only, since Microsoft YaHei has no medium.

- Make the use field state the decision it asks for: 参考 方向对吗？ / 设计 能进制作吗？ / 实机 实现到位吗？ / 资产 能交付吗？, shown above the decision buttons and in quick review. Add `Archive` / `Evidence` (存档) for task evidence that nobody needs to judge: it is excluded from pending counts, progress and quick review. Undeclared material reads 未标注.
- Redesign the sidebar around one project at a time: a project switcher at the top (with search once there are many projects), then collapsible scenes and topics. A topic with a single collection becomes one row, and a multi-collection topic header opens all of them together (new topic scope). Right-hand numbers are real pending counts from `/api/pending`.
- Apple-like visual pass: system font and system blue, borderless tonal controls, a segmented 素材 / 记录 / 交付 control in the top bar, translucent menus, toasts and palette, photo tiles without card chrome, and black stages for detail view and quick review. The toolbar is now a single row.
- Shorten UI labels to one term per concept: statuses 通过 / 待定 / 拒绝 / 待审; feedback is always 意见 and positional notes 标注; the handoff tab becomes 交付; menus use short verbs.
- Unify shortcuts on adjacent keys: `1` / `2` / `3` decide and `0` clears, in quick review, the detail view, and on a hovered or focused grid card. A/M/R are retired.
- Fix topics ending in an explicit 未分类 use splitting from their siblings, and clicking the only collection of a single-collection project not switching scope.

- Rebuild the browser UI as a lighter, art-first "Asset Review" studio: neutral untinted greys so artwork carries the colour, dark/light themes that follow the system with a manual toggle, and a single token-based stylesheet replacing the layered overrides.
- Collapse gallery chrome: status chips with live counts replace the status dropdown and summary line, the project/scene/use filters become a slim secondary row (collapsible on mobile), and a thumbnail density control is remembered per viewer.
- Quieter cards: badges only for reviewed or new assets, a single metadata line, and scene/use chips only when a view spans collections. Hovering a card shows one-click 通过/待定/拒绝 controls; a focused card also accepts A/M/R or 1/2/3.
- Focused review moves to a stage plus inspector layout with visible decision buttons in place of a status `<select>`, wheel zoom, drag pan, double-click or `Z` for 100%, and a position counter. Deciding inside a filter such as 待审 now continues with the next asset instead of closing.
- Unify review vocabulary (通过 / 待定 / 拒绝) across the gallery, focused review and quick review, accept both A/M/R and 1/2/3 everywhere, and localize the remaining English UI copy, including activity summaries rendered client-side while the server summary stays a stable English contract.
- Group activity by day, add a toast for confirmations and errors (it previously overwrote the collection summary), add a searchable Chinese/English command palette with shortcut hints, `/` to focus search, and `N` to open the quick-review note.
- Fix a duplicate `formatBytes` definition that shadowed the safe version, and update selection in place instead of re-rendering the whole grid on every click.

- Add first-class local video assets (`.mp4`, `.webm`, `.mov`, `.m4v`) without copying or transcoding originals.
- Generate bounded, cached JPEG poster frames with FFmpeg for fast video thumbnails while native browser playback streams the original file in place.
- Keep video poster extraction single-threaded inside the existing isolated preview boundary to control memory and CPU use.

- Redesign the browser UI around the real review workflow with a light studio shell, persistent desktop collection rail, collection context, and first-class Gallery / Activity / Approved navigation.
- Increase gallery image prominence while preserving uncropped `object-fit: contain` review semantics, and strengthen decision/family metadata hierarchy without changing source ownership or canonical review state.
- Add an Approved workspace backed by the existing approved review filter and integrity-checked handoff/report flows; no second approval store or export model is introduced.
- Rework Activity into a readable timeline that can show the real matching asset thumbnail when the canonical event references one, without inventing reviewer identity or collaboration data.
- Refine variant comparison into a lighter inspection workspace while preserving side-by-side, overlay/difference, linked zoom/pan, and the full image area.
- Add desktop/mobile browser invariants for workspace navigation, Approved-filter restoration, horizontal overflow, and compare image area, plus refresh README screenshots from the actual implemented UI.

## 0.9.1 — 2026-09-15

- Stop filesystem watching from treating read-only `opened` / `closed_no_write` events as mutations, preventing Asset Viewer reconciliations from feeding back into themselves and consuming CPU while idle.

## 0.9.0 — 2026-09-15

- Replace the generic six-card demo with a coherent fictional Northline Coffee summer campaign designed to demonstrate a real creative-review loop.
- Seed example approve / maybe / reject decisions, review notes, provenance, and a preferred two-variant hero family while leaving two assets genuinely pending for first-run review.
- Make demo generation idempotent by removing only known current/legacy demo filenames and preserving unrelated files in the target folder.
- Refresh the README with screenshots captured from the actual Asset Viewer UI running the new demo, including the real side-by-side family comparison workflow.
- Add project-native `.asset-viewer.toml` discovery and idempotent project sync.
- Add sidecar and isolated PNG metadata adapters for generation provenance.
- Add durable outbound review webhooks with signed delivery and cursor state.
- Add project integration and sync tools to the optional MCP adapter.
- Add saved-view and command-palette UI affordances while preserving progressive disclosure.
- Extend doctor diagnostics for project integration, metadata adapters, watcher state, and webhooks.

## 0.8.0 — 2026-09-13

Context-and-handoff release.

- Replace permanent action-button clutter with state selectors and context-aware Actions menus across gallery, selection, asset review, family, annotation, and compare workflows while preserving keyboard review.
- Add optional per-asset provenance/generation metadata (`source_project`, tool, agent, model, prompt, seed, run ID, Git commit, parent asset, and bounded arbitrary JSON extras) without changing source-file ownership.
- Add metadata-aware browser search plus provenance exposure in review manifests, HTTP API, CLI, and MCP v2.
- Add a human-readable Activity view backed by the existing durable event stream, with recent-event slicing for long histories.
- Add exact approved-set handoff manifests plus optional explicit copy export; source originals are never moved or rewritten and public handoff payloads do not expose absolute server paths.
- Fail approved handoff closed when current source bytes no longer match the reviewed SHA-256; copy export re-verifies streamed bytes before atomically publishing each destination file.
- Add metadata-only collection grouping for nested navigation without reorganizing source directories.
- Add autosaved review notes with asset-identity capture so rapid navigation cannot save a note onto the wrong asset.
- Expand regression coverage for smart-control UI invariants, provenance, Activity, grouped collections, approved handoff, CLI/API/MCP parity, recent-event ordering, and autosave identity safety.

## 0.7.0 — 2026-09-13

Live-review-loop release.

- Add an optional watchdog-backed filesystem watcher that runs with `asset-viewer serve` by default, coalesces filesystem churn, and retains periodic full reconciliation as the correctness fallback.
- Add `asset-viewer watch` for a standalone watcher process and watcher capability reporting.
- Add human-readable review reports in printable HTML or Markdown, including decisions, comments, annotations, variant-family context, and optional embedded thumbnails.
- Add a visible **Report** control in the gallery plus an authenticated `/report` route and `asset-viewer report` CLI command.
- Add an optional MCP v2 adapter over the existing durable review protocol, exposing collections, reviews, pending/completion state, ordered events, annotations, families, stable URLs, and explicit refresh without creating a second source of truth.
- Keep MCP optional via `local-asset-viewer[mcp]`; the normal viewer remains lightweight.
- Add regression coverage for real filesystem events, report escaping/output, report HTTP/CLI flows, and actual MCP v2 server tool registration.
- Refresh pinned GitHub Actions on current `main` before the v0.7 feature branch, avoiding stale maintenance history.

## 0.6.2 — 2026-09-13

Selection workflow patch.

- Add a persistent **Select all** button to the gallery toolbar.
- Select all acts on the current filtered/search view, so review batches can be narrowed first and then selected in one click.
- Disable the control when the current view is empty or already fully selected; existing **Clear selection** remains the explicit reset.
- Make the enlarged review view show a persistent decision indicator and visibly latch the active **Approve**, **Maybe**, **Reject**, or **Clear** button.

## 0.6.1 — 2026-09-13

Public-release sign-off patch.

- Make setuptools package-data intent explicit so source and wheel builds are warning-free while continuing to ship the static web UI.
- Add a final public-release audit/sign-off covering package integrity, privacy, security controls, protocol validation, deployment boundaries, and residual risks.
- Normalize family-member `present` values to JSON booleans so generated review manifests conform exactly to the shipped schema.
- No source-file ownership or destructive-file semantics changed from v0.6.0.

## 0.6.0 — 2026-09-13

Variant-family release.

- Add durable variant/version families keyed by stable asset UUIDs, without moving, renaming, copying, or modifying source files.
- Add explicit preferred-member state plus computed latest-member state for each family.
- Preserve family membership across same-filesystem renames because membership follows stable asset identity rather than relative paths.
- Add family metadata to gallery rows, review manifests, the versioned JSON schema, `/api/families`, and ordered event feeds.
- Add CSRF-protected family create/add/remove/prefer/rename/delete mutations to both production WSGI and development HTTP paths.
- Add CLI commands for family creation, inspection, membership changes, preferred-member selection, rename, and metadata-only deletion.
- Add gallery controls to group selected variants, extend an existing family, mark a preferred member, leave a family, and compare family members directly.
- Add regression coverage for rename survival, exclusive membership, source-file non-destruction, agent events, CLI flow, and production WSGI APIs.

## 0.5.0 — 2026-09-13

Precision-review and production-runtime release.

- Add point and rectangle annotations using normalized image coordinates, stable asset IDs, resolve/reopen state, event-feed integration, CLI/API support, and machine-readable manifest output.
- Bind spatial annotations to SHA-256 fingerprints of the annotated bytes so same-size/same-timestamp replacements mark location feedback stale instead of silently applying it to different pixels.
- Add in-view annotation tools and pinned feedback overlays without modifying source assets.
- Add linked/independent zoom and pan across comparison panes, plus reset controls, while preserving side-by-side, overlay, and difference modes.
- Make Waitress the default production HTTP serving layer; retain the stdlib server only as an explicit development mode.
- Move raster metadata inspection and thumbnail/review-preview decoding into disposable child processes with timeout, address-space/file-size limits, atomic cache publication, and bounded concurrency.
- Add preview-cache size/age policy, deterministic eviction, `asset-viewer cache`, and cache diagnostics in `doctor`.
- Add annotation, WSGI, worker-timeout, cache-budget, fingerprint-integrity, API, and CLI regression coverage.
- Keep full-file integrity hashing outside SQLite write transactions to avoid blocking concurrent review-state writes on large originals.
- Allow deliberate multi-process WSGI deployments to share CSRF state through `ASSET_VIEWER_CSRF_TOKEN`.
- Expand the public API/schema contract and publish a third security audit plus a second competitive feature audit.

## 0.4.0 — 2026-09-13

Durable catalog and agent-event release.

- Add stable UUID asset identities that survive same-filesystem renames.
- Add a durable SQLite asset catalog with scan generations, present/missing tombstones, dimensions, file identity, and change metadata.
- Preserve review status, comments, and history across renames; invalidate stale approvals when file content changes.
- Bind non-empty review decisions to a streaming SHA-256 fingerprint of the reviewed bytes; forced reconciliation detects same-size/same-timestamp replacements and completion re-verifies fingerprints.
- Keep deleted assets in manifests as missing/tombstoned records so automation can account for reviewed work that disappeared.
- Add cached gallery reads with bounded explicit rescans instead of recursive filesystem discovery on every page refresh.
- Prevent truncated scans from incorrectly tombstoning unvisited files or allowing a collection to be marked review-complete.
- Add ordered review/system event feeds through `/api/events` and `asset-viewer events`, including asset add/rename/change/missing/restore lifecycle events.
- Add `asset-viewer wait-for-review` for automation that needs an explicit human-completion gate.
- Allow review/history requests to target stable `asset_id` values.
- Bound concurrent thumbnail/review-preview decoding and mark generated preview responses private-cache only.
- Use bounded 2048px review previews in the carousel instead of loading full-resolution originals by default.
- Add stable per-asset deep links and `asset-viewer asset-url`.
- Add side-by-side, opacity-overlay, and difference comparison modes; sort by file size/resolution and search review comments.
- Keep configured collections visible when their source path is temporarily unavailable; fail review completion closed and report the condition in `doctor`.
- Harden direct non-loopback serving so authentication is required unless an operator explicitly acknowledges an external access-control boundary.
- Expand migration, catalog-integrity, rename, deletion, replacement, symlink, event, preview, and automation regression coverage.
- Add a tag-driven release workflow that publishes wheel/sdist artifacts, a CycloneDX SBOM, and SHA-256 checksums.

## 0.3.0 — 2026-09-13

Agent-review workflow release.

- Add explicit collection review completion/reopen state.
- Add `asset-viewer pending` with automation-friendly exit codes (`2` pending, `0` complete).
- Add review event history and one-step undo for status/comment changes.
- Add `history`, `undo`, and `collection-url` CLI commands.
- Add read-only pending/history APIs plus CSRF-protected complete/reopen/undo endpoints.
- Add filename/path search and client-side sort controls.
- Surface review progress/completion directly in the gallery.
- Fix duplicate `New` filter and duplicate review-note DOM IDs from the v0.2 UI.
- Increase review timestamps to microsecond precision so new work reliably invalidates older completion state.

## 0.2.0 — 2026-09-13

Security hardening and closed-loop review foundation.

- Prevent non-image files inside registered folders from being served by guessed URLs.
- Validate HTTP `Host` headers to reduce localhost DNS-rebinding exposure.
- Reject cross-origin review mutations with strict `Origin`/`Host` validation.
- Require explicit trusted hosts for non-loopback/reverse-proxy deployments.
- Treat Pillow decompression-bomb warnings as preview failures.
- Store registry/review data with private filesystem permissions where supported.
- Add regression tests for traversal, non-image disclosure, and Host validation.
- Move review metadata to SQLite with automatic migration from legacy `reviews.json`.
- Add review notes, new/seen state, batch-review backend support, and machine-readable review manifests.
- Add CSRF tokens and optional HTTP Basic authentication for remote/private deployments.
- Serve SVG originals as downloads and use inert preview placeholders instead of rendering active SVG content.
- Add bounded collection scans, image size/pixel limits, structured logging, and `asset-viewer doctor`.
- Add stable collection URLs (`/c/<slug>`) and a `New` review filter.
- Pin GitHub Actions to immutable commit SHAs and add Dependabot configuration.

## 0.1.0 — 2026-09-13

Initial public release.

- Register existing image directories without importing or moving originals.
- Browse multiple collections from a thumbnail gallery.
- Full-screen carousel navigation.
- Approve / Maybe / Reject review state.
- Keyboard review shortcuts.
- Cached raster thumbnails and direct SVG previews.
- Localhost-by-default HTTP server.
- Agent-oriented CLI and workflow documentation.
- Synthetic demo collection and screenshot.
