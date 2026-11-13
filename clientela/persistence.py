import copy
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("clientela.persistence")

_lock = threading.Lock()


def _tmp_path(path: Path) -> Path:
    return path.parent / (path.name + ".tmp")


def _bak_path(path: Path) -> Path:
    return path.parent / (path.name + ".bak")


def _replace(src: Path, dst: Path, attempts: int = 10, delay: float = 0.1) -> None:
    """``os.replace`` que aguenta o Windows. Lá a troca falha com
    PermissionError enquanto outro processo está com o destino aberto — e
    com data/ no Google Drive, o próprio Drive (e o antivírus) abre os
    arquivos logo depois de cada escrita pra sincronizar. No macOS a primeira
    tentativa sempre passa."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def safe_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: serialize to a temp file, keep the previous
    version as .bak, then atomically swap it into place with os.replace.
    Never leaves the target file in a half-written state."""
    path = Path(path)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _tmp_path(path)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copy2(path, _bak_path(path))
        _replace(tmp, path)


def safe_read_json(path: Path, default: Any) -> Any:
    """Read JSON, falling back to the .bak copy if the primary file is
    missing or corrupted, instead of silently returning `default` and
    losing real data.

    `default` is deep-copied before being handed back. Callers pass module
    constants like `DEFAULT_TIME = {"sessions": []}`, usually wrapped in a
    shallow `dict(...)` — which still shares the inner list. Whoever got the
    default then appends to it (a timer session, a tag, an extra cost) and
    silently mutates the constant, so the *next* project without that file
    inherits the previous project's data. Copying here fixes every call site
    at once instead of trusting each one to copy correctly.
    """
    path = Path(path)
    for candidate, is_bak in ((path, False), (_bak_path(path), True)):
        if not candidate.exists():
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            if is_bak:
                logger.warning("recovered %s from .bak (primary was missing/corrupt)", path)
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("failed to read %s: %s", candidate, exc)
            continue
    return copy.deepcopy(default)


def safe_write_text(path: Path, text: str) -> None:
    """Same atomic temp-file + .bak + os.replace dance as safe_write_json, for
    the plain-text files of an identity profile (marca.md, frame.md,
    regras.md, pessoas.csv) — they are edited in the app and by the editor
    CLI, and a half-written manual would be as bad as a half-written JSON."""
    path = Path(path)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _tmp_path(path)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copy2(path, _bak_path(path))
        _replace(tmp, path)


def safe_read_text(path: Path, default: str = "") -> str:
    """Read a text file, falling back to .bak, then to `default`."""
    path = Path(path)
    for candidate in (path, _bak_path(path)):
        if not candidate.exists():
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("failed to read %s: %s", candidate, exc)
    return default
