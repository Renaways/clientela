"""O histórico acumulou três formatos de evento ao longo das versões do app.
Contados nos dados reais: 58 no formato atual e 41 em dois formatos antigos.
Ler só o atual acharia data de liberação em pouco mais da metade dos eventos, e
a ordenação do arquivo cairia no fallback à toa."""

from clientela import clients as clients_module
from clientela import history, store
from clientela import projects as projects_module
from clientela import stages


def _make_client(tmp_path, name: str = "Cliente"):
    client_dir = tmp_path / name
    client_dir.mkdir()
    return clients_module.create_client(name=name, path=str(client_dir))


def _set(project_id: str, **fields):
    store.update(project_id, lambda rec: rec.update(fields))


def test_le_o_formato_atual():
    eventos = [
        {"action": "etapa_alterada", "details": {"de": "revisao", "para": "liberado"},
         "at": "2026-08-28T16:40:00+00:00"},
    ]
    assert history.last_stage_change_to(eventos, "liberado") == "2026-08-28T16:40:00+00:00"


def test_le_o_formato_antigo_com_to_stage():
    eventos = [
        {"ts": "2026-08-05T17:54:55", "type": "stage_change",
         "from_stage": "aprovacao_tecnica", "to_stage": "liberado"},
    ]
    assert history.last_stage_change_to(eventos, "liberado") == "2026-08-05T17:54:55"


def test_le_o_formato_antigo_com_to():
    eventos = [
        {"ts": "2026-08-11T13:04:25", "type": "stage_change",
         "from": "aprovacao_tecnica", "to": "liberado"},
    ]
    assert history.last_stage_change_to(eventos, "liberado") == "2026-08-11T13:04:25"


def test_pega_a_ultima_passagem_quando_o_projeto_voltou_e_liberou_de_novo():
    eventos = [
        {"ts": "2026-01-01T10:00:00", "type": "stage_change", "to_stage": "liberado"},
        {"action": "etapa_alterada", "details": {"para": "ajuste"}, "at": "2026-02-01T10:00:00"},
        {"action": "etapa_alterada", "details": {"para": "liberado"}, "at": "2026-03-01T10:00:00"},
    ]
    assert history.last_stage_change_to(eventos, "liberado") == "2026-03-01T10:00:00"


def test_sem_evento_da_etapa_devolve_none():
    eventos = [{"action": "etapa_alterada", "details": {"para": "revisao"}, "at": "2026-01-01"}]
    assert history.last_stage_change_to(eventos, "liberado") is None
    assert history.last_stage_change_to([], "liberado") is None


def test_evento_de_formato_desconhecido_nao_quebra():
    eventos = [{"algo": "inesperado"}, {"details": None}]
    assert history.last_stage_change_to(eventos, "liberado") is None


def test_projeto_concluido_expoe_a_data_no_dicionario(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    stages.set_stage(project["id"], "concluido")

    assert projects_module.get_project(project["id"])["concluido_em"] is not None


def test_projeto_nao_concluido_nao_paga_a_leitura_do_historico(tmp_path):
    """Só o arquivo usa esta data; ler o histórico dos 102 projetos na listagem
    seria um quinto arquivo por projeto sem ninguém consumir."""
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    stages.set_stage(project["id"], "liberado")

    assert projects_module.get_project(project["id"])["concluido_em"] is None


def test_concluido_sem_historico_devolve_none_em_vez_de_quebrar(tmp_path):
    """Projeto concluído por uma versão antiga que não gravava histórico —
    o arquivo cai na ordenação por número."""
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    _set(project["id"], stage="concluido", history=[])

    assert projects_module.get_project(project["id"])["concluido_em"] is None


def test_concluido_pela_migracao_usa_a_data_da_liberacao_antiga(tmp_path):
    """Quem foi "liberado" quando essa era a etapa final não tem evento de
    conclusão: a migração não grava um (seria a data da migração em todos).
    A data de conclusão dele é a da liberação antiga."""
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    _set(project["id"], stage="concluido", history=[
        {"action": "etapa_alterada", "details": {"de": "ajuste", "para": "liberado"},
         "at": "2026-05-10T15:00:00"},
    ])

    assert projects_module.get_project(project["id"])["concluido_em"] == "2026-05-10T15:00:00"


def test_conclusao_de_verdade_ganha_da_liberacao_anterior(tmp_path):
    """No fluxo novo o projeto passa por "liberado" antes de "concluido":
    a data do arquivo é a da conclusão, não a do degrau anterior."""
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    _set(project["id"], stage="concluido", history=[
        {"action": "etapa_alterada", "details": {"de": "ajuste", "para": "liberado"},
         "at": "2026-05-10T15:00:00"},
        {"action": "etapa_alterada", "details": {"de": "liberado", "para": "concluido"},
         "at": "2026-05-20T09:00:00"},
    ])

    assert projects_module.get_project(project["id"])["concluido_em"] == "2026-05-20T09:00:00"
