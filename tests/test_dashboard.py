import json
from pathlib import Path

from clientela import clients as clients_module
from clientela import store
from clientela import dashboard
from clientela import projects as projects_module
from clientela import stages as stages_module


def test_home_ignores_history_events_from_a_different_schema(tmp_path):
    """The history can contain events written by another tool/schema (real
    folders on disk aren't guaranteed to only ever have been touched by this
    app). home() must not crash on events missing the keys it expects."""
    client_dir = tmp_path / "Cliente Legado"
    client_dir.mkdir()
    client = clients_module.create_client(name="Cliente Legado", path=str(client_dir))
    project = projects_module.create_project(client_id=client["id"], name="Projeto Antigo")
    stages_module.set_stage(project["id"], "concluido")

    # o histórico vive no registro do projeto em data/projects/
    store.update(
        project["id"],
        lambda rec: rec.__setitem__(
            "history",
            [{"ts": "2020-01-01T00:00:00", "type": "stage_change", "from_stage": "", "to_stage": "concluido"}],
        ),
    )

    data = dashboard.home()
    assert data["stats"]["delivered_this_week"] == 0


def test_destinos_sao_os_projetos_ativos_e_a_pasta_de_downloads(tmp_path):
    """Destinos do "Baixar mídia" / "Edição IA" do topo: fora Concluído, a pasta
    de assets 00 - … e os clientes desativados; o cronômetro rodando vem antes."""
    a_dir = tmp_path / "Alfa"
    a_dir.mkdir()
    b_dir = tmp_path / "Beta"
    b_dir.mkdir()
    off_dir = tmp_path / "Antigo"
    off_dir.mkdir()
    alfa = clients_module.create_client(name="Alfa", path=str(a_dir))
    beta = clients_module.create_client(name="Beta", path=str(b_dir))
    off = clients_module.create_client(name="Antigo", path=str(off_dir))
    clients_module.update_client(off["id"], ativo=False)
    a1 = projects_module.create_project(client_id=alfa["id"], name="Reel")
    a2 = projects_module.create_project(client_id=alfa["id"], name="Fechado")
    stages_module.set_stage(a2["id"], "concluido")
    (a_dir / "00 - Projeto Base").mkdir()
    b1 = projects_module.create_project(client_id=beta["id"], name="Gravando")
    stages_module.set_stage(b1["id"], "producao")  # liga o cronômetro
    projects_module.create_project(client_id=off["id"], name="Velho")
    sem = projects_module.create_project(client_id=beta["id"], name="Tarefa", criar_pasta=False)

    data = dashboard.destinations()
    ids = [p["id"] for p in data["projetos"]]
    assert ids[0] == b1["id"] and data["projetos"][0]["timer_running"] is True
    assert set(ids) == {a1["id"], b1["id"], sem["id"]}
    sem_item = next(p for p in data["projetos"] if p["id"] == sem["id"])
    assert sem_item["folder_available"] is False and sem_item["sem_pasta"] is True
    assert data["downloads"]["path"].endswith("Downloads")
