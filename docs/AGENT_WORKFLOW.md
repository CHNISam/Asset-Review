# Agent workflow

Asset Viewer was built around a simple boundary: **agents create files; humans review them; the viewer does not become the source of truth.**

## Production assets

When the work is a game asset rather than a loose batch of images:

1. Declare it once: `asset-viewer prod create --project P --name 音乐厅 --key music-hall --kind environment --view front --view side --view top --collection <slug> --prefix music-hall`.
2. Deliver design revisions into `<prefix>/design/<view>/` (or `<prefix>/design/<view>_vN.png`) and run `asset-viewer prod discover music-hall`. Obvious drafts are culled automatically; cull more with `prod cull <revision_id> --reason ...`.
3. Wait for the owner to select revisions and lock the design. Build only from `asset-viewer prod lock music-hall`: it returns the verified files (path, sha256, notes, annotations). If `locked` is false or `verified` is false, stop and say so.
4. Put model, UV, texture, rig and engine evidence in `<prefix>/<stage>/`, or attach a source reference with `prod evidence --source`. Report `prod stage <asset> <stage> --status wip|ready --summary ... --next ...`.
5. Variants vs. versions: configurations that exist side by side (piano / round key layout, skins) are **variants** (`--variant` slots), never v1 / v2 of each other. A new revision needs an explicit vN (folder `design/<slot>/v3/` or `_v3` in the name) or an explicit `prod attach` without `--preview-of`. More screenshots, angles, states or clips of the same revision go in with `--preview-of N`. When the slot or version is not a known fact, leave the file in 待归类 (`prod unsorted`) instead of guessing. Fix earlier misfiling with `prod move` / `prod detach`.
6. Read the build input from `prod lock <asset>`: every required variant and view, each with `files` (primary plus previews, path and sha256). Do not pick "the latest upload".
7. Keep calls cheap: `prod show` prints a few lines, `prod show --json` and `prod lock` print compact JSON (`--pretty` only when a human reads it).
8. Never accept a stage, select a revision, lock a design or clean up files. Those are owner decisions in the browser, and the CLI and MCP tools do not offer them.

## Recommended contract

When an agent produces images:

1. Write them to a stable project-owned output directory.
2. Register that directory once with Asset Viewer.
3. Leave the originals where they are.
4. Tell the human which Asset Viewer collection contains the new work.
5. Treat gallery review state as feedback metadata, not as a file-moving operation.

Example:

```bash
asset-viewer add "$REPO/artifacts/brand-concepts" "Brand Concepts" --group "Project / Brand"
```

Agent handoff:

> New brand concepts are in Asset Viewer under **Brand Concepts**. The originals remain in `artifacts/brand-concepts`.

## Why this matters

Without a review surface, image-producing agents often create accidental workflow debt:

- giant batches embedded into chat clients;
- temporary uploads that become de facto storage;
- cloud-drive copies that drift from the project originals;
- IDE-based review that is slow and visually poor;
- uncertainty about which variants have been approved.

The viewer removes that pressure by giving both the agent and the human one predictable interface.

## Suggested repository instruction

```md
## Image asset review

When this repo creates image assets for human review, keep originals in the project-owned output directory and register that directory with Asset Viewer using `asset-viewer add`. Tell the user the collection name when the work is ready. Do not use chat attachments or cloud-drive duplication as the default review workflow.
```

## Closing the loop

After a human reviews a collection, an agent can consume the decisions directly:

```bash
asset-viewer reviews --collection brand-concepts --json
```

Or export a stable manifest for another process:

```bash
asset-viewer export-manifest --collection brand-concepts --output review-manifest.json
```

The manifest includes status, comments, new/seen timestamps, update timestamps, and variant-family metadata when related attempts have been grouped. This is preferred over asking an agent to infer approval state from chat text.

## Visual approval convention

Asset Viewer records **human** decisions. It is not an autonomous approval
engine, a rights-management system, or a production-readiness gate.

| Material purpose | What an `Approved` review means | What it does not mean |
| --- | --- | --- |
| Reference | The Owner allows it to inform the next creative pass. | Copyright, licence, attribution, or redistributability clearance. |
| Design | The Owner selected the current visual direction. | A final scene, geometry, implementation, or production-quality baseline. |
| Runtime / recording | A human found this capture useful as evidence or feedback. Routine screenshots and logs do not require one-by-one review. | Final Godot art acceptance or release sign-off. |
| Asset | The Owner accepts it as a visual candidate for the stated use. | Import success, performance validation, legal clearance, or final in-engine art acceptance. |

The status meanings are deliberately narrow:

- **Unreviewed**: an agent must not infer a final decision.
- **Maybe**: iterate from the attached feedback; do not treat it as a final
  version.
