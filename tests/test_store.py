"""O registro central de projetos (data/projects/) é a fonte da verdade de tudo
que não é arquivo de trabalho. A pasta do projeto pode sumir do disco que o
histórico, as horas e a etapa continuam — e o projeto continua listado, com
o aviso de pasta indisponível."""

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from clientela import backup
from clientela import clients as clients_module
from clientela import dashboard, feed, financials, paths
from clientela import projects as projects_module
from clientela import stages, store, tags, timer
from clientela.persistence import safe_write_json


def _make_client(tmp_path, name: str = "Cliente"):
    client_dir = tmp_path / name
    client_dir.mkdir()
    return clients_module.create_client(name=name, path=str(client_dir))


def _hours(project_id: str) -> float:
    project = projects_module.get_project(project_id)
    return sum(timer.effective_seconds(s) for s in project["sessions"]) / 3600


# ------------------------------------------------------------- onde grava


def test_projeto_novo_nasce_no_registro_e_nao_cria_log_na_pasta(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job", tags=["Video"])

    record_path = paths.PROJECTS_DIR / client["id"] / f"{project['num']}.json"
    assert record_path.is_file()
    record = json.loads(record_path.read_text())
    assert record["id"] == project["id"]
    assert record["tags"] == ["Video"]
    assert record["history"][0]["action"] == "criado"

    assert not (Path(project["path"]) / "LOG").exists()
    assert not any(p.name.startswith(".") for p in Path(project["path"]).iterdir())


def test_escritas_seguintes_vao_pro_registro_e_nao_pra_pasta(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    pid = project["id"]

    stages.set_stage(pid, "revisao")
    tags.set_project_tags(pid, ["Motion"])
    tags.set_due_date(pid, "2026-10-01")
    projects_module.set_notes(pid, "obs")
    timer.log_manual(pid, "2026-09-01T10:00:00", "2026-09-01T12:00:00")
    financials.set_valor_individual(pid, 500.0)
    financials.add_extra_cost(pid, "freela", 100.0)
    feed.add_item(pid, "comments", "trocar trilha")
    feed.add_item(pid, "checklist", "exportar")

    record = store.load(pid)
    assert record["stage"] == "revisao"
    assert record["tags"] == ["Motion"]
    assert record["due_date"] == "2026-10-01"
    assert record["notes"] == "obs"
    assert len(record["timer"]["sessions"]) == 1
    assert record["cost"]["valor_individual"] == 500.0
    assert record["cost"]["custos_extra"][0]["nome"] == "freela"
    assert record["comments"][0]["text"] == "trocar trilha"
    assert record["checklist"][0]["text"] == "exportar"
    assert not (Path(project["path"]) / "LOG").exists()


# ------------------------------------------------------------- importação


def _legacy_folder(client_dir: Path, name: str = "003 - Antigo", where: str = "LOG") -> Path:
    folder = client_dir / name
    folder.mkdir()
    base = folder / where if where else folder
    base.mkdir(exist_ok=True)
    safe_write_json(base / ".meta.json", {"tags": ["Legado"], "due_date": "2026-01-01", "notes": "n"})
    safe_write_json(base / ".stage.json", {"stage": "concluido"})
    safe_write_json(
        base / ".time.json",
        {
            "status": "idle",
            "running_start": None,
            "sessions": [{"start": "2026-01-01T10:00:00", "end": "2026-01-01T12:00:00", "seconds": 7200, "manual": True}],
        },
    )
    safe_write_json(base / ".cost.json", {"valor_individual": 300.0, "custos_extra": [], "oculto_estatisticas": False})
    safe_write_json(
        base / ".history.json",
        [{"action": "etapa_alterada", "details": {"de": "ajuste", "para": "concluido"}, "at": "2026-01-02T00:00:00"}],
    )
    if where:
        safe_write_json(base / ".comments.json", {"items": [{"id": "a1", "text": "pedido", "at": "x", "done": False, "done_at": None}]})
    links = folder / "Links"
    links.mkdir()
    (links / "links.txt").write_text("Frame.io: https://frame.io/x\nRef: https://ref\n")
    return folder


def test_primeira_varredura_importa_o_log_da_pasta(tmp_path):
    client = _make_client(tmp_path)
    folder = _legacy_folder(Path(client["path"]))

    [project] = projects_module.list_projects(client["id"])

    assert project["id"] == f"{client['id']}:003"
    assert project["stage"] == "concluido"
    assert project["tags"] == ["Legado"]
    assert project["due_date"] == "2026-01-01"
    assert project["notes"] == "n"
    assert project["valor_individual"] == 300.0
    assert project["concluido_em"] == "2026-01-02T00:00:00"
    assert project["links"] == [
        {"label": "Frame.io", "url": "https://frame.io/x"},
        {"label": "Ref", "url": "https://ref"},
    ]
    assert feed.get_feed(project["id"])["comments"][0]["text"] == "pedido"
    assert _hours(project["id"]) == 2.0
    # os arquivos de origem ficam onde estão
    assert (folder / "LOG" / ".history.json").is_file()


def test_importa_metadados_soltos_na_raiz_de_pastas_nunca_migradas(tmp_path):
    client = _make_client(tmp_path)
    _legacy_folder(Path(client["path"]), where="")

    [project] = projects_module.list_projects(client["id"])

    assert project["stage"] == "concluido"
    assert project["tags"] == ["Legado"]
    assert _hours(project["id"]) == 2.0


def test_depois_de_importado_o_log_e_ignorado(tmp_path):
    client = _make_client(tmp_path)
    folder = _legacy_folder(Path(client["path"]))
    [project] = projects_module.list_projects(client["id"])
    stages.set_stage(project["id"], "producao")
    stages.set_stage(project["id"], "revisao")

    # alguém (ou uma versão antiga do app) mexe no LOG/ — não pode voltar
    safe_write_json(folder / "LOG" / ".stage.json", {"stage": "backlog"})

    assert projects_module.get_project(project["id"])["stage"] == "revisao"


def test_boot_registra_todas_as_pastas_de_todos_os_clientes(tmp_path):
    a = _make_client(tmp_path, "A")
    b = _make_client(tmp_path, "B")
    _legacy_folder(Path(a["path"]), "001 - X")
    _legacy_folder(Path(a["path"]), "002 - Y")
    _legacy_folder(Path(b["path"]), "001 - Z")
    clients_module.create_client(name="Sumido", path=str(tmp_path / "nao-existe"))

    assert projects_module.import_all_from_disk() == 3
    assert projects_module.import_all_from_disk() == 0
    assert len(store.list_for_client(a["id"])) == 2
    assert len(store.list_for_client(b["id"])) == 1


# --------------------------------------------------------- pasta que sumiu


def test_pasta_removida_mantem_projeto_listado_com_aviso_e_horas(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job", tags=["Video"])
    pid = project["id"]
    # No mês corrente: client_summary só soma as horas do mês. Com data fixa o
    # teste passava em setembro e quebrava a partir de outubro.
    inicio = datetime.now().replace(day=15, hour=10, minute=0, second=0, microsecond=0)
    timer.log_manual(pid, inicio.isoformat(), (inicio + timedelta(hours=3)).isoformat())
    stages.set_stage(pid, "revisao")
    projects_module.add_link(pid, "Ref", "https://ref")
    assert project["folder_available"] is True

    shutil.rmtree(project["path"])

    [listed] = projects_module.list_projects(client["id"])
    assert listed["id"] == pid
    assert listed["folder_available"] is False
    assert listed["path"] == project["path"]  # último caminho conhecido, pro aviso
    assert listed["name"] == "Job"
    assert listed["stage"] == "revisao"
    assert listed["tags"] == ["Video"]
    assert listed["links"] == [{"label": "Ref", "url": "https://ref"}]
    assert _hours(pid) == 3.0
    assert stages.get_history(pid)[-1]["action"] == "etapa_alterada"

    # as estatísticas continuam contando
    resumo = dashboard.client_summary(clients_module.get_client(client["id"]))
    assert resumo["hours"] == pytest.approx(3.0)


def test_sem_pasta_ainda_da_pra_mexer_em_etapa_notas_feed_e_cronometro(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    pid = project["id"]
    shutil.rmtree(project["path"])

    stages.set_stage(pid, "ajuste")
    projects_module.set_notes(pid, "sem pasta")
    feed.add_item(pid, "checklist", "cobrar")
    timer.log_manual(pid, "2026-09-01T10:00:00", "2026-09-01T11:00:00")
    projects_module.rename_project(pid, "Job renomeado")

    got = projects_module.get_project(pid)
    assert got["stage"] == "ajuste"
    assert got["notes"] == "sem pasta"
    assert got["name"] == "Job renomeado"
    assert got["folder_available"] is False
    assert feed.get_feed(pid)["checklist"][0]["text"] == "cobrar"
    assert _hours(pid) == 1.0


def test_sem_pasta_o_que_depende_de_arquivo_desliga_sem_quebrar(tmp_path):
    from clientela import organizer

    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    pid = project["id"]
    shutil.rmtree(project["path"])

    assert organizer.list_project_files(pid) == []
    assert organizer.apply_organization(pid, [{"type": "create", "folder": "Edit"}]) == {"applied": []}
    assert timer.estimate_sessions(pid) == []
    assert projects_module.get_project_dir(pid) is None


def test_pasta_recriada_com_o_mesmo_numero_reencaixa_no_registro(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    pid = project["id"]
    timer.log_manual(pid, "2026-09-01T10:00:00", "2026-09-01T12:00:00")
    shutil.rmtree(project["path"])
    assert projects_module.get_project(pid)["folder_available"] is False

    (Path(client["path"]) / f"{project['num']} - Job de volta").mkdir()

    got = projects_module.get_project(pid)
    assert got["folder_available"] is True
    assert got["name"] == "Job de volta"  # a pasta manda no nome enquanto existe
    assert _hours(pid) == 2.0


def test_cliente_com_pasta_sumida_lista_os_projetos_do_registro(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    shutil.rmtree(client["path"])

    [listed] = projects_module.list_projects(client["id"])
    assert listed["id"] == project["id"]
    assert listed["folder_available"] is False
    assert clients_module.with_folder_status(client)["folder_available"] is False


# ------------------------------------------------------- remover do registro


def test_remover_registro_so_sem_a_pasta(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    pid = project["id"]

    with pytest.raises(ValueError):
        projects_module.remove_project_record(pid)
    assert store.exists(pid)

    shutil.rmtree(project["path"])
    assert projects_module.remove_project_record(pid) is True
    assert not store.exists(pid)
    assert projects_module.list_projects(client["id"]) == []
    assert projects_module.get_project(pid) is None


def test_api_remover_registro(tmp_path):
    from app import create_app

    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    api = create_app().test_client()

    r = api.delete("/api/projects", json={"project_id": project["id"]})
    assert r.status_code == 409

    shutil.rmtree(project["path"])
    r = api.delete("/api/projects", json={"project_id": project["id"]})
    assert r.status_code == 200
    assert api.get(f"/api/clients/{client['id']}/projects").get_json() == []


def test_api_rotas_de_arquivo_respondem_409_sem_pasta(tmp_path):
    from app import create_app

    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    api = create_app().test_client()
    shutil.rmtree(project["path"])

    assert api.get(f"/api/project/contents?project_id={project['id']}").status_code == 409
    assert api.post("/api/files/open-all", json={"project_id": project["id"]}).status_code == 409
    assert api.get(f"/api/project-files?project_id={project['id']}").get_json() == []
    # e o resto continua respondendo
    assert api.get(f"/api/projects/feed?project_id={project['id']}").status_code == 200


# ------------------------------------------------------------ numeração


def test_numero_de_projeto_apagado_nao_e_reaproveitado(tmp_path):
    """Reusar o número colaria o histórico do projeto antigo no novo."""
    client = _make_client(tmp_path)
    a = projects_module.create_project(client_id=client["id"], name="A")
    b = projects_module.create_project(client_id=client["id"], name="B")
    timer.log_manual(b["id"], "2026-09-01T10:00:00", "2026-09-01T12:00:00")
    shutil.rmtree(b["path"])

    c = projects_module.create_project(client_id=client["id"], name="C")

    assert [a["num"], b["num"], c["num"]] == ["001", "002", "003"]
    assert _hours(c["id"]) == 0.0
    assert _hours(b["id"]) == 2.0


def test_criar_projeto_que_falha_nao_deixa_registro(tmp_path, monkeypatch):
    client = _make_client(tmp_path)
    monkeypatch.setattr(projects_module.history, "append_event", lambda *a, **k: 1 / 0)

    with pytest.raises(ZeroDivisionError):
        projects_module.create_project(client_id=client["id"], name="Job")

    assert store.list_for_client(client["id"]) == []
    assert list(Path(client["path"]).iterdir()) == []


# ------------------------------------------------------- excluir cliente


def test_excluir_cliente_apaga_os_registros_dele(tmp_path):
    client = _make_client(tmp_path)
    outro = _make_client(tmp_path, "Outro")
    projects_module.create_project(client_id=client["id"], name="Job")
    projects_module.create_project(client_id=outro["id"], name="Job do outro")

    assert clients_module.delete_client(client["id"]) is True

    assert not (paths.PROJECTS_DIR / client["id"]).exists()
    assert len(store.list_for_client(outro["id"])) == 1


# ------------------------------------------------------------- conflitos


def test_duas_pastas_com_o_mesmo_numero_viram_um_item_com_aviso(tmp_path):
    client = _make_client(tmp_path)
    (Path(client["path"]) / "007 - A").mkdir()
    (Path(client["path"]) / "007 - B").mkdir()

    [project] = projects_module.list_projects(client["id"])
    assert project["pastas_conflitantes"] == ["007 - A", "007 - B"]

    stats = dashboard.global_stats()
    assert stats["conflitos_de_numero"][0]["folders"] == ["007 - A", "007 - B"]


# ---------------------------------------------------------------- backup


def test_backup_leva_o_registro_e_nao_depende_da_pasta(tmp_path):
    import zipfile

    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    shutil.rmtree(project["path"])

    zip_path = backup.run_backup()

    names = zipfile.ZipFile(zip_path).namelist()
    assert f"data/projects/{client['id']}/{project['num']}.json" in names
    assert not any(n.endswith(".bak") for n in names)


# ------------------------------------------------------------ atomicidade


def test_update_nao_perde_escrita_concorrente(tmp_path):
    import threading

    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    pid = project["id"]

    def worker(n: int) -> None:
        for _ in range(20):
            feed.add_item(pid, "checklist", f"t{n}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.load(pid)["checklist"]) == 80


def test_excluir_projeto_manda_a_pasta_pra_lixeira_e_apaga_o_registro(tmp_path, monkeypatch):
    from clientela import files_native, timer

    lixo = []
    monkeypatch.setattr(files_native, "move_to_trash", lambda p: (lixo.append(p), shutil.rmtree(p))[0] is None)
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    pid = project["id"]
    timer.start_timer(pid)

    from app import create_app
    resp = create_app().test_client().delete("/api/projects", json={"project_id": pid, "excluir": True})
    assert resp.status_code == 200
    assert lixo == [project["path"]]
    assert not store.exists(pid)
    assert projects_module.list_projects(client["id"]) == []
    assert timer.get_active() is None
