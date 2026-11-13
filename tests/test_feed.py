from pathlib import Path

import pytest

from clientela import clients as clients_module
from clientela import feed, store
from clientela import projects as projects_module


def _make_project(tmp_path, name="Job"):
    client_dir = tmp_path / "Cliente"
    client_dir.mkdir(exist_ok=True)
    client = clients_module.create_client(name="Cliente", path=str(client_dir))
    return projects_module.create_project(client_id=client["id"], name=name)


def test_add_lista_em_ordem_e_marca_data(tmp_path):
    project = _make_project(tmp_path)
    feed.add_item(project["id"], "comments", "trocar a trilha")
    items = feed.add_item(project["id"], "comments", "  reduzir lettering  ")
    assert [i["text"] for i in items] == ["trocar a trilha", "reduzir lettering"]
    assert all(i["at"] and i["done"] is False and i["done_at"] is None for i in items)
    assert len({i["id"] for i in items}) == 2


def test_texto_vazio_e_recusado(tmp_path):
    project = _make_project(tmp_path)
    with pytest.raises(ValueError):
        feed.add_item(project["id"], "comments", "   ")
    assert feed.list_items(project["id"], "comments") == []


def test_tipo_desconhecido_e_recusado(tmp_path):
    project = _make_project(tmp_path)
    with pytest.raises(ValueError):
        feed.add_item(project["id"], "notas", "x")


def test_projeto_inexistente_devolve_none(tmp_path):
    _make_project(tmp_path)
    assert feed.get_feed("nao-existe:999") is None
    assert feed.add_item("nao-existe:999", "comments", "x") is None


def test_marcar_resolvido_e_voltar(tmp_path):
    project = _make_project(tmp_path)
    (item,) = feed.add_item(project["id"], "checklist", "exportar 4K")
    (done,) = feed.set_done(project["id"], "checklist", item["id"], True)
    assert done["done"] is True and done["done_at"]
    (undone,) = feed.set_done(project["id"], "checklist", item["id"], False)
    assert undone["done"] is False and undone["done_at"] is None


def test_apagar_item_e_ids_desconhecidos(tmp_path):
    project = _make_project(tmp_path)
    feed.add_item(project["id"], "comments", "a")
    a, b = feed.add_item(project["id"], "comments", "b")
    remaining = feed.delete_item(project["id"], "comments", a["id"])
    assert [i["id"] for i in remaining] == [b["id"]]
    with pytest.raises(KeyError):
        feed.delete_item(project["id"], "comments", a["id"])
    with pytest.raises(KeyError):
        feed.set_done(project["id"], "comments", "zzz", True)


def test_listas_sao_independentes_e_vivem_no_registro(tmp_path):
    project = _make_project(tmp_path)
    feed.add_item(project["id"], "comments", "pedido")
    feed.add_item(project["id"], "checklist", "tarefa")
    data = feed.get_feed(project["id"])
    assert [i["text"] for i in data["comments"]] == ["pedido"]
    assert [i["text"] for i in data["checklist"]] == ["tarefa"]
    record = store.load(project["id"])
    assert [i["text"] for i in record["comments"]] == ["pedido"]
    assert [i["text"] for i in record["checklist"]] == ["tarefa"]
    # nada vai mais pra dentro da pasta do projeto
    assert not (Path(project["path"]) / "LOG").exists()


def test_itens_nao_vazam_entre_projetos_sem_arquivo(tmp_path):
    # Mesma armadilha do cronômetro: o default é um módulo-constante e o
    # primeiro append não pode contaminar a leitura do próximo projeto.
    client_dir = tmp_path / "Cliente"
    client_dir.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(client_dir))
    a = projects_module.create_project(client_id=client["id"], name="A")
    b = projects_module.create_project(client_id=client["id"], name="B")
    feed.add_item(a["id"], "comments", "só do A")
    assert feed.list_items(b["id"], "comments") == []


def test_api_roundtrip(tmp_path):
    from app import create_app

    project = _make_project(tmp_path)
    client = create_app().test_client()
    pid = project["id"]

    r = client.post("/api/projects/feed/items", json={"project_id": pid, "kind": "comments", "text": "cliente pediu"})
    assert r.status_code == 200
    item = r.get_json()["comments"][0]

    r = client.patch("/api/projects/feed/items", json={"project_id": pid, "kind": "comments", "id": item["id"], "done": True})
    assert r.get_json()["comments"][0]["done"] is True

    r = client.post("/api/projects/feed/items", json={"project_id": pid, "kind": "comments", "text": ""})
    assert r.status_code == 400

    r = client.delete(f"/api/projects/feed/items?project_id={pid}&kind=comments&id={item['id']}")
    assert r.status_code == 200 and r.get_json()["comments"] == []

    r = client.get(f"/api/projects/feed?project_id={pid}")
    assert r.get_json() == {"comments": [], "checklist": []}
