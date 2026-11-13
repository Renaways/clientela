import json

from clientela.persistence import safe_read_json, safe_write_json


def test_write_then_read_round_trip(tmp_path):
    path = tmp_path / "clients.json"
    safe_write_json(path, {"a": 1})
    assert safe_read_json(path, {}) == {"a": 1}


def test_write_keeps_previous_version_as_bak(tmp_path):
    path = tmp_path / "clients.json"
    safe_write_json(path, {"a": 1})
    safe_write_json(path, {"a": 2})
    bak = tmp_path / "clients.json.bak"
    assert bak.exists()
    assert json.loads(bak.read_text()) == {"a": 1}


def test_read_falls_back_to_bak_when_primary_corrupted(tmp_path):
    path = tmp_path / "clients.json"
    safe_write_json(path, {"a": 1})
    safe_write_json(path, {"a": 2})
    path.write_text("{not valid json")
    assert safe_read_json(path, {}) == {"a": 1}


def test_read_missing_file_returns_default(tmp_path):
    path = tmp_path / "missing.json"
    assert safe_read_json(path, {"fallback": True}) == {"fallback": True}
    assert safe_read_json(path, []) == []


def test_interrupted_write_leaves_primary_intact(tmp_path):
    """Simulates killing the process mid-write: only the .tmp file is
    touched before os.replace, so the primary file must be untouched."""
    path = tmp_path / "clients.json"
    safe_write_json(path, {"a": 1})
    tmp = tmp_path / "clients.json.tmp"
    tmp.write_text("{half-written")
    assert safe_read_json(path, {}) == {"a": 1}


def test_default_nao_e_compartilhado_entre_leituras(tmp_path):
    """`safe_read_json` devolve cópia profunda do default.

    Sem isso, quem recebe o default e faz `append` numa lista de dentro dele
    (sessão de cronômetro, tag, custo extra) muta a constante do módulo — e a
    próxima leitura de um arquivo inexistente vem com o dado do anterior.
    """
    default = {"sessions": [], "meta": {"tags": []}}

    primeiro = safe_read_json(tmp_path / "nao-existe-a.json", default)
    primeiro["sessions"].append({"seconds": 3600})
    primeiro["meta"]["tags"].append("motion")

    segundo = safe_read_json(tmp_path / "nao-existe-b.json", default)

    assert segundo["sessions"] == []
    assert segundo["meta"]["tags"] == []
    assert default == {"sessions": [], "meta": {"tags": []}}


def test_horas_de_um_projeto_nao_vazam_para_outro_sem_time_json(tmp_path):
    """Regressão do bug de verdade.

    `create_project` já grava um `.time.json`, então projetos criados pelo app
    nunca caem no default. O vazamento atingia as pastas criadas **fora** do
    app — no Finder — que é justamente o caso das "00 - Projetos" e
    "000 - Eventos" criadas à mão no Finder. Lançar horas na primeira
    contaminava a segunda com as mesmas sessões.
    """
    from clientela import clients as clients_module
    from clientela import projects as projects_module
    from clientela import timer

    client_dir = tmp_path / "Cliente"
    client_dir.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(client_dir))
    (client_dir / "00 - Projetos").mkdir()
    (client_dir / "000 - Eventos").mkdir()

    timer.log_manual(f"{client['id']}:00", "2026-08-20T10:00:00", "2026-08-20T14:00:00")   # 4h
    timer.log_manual(f"{client['id']}:000", "2026-08-20T10:00:00", "2026-08-20T12:00:00")  # 2h

    assets = timer.get_status(f"{client['id']}:00")
    eventos = timer.get_status(f"{client['id']}:000")

    assert len(assets["sessions"]) == 1
    assert assets["total_seconds"] == 4 * 3600
    # antes do conserto, este aqui vinha com as duas sessões (4h + 2h = 6h)
    assert len(eventos["sessions"]) == 1
    assert eventos["total_seconds"] == 2 * 3600
