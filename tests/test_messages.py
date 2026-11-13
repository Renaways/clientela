from clientela import clients as clients_module
from clientela import messages
from clientela import projects as projects_module
from clientela import stages
from clientela import tags
from clientela import timer


def _make_project(tmp_path, **client_kwargs):
    client_dir = tmp_path / "Cliente"
    client_dir.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(client_dir), **client_kwargs)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    return client, project


def test_task_link_always_checked_regardless_of_stage(tmp_path):
    _, project = _make_project(tmp_path)
    projects_module.add_link(project["id"], "ClickUp", "https://clickup.example/1")

    data = messages.get_link_candidates(project["id"])
    assert data["candidates"][0] == {
        "label": "ClickUp",
        "url": "https://clickup.example/1",
        "checked": True,
        # A regra decidia certo mas em silêncio; a razão existe pra tela poder
        # dizer por que o link foi marcado.
        "reason": "tarefa",
    }


def test_review_stage_checks_frameio_not_drive(tmp_path):
    client, project = _make_project(tmp_path, frameio="https://f.io/xyz")
    stages.set_stage(project["id"], "revisao")
    projects_module.add_link(project["id"], "Google Drive", "https://drive.example/pasta")

    data = messages.get_link_candidates(project["id"])
    by_label = {c["label"]: c for c in data["candidates"]}
    assert by_label["Google Drive"]["checked"] is False
    assert by_label["Frame.io"]["checked"] is True
    assert by_label["Frame.io"]["url"] == "https://f.io/xyz"
    assert data["stage_group"] == "revisao"


def test_other_stage_checks_drive_not_frameio(tmp_path):
    client, project = _make_project(tmp_path, frameio="https://f.io/xyz")
    stages.set_stage(project["id"], "producao")
    projects_module.add_link(project["id"], "Google Drive", "https://drive.example/pasta")

    data = messages.get_link_candidates(project["id"])
    by_label = {c["label"]: c for c in data["candidates"]}
    assert by_label["Google Drive"]["checked"] is True
    assert by_label["Frame.io"]["checked"] is False
    assert data["stage_group"] == "outras"


def test_unmatched_link_labels_default_unchecked(tmp_path):
    _, project = _make_project(tmp_path)
    projects_module.add_link(project["id"], "Roteiro", "https://docs.example/roteiro")

    data = messages.get_link_candidates(project["id"])
    assert data["candidates"][0]["checked"] is False


def test_explicit_frameio_link_not_duplicated_with_client_field(tmp_path):
    _, project = _make_project(tmp_path, frameio="https://f.io/xyz")
    projects_module.add_link(project["id"], "Frame.io", "https://f.io/override")

    data = messages.get_link_candidates(project["id"])
    frameio_entries = [c for c in data["candidates"] if c["label"] == "Frame.io"]
    assert len(frameio_entries) == 1
    assert frameio_entries[0]["url"] == "https://f.io/override"


def test_render_message_default_template_with_due_date_and_links(tmp_path):
    _, project = _make_project(tmp_path)
    tags.set_due_date(project["id"], "2026-08-20")

    message = messages.render_message(
        project["id"], [{"label": "ClickUp", "url": "https://clickup.example/1"}]
    )
    assert "Cliente" in message
    assert "Prazo: 20/08/2026" in message
    assert "CLICKUP: https://clickup.example/1" in message


def test_render_message_collapses_blank_sections_when_no_due_date_or_links(tmp_path):
    _, project = _make_project(tmp_path)

    message = messages.render_message(project["id"], [])
    assert "Prazo" not in message
    assert "Links" not in message
    assert "\n\n\n" not in message


def test_custom_template_uses_saved_codes(tmp_path):
    _, project = _make_project(tmp_path)
    messages.set_settings(template="{cliente} / {etapa}")

    message = messages.render_message(project["id"], [])
    assert message == "Cliente / Backlog"


def test_settings_roundtrip_defaults_when_unset(tmp_path):
    assert messages.get_settings()["template"] == messages.DEFAULT_TEMPLATE
    messages.set_settings(template="oi {cliente}")
    assert messages.get_settings()["template"] == "oi {cliente}"


def test_mensagem_da_tarefa_usa_o_segundo_modelo_com_descricao_e_horas(tmp_path):
    """WhatsApp é curto e vai pro cliente; a tarefa é estruturada e vai pro
    ClickUp, onde descrição e horas fazem sentido e emoji não faz."""
    _, project = _make_project(tmp_path)
    projects_module.set_notes(project["id"], "Cortes verticais, 3 peças de até 60s.")
    timer.log_manual(project["id"], "2026-08-20T10:00:00", "2026-08-20T12:00:00")

    whats = messages.render_message(project["id"], [])
    tarefa = messages.render_message(project["id"], [], kind="task")

    assert "📌" in whats
    assert "Cortes verticais" not in whats

    assert "📌" not in tarefa
    assert "Cortes verticais, 3 peças de até 60s." in tarefa
    assert "Horas até aqui: 2h 00m" in tarefa


def test_os_dois_modelos_sao_editaveis_e_independentes():
    messages.set_settings(template="só whats {projeto}")

    assert messages.get_settings()["template"] == "só whats {projeto}"
    assert messages.get_settings()["task_template"] == messages.DEFAULT_TASK_TEMPLATE

    messages.set_settings(task_template="só tarefa {horas}")

    assert messages.get_settings()["template"] == "só whats {projeto}"
    assert messages.get_settings()["task_template"] == "só tarefa {horas}"
