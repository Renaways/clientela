import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from . import history, paths, projects, store, timer
from .persistence import safe_read_json, safe_write_json

logger = logging.getLogger("clientela.tags")


def list_tags() -> list[str]:
    return safe_read_json(paths.TAGS_JSON, [])


def _count_tags_in_store() -> Counter:
    """Conta o uso de cada tag varrendo os registros de todos os projetos."""
    counts: Counter = Counter()
    for record in store.iter_all():
        for tag in record.get("tags") or []:
            if isinstance(tag, str) and tag.strip():
                counts[tag.strip()] += 1
    return counts


def rebuild_registry_from_disk() -> list[str]:
    """Reconcilia data/tags.json com as tags que existem de fato nos projetos.

    O registro global é só um índice do que os projetos já usam — a fonte da
    verdade é o registro de cada projeto em data/projects/. Mas o índice pode sumir
    (data/ é gitignored, não vem de nenhum clone) ou nascer vazio, e aí tudo
    que lê /api/tags nasce vazio junto: o seletor do "Novo projeto", o filtro
    por tag e os pesos do rateio em cost_settings — mesmo com dezenas de
    projetos tagueados no disco. Era exatamente esse o estado encontrado: 71
    projetos com tag, `tags.json` inexistente.

    União, não substituição: uma tag que está no registro e em nenhum projeto
    foi criada à mão e ainda não usada, então sobrevive. A ordem é por
    frequência de uso (empate resolvido pelo nome), que é a ordem útil num
    seletor. Só grava quando o resultado muda, pra não girar o .bak a cada boot.

    Chamada no boot, depois da importação dos projetos do disco.
    """
    counts = _count_tags_in_store()
    known = list_tags()
    ordered = sorted(counts, key=lambda t: (-counts[t], t.lower()))
    ordered += [t for t in known if t not in counts]
    if ordered != known:
        safe_write_json(paths.TAGS_JSON, ordered)
        novas = [t for t in ordered if t not in known]
        logger.info(
            "registro de tags reconciliado: %s tag(s), %s nova(s) vinda(s) do disco",
            len(ordered),
            len(novas),
        )
    return ordered


def add_tag(name: str) -> list[str]:
    name = name.strip()
    tags = list_tags()
    if name and name not in tags:
        tags.append(name)
        safe_write_json(paths.TAGS_JSON, tags)
    return tags


def get_project_tags(project_id: str) -> list[str]:
    project = projects.get_project(project_id)
    return project["tags"] if project else []


def set_project_tags(project_id: str, tags: list[str]) -> dict[str, Any] | None:
    if not projects.get_project(project_id):
        return None
    store.update(project_id, lambda rec: rec.__setitem__("tags", list(tags)))
    for tag in tags:
        add_tag(tag)
    return projects.get_project(project_id)


def get_due_date(project_id: str) -> str | None:
    project = projects.get_project(project_id)
    return project["due_date"] if project else None


def set_due_date(project_id: str, due_date: str | None) -> dict[str, Any] | None:
    if not projects.get_project(project_id):
        return None
    store.update(project_id, lambda rec: rec.__setitem__("due_date", due_date))
    return projects.get_project(project_id)


def set_waiting_external(project_id: str, motivo: str | None) -> dict[str, Any] | None:
    """Marca (``motivo`` str, pode ser vazio) ou desmarca (``None``) o projeto
    como parado esperando equipe externa.

    É um status à parte da etapa de propósito: o projeto pode travar em
    qualquer ponto do pipeline, e virar etapa apagaria de onde ele parou — e
    mexeria no cronômetro, que a etapa controla. Editar só o motivo mantém o
    ``desde`` original, que é o que conta os dias de espera.

    Marcar pausa o cronômetro se ele estiver rodando neste projeto (esperar
    não é trabalhar); desmarcar não religa nada.
    """
    project = projects.get_project(project_id)
    if not project:
        return None
    atual = project.get("aguardando_externo")

    if motivo is None:
        if not atual:
            return project
        store.update(project_id, lambda rec: rec.__setitem__("aguardando_externo", None))
        history.append_event(project_id, "externo_retomado", {"motivo": atual.get("motivo", "")})
        return projects.get_project(project_id)

    motivo = motivo.strip()
    novo = {
        "motivo": motivo,
        "desde": atual["desde"] if atual else datetime.now(timezone.utc).isoformat(),
    }
    store.update(project_id, lambda rec: rec.__setitem__("aguardando_externo", novo))
    if not atual:
        history.append_event(project_id, "aguardando_externo", {"motivo": motivo})
        if project.get("timer_status") == "running":
            timer.pause_timer(project_id)
    return projects.get_project(project_id)
