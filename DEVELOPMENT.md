# Development

Commands for working on Alpha Harness. Run them from the repository root unless a block says otherwise.

## First-time setup

```bash
cd backend && uv sync          # Python 3.14 and every backend dependency
cd ../frontend && pnpm install # frontend dependencies
lefthook install               # git hooks (pre-commit: ruff, biome, pyrefly)
```

You also need [`rustup`](https://rustup.rs). `lefthook run check` rebuilds the FastExpr wasm to prove the committed copy is fresh, and the desktop app is Rust.

## Run the app

```bash
lefthook run dev               # backend :8000 (auto-reload) + frontend :5173, together
```

Open <http://localhost:5173> and sign in with your BRAIN account.

As a consultant runs it, with one process and the built UI on <http://127.0.0.1:8000>:

```bash
lefthook run build && (cd backend && uv build)
cd backend && uvx -c dist/constraints.txt --from dist/alpha_harness-*.whl alpha-harness
```

## Before you commit

| Command                 | What it does                                                    |
| ----------------------- | --------------------------------------------------------------- |
| `lefthook run format`   | Fixes and formats everything, and regenerates `frontend/src/api/generated.ts` |
| `lefthook run check`    | Lint, types (pyrefly, basedpyright, tsc), API contract, wasm, DESIGN.md |
| `lefthook run test`     | pytest + vitest                                                 |
| `lefthook run build`    | Vite build + `backend/dist/constraints.txt`                     |
| `lefthook run gate`     | check → test → build. **Run this before tagging a release.**    |

Changed a backend response model? Run `lefthook run format`, or `check` fails on a stale `generated.ts`.

## Tests, one at a time

```bash
# Backend (from backend/)
uv run pytest                                  # everything
uv run pytest tests/test_packer.py             # one file
uv run pytest -k retired                       # tests whose name matches
uv run python -m alpha_harness.handover        # one module's built-in self-check

# Frontend (from frontend/)
TZ=America/Los_Angeles pnpm exec vitest run                          # everything
TZ=America/Los_Angeles pnpm exec vitest run src/lib/clipboard.test.ts # one file
```

The `TZ` is deliberate: west of UTC is where a date read in local time shows the day before.

## Generated files

```bash
# FastExpr parser (crates/fastexpr) → the committed wasm. Run it after changing the crate.
sh crates/fastexpr/build.sh frontend/src/lib/fastexpr.wasm

# API types only (lefthook run format also does this)
sh frontend/tools/codegen/generate.sh frontend/src/api/generated.ts

# DESIGN.md lint (fails on any error or warning)
sh frontend/tools/design-lint.sh DESIGN.md
```

## Desktop app (`desktop/src-tauri`)

```bash
cd desktop/src-tauri
cargo clippy --all-targets -- -W clippy::pedantic
cargo test
```

The installers themselves are built only by CI, on a tag (see below).

## Dependencies

```bash
cd backend && uv add <package>            # runtime
cd backend && uv add --dev <package>      # dev only
cd backend && uv lock --upgrade-package <package>
cd frontend && pnpm add <package>         # or: pnpm add -D <package>
```

Use uv and pnpm only, never pip, npm or yarn.

## Releasing

Versions are dates, unpadded: `2026.11.13`, then `2026.11.13.1` for a second release that day. The tag is the only place a version is written; CI stamps it into the wheel and installers.

```bash
git switch main && git pull
lefthook run gate                              # must be green
git tag v2026.11.13                            # note the leading v
git push origin v2026.11.13                    # starts .github/workflows/release.yml
```

**Every installed copy auto-updates to what lands on the latest release.** Be sure before you push the tag.

| Situation                         | Command |
| --------------------------------- | ------- |
| Test a release without shipping it | Tag a pre-release: `git tag v2026.11.13rc1 && git push origin v2026.11.13rc1`. It builds and publishes, but the updater never offers it. |
| See existing tags, newest first   | `git tag --sort=-creatordate \| head` |
| Watch the release build           | `gh run watch` (or `gh run list --workflow release.yml`) |
| Rebuild a version without a new tag | `gh workflow run release.yml -f version=2026.11.13` |
| Tagged the wrong commit, **not yet pushed** | `git tag -d v2026.11.13`, then tag again |
| Pushed a bad tag | Don't move it: users may already have it. Fix forward with `v2026.11.13.1`. |

## Git basics

```bash
git status                       # what changed
git diff                         # unstaged changes; git diff --staged for staged ones
git add -p                       # stage piece by piece
git commit -m "fix: ..."         # pre-commit hooks run here
git push                         # push the current branch
git log --oneline -10            # recent history
git restore <file>               # discard unstaged changes to a file (no undo!)
git stash / git stash pop        # park work and bring it back
```

## Local data (`~/.alpha-harness/`)

| File             | Notes |
| ---------------- | ----- |
| `harness.db`     | SQLite: credentials, tasks, lab runs. **Never delete without a backup.** |
| `catalog.duckdb` | Fields and Alphas. Rebuildable: Download in Data, re-sync from BRAIN. Only one backend can hold it, so stop the dev server before running the packaged app. |
| `key`            | The encryption key for `harness.db`. Losing it loses the saved credentials. |
| `backups/`       | `harness.db` from before each migration. Restore by copying one back with the app closed. |

The desktop app keeps its install in `~/.local/share/AlphaHarness` (Linux), `%LOCALAPPDATA%\AlphaHarness` (Windows) or `~/Library/Application Support/AlphaHarness` (macOS).

## Live BRAIN

The repo-root `.env` holds `BRAIN_EMAIL` and `BRAIN_PASSWORD` for testing against the real platform. Never print or commit them. Every live simulation spends the account's daily quota.
