"""Registro central de projetos: um JSON por projeto em data/projects/.

Tudo que o app sabe sobre um projeto e que não é arquivo de trabalho — etapa,
sessões do cronômetro, custo, histórico, tags, prazo, notas, feed de ajustes,
checklist, links — vive aqui, e não mais dentro da pasta do projeto. A pasta
continua sendo o lugar onde o trabalho acontece (arquivos, Finder, organizador),
mas pode sumir do disco que o registro fica: o projeto continua listado com um
aviso de "pasta não encontrada" e as horas seguem contando nas estatísticas.

O caminho é ``data/projects/<client_id>/<NNN>.json``. A chave é o mesmo id de
sempre (``"<client_id>:<NNN>"``), então ``timer_active.json``, o ledger mensal
e as URLs do frontend continuam válidos sem tradução.

Toda escrita de registro é read-modify-write via ``update`` sob um lock
próprio: o lock de ``persistence.py`` cobre cada gravação isolada, mas o
cronômetro, o watchdog e as requisições mexem no mesmo registro ao mesmo
tempo, e um ``load``/``save`` sem lock entre eles perderia a escrita do outro.
"""

import copy
import logging
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import machine, paths
from .persistence import safe_read_json, safe_write_json

logger = logging.getLogger("clientela.store")

_lock = threading.RLock()

DEFAULT_TIMER = {"status": "idle", "running_start": None, "sessions": []}
DEFAULT_COST = {"valor_individual": 0.0, "custos_extra": [], "oculto_estatisticas": False}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def split_id(project_id: str) -> tuple[str, str] | None:
    if not project_id or ":" not in project_id:
        return None
    client_id, num = project_id.split(":", 1)
    if not client_id or not num:
        return None
    return client_id, num


def make_id(client_id: str, num: str) -> str:
    return f"{client_id}:{num}"


def record_path(project_id: str) -> Path | None:
    parts = split_id(project_id)
    if not parts:
        return None
    client_id, num = parts
    return paths.PROJECTS_DIR / client_id / f"{num}.json"


def new_record(
    client_id: str,
    num: str,
    name: str,
    folder_name: str,
    path: str | None,
    created_at: float | None = None,
    stage: str = "backlog",
    origem: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "id": make_id(client_id, num),
        "client_id": client_id,
        "num": num,
        "name": name,
        "folder_name": folder_name,
        "last_known_path": path,
        "created_at": created_at if created_at is not None else datetime.now().timestamp(),
        "registered_at": _now_iso(),
        # {"os": "mac"|"windows", "host": nome do computador} de onde o
        # registro nasceu (machine.origin). None nos registros de antes do
        # Windows — o app só rodava no Mac. Quem cria passa explicitamente;
        # o default fica None porque _normalize usa new_record como base, e
        # carimbar a máquina atual ali assinaria registros antigos com ela.
        "origem": origem,
        "stage": stage,
        "tags": [],
        "due_date": None,
        "notes": "",
        # URL do post publicado (Instagram, YouTube…), quando o projeto sai.
        "link_postagem": "",
        # None, ou {"motivo": str, "desde": iso} enquanto o projeto espera
        # alguém de fora (locutor, colorista, trilha…). Independe da etapa.
        "aguardando_externo": None,
        # True quando o projeto foi criado sem pasta de propósito (tarefa de
        # cliente pessoal, por exemplo): sem o aviso de "pasta não encontrada".
        "sem_pasta": False,
        # None, ou o clipe bruto que originou o projeto (clips.py): caminho
        # relativo à pasta do projeto em "arquivo", mais de onde ele veio.
        "midia_original": None,
        # None, ou o corte vertical automático de um clipe esperando a pessoa
        # (editor.review_cut): {"pasta", "estado": "pendente" | "aprovado" |
        # "em_ajuste" | "descartado", "desde", "job"}.
        "revisao_corte": None,
        "timer": copy.deepcopy(DEFAULT_TIMER),
        "cost": copy.deepcopy(DEFAULT_COST),
        "history": [],
        "comments": [],
        "checklist": [],
        "links": [],
    }


def _normalize(record: dict[str, Any]) -> dict[str, Any]:
    """Garante todas as chaves num registro lido do disco, pra ninguém
    precisar de ``.get`` com default espalhado pelo código."""
    parts = split_id(record.get("id", ""))
    client_id, num = parts if parts else (record.get("client_id", ""), record.get("num", ""))
    base = new_record(
        client_id,
        num,
        record.get("name", ""),
        record.get("folder_name", ""),
        record.get("last_known_path"),
        record.get("created_at"),
    )
    for key in ("timer", "cost"):
        merged = dict(base[key])
        merged.update(record.get(key) or {})
        record[key] = merged
    for key, value in base.items():
        record.setdefault(key, value)
    return record


def load(project_id: str) -> dict[str, Any] | None:
    path = record_path(project_id)
    if path is None:
        return None
    record = safe_read_json(path, None)
    if not isinstance(record, dict):
        return None
    return _normalize(record)


def exists(project_id: str) -> bool:
    path = record_path(project_id)
    return bool(path) and (path.exists() or path.with_name(path.name + ".bak").exists())


def save(record: dict[str, Any]) -> None:
    path = record_path(record["id"])
    if path is None:
        raise ValueError(f"id de projeto inválido: {record.get('id')!r}")
    safe_write_json(path, record)


