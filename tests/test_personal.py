"""Projetos pessoais: cliente tipo "pessoal" e projeto sem pasta por escolha.

Uma tarefa sem pasta vive só no registro (etapa, cronômetro, notas, links) e
divide a pasta do cliente; não é "pasta perdida" e não leva o aviso. Cliente
pessoal conta horas, mas fica fora de valor/custo/lucro."""

from datetime import datetime, timedelta

import pytest

from clientela import clients as clients_module
from clientela import dashboard, financials, timer
from clientela import projects as projects_module


def _make_client(tmp_path, name: str = "Twitch", tipo: str = "cliente"):
    client_dir = tmp_path / name
    client_dir.mkdir()
    return clients_module.create_client(name=name, path=str(client_dir), tipo=tipo)


def _log_hours_this_month(project_id: str, hours: float) -> None:
    # Dia 15 do mês corrente: "agora menos N horas" cai no mês anterior se o
    # teste roda logo depois da meia-noite do dia 1.
    inicio = datetime.now().replace(day=15, hour=10, minute=0, second=0, microsecond=0)
    fim = inicio + timedelta(hours=hours)
    timer.log_manual(project_id, inicio.isoformat(), fim.isoformat())


def test_criar_sem_pasta_nao_toca_o_disco(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(
        client_id=client["id"],
        name="Tela de abertura",
        links=[{"label": "Ref", "url": "https://ref"}],
        criar_pasta=False,
    )
    assert list((tmp_path / "Twitch").iterdir()) == []
    assert project["sem_pasta"] is True
    assert project["folder_available"] is False
    assert project["links"] == [{"label": "Ref", "url": "https://ref"}]

    [listed] = projects_module.list_projects(client["id"])
    assert listed["id"] == project["id"]
    assert listed["sem_pasta"] is True


def test_numero_seguinte_pula_a_tarefa_sem_pasta(tmp_path):
    client = _make_client(tmp_path)
    tarefa = projects_module.create_project(client_id=client["id"], name="Abertura", criar_pasta=False)
    outra = projects_module.create_project(client_id=client["id"], name="Encerramento", criar_pasta=False)
    com_pasta = projects_module.create_project(client_id=client["id"], name="Overlay")
    assert [tarefa["num"], outra["num"], com_pasta["num"]] == ["001", "002", "003"]
    assert com_pasta["sem_pasta"] is False
    assert [p.name for p in (tmp_path / "Twitch").iterdir()] == ["003 - Overlay"]


def test_criar_pasta_depois(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(
        client_id=client["id"],
        name="Abertura",
        links=[{"label": "Ref", "url": "https://ref"}],
        criar_pasta=False,
    )
    updated = projects_module.create_folder_for(project["id"])
    folder = tmp_path / "Twitch" / "001 - Abertura"
    assert updated["folder_available"] is True
    assert updated["sem_pasta"] is False
    assert updated["path"] == str(folder)
    assert (folder / "001 - Abertura" / "001 - Abertura.prproj").exists()
    assert "Ref: https://ref" in (folder / "Links" / "links.txt").read_text()

    with pytest.raises(ValueError):
        projects_module.create_folder_for(project["id"])


def test_pasta_perdida_nao_vira_sem_pasta(tmp_path):
    import shutil

    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    shutil.rmtree(project["path"])
    [listed] = projects_module.list_projects(client["id"])
    assert listed["folder_available"] is False
    assert listed["sem_pasta"] is False


def test_tipo_do_cliente_e_cadastro_antigo(tmp_path):
    pessoal = _make_client(tmp_path, "Twitch", tipo="pessoal")
    comum = _make_client(tmp_path, "Loja")
    assert clients_module.is_personal(pessoal) is True
    assert clients_module.is_personal(comum) is False
    assert clients_module.with_folder_status({"id": "x", "name": "a", "path": "/nada"})["tipo"] == "cliente"
    assert clients_module.update_client(comum["id"], tipo="pessoal")["tipo"] == "pessoal"
    assert clients_module.update_client(comum["id"], tipo="qualquer")["tipo"] == "cliente"


def test_cliente_pessoal_conta_horas_mas_nao_dinheiro(tmp_path):
    financials.set_cost_settings(pro_labore_mensal=10000.0, horas_capacidade_mensal=100.0)
    pessoal = _make_client(tmp_path, "Twitch", tipo="pessoal")
    comum = _make_client(tmp_path, "Loja")
    clients_module.set_budget(comum["id"], 1000.0)
    tarefa = projects_module.create_project(client_id=pessoal["id"], name="Abertura", criar_pasta=False)
    job = projects_module.create_project(client_id=comum["id"], name="Job")
    _log_hours_this_month(tarefa["id"], 2.0)
    _log_hours_this_month(job["id"], 1.0)

    month = financials.current_month()
    mes_pessoal = financials.compute_client_month(pessoal["id"], month)
    assert mes_pessoal["hours"] == pytest.approx(2.0)
    assert mes_pessoal["cost"] == 0.0
    assert mes_pessoal["profit"] == 0.0

    kpis = dashboard.global_stats(month)["kpis"]
    assert kpis["hours"] == pytest.approx(3.0)
    assert kpis["profit"] == pytest.approx(1000.0 - 100.0)

    assert dashboard.home()["stats"]["profit_this_month"] == pytest.approx(900.0)


def test_api_cria_sem_pasta_e_abre_a_pasta_do_cliente(tmp_path, monkeypatch):
    from app import create_app
    from clientela import files_native

    abertos = []
    monkeypatch.setattr(files_native, "reveal_in_finder", lambda path: abertos.append(path) or True)
    monkeypatch.setattr(files_native, "open_with_default_app", lambda path: abertos.append(path) or True)
    api = create_app().test_client()

    resp = api.post("/api/clients", json={"name": "Twitch", "path": str(tmp_path / "Twitch"), "tipo": "pessoal"})
    client = resp.get_json()
    assert client["tipo"] == "pessoal"

    resp = api.post(f"/api/clients/{client['id']}/projects", data={"name": "Abertura", "criar_pasta": "0"})
    assert resp.status_code == 201
    project = resp.get_json()
    assert project["sem_pasta"] is True
    assert list((tmp_path / "Twitch").iterdir()) == []

    # o botão "Abrir pasta" do cartão (file vazio) e o "revelar" abrem a do cliente
    assert api.post("/api/files/open", json={"project_id": project["id"], "file": ""}).status_code == 200
    assert api.post("/api/files/open-all", json={"project_id": project["id"]}).status_code == 200
    assert abertos == [str(tmp_path / "Twitch")] * 2
    # arquivo específico continua exigindo a pasta do projeto
    assert api.post("/api/files/open", json={"project_id": project["id"], "file": "x.prproj"}).status_code == 409

    resp = api.post("/api/projects/create-folder", json={"project_id": project["id"]})
    assert resp.status_code == 200
    assert resp.get_json()["folder_available"] is True
