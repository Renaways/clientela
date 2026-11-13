"""Feed de ajustes e checklist de um projeto.

Duas listas por projeto, com o mesmo formato de item mas papéis diferentes:

- ``comments`` — o que o cliente pediu depois do
  briefing: "trocar a trilha", "reduzir o lettering do final". Cada entrada é
  datada e pode ser marcada como resolvida; o feed é o registro cronológico
  dos ajustes, então um item resolvido fica riscado em vez de sumir.
- ``checklist`` — to-do livre do projeto.

As duas moram no registro do projeto (store.py), em chaves próprias. Ficam
fora do ``history`` de propósito: aquele é log de sistema (etapa,
cronômetro) com três formatos convivendo, e ``last_stage_change_to`` varre
ele inteiro — texto livre ali só atrapalharia.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from . import projects as projects_module
from . import store

KINDS = ("comments", "checklist")
MAX_TEXT_LEN = 2000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"tipo de lista desconhecido: {kind!r}")


def list_items(project_id: str, kind: str) -> list[dict[str, Any]] | None:
    _check_kind(kind)
    record = store.load(project_id) if projects_module.get_project(project_id) else None
    return None if record is None else list(record.get(kind) or [])


def get_feed(project_id: str) -> dict[str, list[dict[str, Any]]] | None:
    """As duas listas de uma vez — é o que o detalhe do projeto carrega."""
    if not projects_module.get_project(project_id):
        return None
    record = store.load(project_id) or {}
    return {kind: list(record.get(kind) or []) for kind in KINDS}


def _mutate(project_id: str, kind: str, fn) -> list[dict[str, Any]] | None:
    """Aplica ``fn(items)`` na lista e devolve a lista gravada."""
    _check_kind(kind)
    if not projects_module.get_project(project_id):
        return None

    def apply(rec: dict[str, Any]) -> None:
        items = list(rec.get(kind) or [])
        rec[kind] = fn(items)

    record = store.update(project_id, apply)
    return None if record is None else list(record.get(kind) or [])


def add_item(project_id: str, kind: str, text: str) -> list[dict[str, Any]] | None:
    text = (text or "").strip()
    if not text:
        raise ValueError("texto vazio")
    item = {
        "id": uuid4().hex[:8],
        "text": text[:MAX_TEXT_LEN],
        "at": _now_iso(),
        "done": False,
        "done_at": None,
    }
    return _mutate(project_id, kind, lambda items: items + [item])


def set_done(project_id: str, kind: str, item_id: str, done: bool) -> list[dict[str, Any]] | None:
    def apply(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in items:
            if item.get("id") == item_id:
                item["done"] = bool(done)
                item["done_at"] = _now_iso() if done else None
                break
        else:
            raise KeyError(item_id)
        return items

    return _mutate(project_id, kind, apply)


def delete_item(project_id: str, kind: str, item_id: str) -> list[dict[str, Any]] | None:
    def apply(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        remaining = [item for item in items if item.get("id") != item_id]
        if len(remaining) == len(items):
            raise KeyError(item_id)
        return remaining

    return _mutate(project_id, kind, apply)
