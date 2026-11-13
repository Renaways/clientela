"""Estatísticas por período: mês, ano e histórico inteiro.

O ano e o total são a soma dos meses — o mesmo número de navegar mês a mês. O
orçamento só vira receita nos meses do contrato; sem isso o total cobraria o
orçamento em todo mês com uma sessão estimada de anos antes do contrato."""

import pytest

from clientela import clients as clients_module
from clientela import dashboard
from clientela import financials
from clientela import projects as projects_module
from clientela import timer

HOJE = "2026-10"


@pytest.fixture(autouse=True)
def mes_corrente_fixo(monkeypatch):
    monkeypatch.setattr(financials, "current_month", lambda: HOJE)


def _cliente(tmp_path, nome, **kwargs):
    pasta = tmp_path / nome
    pasta.mkdir()
    return clients_module.create_client(name=nome, path=str(pasta), **kwargs)


def _horas(project_id, mes, horas, dia=10):
    """Lançamento manual: sem corte de horário comercial nem piso de 15 min."""
    inicio = f"{mes}-{dia:02d}T09:00:00"
    fim = f"{mes}-{dia:02d}T{9 + horas:02d}:00:00"
    timer.log_manual(project_id, inicio, fim)


def test_ano_e_a_soma_dos_meses(tmp_path):
    c = _cliente(tmp_path, "Loja", contrato_inicio="2026-01-01")
    clients_module.set_budget(c["id"], 1000.0)
    p = projects_module.create_project(client_id=c["id"], name="Job")
    _horas(p["id"], "2026-02", 2)
    _horas(p["id"], "2026-07", 3)

    ano = dashboard.client_stats(c["id"], periodo="ano", ref="2026")["kpis"]
    meses = [dashboard.client_stats(c["id"], m)["kpis"] for m in financials.month_range("2026-01", HOJE)]

    assert ano["hours"] == pytest.approx(sum(m["hours"] for m in meses)) == pytest.approx(5.0)
    assert ano["value"] == pytest.approx(sum(m["value"] for m in meses)) == pytest.approx(10 * 1000.0)


def test_orcamento_so_vira_receita_dentro_do_contrato(tmp_path):
    c = _cliente(tmp_path, "Loja", contrato_inicio="2026-05-25", contrato_fim="2026-08-05")
    clients_module.set_budget(c["id"], 2000.0)
    p = projects_module.create_project(client_id=c["id"], name="Job")
    _horas(p["id"], "2025-03", 4)  # estimativa antiga, anos antes do contrato

    fora = financials.compute_client_month(c["id"], "2025-03")
    assert fora["hours"] == pytest.approx(4.0)  # trabalho conta sempre
    assert fora["value_total"] == 0.0
    assert financials.compute_client_month(c["id"], "2026-05")["value_total"] == 2000.0  # mês parcial conta
    assert financials.compute_client_month(c["id"], "2026-09")["value_total"] == 0.0

    total = dashboard.client_stats(c["id"], periodo="total")["kpis"]
    assert total["value"] == pytest.approx(4 * 2000.0)  # mai, jun, jul, ago
    assert total["hours"] == pytest.approx(4.0)


def test_sem_contrato_o_total_fatura_so_meses_com_atividade(tmp_path):
    c = _cliente(tmp_path, "Avulso")
    clients_module.set_budget(c["id"], 500.0)
    p = projects_module.create_project(client_id=c["id"], name="Job")
    _horas(p["id"], "2024-06", 1)
    _horas(p["id"], "2026-03", 1)

    total = dashboard.client_stats(c["id"], periodo="total")["kpis"]
    assert total["value"] == pytest.approx(2 * 500.0)
    # o mês sozinho continua como sempre foi: sem contrato, fatura
    assert dashboard.client_stats(c["id"], HOJE)["kpis"]["value"] == pytest.approx(500.0)


