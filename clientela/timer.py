import uuid
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Any

from . import clients as clients_module
from . import paths, projects, store
from .persistence import safe_read_json, safe_write_json

BUSINESS_START = dtime(9, 0)
BUSINESS_END = dtime(19, 0)
STATS_FLOOR_SECONDS = 15 * 60
_DEFAULT_SINGLE_EVENT_ESTIMATE_SECONDS = 15 * 60

DEFAULT_TIME = store.DEFAULT_TIMER


def _now_iso() -> str:
    return datetime.now().isoformat()


def _update_time(project_id: str, fn) -> dict[str, Any] | None:
    """Aplica ``fn(time_data)`` no bloco ``timer`` do registro do projeto."""
    return store.update(project_id, lambda rec: fn(rec["timer"]))


def _read_active() -> dict[str, Any]:
    return safe_read_json(paths.TIMER_ACTIVE_JSON, {})


def _write_active(data: dict[str, Any]) -> None:
    safe_write_json(paths.TIMER_ACTIVE_JSON, data)


def crop_to_business_hours(start: datetime, end: datetime) -> int:
    """Seconds of [start, end) that fall on a weekday within 09:00-19:00.
    A session crossing midnight/weekend only counts the in-window fraction."""
    if end <= start:
        return 0
    total = timedelta()
    day = start.date()
    last_day = end.date()
    while day <= last_day:
        if day.weekday() < 5:  # Mon-Fri
            win_start = datetime.combine(day, BUSINESS_START)
            win_end = datetime.combine(day, BUSINESS_END)
            seg_start = max(start, win_start)
            seg_end = min(end, win_end)
            if seg_end > seg_start:
                total += seg_end - seg_start
        day += timedelta(days=1)
    return int(total.total_seconds())


def effective_seconds(session: dict[str, Any]) -> int:
    """Seconds this session contributes to stats. Manual/estimated entries
    count in full; real timer sessions get the business-hours crop and are
    dropped entirely if under the 15-minute floor."""
    if session.get("manual") or session.get("estimated"):
        return int(session.get("seconds", 0))
    business_seconds = session.get("business_seconds", session.get("seconds", 0))
    return business_seconds if business_seconds >= STATS_FLOOR_SECONDS else 0


def get_status(project_id: str) -> dict[str, Any] | None:
    project = projects.get_project(project_id)
    if not project:
        return None
    sessions = project["sessions"]
    return {
        "project_id": project_id,
        "status": project["timer_status"],
        "running_start": project["running_start"],
        "sessions": sessions,
        "total_seconds": sum(s.get("seconds", 0) for s in sessions),
        "effective_seconds": sum(effective_seconds(s) for s in sessions),
    }


def get_active() -> dict[str, Any] | None:
    active = _read_active()
    return active if active and active.get("project_id") else None


# Acima disso, um cronômetro rodando quase certamente foi esquecido ligado.
STALE_TIMER_HOURS = 8


def stale_running_timer(threshold_hours: float = STALE_TIMER_HOURS) -> dict[str, Any] | None:
    """Cronômetro que está rodando há tempo demais para ser real.

    `running_start` é só um timestamp e o tempo decorrido é `agora - start`, então
    fechar o app com o cronômetro ligado faz ele "contar" a noite inteira — e a
    thread de auto-pausa por inatividade não existe enquanto o app está desligado
    para impedir isso. Esta checagem roda quando o frontend carrega.
    """
    active = get_active()
    if not active:
        return None
    try:
        started = datetime.fromisoformat(active["running_start"])
    except (KeyError, TypeError, ValueError):
        return None

    elapsed = (datetime.now() - started).total_seconds()
    if elapsed < threshold_hours * 3600:
        return None

    project = projects.get_project(active["project_id"])
    return {
        "project_id": active["project_id"],
        "project_title": f"{project['num']} - {project['name']}" if project else active["project_id"],
        "client_id": active["project_id"].split(":")[0],
        "running_start": active["running_start"],
        "elapsed_seconds": int(elapsed),
        # o que entraria de fato nas estatísticas: sessões de cronômetro sofrem
        # corte para horário comercial, então as duas contas raramente batem
        "billable_seconds": crop_to_business_hours(started, datetime.now()),
    }


def discard_running_session(project_id: str) -> dict[str, Any] | None:
    """Para o cronômetro jogando a sessão fora, em vez de gravá-la.

    Para o caso do cronômetro esquecido: o tempo decorrido não corresponde a
    trabalho nenhum, então gravá-lo (mesmo cortado) sujaria a estatística.
    """
    if not projects.get_project(project_id):
        return None

    def apply(time_data: dict[str, Any]) -> None:
        time_data["status"] = "idle"
        time_data["running_start"] = None

    _update_time(project_id, apply)
    active = _read_active()
    if active.get("project_id") == project_id:
        _write_active({})
    return get_status(project_id)


