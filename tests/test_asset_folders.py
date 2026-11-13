"""Convenção "00 - …": pasta de assets/projeto-base do cliente, que segue a
nomenclatura de projeto mas não é entrega e não pode entrar nas estatísticas."""

from datetime import datetime, timedelta
from pathlib import Path

from clientela import clients as clients_module
from clientela import dashboard
from clientela import financials
from clientela import projects as projects_module
from clientela import timer


def _make_client(tmp_path, name: str = "Cliente"):
    client_dir = tmp_path / name
    client_dir.mkdir()
    return clients_module.create_client(name=name, path=str(client_dir))


def _asset_folder(client: dict, nome: str) -> Path:
    d = Path(client["path"]) / nome
    d.mkdir()
    return d


def _log_hours(project_id: str, hours: float) -> None:
    """Lançamento manual: não sofre corte de horário comercial nem piso de
    15 min, então o total é exatamente `hours`."""
    base = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    timer.log_manual(project_id, base.isoformat(), (base + timedelta(hours=hours)).isoformat())


def test_reconhece_variacoes_de_pasta_de_assets():
    assert projects_module.is_asset_folder("00 - Projetos")
    assert projects_module.is_asset_folder("00 - Projeto")
    assert projects_module.is_asset_folder("00 - Projetos Campanha")
    assert projects_module.is_asset_folder("00-Assets")
    assert projects_module.is_asset_folder("00  -  Biblioteca")


def test_nao_confunde_com_projeto_numerado_nem_com_agrupador():
    # entregas normais
    assert not projects_module.is_asset_folder("001 - DNA do Eleitor")
    assert not projects_module.is_asset_folder("007 - Discurso")
    assert not projects_module.is_asset_folder("099 - Job")
    # "000 - Eventos" é agrupador com projetos reais aninhados: exigir dois zeros
    # exatos é justamente o que impede a regra de apagar essas horas
    assert not projects_module.is_asset_folder("000 - Eventos")
    assert not projects_module.is_asset_folder("0001 - Coisa")


def test_pasta_de_assets_continua_na_lista_mas_fora_das_estatisticas(tmp_path):
    client = _make_client(tmp_path)
    _asset_folder(client, "00 - Projetos")
    entrega = projects_module.create_project(client_id=client["id"], name="Job")
    _log_hours(entrega["id"], 4)

    listados = projects_module.list_projects(client["id"])
    nomes = sorted(Path(p["path"]).name for p in listados)
    assert nomes == ["00 - Projetos", "001 - Job"]

    assets = next(p for p in listados if p["num"] == "00")
    assert assets["pasta_de_assets"] is True
    assert assets["oculto_estatisticas"] is True

    entrega_lida = next(p for p in listados if p["num"] == "001")
    assert entrega_lida["pasta_de_assets"] is False
    assert entrega_lida["oculto_estatisticas"] is False


def test_horas_da_pasta_de_assets_nao_entram_em_nenhuma_agregacao(tmp_path):
    client = _make_client(tmp_path)
    _asset_folder(client, "00 - Projetos")
    entrega = projects_module.create_project(client_id=client["id"], name="Job")
    _log_hours(entrega["id"], 4)
    # lança horas na pasta de assets também
    assets = next(p for p in projects_module.list_projects(client["id"]) if p["num"] == "00")
    _log_hours(assets["id"], 20)

    mes = financials.current_month()
    breakdown = financials.compute_client_month(client["id"], mes)
    assert breakdown["hours"] == 4          # as 20h da pasta de assets ficam de fora
    assert assets["id"] not in breakdown["projects"]

    glob = dashboard.global_stats()
    assert glob["kpis"]["hours"] == 4
    assert glob["kpis"]["project_count"] == 1
    assert [p["name"] for p in glob["top_projects"]] == ["001 - Job"]

    por_cliente = dashboard.client_stats(client["id"])
    assert por_cliente["kpis"]["hours"] == 4
    assert [h["name"] for h in por_cliente["hours_by_project"]] == ["001 - Job"]


def test_duas_pastas_de_assets_nao_geram_conflito_de_numero(tmp_path):
    """"00 - Projetos" e "00 - Projetos Campanha" colidem no mesmo project_id,
    mas como as duas saem das estatísticas o total volta a fechar com a lista."""
    client = _make_client(tmp_path)
    _asset_folder(client, "00 - Projetos")
    _asset_folder(client, "00 - Projetos Campanha")
    entrega = projects_module.create_project(client_id=client["id"], name="Job")
    _log_hours(entrega["id"], 3)

    data = dashboard.global_stats()

    assert data["conflitos_de_numero"] == []
    assert data["kpis"]["hours"] == sum(c["hours"] for c in data["hours_by_client"])


def test_agrupador_com_tres_zeros_continua_contando(tmp_path):
    """Regressão da armadilha: `int(num) == 0` teria apagado estas horas."""
    client = _make_client(tmp_path)
    agrupador = Path(client["path"]) / "000 - Eventos"
    agrupador.mkdir()

    listado = next(
        p for p in projects_module.list_projects(client["id"]) if p["num"] == "000"
    )
    assert listado["pasta_de_assets"] is False
    assert listado["oculto_estatisticas"] is False

    _log_hours(listado["id"], 6)
    assert dashboard.global_stats()["kpis"]["hours"] == 6


def test_flag_manual_no_cost_json_continua_valendo(tmp_path):
    """A convenção adiciona casos, não substitui `oculto_estatisticas` gravado."""
    client = _make_client(tmp_path)
    projeto = projects_module.create_project(client_id=client["id"], name="Teste interno")
    financials.set_stats_visibility(projeto["id"], True)

    lido = next(p for p in projects_module.list_projects(client["id"]) if p["num"] == "001")
    assert lido["pasta_de_assets"] is False
    assert lido["oculto_estatisticas"] is True
