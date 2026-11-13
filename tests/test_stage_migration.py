""""Concluído" virou a etapa terminal; "Liberado" passou a ser o degrau antes
dela (arquivo final disponível, projeto ainda aberto). Quem estava em
"liberado" com o sentido antigo vai pro arquivo uma única vez."""

from clientela import clients as clients_module
from clientela import paths, store
from clientela import projects as projects_module
from clientela import stages as stages_module
from clientela.persistence import safe_read_json


def _force_stage(project_id: str, stage: str) -> None:
    """Grava a etapa direto no registro, sem passar por set_stage (que
    gravaria evento no histórico e sincronizaria o cronômetro)."""
    store.update(project_id, lambda rec: rec.__setitem__("stage", stage))


def _make_client(tmp_path, name: str = "Cliente"):
    client_dir = tmp_path / name
    client_dir.mkdir()
    return clients_module.create_client(name=name, path=str(client_dir))


def test_pipeline_termina_em_concluido_com_liberado_antes():
    assert stages_module.STAGES[-2:] == ["liberado", "concluido"]
    assert stages_module.FINAL_STAGE == "concluido"


def test_migracao_leva_os_liberados_antigos_pro_arquivo(tmp_path):
    client = _make_client(tmp_path)
    antigo = projects_module.create_project(client_id=client["id"], name="Antigo")
    aberto = projects_module.create_project(client_id=client["id"], name="Aberto")
    _force_stage(antigo["id"], "liberado")
    stages_module.set_stage(aberto["id"], "producao")

    assert stages_module.migrate_liberado_to_concluido() == 1

    assert projects_module.get_project(antigo["id"])["stage"] == "concluido"
    assert projects_module.get_project(aberto["id"])["stage"] == "producao"
    assert safe_read_json(paths.MIGRATIONS_JSON, {})["liberado_para_concluido"] is True


def test_migracao_nao_grava_evento_no_historico(tmp_path):
    """Um evento de conclusão datado de hoje em todos os projetos migrados
    destruiria a ordem cronológica do arquivo."""
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Antigo")
    _force_stage(project["id"], "liberado")
    antes = store.load(project["id"])["history"]

    stages_module.migrate_liberado_to_concluido()

    depois = store.load(project["id"])["history"]
    assert depois == antes


def test_migracao_roda_uma_vez_so(tmp_path):
    """Depois da troca, "liberado" é uma etapa comum: quem for colocado nela
    de propósito não pode ser empurrado pro arquivo no boot seguinte."""
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Job")
    stages_module.migrate_liberado_to_concluido()

    stages_module.set_stage(project["id"], "liberado")
    assert stages_module.migrate_liberado_to_concluido() == 0
    assert projects_module.get_project(project["id"])["stage"] == "liberado"


def test_liberado_novo_continua_ativo_e_fora_do_arquivo(tmp_path):
    from clientela import dashboard

    client = _make_client(tmp_path)
    liberado = projects_module.create_project(client_id=client["id"], name="Liberado")
    concluido = projects_module.create_project(client_id=client["id"], name="Concluido")
    stages_module.set_stage(liberado["id"], "liberado")
    stages_module.set_stage(concluido["id"], "concluido")

    ativos = {p["id"] for p in dashboard.active_projects()}
    assert liberado["id"] in ativos
    assert concluido["id"] not in ativos
    assert dashboard.home()["stats"]["active_projects"] == 1
    assert dashboard.home()["stats"]["delivered_this_week"] == 1
