# Contributing

Thanks for considering a contribution to Asset Viewer.

## Principles

Asset Viewer should stay small, local-first, and storage-agnostic. Features should improve visual review without turning the project into a second source-of-truth asset store.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m unittest discover -v
python -m compileall -q asset_viewer tests
bandit -r asset_viewer
pip-audit --skip-editable
```

For UI work, use isolated local state:

```bash
export ASSET_VIEWER_HOME="$PWD/.dev-data"
export ASSET_VIEWER_CACHE="$PWD/.dev-cache"
asset-viewer demo --path "$PWD/.demo-assets"
asset-viewer serve
```

## Pull requests

Keep changes focused. Include tests for backend/CLI behavior where practical. For visual changes, include a screenshot or short description of the interaction that changed.

Please do not commit private image sets, machine-specific paths, API tokens, or review-state files.

## Work items

Read [Backlog.md](Backlog.md) before starting. Create or reuse a task in `backlog/tasks/`, state the outcome and acceptance criteria, and record verification before marking it Done. Deferred tasks are not part of the active scope. Art conventions are in [docs/ART_ASSETS.md](docs/ART_ASSETS.md).
