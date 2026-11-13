"""Subprojetos: pasta "NN - …" dentro da pasta de um projeto vira o projeto
"<pai>.NN" (id "cliente:012.01"), com etapa, horas e registro próprios."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from clientela import clients as clients_module
from clientela import dashboard, timer
from clientela import projects as projects_module


def _client(tmp_path, name="Prefeitura"):
    d = tmp_path / name
    d.mkdir()
    return clients_module.create_client(name=name, path=str(d))


def test_subpasta_numerada_vira_subprojeto_e_pasta_interna_nao(tmp_path):
    client = _client(tmp_path)
    pai = projects_module.create_project(client_id=client["id"], name="Campanha")
    pai_dir = tmp_path / "Prefeitura" / "001 - Campanha"
    # A pasta interna do Premiere ("001 - Campanha") já existe e tem o número do pai.
    (pai_dir / "01 - Teaser").mkdir()
    (tmp_path / "Prefeitura" / "00 - Assets" / "01 - Logos").mkdir(parents=True)

    por_id = {p["id"]: p for p in projects_module.list_projects(client["id"])}
    sub_id = f"{client['id']}:001.01"
    assert set(por_id) == {pai["id"], sub_id, f"{client['id']}:00"}
    sub = por_id[sub_id]
    assert sub["pai"] == pai["id"] and por_id[pai["id"]]["pai"] is None
    assert sub["name"] == "Teaser"
    assert sub["folder_name"] == "001 - Campanha/01 - Teaser"
    assert sub["folder_available"] is True


def test_criar_subprojeto_numera_dentro_do_pai(tmp_path):
    client = _client(tmp_path)
    pai = projects_module.create_project(client_id=client["id"], name="Campanha")
    a = projects_module.create_project(client_id=client["id"], name="Teaser", pai=pai["id"])
    b = projects_module.create_project(client_id=client["id"], name="Jingle", pai=pai["id"], criar_pasta=False)
    assert (a["num"], b["num"]) == ("001.01", "001.02")
    assert (tmp_path / "Prefeitura" / "001 - Campanha" / "01 - Teaser" / "Brutos").is_dir()
    assert b["sem_pasta"] is True and b["pai"] == pai["id"]
    # O próximo projeto de topo não é afetado pelos subprojetos.
    assert projects_module.create_project(client_id=client["id"], name="Outro")["num"] == "002"
    with pytest.raises(ValueError):
        projects_module.create_project(client_id=client["id"], name="Neto", pai=a["id"])


def test_renomear_subprojeto_e_horas_contam_uma_vez(tmp_path):
    client = _client(tmp_path)
    pai = projects_module.create_project(client_id=client["id"], name="Campanha")
    sub = projects_module.create_project(client_id=client["id"], name="Teaser", pai=pai["id"])
    renomeado = projects_module.rename_project(sub["id"], "Teaser final")
    assert renomeado["folder_name"] == "001 - Campanha/01 - Teaser final"
    assert (tmp_path / "Prefeitura" / "001 - Campanha" / "01 - Teaser final").is_dir()

    inicio = datetime.now().replace(day=15, hour=10, minute=0, second=0, microsecond=0)
    timer.log_manual(sub["id"], inicio.isoformat(), (inicio + timedelta(hours=2)).isoformat())
    stats = dashboard.client_stats(client["id"])
    assert stats["kpis"]["hours"] == pytest.approx(2.0)


def test_pai_com_subprojetos_nao_e_excluido(tmp_path):
    client = _client(tmp_path)
    pai = projects_module.create_project(client_id=client["id"], name="Campanha")
    projects_module.create_project(client_id=client["id"], name="Teaser", pai=pai["id"], criar_pasta=False)
    with pytest.raises(ValueError):
        projects_module.delete_project(pai["id"])


def test_arquivos_do_pai_nao_incluem_os_do_subprojeto(tmp_path):
    from clientela import organizer

    client = _client(tmp_path)
    pai = projects_module.create_project(client_id=client["id"], name="Campanha")
    sub = projects_module.create_project(client_id=client["id"], name="Teaser", pai=pai["id"])
    nomes_pai = {f["name"] for f in organizer.list_project_files(pai["id"])}
    assert "001 - Campanha.prproj" in nomes_pai and "01 - Teaser.prproj" not in nomes_pai
    abrir_pai = [f["name"] for f in organizer.list_open_files(Path(pai["path"]))]
    assert "01 - Teaser.prproj" not in abrir_pai
    abrir_sub = [f["name"] for f in organizer.list_open_files(Path(sub["path"]))]
    assert "01 - Teaser.prproj" in abrir_sub


def test_pendencia_do_subprojeto_diz_de_qual_projeto_ele_e(tmp_path):
    client = _client(tmp_path)
    pai = projects_module.create_project(client_id=client["id"], name="Campanha")
    ontem = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    projects_module.create_project(client_id=client["id"], name="Teaser", pai=pai["id"], due_date=ontem)
    [item] = [i for i in dashboard.pendencias() if i["kind"] == "Atrasado"]
    assert item["title"] == "001.01 - Teaser"
    assert item["sub"] == "Prefeitura · Campanha"


def test_ia_le_as_descricoes_interligadas(tmp_path):
    from clientela import projeto_ia

    client = _client(tmp_path)
    pai = projects_module.create_project(client_id=client["id"], name="Campanha", notes="Campanha de inverno")
    sub = projects_module.create_project(client_id=client["id"], name="Teaser", pai=pai["id"], notes="Teaser de 15s")
    assert projeto_ia.fatos(sub["id"], com_transcricao=False)["projeto_principal"] == {
        "nome": "Campanha", "descricao": "Campanha de inverno"}
    assert projeto_ia.fatos(pai["id"], com_transcricao=False)["subprojetos"] == [
        {"nome": "Teaser", "descricao": "Teaser de 15s"}]
    # Projeto sem grupo: nada novo nos fatos, a assinatura do resumo não muda.
    solo = projects_module.create_project(client_id=client["id"], name="Solo")
    f = projeto_ia.fatos(solo["id"], com_transcricao=False)
    assert "subprojetos" not in f and "projeto_principal" not in f