- **Rejected**: retain the original and its history, but do not carry it
  forward as the selected direction.
- **Approved**: the Owner's current visual selection. An agent may use it,
  its comments, annotations, and selected family member as input to the next
  design or search pass.

Only an Owner may set review state, undo a review, or complete/reopen a
collection. Agents must not call `review`, `undo`, `complete`, or `reopen`,
must not mutate the SQLite database, and must not silently turn an unreviewed
or `Maybe` item into a final decision. Agents may register a stable folder,
read review data, attach non-review provenance, and report what still needs a
human decision.

For a downstream pass, read the approved set and exact feedback rather than
guessing from filenames or chat:

```bash
asset-viewer reviews --collection brand-concepts --status approved --present-only --json
asset-viewer handoff brand-concepts --output approved-handoff.json
asset-viewer families brand-concepts --json
asset-viewer annotations brand-concepts concept-17.png --active-only --json
asset-viewer history brand-concepts concept-17.png --json
```

The optional MCP adapter exposes corresponding read operations such as review,
event, family, annotation, and approved-handoff retrieval. It intentionally
does not create a second review database or grant an agent authority to change
human approval state.

When a revised candidate is generated, preserve its relationship only when it
is known: use existing `parent_asset_id` / `run_id` provenance or a human-made
variant family. Do not invent family links. A preferred family member is a
comparison preference, not an approval decision; a later runtime or Godot
review remains a separate, traceable human step.

## Variant families

Generative work often arrives as related attempts rather than unrelated files. Group those attempts in Asset Viewer without renaming or moving the originals:

```bash
asset-viewer family-create brand-concepts "Logo exploration" concept-v1.png concept-v2.png concept-v3.png --json
asset-viewer family-prefer brand-concepts <family-id> concept-v3.png --json
asset-viewer families brand-concepts --json
```

Family membership follows stable asset IDs across ordinary same-filesystem renames. Family create/add/remove/prefer/rename/delete actions also appear in the ordered event feed, so an agent can react to lineage and preference changes as structured feedback.

## Provenance and approved handoff

When generation context is available, attach it to the stable asset rather than encoding it into filenames or moving the original:

```bash
asset-viewer metadata brand-concepts concept-17.png --set source_project=Whetstone --set model=imagegen --set run_id=brand-007 --json
```

After review, consume the exact approved set with:

```bash
asset-viewer handoff brand-concepts --output approved-handoff.json
```

The handoff contains stable IDs, relative paths, review hashes, notes, annotations, family/preferred state, and provenance. It does not expose absolute server paths. `--copy-to` is available only when an explicit separate copy is wanted; originals remain untouched.

## Waiting for explicit human completion

A non-empty folder is not the same thing as a finished human review. Asset Viewer exposes explicit collection completion so orchestrators can distinguish those states:

```bash
asset-viewer pending --collection brand-concepts --json
```

Exit code `2` means human review is still pending. Exit code `0` means the collection was explicitly marked complete after every asset received a review status.

Once decisions are available:

```bash
asset-viewer reviews --collection brand-concepts --json
```

If a human changes their mind, Asset Viewer records review events and supports undo/history without touching the source image:

```bash
asset-viewer history brand-concepts concept-17.png --json
asset-viewer undo brand-concepts concept-17.png
```


## Event-driven consumption

Long-running agents do not need to repeatedly diff full manifests. Read ordered events using a cursor:

```bash
asset-viewer events --collection brand-concepts --after 42 --json
```

For orchestration that should stop until the reviewer explicitly finishes:

```bash
asset-viewer wait-for-review --collection brand-concepts --timeout 900 --json
```

A successful wait is stronger than "there are files in the folder": it requires a complete filesystem scan, present assets, classifications for every current asset, and explicit human completion. If source bytes change after approval or a file disappears, the collection becomes pending again.

## Stable identity and deep links

Each cataloged asset receives a stable UUID. An unambiguous same-filesystem rename keeps the same identity, review history, and decision. Replacing the file contents invalidates the old decision.

Use a stable asset link when handing one exact item to a human:

```bash
asset-viewer asset-url brand-concepts concept-17.png --base-url https://viewer.example.com
```

Manifests retain deleted assets as `present: false` tombstones by default so an agent can distinguish deletion from omission. Use `--present-only` when only the current filesystem set matters.


## MCP adapter

Asset Viewer v0.8 can expose the same durable review protocol through an optional MCP v2 adapter. Install the optional dependency and run `asset-viewer mcp` (stdio by default). The MCP layer is an adapter only: SQLite/catalog/review state remains canonical, and agents receive the same collections, manifests, pending/completion state, events, human Activity, provenance metadata, approved handoff, annotations, families, stable URLs, and refresh semantics already available through the CLI/API.
