"""Cronômetro amarrado à etapa, e auto-pausa que se desarma quando cega.

Regressão de um caso real (um cliente, projeto 090): sem permissão de Acessibilidade no
macOS o pynput sobe, fica `is_alive()`, não loga nada e **nunca** entrega um
evento — `_last_activity` congelava no import, a auto-pausa passava a achar que
a máquina estava ociosa e matava o cronômetro a cada tick de 20s. O autosave do
Premiere religava logo em seguida via watchdog. Resultado: 26 sessões de 2 a 19
segundos, todas abaixo do piso de 15 min de `effective_seconds` — 45 minutos
reais de trabalho viraram 0h nas estatísticas.
"""

from clientela import automation
from clientela import clients as clients_module
from clientela import projects as projects_module
from clientela import stages, timer


def _make_project(tmp_path, stage="backlog"):
    client_dir = tmp_path / "Cliente"
    client_dir.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(client_dir))
    return projects_module.create_project(
        client_id=client["id"], name="Job", initial_stage=stage
    )


# ------------------------------------------------ cronômetro segue a etapa


def test_entrar_em_producao_liga_o_cronometro(tmp_path):
    project = _make_project(tmp_path)
    assert timer.get_status(project["id"])["status"] == "idle"

    stages.set_stage(project["id"], "producao")

    assert timer.get_status(project["id"])["status"] == "running"
    assert timer.get_active()["project_id"] == project["id"]


def test_sair_de_producao_para_e_grava_a_sessao(tmp_path):
    project = _make_project(tmp_path)
    stages.set_stage(project["id"], "producao")

    stages.set_stage(project["id"], "revisao")

    status = timer.get_status(project["id"])
    assert status["status"] == "paused"
    assert len(status["sessions"]) == 1, "a sessão precisa ser gravada, não descartada"
    assert timer.get_active() is None


def test_mudanca_entre_etapas_fora_de_producao_nao_mexe_no_cronometro(tmp_path):
    project = _make_project(tmp_path, stage="revisao")

    stages.set_stage(project["id"], "aprovacao_tecnica")

    status = timer.get_status(project["id"])
    assert status["status"] == "idle"
    assert status["sessions"] == []


def test_reentrar_em_producao_nao_reinicia_uma_sessao_ja_em_curso(tmp_path):
    project = _make_project(tmp_path)
    stages.set_stage(project["id"], "producao")
    inicio = timer.get_status(project["id"])["running_start"]

    # gravar a mesma etapa de novo não é transição: não deve reiniciar nada
    stages.set_stage(project["id"], "producao")

    status = timer.get_status(project["id"])
    assert status["running_start"] == inicio
    assert status["sessions"] == []


def test_desligar_auto_timer_devolve_o_controle_manual(tmp_path):
    automation.set_automation_config(auto_timer_on_producao=False)
    project = _make_project(tmp_path)

    stages.set_stage(project["id"], "producao")

    assert timer.get_status(project["id"])["status"] == "idle"


# ------------------------------------------------ auto-pausa à prova de falha


def test_auto_pausa_desarmada_quando_o_monitoramento_esta_cego(monkeypatch):
    """Sem prova de que teclado/mouse são visíveis, a auto-pausa fica inerte."""
    monkeypatch.setattr(automation, "PYNPUT_AVAILABLE", True)
    monkeypatch.setattr(automation, "_activity_seen", False)
    monkeypatch.setattr(automation, "AX_TRUST_PROBE_AVAILABLE", True)
    monkeypatch.setattr(automation, "AXIsProcessTrusted", lambda: False, raising=False)

    assert automation.input_monitoring_ok() is False
    assert automation.automation_status()["auto_pause_armed"] is False


def test_um_unico_evento_real_arma_a_auto_pausa(monkeypatch):
    monkeypatch.setattr(automation, "PYNPUT_AVAILABLE", True)
    monkeypatch.setattr(automation, "_activity_seen", False)
    monkeypatch.setattr(automation, "AX_TRUST_PROBE_AVAILABLE", True)
    monkeypatch.setattr(automation, "AXIsProcessTrusted", lambda: False, raising=False)

    automation._on_activity()

    assert automation.input_monitoring_ok() is True
    assert automation.automation_status()["auto_pause_armed"] is True


def test_sem_probe_e_sem_evento_o_estado_e_indefinido_e_nao_pausa(monkeypatch):
    """Fora do macOS não há como perguntar; na dúvida não se pausa nada."""
    monkeypatch.setattr(automation, "PYNPUT_AVAILABLE", True)
    monkeypatch.setattr(automation, "_activity_seen", False)
    monkeypatch.setattr(automation, "AX_TRUST_PROBE_AVAILABLE", False)

    assert automation.input_monitoring_ok() is None
    assert automation.automation_status()["auto_pause_armed"] is False


def test_auto_start_por_atividade_de_arquivo_vem_desligado():
    """Era o autosave do Premiere religando o cronômetro a cada poucos minutos."""
    assert automation.get_automation_config()["auto_start_enabled"] is False
