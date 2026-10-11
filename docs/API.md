# Agent/API contract

Asset Viewer exposes a small local HTTP API and matching CLI surface. The goal is not to make the browser UI an API; the goal is to give agents and automation a stable contract for **discover → review → complete → consume feedback**.

## Protocol version

The current machine-readable contract is **version 1**. Manifests and event feeds include a top-level `version` field. Backward-compatible fields may be added within a protocol version; breaking changes require a new protocol version.

Use:

```bash
asset-viewer capabilities --json
```

or `GET /api/capabilities` to discover the running application/protocol version and supported features.

## Read APIs

All endpoints except `/api/health` require configured authentication when Basic auth is enabled.

### `GET /api/gallery?collection=<slug>&refresh=1`

Returns the browser-oriented current collection, present assets, variant families, scan state, and collection review state. `refresh=1` forces a filesystem reconciliation; otherwise a fresh catalog snapshot can be served without recursively walking the source directory.

Each asset has a stable `asset_id`. Use that identifier for durable integrations. Relative paths are human-readable locators and can change after a rename.

### `GET /api/reviews`

Query parameters:

- `collection=<slug>` — optional collection filter
- `status=approved|maybe|rejected|unreviewed|new` — optional decision filter
- `present=1` — omit tombstoned/missing assets

Response conforms to [`schemas/review-manifest.schema.json`](../schemas/review-manifest.schema.json).

Equivalent CLI:

```bash
asset-viewer reviews --collection brand-concepts --json
asset-viewer export-manifest --collection brand-concepts --output review.json
```

### `GET /api/activity?collection=<slug>&limit=<n>`

Returns the newest human-readable activity slice for a collection, using the same durable event records as `/api/events`. This is a presentation view; cursor-based automation should continue using `/api/events`.

### `GET /api/metadata?collection=<slug>&asset=<id-or-rel>`

Returns optional provenance/generation metadata for one stable asset. Fields include source project, tool, agent, model, prompt, seed, run ID, Git commit, parent asset ID, and bounded arbitrary JSON `extra` metadata. Metadata is review context only; it never changes the original file.

Equivalent CLI:

```bash
asset-viewer metadata brand-concepts concept-17.png --json
asset-viewer metadata brand-concepts concept-17.png --set model=imagegen --set run_id=brand-007 --json
```

### `GET /api/handoff?collection=<slug>`

Returns the exact present approved set with stable asset IDs, relative paths, review SHA-256, comments, annotations, family state, and provenance. It deliberately omits absolute server source paths.
Handoff generation fails closed when current source bytes no longer match the stored review SHA-256; copy-out re-verifies the streamed bytes before publishing each destination file.

Equivalent CLI:

```bash
asset-viewer handoff brand-concepts --output approved-handoff.json
asset-viewer handoff brand-concepts --copy-to ./approved-export
```

### `GET /api/events?collection=<slug>&after=<id>&limit=<n>`

Returns ordered review/system events after an event cursor. This is the preferred polling primitive for long-running agents because callers can persist `last_event_id` and request only later events. In addition to review/comment/completion activity, v0.4 emits filesystem lifecycle actions such as `asset_added`, `asset_renamed`, `content_changed`, `asset_missing`, and `asset_restored`; event `details` carries action-specific metadata such as old/new paths.

Response conforms to [`schemas/event-feed.schema.json`](../schemas/event-feed.schema.json).

Equivalent CLI:

```bash
asset-viewer events --collection brand-concepts --after 120 --json
```

### `GET /api/pending?collection=<slug>`

Returns explicit human-completion state. A collection is complete only when its latest complete scan is known, all present assets have decisions, and the human has explicitly marked the review complete. New, changed, or removed content makes a previously completed review stale/pending.

CLI automation can use exit codes:

```bash
asset-viewer pending --collection brand-concepts --json
# 0 = explicitly complete
# 2 = human review still required

asset-viewer wait-for-review --collection brand-concepts --timeout 900 --json
```

### `GET /api/families?collection=<slug>`

Returns durable variant/version families for a collection. Family membership is keyed by stable asset UUID, so ordinary same-filesystem renames do not break grouping. Each family exposes `preferred_asset_id`, computed `latest_asset_id`, and member review/presence metadata. `present=1` omits missing members.

Equivalent CLI:

```bash
asset-viewer families brand-concepts --json
asset-viewer family-create brand-concepts "Logo exploration" concept-v1.png concept-v2.png --json
asset-viewer family-prefer brand-concepts <family-id> concept-v2.png --json
```

### `GET /api/annotations?collection=<slug>&asset_id=<uuid>`

