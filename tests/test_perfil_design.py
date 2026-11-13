"""Perfil de uso "design" (config.json → perfil_uso): nomes das etapas e arquivos de design como atalho."""

from pathlib import Path

from clientela import clients as clients_module
from clientela import organizer, paths, stages
from clientela import projects as projects_module
from clientela.persistence import safe_write_json


def _perfil(valor):
    safe_write_json(paths.CONFIG_JSON, {"perfil_uso": valor} if valor else {})


def test_sem_chave_e_video_e_design_troca_os_nomes():
    _perfil(None)
    assert stages.perfil_uso() == "video" and stages.labels()["producao"] == "Produção"
    _perfil("design")
    assert stages.labels()["producao"] == "Criação" and stages.labels()["concluido"] == "Entregue"
    assert set(stages.STAGE_LABELS_DESIGN) == set(stages.STAGES)


def test_psd_vira_atalho_so_no_perfil_design(tmp_path):
    d = tmp_path / "Cliente"
    d.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(d))
    project = projects_module.create_project(client["id"], "Identidade")
    folder = Path(project["path"])
    (folder / "logo.psd").write_bytes(b"x")
    (folder / "marca.ai").write_bytes(b"x")
    _perfil(None)
    nomes = lambda: {a["name"] for p in organizer.attach_quick_actions(client["id"], projects_module.list_projects(client["id"])) for a in p["atalhos"]["arquivos"]}
    assert not {"logo.psd", "marca.ai"} & nomes()
    _perfil("design")
    assert {"logo.psd", "marca.ai"} <= nomes()
