from typing import Any

from . import automation, clients, history, paths, projects, store, timer
from .persistence import safe_read_json, safe_write_json

STAGES = ["backlog", "producao", "revisao", "aprovacao_tecnica", "ajuste", "liberado", "concluido"]

STAGE_LABELS = {
    "backlog": "Backlog",
    "producao": "Produção",
    "revisao": "Revisão",
    "aprovacao_tecnica": "Aprovação técnica",
    "ajuste": "Ajuste",
    "liberado": "Liberado",
    "concluido": "Concluído",
}
# Perfil "design" (config.json → perfil_uso, escolhido nas boas-vindas): mesmas etapas, nomes de estúdio de design.
# Espelho: STAGE_LABELS_DESIGN no index.html — mude os dois juntos.
STAGE_LABELS_DESIGN = {
    "backlog": "Briefing",
    "producao": "Criação",
    "revisao": "Revisão",
    "aprovacao_tecnica": "Apresentação",
    "ajuste": "Ajuste",
    "liberado": "Arte final",
    "concluido": "Entregue",
}


def perfil_uso() -> str:
    """'design' ou 'video' (padrão, e o de quem já usava o app)."""
    return "design" if safe_read_json(paths.CONFIG_JSON, {}).get("perfil_uso") == "design" else "video"


def labels() -> dict[str, str]:
    return STAGE_LABELS_DESIGN if perfil_uso() == "design" else STAGE_LABELS

# A etapa terminal: sai do quadro, vai pro arquivo, deixa de contar como
# "ativo". "Liberado" já foi essa etapa; hoje ela é o degrau anterior — o
# arquivo final está disponível, mas o projeto ainda não foi fechado. Os
# agregadores comparam com esta constante, não com a string, pra não repetir
# a troca da próxima vez que a ponta do pipeline mudar.
FINAL_STAGE = "concluido"

# A etapa em que o trabalho de fato acontece. Entrar nela liga o cronometro,
# sair dela para e grava a sessao.
PRODUCTION_STAGE = "producao"


def _sync_timer_with_stage(project_id: str, previous: str, stage: str) -> None:
    """Amarra o cronometro a etapa.

    O gatilho antigo era atividade de arquivo (watchdog), e o autosave do
    Premiere religava o cronometro a cada poucos minutos dentro de uma pasta que
    ja tinha saido de producao. A etapa e um sinal que o usuario controla.
    """
    if not automation.get_automation_config().get("auto_timer_on_producao", True):
        return
    if stage == PRODUCTION_STAGE and previous != PRODUCTION_STAGE:
        timer.start_timer(project_id)
    elif previous == PRODUCTION_STAGE and stage != PRODUCTION_STAGE:
        # pause_timer fecha a sessao em andamento (com corte de horario
        # comercial) em vez de descarta-la.
        timer.pause_timer(project_id)


def get_stage(project_id: str) -> str | None:
    project = projects.get_project(project_id)
    return project["stage"] if project else None


def set_stage(project_id: str, stage: str) -> dict[str, Any] | None:
    if stage not in STAGES:
        raise ValueError(f"invalid stage {stage!r}, must be one of {STAGES}")
    project = projects.get_project(project_id)
    if not project:
        return None
    previous = project["stage"]
    store.update(project_id, lambda rec: rec.__setitem__("stage", stage))
    if previous != stage:
        history.append_event(project_id, "etapa_alterada", {"de": previous, "para": stage})
        _sync_timer_with_stage(project_id, previous, stage)
    return projects.get_project(project_id)


def get_history(project_id: str) -> list[dict[str, Any]]:
    if not projects.get_project(project_id):
        return []
    return history.get_history(project_id)


def migrate_liberado_to_concluido() -> int:
    """Move quem estava em "liberado" quando ela era a etapa terminal para
    "concluido", que assumiu esse papel. Devolve quantos projetos mudaram.

    Só pode rodar uma vez: depois da troca, "liberado" é uma etapa comum do
    quadro, e um projeto colocado nela de propósito não pode ser empurrado pro
    arquivo no boot seguinte. O marcador fica em data/migrations.json, e não em
    cada projeto, porque projetos criados depois da troca nunca tiveram o
    sentido antigo.

    Não grava evento no histórico de propósito — ver projects._concluded_at.
    """
    marcadores = safe_read_json(paths.MIGRATIONS_JSON, {})
    if marcadores.get("liberado_para_concluido"):
        return 0
    movidos = 0
    for client in clients.list_clients():
        for project in projects.list_projects(client["id"]):
            if project["stage"] != "liberado":
                continue
            store.update(project["id"], lambda rec: rec.__setitem__("stage", FINAL_STAGE))
            movidos += 1
    marcadores["liberado_para_concluido"] = True
    safe_write_json(paths.MIGRATIONS_JSON, marcadores)
    return movidos
