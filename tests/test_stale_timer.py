"""Cronômetro esquecido ligado.

`running_start` é só um timestamp e o decorrido é `agora - start`, então fechar
o app com o cronômetro ligado faz ele contar a noite/o fim de semana inteiro —
e a auto-pausa por inatividade não roda com o app desligado para impedir.
"""

from datetime import datetime, timedelta

from clientela import clients as clients_module
from clientela import projects as projects_module
from clientela import timer


def _make_project(tmp_path):
    client_dir = tmp_path / "Cliente"
    client_dir.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(client_dir))
    return projects_module.create_project(client_id=client["id"], name="Job")


def _start_at(project_id: str, quando: datetime) -> None:
    """Coloca o cronômetro rodando desde `quando`, como se o app tivesse sido
    fechado com ele ligado."""
    timer.start_timer(project_id)
    from clientela import paths, store
    from clientela.persistence import safe_write_json

    store.update(project_id, lambda rec: rec["timer"].__setitem__("running_start", quando.isoformat()))
    safe_write_json(paths.TIMER_ACTIVE_JSON, {"project_id": project_id, "running_start": quando.isoformat()})


def test_sem_cronometro_rodando_nao_ha_alerta(tmp_path):
    _make_project(tmp_path)
    assert timer.stale_running_timer() is None


def test_cronometro_recente_nao_dispara_alerta(tmp_path):
    project = _make_project(tmp_path)
    _start_at(project["id"], datetime.now() - timedelta(hours=2))

    assert timer.stale_running_timer() is None


def test_cronometro_rodando_ha_dias_dispara_alerta(tmp_path):
    project = _make_project(tmp_path)
    inicio = datetime.now() - timedelta(hours=61)
    _start_at(project["id"], inicio)

    info = timer.stale_running_timer()

    assert info is not None
    assert info["project_id"] == project["id"]
    assert info["project_title"] == "001 - Job"
    assert 60 * 3600 < info["elapsed_seconds"] < 62 * 3600
    # o corte para horário comercial reduz bastante o que entraria de fato
    assert info["billable_seconds"] < info["elapsed_seconds"]


def test_limite_de_horas_e_configuravel(tmp_path):
    project = _make_project(tmp_path)
    _start_at(project["id"], datetime.now() - timedelta(hours=5))

    assert timer.stale_running_timer(threshold_hours=8) is None
    assert timer.stale_running_timer(threshold_hours=4) is not None


def test_descartar_para_o_cronometro_sem_gravar_a_sessao(tmp_path):
    project = _make_project(tmp_path)
    _start_at(project["id"], datetime.now() - timedelta(hours=61))

    status = timer.discard_running_session(project["id"])

    assert status["status"] == "idle"
    assert status["sessions"] == []          # nada foi gravado
    assert status["total_seconds"] == 0
    assert timer.get_active() is None
    assert timer.stale_running_timer() is None


def test_parar_grava_a_sessao_cortada_ao_horario_comercial(tmp_path):
    """Contraste com descartar: parar preserva a sessão, mas cortada."""
    project = _make_project(tmp_path)
    inicio = datetime.now() - timedelta(hours=61)
    _start_at(project["id"], inicio)

    status = timer.stop_timer(project["id"])

    assert status["status"] == "idle"
    assert len(status["sessions"]) == 1
    assert status["effective_seconds"] < 61 * 3600
    assert timer.get_active() is None


def test_alerta_nao_quebra_com_running_start_invalido(tmp_path):
    from clientela import paths
    from clientela.persistence import safe_write_json

    project = _make_project(tmp_path)
    safe_write_json(paths.TIMER_ACTIVE_JSON, {"project_id": project["id"], "running_start": "nao-e-data"})

    assert timer.stale_running_timer() is None
