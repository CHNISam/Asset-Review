# AGENTS.md

## Project intent

Asset Viewer is a small, local-first visual review surface for existing image folders. Keep it storage-agnostic and avoid turning it into a second source-of-truth DAM.

## Work and scope

Use Backlog.md: [Backlog.md](Backlog.md) is the entry point; `backlog/tasks/` is the sole work-item status source. Read the existing task before starting, set it In Progress, and record evidence before Done. Do not duplicate task states in ROADMAP or another checklist. Tasks labelled `deferred` require explicit scope reactivation.

Follow [docs/ART_ASSETS.md](docs/ART_ASSETS.md) and [docs/AGENT_WORKFLOW.md](docs/AGENT_WORKFLOW.md) for art handoff. Audio management is deferred: do not restore audio scanning, playback, waveforms, sample-bank workflows or audio production kinds as incidental work. Retain old source files and database records.

## Development checks

Before committing backend or CLI changes:

```bash
python -m unittest discover -v
python -m compileall -q asset_viewer tests
bandit -r asset_viewer
pip-audit --skip-editable
```

For UI changes, use synthetic demo assets rather than private project imagery:

```bash
export ASSET_VIEWER_HOME="$PWD/.dev-data"
export ASSET_VIEWER_CACHE="$PWD/.dev-cache"
asset-viewer demo --path "$PWD/.demo-assets"
asset-viewer serve
```

Do not commit machine-specific absolute paths, tokens, private review state, or user image collections.
