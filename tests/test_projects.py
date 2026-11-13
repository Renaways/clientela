from pathlib import Path

from clientela import clients as clients_module
from clientela import projects as projects_module


def _make_client(tmp_path, **kwargs):
    client_dir = tmp_path / "Cliente"
    client_dir.mkdir()
    return clients_module.create_client(name="Cliente", path=str(client_dir), **kwargs)


def _inner_dir(project: dict) -> Path:
    project_dir = Path(project["path"])
    return project_dir / project_dir.name


def test_create_project_without_template_touches_empty_files(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    inner = _inner_dir(project)
    prproj = inner / f"{inner.name}.prproj"
    aep = inner / f"{inner.name}.aep"
    assert prproj.exists() and prproj.stat().st_size == 0
    assert aep.exists() and aep.stat().st_size == 0


def test_create_project_with_use_template_copies_and_renames_files(tmp_path):
    premiere_src = tmp_path / "Base.prproj"
    premiere_src.write_text("premiere-template-content")
    ae_src = tmp_path / "Base.aep"
    ae_src.write_text("ae-template-content")

    client = _make_client(
        tmp_path,
        template_path_premiere=str(premiere_src),
        template_path_ae=str(ae_src),
    )
    project = projects_module.create_project(client_id=client["id"], name="Job", use_template=True)
    inner = _inner_dir(project)
    prproj = inner / f"{inner.name}.prproj"
    aep = inner / f"{inner.name}.aep"
    assert prproj.read_text() == "premiere-template-content"
    assert aep.read_text() == "ae-template-content"


def test_create_project_without_use_template_flag_ignores_configured_templates(tmp_path):
    premiere_src = tmp_path / "Base.prproj"
    premiere_src.write_text("premiere-template-content")

    client = _make_client(tmp_path, template_path_premiere=str(premiere_src))
    project = projects_module.create_project(client_id=client["id"], name="Job", use_template=False)
    inner = _inner_dir(project)
    prproj = inner / f"{inner.name}.prproj"
    assert prproj.stat().st_size == 0


def test_create_project_falls_back_when_template_file_missing(tmp_path):
    missing_src = tmp_path / "does-not-exist.prproj"
    client = _make_client(tmp_path, template_path_premiere=str(missing_src))
    project = projects_module.create_project(client_id=client["id"], name="Job", use_template=True)
    inner = _inner_dir(project)
    prproj = inner / f"{inner.name}.prproj"
    assert prproj.exists() and prproj.stat().st_size == 0


def test_create_project_grava_descricao_inicial(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(
        client_id=client["id"], name="Job", notes="Briefing colado do ClickUp"
    )
    assert project["notes"] == "Briefing colado do ClickUp"
    assert projects_module.get_project(project["id"])["notes"] == "Briefing colado do ClickUp"


def test_create_project_sem_descricao_continua_vazia(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    assert project["notes"] == ""


def test_frameio_do_cliente_aparece_nos_links_do_projeto(tmp_path):
    # O Frame.io é cadastrado no cliente e nunca chega no Links/links.txt, mas
    # o card de Links do projeto é onde o usuário vai procurar por ele.
    client = _make_client(tmp_path, frameio="https://f.io/xyz")
    project = projects_module.create_project(client_id=client["id"], name="Job")

    links = projects_module.list_links_with_client_defaults(project["id"])
    assert {"label": "Frame.io", "url": "https://f.io/xyz"} in links


def test_frameio_do_projeto_ganha_do_frameio_do_cliente(tmp_path):
    client = _make_client(tmp_path, frameio="https://f.io/cliente")
    project = projects_module.create_project(client_id=client["id"], name="Job")
    projects_module.add_link(project["id"], "Frame.io", "https://f.io/projeto")

    links = projects_module.list_links_with_client_defaults(project["id"])
    frameio = [l for l in links if projects_module.is_frameio_label(l["label"])]
    assert len(frameio) == 1
    assert frameio[0]["url"] == "https://f.io/projeto"


def test_sem_frameio_no_cliente_a_lista_fica_como_esta(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    projects_module.add_link(project["id"], "ClickUp", "https://clickup.example/1")

    links = projects_module.list_links_with_client_defaults(project["id"])
    assert links == [{"label": "ClickUp", "url": "https://clickup.example/1"}]
