"""O registro global de tags (data/tags.json) é só um índice do que os projetos
já usam. Ele pode sumir — data/ é gitignored e não vem de nenhum clone —
enquanto as tags seguem nos registros dos projetos. Quando isso acontece,
tudo que lê /api/tags nasce vazio: o seletor do "Novo projeto", o filtro por
tag e os pesos do rateio."""

from pathlib import Path

from clientela import clients as clients_module
from clientela import paths, store
from clientela import projects as projects_module
from clientela import tags as tags_module


def _apagar_registro():
    """Some com o registro como no caso real. Apaga o .bak junto: safe_read_json
    cai pra ele quando o principal falta, então apagar só um não simula nada."""
    paths.TAGS_JSON.unlink(missing_ok=True)
    paths.TAGS_JSON.with_suffix(".json.bak").unlink(missing_ok=True)


def _make_client(tmp_path, name: str = "Cliente"):
    client_dir = tmp_path / name
    client_dir.mkdir()
    return clients_module.create_client(name=name, path=str(client_dir))


def _projeto_com_tags(client, nome: str, tags: list[str]):
    project = projects_module.create_project(client_id=client["id"], name=nome)
    tags_module.set_project_tags(project["id"], tags)
    return project


def test_registro_perdido_e_reconstruido_a_partir_dos_projetos(tmp_path):
    """O caso real encontrado: 71 projetos com tag e nenhum tags.json."""
    client = _make_client(tmp_path)
    _projeto_com_tags(client, "Campanha", ["Video", "Motion"])
    _projeto_com_tags(client, "Podcast", ["Video"])

    _apagar_registro()
    assert tags_module.list_tags() == []

    tags_module.rebuild_registry_from_disk()

    assert sorted(tags_module.list_tags()) == ["Motion", "Video"]


def test_ordem_e_por_frequencia_de_uso(tmp_path):
    client = _make_client(tmp_path)
    _projeto_com_tags(client, "A", ["Video", "Motion", "Legenda"])
    _projeto_com_tags(client, "B", ["Video", "Motion"])
    _projeto_com_tags(client, "C", ["Video"])

    _apagar_registro()

    assert tags_module.rebuild_registry_from_disk() == ["Video", "Motion", "Legenda"]


def test_tag_criada_a_mao_e_ainda_nao_usada_sobrevive(tmp_path):
    """União, não substituição: quem criou a tag pra usar depois não a perde."""
    client = _make_client(tmp_path)
    _projeto_com_tags(client, "Campanha", ["Video"])
    tags_module.add_tag("Reserva")

    tags_module.rebuild_registry_from_disk()

    assert tags_module.list_tags() == ["Video", "Reserva"]


def test_reconciliacao_e_idempotente_e_nao_regrava_a_toa(tmp_path, monkeypatch):
    """Roda no boot; regravar sem mudança giraria o .bak toda vez que o app sobe."""
    client = _make_client(tmp_path)
    _projeto_com_tags(client, "Campanha", ["Video", "Motion"])
    primeira = tags_module.rebuild_registry_from_disk()

    escritas = []
    original = tags_module.safe_write_json
    monkeypatch.setattr(
        tags_module,
        "safe_write_json",
        lambda path, data: (escritas.append(path), original(path, data))[1],
    )

    assert tags_module.rebuild_registry_from_disk() == primeira
    assert escritas == []


def test_projeto_sem_meta_json_nao_entra_no_registro(tmp_path):
    """Pastas criadas no Finder nascem sem tag — o padrão lido não pode vazar tag."""
    client = _make_client(tmp_path)
    _projeto_com_tags(client, "Campanha", ["Video"])
    (Path(client["path"]) / "002 - Feito no Finder").mkdir()

    _apagar_registro()

    assert tags_module.rebuild_registry_from_disk() == ["Video"]


def test_cliente_com_pasta_inexistente_nao_quebra_o_boot(tmp_path):
    client = _make_client(tmp_path)
    _projeto_com_tags(client, "Campanha", ["Video"])
    clients_module.create_client(name="Sumido", path=str(tmp_path / "nao-existe"))

    _apagar_registro()

    assert tags_module.rebuild_registry_from_disk() == ["Video"]


def test_tag_so_com_espacos_e_ignorada(tmp_path):
    client = _make_client(tmp_path)
    project = projects_module.create_project(client_id=client["id"], name="Campanha")
    store.update(project["id"], lambda rec: rec.__setitem__("tags", ["Video", "   ", ""]))

    _apagar_registro()

    assert tags_module.rebuild_registry_from_disk() == ["Video"]


def test_criar_projeto_com_tag_nova_registra_no_indice_na_hora(tmp_path):
    """A tag criada no modal de novo projeto tem que aparecer no seletor do
    projeto seguinte, não só depois do próximo boot. create_project gravava só
    o registro; set_project_tags é que registrava."""
    client = _make_client(tmp_path)

    projects_module.create_project(
        client_id=client["id"], name="Campanha", tags=["Vertical"]
    )

    assert "Vertical" in tags_module.list_tags()


def test_criar_projeto_sem_tag_nao_mexe_no_indice(tmp_path):
    client = _make_client(tmp_path)
    _projeto_com_tags(client, "Com tag", ["Video"])
    antes = tags_module.list_tags()

    projects_module.create_project(client_id=client["id"], name="Sem tag")

    assert tags_module.list_tags() == antes