def update(project_id: str, fn: Callable[[dict[str, Any]], Any]) -> dict[str, Any] | None:
    """Read-modify-write atômico. ``fn`` recebe o registro e muda no lugar."""
    with _lock:
        record = load(project_id)
        if record is None:
            return None
        fn(record)
        save(record)
        return record


def list_for_client(client_id: str) -> list[dict[str, Any]]:
    client_dir = paths.PROJECTS_DIR / client_id
    if not client_dir.is_dir():
        return []
    out = []
    for path in sorted(client_dir.glob("*.json")):
        record = load(make_id(client_id, path.stem))
        if record is not None:
            out.append(record)
    return out


def iter_all() -> Iterator[dict[str, Any]]:
    if not paths.PROJECTS_DIR.is_dir():
        return
    for client_dir in sorted(paths.PROJECTS_DIR.iterdir()):
        if client_dir.is_dir():
            yield from list_for_client(client_dir.name)


def delete(project_id: str) -> bool:
    path = record_path(project_id)
    if path is None:
        return False
    with _lock:
        removed = False
        for candidate in (path, path.with_name(path.name + ".bak"), path.with_name(path.name + ".tmp")):
            if candidate.exists():
                candidate.unlink()
                removed = True
        return removed


def delete_client(client_id: str) -> int:
    """Apaga todos os registros de um cliente. Devolve quantos eram."""
    client_dir = paths.PROJECTS_DIR / client_id
    if not client_dir.is_dir():
        return 0
    with _lock:
        count = len(list(client_dir.glob("*.json")))
        shutil.rmtree(client_dir, ignore_errors=True)
        return count


# ----------------------------------------------------------------- importação


def _read_legacy(folder_dir: Path, log_relative: str, root_name: str | None) -> Any:
    """Lê um arquivo legado: primeiro em LOG/, depois solto na raiz da pasta
    (formato anterior à migração pra LOG/). ``None`` se não há nenhum."""
    data = safe_read_json(folder_dir / log_relative, None)
    if data is None and root_name:
        data = safe_read_json(folder_dir / root_name, None)
    return data


def _import_legacy(record: dict[str, Any], folder_dir: Path) -> list[str]:
    """Copia pro registro o que existir de metadados antigos dentro da pasta.
    Nunca apaga nem reescreve os arquivos de origem."""
    imported: list[str] = []

    meta = _read_legacy(folder_dir, paths.PROJECT_META, ".meta.json")
    if isinstance(meta, dict):
        record["tags"] = [t for t in meta.get("tags") or [] if isinstance(t, str)]
        record["due_date"] = meta.get("due_date")
        record["notes"] = meta.get("notes") or ""
        imported.append("meta")

    stage = _read_legacy(folder_dir, paths.PROJECT_STAGE, ".stage.json")
    if isinstance(stage, dict) and stage.get("stage"):
        record["stage"] = stage["stage"]
        imported.append("stage")

    time_data = _read_legacy(folder_dir, paths.PROJECT_TIME, ".time.json")
    if isinstance(time_data, dict):
        record["timer"] = {
            "status": time_data.get("status", "idle"),
            "running_start": time_data.get("running_start"),
            "sessions": list(time_data.get("sessions") or []),
        }
        imported.append("time")

    cost = _read_legacy(folder_dir, paths.PROJECT_COST, ".cost.json")
    if isinstance(cost, dict):
        record["cost"] = {
            "valor_individual": cost.get("valor_individual", 0.0),
            "custos_extra": list(cost.get("custos_extra") or []),
            "oculto_estatisticas": bool(cost.get("oculto_estatisticas", False)),
        }
        imported.append("cost")

    events = _read_legacy(folder_dir, paths.PROJECT_HISTORY, ".history.json")
    if isinstance(events, list):
        record["history"] = events
        imported.append("history")

    for key, relative in (("comments", paths.PROJECT_COMMENTS), ("checklist", paths.PROJECT_CHECKLIST)):
        feed = _read_legacy(folder_dir, relative, None)
        if isinstance(feed, dict) and isinstance(feed.get("items"), list):
            record[key] = feed["items"]
            imported.append(key)

    links = read_links_txt(folder_dir)
    if links:
        record["links"] = links
        imported.append("links")

    return imported


def read_links_txt(folder_dir: Path) -> list[dict[str, str]]:
    links_txt = folder_dir / "Links" / "links.txt"
    if not links_txt.is_file():
        return []
    try:
        text = links_txt.read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, rest = line.partition(":")
        if label.strip() and rest.strip():
            out.append({"label": label.strip(), "url": rest.strip()})
    return out


def register_from_folder(
    client_id: str,
    num: str,
    name: str,
    folder_dir: Path,
    stage: str = "backlog",
    folder_name: str | None = None,
) -> dict[str, Any]:
    """Cria o registro de uma pasta vista pela primeira vez, importando os
    metadados legados que ela tiver. Se o registro já existe, devolve ele
    intocado — a partir daí a pasta é só a pasta."""
    project_id = make_id(client_id, num)
    with _lock:
        existing = load(project_id)
        if existing is not None:
            return existing
        try:
            created_at = folder_dir.stat().st_ctime
        except OSError:
            created_at = None
        record = new_record(
            client_id, num, name, folder_name or folder_dir.name, str(folder_dir), created_at, stage, machine.origin()
        )
        imported = _import_legacy(record, folder_dir)
        save(record)
        if imported:
            logger.info("projeto %s registrado a partir da pasta (importado: %s)", project_id, ", ".join(imported))
        return record
