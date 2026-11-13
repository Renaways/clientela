import logging
import shutil
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths
from .persistence import safe_read_json, safe_write_json

logger = logging.getLogger("clientela.backup")

RETENTION = 14
DAILY_INTERVAL_SECONDS = 24 * 3600

GLOBAL_JSON_FILES = (
    paths.CLIENTS_JSON,
    paths.TAGS_JSON,
    paths.COST_SETTINGS_JSON,
    paths.MONTHLY_LEDGER_JSON,
    paths.CONFIG_JSON,
    paths.TIMER_ACTIVE_JSON,
)


def run_backup() -> Path:
    """Zip only business-data JSON — global config + the project registry in
    data/projects/. Never includes media (Brutos/Edit/Export), which the
    user's own backup already covers."""
    paths.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = paths.BACKUPS_DIR / f"backup-{timestamp}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in GLOBAL_JSON_FILES:
            if f.exists():
                zf.write(f, arcname=f"data/{f.name}")
        if paths.PROJECTS_DIR.is_dir():
            for record in sorted(paths.PROJECTS_DIR.rglob("*.json")):
                rel = record.relative_to(paths.PROJECTS_DIR)
                zf.write(record, arcname=f"data/projects/{rel.as_posix()}")
        # Os perfis de identidade moram na pasta de cada cliente
        # (<cliente>/_Estagiário/), que o backup do próprio usuário já cobre.
        if paths.EDITOR_CONFIG_JSON.exists():
            zf.write(paths.EDITOR_CONFIG_JSON, arcname="data/editor/config.json")

    _apply_retention()

    cfg = get_backup_config()
    destination = cfg.get("external_destination")
    if destination:
        try:
            shutil.copy2(zip_path, Path(destination) / zip_path.name)
        except OSError:
            logger.warning("failed to copy backup to external destination %s", destination)

    return zip_path


def _apply_retention() -> None:
    backups = sorted(paths.BACKUPS_DIR.glob("backup-*.zip"))
    excess = len(backups) - RETENTION
    for old in backups[: max(0, excess)]:
        old.unlink(missing_ok=True)


def list_backups() -> list[dict[str, Any]]:
    return [
        {"name": p.name, "size": p.stat().st_size, "created_at": p.stat().st_mtime}
        for p in sorted(paths.BACKUPS_DIR.glob("backup-*.zip"), reverse=True)
    ]


def get_backup_config() -> dict[str, Any]:
    config = safe_read_json(paths.CONFIG_JSON, {})
    return {"external_destination": None, **config.get("backup", {})}


def set_backup_config(**fields: Any) -> dict[str, Any]:
    config = safe_read_json(paths.CONFIG_JSON, {})
    backup_cfg = {**get_backup_config(), **fields}
    config["backup"] = backup_cfg
    safe_write_json(paths.CONFIG_JSON, config)
    return backup_cfg


def start_daily_scheduler() -> threading.Thread:
    def loop() -> None:
        while True:
            try:
                run_backup()
            except Exception:
                logger.exception("daily backup failed")
            time.sleep(DAILY_INTERVAL_SECONDS)

    thread = threading.Thread(target=loop, daemon=True, name="daily-backup")
    thread.start()
    return thread
