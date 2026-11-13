"""Status "aguardando equipe externa": vale em qualquer etapa, guarda o motivo e
desde quando, pausa o cronômetro ao marcar e vira pendência depois de alguns
dias sem resposta."""

from datetime import datetime, timedelta, timezone

from clientela import clients as clients_module
from clientela import dashboard, history, stages, store, tags, timer
from clientela import projects as projects_module


def _make_project(tmp_path, name="Job"):
    client_dir = tmp_path / "Cliente"
    client_dir.mkdir(exist_ok=True)
    client = clients_module.create_client(name="Cliente", path=str(client_dir))
    return projects_module.create_project(client_id=client["id"], name=name)


def test_projeto_nasce_sem_espera(tmp_path):
    project = _make_project(tmp_path)
    assert project["aguardando_externo"] is None


def test_marcar_e_desmarcar_grava_motivo_desde_e_historico(tmp_path):
    project = _make_project(tmp_path)
    marcado = tags.set_waiting_external(project["id"], "  locução do estúdio X ")
    assert marcado["aguardando_externo"]["motivo"] == "locução do estúdio X"
    assert marcado["aguardando_externo"]["desde"]
    assert marcado["stage"] == project["stage"]

    desmarcado = tags.set_waiting_external(project["id"], None)
    assert desmarcado["aguardando_externo"] is None
    acoes = [e["action"] for e in history.get_history(project["id"])]
    assert acoes[-2:] == ["aguardando_externo", "externo_retomado"]


def test_editar_motivo_mantem_o_desde_e_nao_duplica_evento(tmp_path):
    project = _make_project(tmp_path)
    primeiro = tags.set_waiting_external(project["id"], "trilha")["aguardando_externo"]["desde"]
    editado = tags.set_waiting_external(project["id"], "trilha do músico")
    assert editado["aguardando_externo"] == {"motivo": "trilha do músico", "desde": primeiro}
    acoes = [e["action"] for e in history.get_history(project["id"])]
    assert acoes.count("aguardando_externo") == 1


def test_marcar_pausa_o_cronometro_e_nao_muda_a_etapa(tmp_path):
    project = _make_project(tmp_path)
    stages.set_stage(project["id"], "producao")
    timer.start_timer(project["id"])
    resultado = tags.set_waiting_external(project["id"], "cor")
    assert resultado["timer_status"] == "paused"
    assert resultado["stage"] == "producao"
    assert timer.get_active() is None
    # desmarcar não religa
    assert tags.set_waiting_external(project["id"], None)["timer_status"] == "paused"


def test_vira_pendencia_depois_de_alguns_dias(tmp_path):
    project = _make_project(tmp_path)
    tags.set_waiting_external(project["id"], "aprovação da agência")
    assert not [p for p in dashboard.pendencias() if p["kind"] == "Aguardando"]

    antigo = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    store.update(project["id"], lambda rec: rec["aguardando_externo"].__setitem__("desde", antigo))
    itens = [p for p in dashboard.pendencias() if p["kind"] == "Aguardando"]
    assert len(itens) == 1
    assert itens[0]["value"] == "5d"
    assert "aprovação da agência" in itens[0]["sub"]


def test_registro_antigo_sem_a_chave_le_como_none(tmp_path):
    project = _make_project(tmp_path)
    store.update(project["id"], lambda rec: rec.pop("aguardando_externo"))
    assert projects_module.get_project(project["id"])["aguardando_externo"] is None
