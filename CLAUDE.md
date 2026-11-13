# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

**Clientela**: a local, single-user client/project manager for video editors and motion designers. Flask backend, a single-file vanilla HTML/CSS/JS frontend (`templates/index.html`), JSON persistence, and the filesystem as the source of truth for clients and projects. The UI is in Portuguese.

## Commands

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt   # needs 3.10+ (macOS /usr/bin/python3 is 3.9)   # setup
.venv/bin/python -m pytest tests/ -q                                  # all tests
.venv/bin/python -m pytest tests/test_financials.py -q                # one file
.venv/bin/python app.py                                                # http://127.0.0.1:8000
.venv/bin/python app.py --remover-senha                                # forgotten password
```

No frontend build step. Jinja auto-reload is off outside debug mode: restart `app.py` to pick up template edits.

## Non-negotiable decisions

- Plain **Flask**, no ORM, no migrations. **JSON files only**, no database: the data folder is often synced between a Mac and a Windows machine through Google Drive, and per-file JSON survives that sync where SQLite corrupts.
- **One** frontend file, no framework, no build. Icons are inline SVGs.
- Optional dependencies (`yt-dlp`, `watchdog`, `pynput`, ffmpeg, Node, the Claude Code CLI, whisper) must degrade gracefully: the app runs without any of them. `clientela/startup.py` checks them in the background and the UI blocks only the features that need what is missing.
- Single user, `localhost` by default. The only opening is the opt-in password + remote access (`clientela/acesso.py`).

## Core architecture

- Each **client** is a real folder (path per OS in `data/clients.json`); each **project** is a subfolder named `"<NNN> - <name>"`. The folder decides existence, number, name and where the files are.
- Everything else about a project (stage, timer sessions, cost, history, notes, checklist, links) lives in a central registry, one JSON per project at `data/projects/<client_id>/<NNN>.json`, owned by `clientela/store.py`. A project id is `"<client_id>:<NNN>"`. **The project outlives its folder**: listing is the union of folders on disk and records in the store; a record without a folder shows as "sem pasta". Every per-project write goes through `store.update(project_id, fn)`.
- A folder named `00 - …` is the client's asset library, not a deliverable: it is listed but excluded from every statistic (`projects.is_asset_folder`).
- Subprojects: a `NN - …` folder inside a project folder (one level only).
- `business logic` lives in its module; `clientela/api.py` is a thin Blueprint that only calls into modules.

## Persistence

Every JSON write goes through `clientela/persistence.py` (`safe_write_json` / `safe_read_json`): atomic temp file + `os.replace`, previous version kept as `.bak`, reads fall back to `.bak`. Do not add any other `open()`/`json.dump`. `safe_read_json` deep-copies its default on purpose (module constants are passed as defaults).

Where `data/` lives: `paths.resolve_data_dir()` — env `CLIENTELA_DATA_DIR`, else `data_dir.json` beside the app (`{"mac": "...", "windows": "..."}`), else `./data`.

## Frontend

- One `state` object, render functions per screen, hash router (`#/`, `#/client/<id>/projetos`, `#/config/<section>`…), and a single body-level click listener dispatching on `data-action`.
- Design tokens are in `:root` of `templates/index.html`. Use `toast()` and `confirmDialog()` instead of `alert()`/`confirm()`.
- **Simple / complete interface**: `config.json` → `interface`. `body.modo-simples` hides every `[data-avancado]` element. Mark new editing/AI UI with `data-avancado`, and skip anything that calls an AI by itself when the interface is simple.
- **Usage profile**: `config.json` → `perfil_uso` (`video` | `design`) renames the stages (`stages.STAGE_LABELS_DESIGN`, mirrored in `index.html`) and adds design files as quick-open buttons (`organizer.DESIGN_OPEN_EXTS`).
- **Jinja trap**: the template goes through Jinja, so two opening braces in a row anywhere in the JS break the whole page (500). `tests/test_fase4.py::test_pagina_renderiza` renders `/`.

## Tests

`tests/conftest.py` isolates every `paths.*` constant into a temp dir — including `paths.PROJECTS_DIR` and `paths.USER_DOWNLOADS_DIR`, which are computed at import time. Any new path constant must be monkeypatched there too, or tests will write into real folders. `financials.py` changes need `tests/test_financials.py`.

## Releases

Bump `VERSAO` in `clientela/atualizacao.py` and publish a GitHub release tagged `v` + that version: installed copies compare against the latest release and show a notice (they never install anything by themselves).

Before publishing, fill `GOOGLE_FORM_LINK` and `GITHUB_REPO` in `clientela/feedback.py` (empty = feedback is only saved locally).
