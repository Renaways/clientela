import re
from datetime import datetime
from typing import Any

from . import clients as clients_module
from . import paths
from . import projects as projects_module
from . import stages
from .persistence import safe_read_json, safe_write_json

DEFAULT_TEMPLATE = (
    "📌 {cliente} — {projeto}\n"
    "Etapa atual: {etapa}\n"
    "{prazo}\n"
    "\n"
    "{links}"
)

# A mensagem do WhatsApp é curta e vai pro cliente; a da tarefa é estruturada e
# vai pro ClickUp, onde descrição e horas fazem sentido e emoji não faz.
DEFAULT_TASK_TEMPLATE = (
    "{projeto}\n"
    "\n"
    "Etapa: {etapa}\n"
    "{prazo}\n"
    "Horas até aqui: {horas}\n"
    "\n"
    "{descricao}\n"
    "\n"
    "{links}"
)

# Stages where the client cares about reviewing/approving work: send the task
# link + Frame.io. Everywhere else (backlog/produção/liberado/concluído) the client
# cares about the deliverable: send the task link + Google Drive.
REVIEW_STAGES = {"revisao", "aprovacao_tecnica", "ajuste"}

_TASK_KEYWORDS = ("clickup", "tarefa", "task")
# Mesma lista que decide se o link do cliente entra na lista do projeto —
# um só lugar para as duas telas concordarem sobre o que é "um Frame.io".
_FRAMEIO_KEYWORDS = projects_module.FRAMEIO_KEYWORDS
_DRIVE_KEYWORDS = ("drive",)


def _matches(label: str, keywords: tuple[str, ...]) -> bool:
    low = label.lower()
    return any(k in low for k in keywords)


def _format_due_date(due_date: str | None) -> str:
    if not due_date:
        return ""
    try:
        dt = datetime.fromisoformat(due_date)
    except ValueError:
        try:
            dt = datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            return f"Prazo: {due_date}"
    return f"Prazo: {dt.strftime('%d/%m/%Y')}"


def get_settings() -> dict[str, Any]:
    settings = safe_read_json(paths.MESSAGE_SETTINGS_JSON, {})
    return {
        "template": settings.get("template") or DEFAULT_TEMPLATE,
        "task_template": settings.get("task_template") or DEFAULT_TASK_TEMPLATE,
    }


def set_settings(
    template: str | None = None, task_template: str | None = None
) -> dict[str, Any]:
    current = get_settings()
    if template is not None:
        current["template"] = template
    if task_template is not None:
        current["task_template"] = task_template
    safe_write_json(paths.MESSAGE_SETTINGS_JSON, current)
    return current


def _format_hours(project: dict[str, Any]) -> str:
    seconds = sum(s.get("seconds", 0) for s in project.get("sessions") or [])
    horas, resto = divmod(int(seconds), 3600)
    return f"{horas}h {resto // 60:02d}m"


def get_link_candidates(project_id: str) -> dict[str, Any] | None:
    """List every link that could go into the status message for this
    project, pre-checking the ones the stage-based rule would send, so the
    UI can offer them as a checklist the user can still override."""
    project = projects_module.get_project(project_id)
    if not project:
        return None
    is_review = project["stage"] in REVIEW_STAGES

    candidates = []
    for link in projects_module.list_links_with_client_defaults(project_id):
        label = link["label"]
        if _matches(label, _TASK_KEYWORDS):
            checked, reason = True, "tarefa"
        elif _matches(label, _FRAMEIO_KEYWORDS):
            checked, reason = is_review, "revisão"
        elif _matches(label, _DRIVE_KEYWORDS):
            checked, reason = not is_review, "entrega"
        else:
            checked, reason = False, ""
        candidates.append(
            {"label": label, "url": link["url"], "checked": checked, "reason": reason}
        )

    return {
        "stage": project["stage"],
        "stage_group": "revisao" if is_review else "outras",
        "candidates": candidates,
    }


def render_message(
    project_id: str,
    selected_links: list[dict[str, str]],
    kind: str = "whatsapp",
) -> str | None:
    project = projects_module.get_project(project_id)
    if not project:
        return None
    client = clients_module.get_client(project["client_id"])
    settings = get_settings()
    template = settings["task_template"] if kind == "task" else settings["template"]

    if selected_links:
        links_block = "\n".join(
            f"{link.get('label', '').upper()}: {link.get('url', '')}" for link in selected_links
        )
    else:
        links_block = ""

    values = {
        "cliente": client["name"] if client else "",
        "projeto": f"{project['num']} - {project['name']}",
        "etapa": stages.labels().get(project["stage"], project["stage"]),
        "prazo": _format_due_date(project.get("due_date")),
        "links": links_block,
        "descricao": project.get("notes") or "",
        "horas": _format_hours(project),
    }
    message = template
    for key, value in values.items():
        message = message.replace("{" + key + "}", value)

    # Codes left blank (no prazo / no links) leave behind empty lines —
    # collapse runs of them so the message doesn't look half-broken.
    message = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", message)
    return message.strip("\n")
