import pytest

from clientela import clients, financials, projects, timer

MONTH = "2026-08"


def _log(project_id, hours, day="10"):
    timer.log_manual(
        project_id, f"{MONTH}-{day}T09:00:00", f"{MONTH}-{day}T{9 + hours:02d}:00:00"
    )


def test_hourly_cost_formula():
    financials.set_cost_settings(
        pro_labore_mensal=9000.0, horas_capacidade_mensal=150.0, custos_fixos=[]
    )
    financials.add_fixed_cost("adobe", 2760.0 - 0)  # single fixed cost line
    settings = financials.get_cost_settings()
    # (9000 + 2760) / 150 = 78.4
    assert financials.hourly_cost(settings) == pytest.approx(78.4)


def test_apportionment_all_manual_values_kept_as_is(tmp_path):
    c = clients.create_client("Vertex", str(tmp_path / "vertex"))
    clients.set_budget(c["id"], 1000.0)
    p1 = projects.create_project(c["id"], "A", valor_individual=400.0)
    p2 = projects.create_project(c["id"], "B", valor_individual=300.0)
    values = financials.apportion_project_values(c["id"], MONTH)
    assert values[p1["id"]] == 400.0
    assert values[p2["id"]] == 300.0


def test_apportionment_splits_remaining_budget_by_tag_weight(tmp_path):
    financials.set_cost_settings(tag_weights={"Motion": 2.0, "Video": 1.0})
    c = clients.create_client("Vertex", str(tmp_path / "vertex"))
    clients.set_budget(c["id"], 900.0)
    motion = projects.create_project(c["id"], "Motion job", tags=["Motion"])
    video = projects.create_project(c["id"], "Video job", tags=["Video"])
    untagged = projects.create_project(c["id"], "Untagged job")

    values = financials.apportion_project_values(c["id"], MONTH)
    # weights: Motion=2, Video=1, untagged=1 (no tag => weight 1) => total 4
    # 900 split 2:1:1 => 450 / 225 / 225
    assert values[motion["id"]] == pytest.approx(450.0)
    assert values[video["id"]] == pytest.approx(225.0)
    assert values[untagged["id"]] == pytest.approx(225.0)


def test_apportionment_unweighted_tag_defaults_to_one(tmp_path):
    financials.set_cost_settings(tag_weights={"Motion": 2.0})
    c = clients.create_client("Vertex", str(tmp_path / "vertex"))
    clients.set_budget(c["id"], 300.0)
    motion = projects.create_project(c["id"], "Motion job", tags=["Motion"])
    other = projects.create_project(c["id"], "Other job", tags=["Legenda"])  # unweighted tag => 1
    values = financials.apportion_project_values(c["id"], MONTH)
    # weights 2:1 of 300 => 200 / 100
    assert values[motion["id"]] == pytest.approx(200.0)
    assert values[other["id"]] == pytest.approx(100.0)


def test_project_cost_and_profit(tmp_path):
    financials.set_cost_settings(
        pro_labore_mensal=9000.0,
        horas_capacidade_mensal=150.0,
        imposto_percentual=6.0,
        custos_fixos=[{"id": "x", "nome": "adobe", "valor_mensal": 2760.0}],
        tag_weights={},
    )
    c = clients.create_client("Vertex", str(tmp_path / "vertex"))
    clients.set_budget(c["id"], 1000.0)
    p = projects.create_project(c["id"], "Job", valor_individual=1000.0)
    _log(p["id"], 2)  # 2 manual hours in MONTH

    breakdown = financials.project_cost(p["id"], MONTH)
    hourly = 78.4
    expected_cost = 2 * hourly + 0 + 1000.0 * 0.06
    assert breakdown["horas"] == pytest.approx(2.0)
    assert breakdown["valor"] == pytest.approx(1000.0)
    assert breakdown["custo"] == pytest.approx(expected_cost)
    assert breakdown["lucro"] == pytest.approx(1000.0 - expected_cost)


def test_project_hidden_from_statistics_is_excluded(tmp_path):
    c = clients.create_client("Vertex", str(tmp_path / "vertex"))
    clients.set_budget(c["id"], 500.0)
    visible = projects.create_project(c["id"], "Visible", valor_individual=200.0)
    hidden = projects.create_project(c["id"], "Hidden", valor_individual=999.0)
    financials.set_stats_visibility(hidden["id"], True)

    breakdown = financials.compute_client_month(c["id"], MONTH)
    assert visible["id"] in breakdown["projects"]
    assert hidden["id"] not in breakdown["projects"]


def test_close_month_writes_immutable_ledger_entry(tmp_path):
    c = clients.create_client("Vertex", str(tmp_path / "vertex"))
    clients.set_budget(c["id"], 500.0)
    projects.create_project(c["id"], "Job", valor_individual=500.0)

    entry = financials.close_month(c["id"], MONTH)
    assert entry["orcamento"] == 500.0
    assert "closed_at" in entry

    stored = financials.get_ledger_entry(c["id"], MONTH)
    assert stored == entry