def test_periodo_nao_passa_do_mes_corrente():
    assert dashboard.resolve_period("mes", "2031-01")["ref"] == HOJE
    assert dashboard.resolve_period("ano", "2031")["ref"] == "2026"
    ano = dashboard.resolve_period("ano", "2026")
    assert ano["months"][-1] == HOJE and ano["next_ref"] is None
    assert dashboard.resolve_period("ano", "2025")["months"][-1] == "2025-12"
    assert dashboard.resolve_period(None)["kind"] == "mes"
    assert dashboard.resolve_period("qualquer")["kind"] == "mes"
    # parâmetro antigo `month` continua valendo
    assert dashboard.resolve_period(None, None, "2026-03")["ref"] == "2026-03"


def test_serie_do_ano_tem_doze_meses_e_marca_os_futuros(tmp_path):
    c = _cliente(tmp_path, "Loja")
    p = projects_module.create_project(client_id=c["id"], name="Job")
    _horas(p["id"], "2026-07", 3)

    serie = dashboard.client_stats(c["id"], periodo="ano", ref="2026")["series"]
    assert serie["granularity"] == "mes"
    assert [pt["key"] for pt in serie["points"]] == financials.month_range("2026-01", "2026-12")
    assert [pt["key"] for pt in serie["points"] if pt["future"]] == ["2026-11", "2026-12"]
    assert next(pt for pt in serie["points"] if pt["key"] == "2026-07")["hours"] == pytest.approx(3.0)


def test_serie_do_total_e_por_ano_e_empilha_por_cliente(tmp_path):
    a = _cliente(tmp_path, "A")
    b = _cliente(tmp_path, "B")
    pa = projects_module.create_project(client_id=a["id"], name="Job A")
    pb = projects_module.create_project(client_id=b["id"], name="Job B")
    _horas(pa["id"], "2024-02", 2)
    _horas(pb["id"], "2026-04", 5)
    _horas(pa["id"], "2026-08", 1)

    serie = dashboard.global_stats(periodo="total")["series"]
    assert serie["granularity"] == "ano"
    assert [pt["key"] for pt in serie["points"]] == ["2024", "2025", "2026"]
    ano_2026 = serie["points"][-1]
    assert ano_2026["by_client"] == {a["id"]: pytest.approx(1.0), b["id"]: pytest.approx(5.0)}
    assert ano_2026["hours"] == pytest.approx(6.0)


def test_comparacao_com_o_periodo_anterior(tmp_path):
    c = _cliente(tmp_path, "Loja")
    p = projects_module.create_project(client_id=c["id"], name="Job")
    _horas(p["id"], "2026-09", 4)
    _horas(p["id"], "2025-02", 1)
    _horas(p["id"], "2025-11", 6)  # fora de jan–out, não entra na comparação do ano

    mes = dashboard.client_stats(c["id"])["comparison"]
    assert mes == {"label": "setembro de 2026", "hours": pytest.approx(4.0)}
    ano = dashboard.client_stats(c["id"], periodo="ano")["comparison"]
    assert ano["label"] == "jan–out de 2025"
    assert ano["hours"] == pytest.approx(1.0)
    assert dashboard.client_stats(c["id"], periodo="total")["comparison"] is None


def test_api_aceita_periodo(tmp_path):
    from app import create_app

    c = _cliente(tmp_path, "Loja")
    p = projects_module.create_project(client_id=c["id"], name="Job")
    _horas(p["id"], "2026-02", 2)
    client = create_app().test_client()

    geral = client.get("/api/dashboard/global?periodo=ano&ref=2026").get_json()
    assert geral["period"]["kind"] == "ano" and geral["kpis"]["hours"] == pytest.approx(2.0)
    do_cliente = client.get(f"/api/dashboard?client_id={c['id']}&periodo=total").get_json()
    assert do_cliente["period"]["kind"] == "total" and do_cliente["kpis"]["hours"] == pytest.approx(2.0)
