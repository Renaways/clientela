"""Cobertura da agregação `dashboard.global_stats` — a tela de estatísticas
gerais (todos os clientes juntos)."""

from datetime import datetime
from pathlib import Path

from clientela import clients as clients_module
from clientela import dashboard
from clientela import financials
from clientela import projects as projects_module
from clientela import stages as stages_module
from clientela import tags as tags_module
from clientela import timer


def _month() -> str:
    return financials.current_month()


def _log_hours(project_id: str, hours: float) -> None:
    """Lança horas via lançamento manual — sessões manuais não sofrem o corte
    de horário comercial nem o piso de 15 min, então o número é previsível."""
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    end = now.replace(hour=10 + int(hours), minute=int((hours % 1) * 60))
    timer.log_manual(project_id, now.isoformat(), end.isoformat())


def _make_client(tmp_path, name: str, **kwargs):
    client_dir = tmp_path / name
    client_dir.mkdir()
    return clients_module.create_client(name=name, path=str(client_dir), **kwargs)


def test_global_stats_soma_horas_de_todos_os_clientes(tmp_path):
    a = _make_client(tmp_path, "Cliente A")
    b = _make_client(tmp_path, "Cliente B")
    pa = projects_module.create_project(client_id=a["id"], name="Job A")
    pb = projects_module.create_project(client_id=b["id"], name="Job B")
    _log_hours(pa["id"], 2)
    _log_hours(pb["id"], 3)

    data = dashboard.global_stats()

    assert data["kpis"]["hours"] == 5
    assert data["kpis"]["client_count"] == 2
    assert data["kpis"]["project_count"] == 2
    # ordenado por horas, maior primeiro
    assert [c["name"] for c in data["hours_by_client"]] == ["Cliente B", "Cliente A"]
    assert data["hours_by_client"][0]["hours"] == 3


def test_global_stats_exclui_projetos_ocultos_das_estatisticas(tmp_path):
    client = _make_client(tmp_path, "Cliente")
    visivel = projects_module.create_project(client_id=client["id"], name="Comercial")
    oculto = projects_module.create_project(client_id=client["id"], name="Teste interno")
    _log_hours(visivel["id"], 4)
    _log_hours(oculto["id"], 10)
    financials.set_stats_visibility(oculto["id"], True)

    data = dashboard.global_stats()

    assert data["kpis"]["hours"] == 4
    assert data["kpis"]["project_count"] == 1
    assert [p["name"] for p in data["top_projects"]] == ["001 - Comercial"]


def test_global_stats_distribui_horas_entre_as_tags_do_projeto(tmp_path):
    client = _make_client(tmp_path, "Cliente")
    project = projects_module.create_project(client_id=client["id"], name="Job")
    tags_module.set_project_tags(project["id"], ["motion", "social"])
    _log_hours(project["id"], 6)

    data = dashboard.global_stats()

    # projeto com 2 tags divide as horas igualmente entre elas
    assert {t["tag"]: t["hours"] for t in data["tag_distribution"]} == {
        "motion": 3.0,
        "social": 3.0,
    }


def test_global_stats_conta_projetos_por_etapa_do_pipeline(tmp_path):
    client = _make_client(tmp_path, "Cliente")
    p1 = projects_module.create_project(client_id=client["id"], name="Um")
    p2 = projects_module.create_project(client_id=client["id"], name="Dois")
    projects_module.create_project(client_id=client["id"], name="Tres")
    stages_module.set_stage(p1["id"], "producao")
    stages_module.set_stage(p2["id"], "concluido")

    data = dashboard.global_stats()
    counts = {s["stage"]: s["count"] for s in data["stage_distribution"]}

    assert counts["backlog"] == 1
    assert counts["producao"] == 1
    assert counts["concluido"] == 1
    # "ativos" = tudo que ainda não foi concluído
    assert data["kpis"]["active_projects"] == 2
    # todas as 7 etapas aparecem, na ordem do pipeline, mesmo zeradas
    assert [s["stage"] for s in data["stage_distribution"]] == stages_module.STAGES


def test_global_stats_sem_clientes_nao_quebra(tmp_path):
    data = dashboard.global_stats()

    assert data["kpis"] == {
        "hours": 0.0,
        "value": 0.0,
        "cost": 0.0,
        "profit": 0.0,
        "client_count": 0,
        "project_count": 0,
        "active_projects": 0,
    }
    assert data["hours_by_client"] == []
    assert data["tag_distribution"] == []


def test_financeiro_configurado_ignora_orcamento_sem_lado_de_custo(tmp_path):
    """Orçamento sozinho não conta: sem pró-labore/custos fixos o custo-hora é
    zero, o custo sai zero e o "lucro" vira só uma cópia do orçamento."""
    client = _make_client(tmp_path, "Cliente")
    assert dashboard.financeiro_configurado() is False

    clients_module.set_budget(client["id"], 5000.0)
    assert dashboard.financeiro_configurado() is False
    assert financials.hourly_cost() == 0.0

    financials.set_cost_settings(pro_labore_mensal=8000.0)
    assert dashboard.financeiro_configurado() is True


def test_financeiro_configurado_reconhece_pro_labore_e_custo_fixo(tmp_path):
    _make_client(tmp_path, "Cliente")
    assert dashboard.financeiro_configurado() is False

    financials.add_fixed_cost("Aluguel", 1200.0)
    assert dashboard.financeiro_configurado() is True

    financials.set_cost_settings(custos_fixos=[])
    assert dashboard.financeiro_configurado() is False

    financials.set_cost_settings(pro_labore_mensal=8000.0)
    assert dashboard.financeiro_configurado() is True


def test_global_stats_denuncia_pastas_que_dividem_o_mesmo_numero(tmp_path):
    """Duas pastas com o mesmo número viram o mesmo project_id: o total de horas
    conta as duas, mas o dicionário por projeto guarda só uma. A tela precisa
    avisar em vez de exibir um total que não fecha com a lista."""
    client = _make_client(tmp_path, "Cliente")
    projects_module.create_project(client_id=client["id"], name="Projetos")
    # segunda pasta com o mesmo prefixo numérico, criada fora do fluxo do app
    # (é assim que acontece de verdade: alguém renomeia/cria a pasta no Finder)
    (Path(client["path"]) / "001 - Projetos Campanha").mkdir()

    data = dashboard.global_stats()

    assert len(data["conflitos_de_numero"]) == 1
    conflito = data["conflitos_de_numero"][0]
    assert conflito["client_name"] == "Cliente"
    assert conflito["project_id"].endswith(":001")
    assert conflito["folders"] == ["001 - Projetos", "001 - Projetos Campanha"]


def test_global_stats_sem_conflito_devolve_lista_vazia(tmp_path):
    client = _make_client(tmp_path, "Cliente")
    projects_module.create_project(client_id=client["id"], name="Um")
    projects_module.create_project(client_id=client["id"], name="Dois")

    assert dashboard.global_stats()["conflitos_de_numero"] == []