Returns point/region visual feedback for one stable asset ID. Annotation coordinates are normalized to `0..1`, include resolved/stale state, and carry a content SHA-256 binding. `stale=0` can hide feedback invalidated by later content replacement.

Equivalent CLI:

```bash
asset-viewer annotations brand-concepts <asset-id> --json
asset-viewer annotate brand-concepts <asset-id> point 0.42 0.61 "Remove this object"
```

### `GET /api/review-history`

Accepts `collection` plus `asset_id` (preferred) or `rel`. Returns the asset's decision/comment history, including undone actions and system content-change events.

## Production APIs

Production assets point at catalog files by stable `asset_id`; files never move except through owner cleanup.

- `GET /api/production/projects` — projects from assets and collection groups
- `GET /api/production/overview?project=` — assets by scene with cover, gates, next step and owner decisions
- `GET /api/production/asset?key=` — one asset: views with revisions (`v1`…), design locks, stages with evidence, `making_of` chapters and the event log. Picks up new files that follow the folder convention first.
- `GET /api/production/lock?key=` — the current design lock with absolute paths, sha256, notes and annotations, re-verified on every read (`verified`, `problems`). `locked: false` until the owner confirms one; candidates are never implementation authority.
- `GET /api/production/cleanup[?asset=]` — revisions that could be parked safely
- `GET /api/production/file?file_id=` — where a catalog file is used, plus assign targets
- `GET /api/production/meta` — kinds, stages, view presets, collections

Owner mutations (CSRF, same as below): `POST /api/production/asset` (`create`/`update`), `/view` (`add`/`update`/`remove`), `/select`, `/lock`, `/stage` (`accepted` or `wip` with `next_step`), `/revision` (`attach`/`cull`/`restore`), `/evidence` (`attach`/`remove`), `/cleanup`, and `POST /api/production/upload?key=&view=|stage=` with the raw file body and an `X-Filename` header. Uploads are written as the next `<view>_vN` inside the asset folder and never overwrite.

Agents use `asset-viewer prod …` or the MCP tools `get_production_asset`, `get_design_lock`, `attach_design_revision`, `attach_stage_evidence`, `report_stage_progress` and `cull_revision`. None of them can select, lock, accept or clean up.

## Mutation APIs

Browser mutations use JSON POST requests and require the session CSRF token from `/api/session` in `X-Asset-Viewer-CSRF`. Origin/Host checks are also enforced.

- `POST /api/review` — set one status/comment or batch status
- `POST /api/comment` — set a note
- `POST /api/seen` — mark assets seen
- `POST /api/complete` — explicitly complete a fully reviewed collection
- `POST /api/reopen` — reopen a collection
- `POST /api/undo` — undo the most recent user review/comment change
- `POST /api/annotation` — create/update/delete point or region feedback
- `POST /api/family` — create/rename/delete a family, add/remove members, or set/clear its preferred member
- `POST /api/metadata` — attach/update optional provenance metadata for a known asset

Third-party automation should normally use the CLI for mutations unless it deliberately implements the browser security contract. Read APIs and CLI exports are designed as the stable integration surface first.

## Stable browser links

Collection:

```bash
asset-viewer collection-url brand-concepts --base-url https://viewer.example.com
```

Individual asset:

```bash
asset-viewer asset-url brand-concepts <asset-id-or-relative-path> --base-url https://viewer.example.com
```

The generated asset link uses the stable UUID, so ordinary same-filesystem renames do not invalidate the handoff.

## Storage semantics

- Source files remain authoritative and are never rewritten by Asset Viewer.
- Review state follows stable asset identity across detected same-filesystem renames.
- Non-empty review decisions are bound to a SHA-256 fingerprint (`review_sha256`) of the reviewed bytes. Replacing the bytes at an existing path invalidates the previous status/comment and makes the asset new again—even when size and nanosecond mtime are preserved and the replacement is detected by fingerprint reconciliation.
- Deleted assets are tombstoned rather than silently removed from manifests.
- Variant families are review metadata only: grouping never moves, renames, copies, or deletes source files. Family membership follows stable asset IDs across detected renames.
- Provenance metadata and collection groups are context/navigation metadata only and never reorganize originals.
- Approved handoff payloads use relative paths and stable IDs; optional copy export is explicit and never moves or rewrites the source set.
- Truncated/incomplete scans cannot declare missing files or produce a completed review state.

## Runtime capabilities

`asset-viewer serve` uses Waitress by default. `GET /api/capabilities` reports `production_wsgi_server`, process-isolated previews, compare/annotation support, and the effective safety/resource limits. The legacy stdlib server exists only for `--development-server`.

When embedding the WSGI application in multiple OS processes, provide a shared `ASSET_VIEWER_CSRF_TOKEN` to keep browser mutation tokens consistent across workers.
