"""Rodapé de ações rápidas do cartão do pipeline: os .prproj/.aep da pasta e
os links do projeto (mais o Frame.io do cliente) chegam junto com a lista do
quadro, sem uma requisição por cartão."""

import os
import time
from pathlib import Path

from clientela import clients as clients_module
from clientela import organizer
from clientela import projects as projects_module


def _client(tmp_path, **extra):
    d = tmp_path / "Cliente"
    d.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(d))
    if extra:
        client = clients_module.update_client(client["id"], **extra)
    return client


def _limpa_modelos(folder: Path) -> None:
    """create_project já deixa um .prproj e um .aep vazios na pasta; os testes
    querem controlar exatamente quais arquivos existem."""
    for p in folder.rglob("*"):
        if p.suffix in (".prproj", ".aep"):
            p.unlink()


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))


def test_arquivos_de_projeto_mais_recente_primeiro_e_sem_autosave(tmp_path):
    client = _client(tmp_path)
    project = projects_module.create_project(client["id"], "Reels")
    folder = Path(project["path"])
    _limpa_modelos(folder)
    agora = time.time()
    _touch(folder / "Edit" / "reels_v1.prproj", agora - 300)
    _touch(folder / "Edit" / "reels_v2.prproj", agora - 10)
    _touch(folder / "vinheta.aep", agora - 100)
    _touch(folder / "Edit" / "Adobe Premiere Pro Auto-Save" / "reels_v2-1.prproj", agora)
    _touch(folder / ".oculto" / "x.prproj", agora)
    _touch(folder / "Export" / "final.mp4", agora)

    arquivos = organizer.list_open_files(folder)

    assert [a["name"] for a in arquivos] == ["reels_v2.prproj", "vinheta.aep", "reels_v1.prproj"]
    assert arquivos[0] == {"name": "reels_v2.prproj", "ext": "PRPROJ", "path": "Edit/reels_v2.prproj"}


def test_quadro_recebe_atalhos_com_frameio_do_cliente(tmp_path):
    client = _client(tmp_path, frameio="https://f.io/cliente")
    project = projects_module.create_project(
        client["id"], "Reels", links=[{"label": "ClickUp", "url": "https://app.clickup.com/t/1"}]
    )
    _limpa_modelos(Path(project["path"]))
    _touch(Path(project["path"]) / "reels.prproj", time.time())

    lista = organizer.attach_quick_actions(client["id"], projects_module.list_projects(client["id"]))

    atalhos = lista[0]["atalhos"]
    assert [a["name"] for a in atalhos["arquivos"]] == ["reels.prproj"]
    assert atalhos["links"] == [
        {"label": "ClickUp", "url": "https://app.clickup.com/t/1"},
        {"label": "Frame.io", "url": "https://f.io/cliente"},
    ]
    # o painel de detalhe e o cartão têm que concordar
    assert atalhos["links"] == projects_module.list_links_with_client_defaults(project["id"])


def test_frameio_do_projeto_ganha_do_cliente(tmp_path):
    client = _client(tmp_path, frameio="https://f.io/cliente")
    projects_module.create_project(
        client["id"], "Reels", links=[{"label": "Frame io revisão", "url": "https://f.io/projeto"}]
    )
    lista = organizer.attach_quick_actions(client["id"], projects_module.list_projects(client["id"]))
    assert [l["url"] for l in lista[0]["atalhos"]["links"]] == ["https://f.io/projeto"]


def test_projeto_sem_pasta_vem_com_arquivos_vazios(tmp_path):
    client = _client(tmp_path)
    project = projects_module.create_project(client["id"], "Reels")
    import shutil

    shutil.rmtree(project["path"])
    lista = organizer.attach_quick_actions(client["id"], projects_module.list_projects(client["id"]))
    assert lista[0]["folder_available"] is False
    assert lista[0]["atalhos"] == {"arquivos": [], "sequencias": [], "links": []}
