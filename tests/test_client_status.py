"""Cliente ativo/desativado. Desativar é encerrar o contrato sem perder nada:
o cliente sai do quadro, das pendências e dos KPIs da Home, mas continua
abrível, com os projetos e as horas, e segue nas estatísticas históricas."""

from datetime import datetime, timedelta

from clientela import clients as clients_module
from clientela import dashboard, search, timer
from clientela import projects as projects_module


def _make_client(tmp_path, name: str = "Cliente"):
    client_dir = tmp_path / name
    client_dir.mkdir()
    return clients_module.create_client(name=name, path=str(client_dir))


def test_cliente_nasce_ativo_e_cadastro_antigo_sem_a_chave_conta_como_ativo(tmp_path):
    client = _make_client(tmp_path)
    assert client["ativo"] is True
    assert clients_module.is_active({"id": "x", "name": "antigo", "path": "/nada"}) is True
    assert clients_module.with_folder_status({"id": "x", "name": "a", "path": "/nada"})["ativo"] is True


def test_desativar_e_reativar(tmp_path):
    client = _make_client(tmp_path)
    assert clients_module.update_client(client["id"], ativo=False)["ativo"] is False
    assert clients_module.is_active(clients_module.get_client(client["id"])) is False
    assert clients_module.update_client(client["id"], ativo=True)["ativo"] is True


def test_desativado_some_das_pendencias_e_dos_kpis_da_home(tmp_path):
    ativo = _make_client(tmp_path, "Ativo")
    parado = _make_client(tmp_path, "Parado")
    ontem = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for client in (ativo, parado):
        project = projects_module.create_project(client_id=client["id"], name="Job", due_date=ontem)
        agora = datetime.now()
        timer.log_manual(project["id"], (agora - timedelta(hours=2)).isoformat(), agora.isoformat())
    clients_module.update_client(parado["id"], ativo=False)

    home = dashboard.home()

    assert [p["sub"] for p in home["pendencias"] if p["kind"] == "Atrasado"] == ["Ativo"]
    assert home["stats"]["active_projects"] == 1
    assert home["stats"]["hours_this_week"] == 2.0
    # continua na lista de clientes, marcado, pro frontend separar
    por_nome = {c["name"]: c for c in home["clients"]}
    assert por_nome["Parado"]["ativo"] is False
    assert por_nome["Ativo"]["ativo"] is True


def test_desativado_continua_com_projetos_horas_e_estatisticas_historicas(tmp_path):
    client = _make_client(tmp_path, "Parado")
    project = projects_module.create_project(client_id=client["id"], name="Job")
    timer.log_manual(project["id"], "2026-03-02T10:00:00", "2026-03-02T13:00:00")
    clients_module.update_client(client["id"], ativo=False)

    [listed] = projects_module.list_projects(client["id"])
    assert listed["id"] == project["id"]
    stats = dashboard.global_stats("2026-03")
    assert [c["name"] for c in stats["hours_by_client"] if c["name"] == "Parado"] == ["Parado"]
    assert stats["kpis"]["hours"] == 3.0


def test_busca_marca_o_cliente_desativado(tmp_path):
    client = _make_client(tmp_path, "Parado")
    clients_module.update_client(client["id"], ativo=False)

    [hit] = [r for r in search.search("parado") if r["kind"] == "Cliente"]
    assert "desativado" in hit["sub"]


def test_api_edita_o_status(tmp_path):
    from app import create_app

    client = _make_client(tmp_path)
    api = create_app().test_client()

    r = api.put(f"/api/clients/{client['id']}", json={"ativo": False})
    assert r.status_code == 200
    assert r.get_json()["ativo"] is False
    assert [c["ativo"] for c in api.get("/api/clients").get_json()] == [False]