def _finalize_running_session(time_data: dict[str, Any]) -> None:
    if time_data.get("status") != "running" or not time_data.get("running_start"):
        return
    start_dt = datetime.fromisoformat(time_data["running_start"])
    end_dt = datetime.now()
    seconds = max(0, int((end_dt - start_dt).total_seconds()))
    business_seconds = crop_to_business_hours(start_dt, end_dt)
    time_data["sessions"].append(
        {
            "start": time_data["running_start"],
            "end": end_dt.isoformat(),
            "seconds": seconds,
            "business_seconds": business_seconds,
            "manual": False,
            "estimated": False,
        }
    )
    time_data["running_start"] = None


def start_timer(project_id: str) -> dict[str, Any] | None:
    project = projects.get_project(project_id)
    if not project:
        return None

    active = _read_active()
    if active.get("project_id") and active["project_id"] != project_id:
        pause_timer(active["project_id"])

    def apply(time_data: dict[str, Any]) -> None:
        if time_data.get("status") != "running":
            time_data["status"] = "running"
            time_data["running_start"] = _now_iso()

    record = _update_time(project_id, apply)
    _write_active({"project_id": project_id, "running_start": record["timer"]["running_start"]})
    return get_status(project_id)


def pause_timer(project_id: str) -> dict[str, Any] | None:
    if not projects.get_project(project_id):
        return None

    def apply(time_data: dict[str, Any]) -> None:
        if time_data.get("status") == "running":
            _finalize_running_session(time_data)
            time_data["status"] = "paused"

    _update_time(project_id, apply)
    active = _read_active()
    if active.get("project_id") == project_id:
        _write_active({})
    return get_status(project_id)


def stop_timer(project_id: str) -> dict[str, Any] | None:
    if not projects.get_project(project_id):
        return None

    def apply(time_data: dict[str, Any]) -> None:
        if time_data.get("status") == "running":
            _finalize_running_session(time_data)
        time_data["status"] = "idle"
        time_data["running_start"] = None

    _update_time(project_id, apply)
    active = _read_active()
    if active.get("project_id") == project_id:
        _write_active({})
    return get_status(project_id)


def log_manual(
    project_id: str, start_iso: str, end_iso: str, note: str = ""
) -> dict[str, Any] | None:
    if not projects.get_project(project_id):
        return None
    start_dt = datetime.fromisoformat(start_iso)
    end_dt = datetime.fromisoformat(end_iso)
    seconds = max(0, int((end_dt - start_dt).total_seconds()))
    session = {
        "start": start_iso,
        "end": end_iso,
        "seconds": seconds,
        "manual": True,
        "estimated": False,
        "note": note,
    }
    _update_time(project_id, lambda time_data: time_data["sessions"].append(session))
    return get_status(project_id)


def estimate_sessions(project_id: str, gap_minutes: int = 90) -> list[dict[str, Any]]:
    """Group file-modification timestamps under the project folder into
    candidate work sessions (events within `gap_minutes` of each other)."""
    project = projects.get_project(project_id)
    if not project or not project["folder_available"]:
        return []
    project_dir = Path(project["path"])
    subs = projects.subproject_dirs(project_dir)
    mtimes = sorted(
        datetime.fromtimestamp(p.stat().st_mtime)
        for p in project_dir.rglob("*")
        if p.is_file() and not p.name.startswith(".")
        and not (subs and project_dir / p.relative_to(project_dir).parts[0] in subs)
    )
    groups: list[list[datetime]] = []
    gap = timedelta(minutes=gap_minutes)
    for t in mtimes:
        if groups and (t - groups[-1][-1]) <= gap:
            groups[-1].append(t)
        else:
            groups.append([t])

    sessions = []
    for g in groups:
        start, end = g[0], g[-1]
        seconds = int((end - start).total_seconds()) or _DEFAULT_SINGLE_EVENT_ESTIMATE_SECONDS
        sessions.append(
            {
                "id": uuid.uuid4().hex,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "seconds": seconds,
                "manual": False,
                "estimated": True,
            }
        )
    return sessions


def apply_estimate(project_id: str, sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not projects.get_project(project_id):
        return None
    _update_time(project_id, lambda time_data: time_data["sessions"].extend(sessions))
    return get_status(project_id)


def remove_estimate(project_id: str, session_id: str) -> dict[str, Any] | None:
    if not projects.get_project(project_id):
        return None

    def apply(time_data: dict[str, Any]) -> None:
        time_data["sessions"] = [s for s in time_data["sessions"] if s.get("id") != session_id]

    _update_time(project_id, apply)
    return get_status(project_id)


def estimate_all(gap_minutes: int = 90) -> dict[str, int]:
    results = {}
    for client in clients_module.list_clients():
        for project in projects.list_projects(client["id"]):
            sessions = estimate_sessions(project["id"], gap_minutes)
            if sessions:
                apply_estimate(project["id"], sessions)
                results[project["id"]] = len(sessions)
    return results
