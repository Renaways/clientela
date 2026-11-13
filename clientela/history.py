from datetime import datetime, timezone
from typing import Any

from . import store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_event(project_id: str, action: str, details: dict[str, Any] | None = None) -> None:
    event = {"action": action, "details": details or {}, "at": _now_iso()}
    store.update(project_id, lambda rec: rec.setdefault("history", []).append(event))


def get_history(project_id: str) -> list[dict[str, Any]]:
    record = store.load(project_id)
    return list(record.get("history") or []) if record else []


def last_stage_change_to(events: list[dict[str, Any]], stage: str) -> str | None:
    """Quando o projeto entrou nesta etapa pela última vez, ou None.

    Três formatos de evento convivem nos registros, de versões diferentes do
    app — contados nos dados reais: 58 no formato atual
    ({"action": "etapa_alterada", "details": {"para": ...}, "at": ...}) e 41 em
    dois formatos antigos ({"type": "stage_change", "to"/"to_stage", "ts"}).
    Ler só o atual acharia data em pouco mais da metade dos eventos, e a
    ordenação do arquivo cairia no fallback à toa.
    """
    for event in reversed(events):
        destino = (
            (event.get("details") or {}).get("para")
            or event.get("to_stage")
            or event.get("to")
        )
        if destino == stage:
            return event.get("at") or event.get("ts")
    return None
