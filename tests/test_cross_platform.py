"""Mac + Windows com a mesma data/ (Google Drive).

Cada cliente guarda a pasta uma vez por sistema (path_mac / path_windows) e
`path` é derivado com a desta máquina; cada projeto guarda onde nasceu
(origem: sistema + computador), que vira o ícone do card."""

import json

import pytest

from app import create_app
from clientela import clients as clients_module
from clientela import editor, machine, paths, store
from clientela import projects as projects_module
from clientela.persistence import safe_read_json, safe_write_json


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.WINDOWS)
    monkeypatch.setattr(machine, "hostname", lambda: "Desktop-Main")


@pytest.fixture
def on_mac(monkeypatch):
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.MAC)
    monkeypatch.setattr(machine, "hostname", lambda: "MacBook-Pro")


# ------------------------------------------------------------------ machine
def test_current_os_por_plataforma():
    assert machine.current_os("darwin") == machine.MAC
    assert machine.current_os("win32") == machine.WINDOWS
    assert machine.current_os("linux") == machine.LINUX


@pytest.fixture
def fresh_hostname():
    machine.hostname.cache_clear()
    yield
    machine.hostname.cache_clear()


def test_hostname_sem_dominio(monkeypatch, fresh_hostname):
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.LINUX)
    monkeypatch.setattr(machine.socket, "gethostname", lambda: "MacBook-Pro.local")
    assert machine.hostname() == "MacBook-Pro"


def test_hostname_ip_nao_vira_pedaco(monkeypatch, fresh_hostname):
    """No Mac o gethostname devolveu "192.168.0.12" e o nome virou "192"."""
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.LINUX)
    monkeypatch.setattr(machine.socket, "gethostname", lambda: "192.168.0.12")
    assert machine.hostname() == "192.168.0.12"


def test_hostname_windows_usa_computername(monkeypatch, fresh_hostname):
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.WINDOWS)
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-MAIN")
    assert machine.hostname() == "DESKTOP-MAIN"


def test_icone_por_origem_e_mapeamento():
    so_mac = {"path_mac": "/Users/r/Cliente", "path_windows": ""}
    ambos = {"path_mac": "/Users/r/Cliente", "path_windows": r"G:\Cliente"}
    win = {"os": "windows", "host": "Desktop-Main"}

    assert machine.project_badge({"os": "mac", "host": "MacBook-Pro"}, so_mac)["icone"] == "apple"
    badge = machine.project_badge(win, so_mac)
    assert badge["icone"] == "windows"
    assert badge["rotulo"] == "Windows - Desktop-Main"
    # Acessível pelos dois sistemas: nuvem, venha de onde vier.
    assert machine.project_badge(win, ambos)["icone"] == "nuvem"
    # Registro de antes do Windows (sem origem) = Mac.
    assert machine.project_badge(None, so_mac)["icone"] == "apple"


# ------------------------------------------------------------------ clientes
def test_cadastro_antigo_vira_path_mac(tmp_path, on_mac):
    """clients.json de antes desta mudança só tem `path` — era sempre o Mac."""
    d = tmp_path / "Antigo"
    d.mkdir()
    safe_write_json(paths.CLIENTS_JSON, [{"id": "abc", "name": "Antigo", "path": str(d)}])
    c = clients_module.get_client("abc")
    assert c["path_mac"] == str(d)
    assert c["path_windows"] == ""
    assert c["path"] == str(d)
    assert clients_module.folder_available(c)


def test_path_derivado_do_sistema(tmp_path, monkeypatch):
    mac_dir = tmp_path / "mac"
    win_dir = tmp_path / "win"
    mac_dir.mkdir()
    win_dir.mkdir()
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.MAC)
    c = clients_module.create_client(name="X", path_mac=str(mac_dir), path_windows=str(win_dir))
    assert clients_module.get_client(c["id"])["path"] == str(mac_dir)

    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.WINDOWS)
    assert clients_module.get_client(c["id"])["path"] == str(win_dir)


def test_path_gravado_e_sempre_o_do_mac(tmp_path, on_windows):
    """Com data/ compartilhado, o Windows salvando um cliente não pode trocar
    o `path` gravado (que um app antigo no Mac leria) pelo caminho dele."""
    c = clients_module.create_client(name="X", path_mac="/Users/r/X", path_windows=str(tmp_path / "X"))
    clients_module.update_client(c["id"], name="X2")
    raw = safe_read_json(paths.CLIENTS_JSON, [])[0]
    assert raw["path"] == "/Users/r/X"
    assert raw["path_windows"] == str(tmp_path / "X")


def test_path_sozinho_vale_para_este_sistema(tmp_path, on_windows):
    c = clients_module.create_client(name="X", path_mac="/Users/r/X")
    clients_module.update_client(c["id"], path=str(tmp_path / "X"))
    c = clients_module.get_client(c["id"])
    assert c["path_windows"] == str(tmp_path / "X")
    assert c["path_mac"] == "/Users/r/X"


def test_cliente_sem_pasta_neste_sistema_nao_escaneia_o_diretorio_atual(tmp_path, on_windows, monkeypatch):
    """`Path("")` é o diretório atual: sem o guarda, um cliente mapeado só no
    Mac faria o Windows registrar as pastas do diretório de trabalho."""
    (tmp_path / "001 - Isca").mkdir()
    monkeypatch.chdir(tmp_path)
    c = clients_module.create_client(name="So Mac", path_mac="/Users/r/SoMac")
    assert clients_module.client_dir(c) is None
    assert not clients_module.folder_available(c)
    assert projects_module.list_projects(c["id"]) == []
    status = clients_module.with_folder_status(c)
    assert status["mapeado_neste_sistema"] is False


def test_criar_projeto_sem_pasta_neste_sistema_explica(on_windows):
    c = clients_module.create_client(name="So Mac", path_mac="/Users/r/SoMac")
    with pytest.raises(ValueError, match="Windows"):
        projects_module.create_project(c["id"], "Vinheta")
    # Sem pasta por escolha continua funcionando: o registro é compartilhado.
    p = projects_module.create_project(c["id"], "Tarefa", criar_pasta=False)
    assert p["sem_pasta"]


# ------------------------------------------------------------------ projetos
def test_projeto_guarda_origem(tmp_path, on_windows):
    d = tmp_path / "Cliente"
    d.mkdir()
    c = clients_module.create_client(name="Cliente", path_windows=str(d))
    p = projects_module.create_project(c["id"], "Vinheta")
    assert p["origem"] == {"os": "windows", "host": "Desktop-Main"}
    assert p["maquina"]["icone"] == "windows"
    assert p["maquina"]["rotulo"] == "Windows - Desktop-Main"


def test_pasta_criada_no_finder_registra_esta_maquina(tmp_path, on_mac):
    d = tmp_path / "Cliente"
    (d / "007 - Do Finder").mkdir(parents=True)
    c = clients_module.create_client(name="Cliente", path=str(d))
    [p] = projects_module.list_projects(c["id"])
    assert p["origem"] == {"os": "mac", "host": "MacBook-Pro"}


def test_registro_antigo_nao_ganha_origem_na_leitura(tmp_path, on_windows):
    """_normalize usa new_record como base: se a origem padrão fosse a
    máquina atual, abrir no Windows assinaria todo registro antigo com ela."""
    rec = store.new_record("c1", "001", "Velho", "001 - Velho", "/Users/r/Velho/001 - Velho")
    rec.pop("origem")
    store.save(rec)
    assert store.load("c1:001")["origem"] is None


def test_cliente_nos_dois_sistemas_vira_nuvem(tmp_path, on_mac):
    d = tmp_path / "Cliente"
    d.mkdir()
    c = clients_module.create_client(name="Cliente", path_mac=str(d), path_windows=r"G:\Cliente")
    p = projects_module.create_project(c["id"], "Vinheta")
    assert p["maquina"]["icone"] == "nuvem"


def test_listar_nas_duas_maquinas_nao_regrava_registros(tmp_path, monkeypatch):
    """last_known_path absoluto fora da comparação: senão Mac e Windows
    regravariam cada registro a cada listagem (e o Drive sincronizaria 126
    arquivos a cada abertura do quadro)."""
    mac_dir = tmp_path / "mac"
    win_dir = tmp_path / "win"
    for base in (mac_dir, win_dir):
        (base / "001 - Vinheta").mkdir(parents=True)
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.MAC)
    c = clients_module.create_client(name="X", path_mac=str(mac_dir), path_windows=str(win_dir))
    projects_module.list_projects(c["id"])

    gravacoes = []
    original = store.save
    monkeypatch.setattr(store, "save", lambda rec: gravacoes.append(rec["id"]) or original(rec))
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.WINDOWS)
    [p] = projects_module.list_projects(c["id"])
    assert p["path"] == str(win_dir / "001 - Vinheta")
    assert gravacoes == []


def test_pasta_ausente_aponta_para_o_caminho_deste_sistema(tmp_path, monkeypatch):
    win_dir = tmp_path / "win"
    win_dir.mkdir()
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.MAC)
    rec = store.new_record("c1", "003", "Some", "003 - Some", "/Users/r/X/003 - Some")
    store.save(rec)
    safe_write_json(
        paths.CLIENTS_JSON,
        [{"id": "c1", "name": "X", "path": "/Users/r/X", "path_mac": "/Users/r/X", "path_windows": str(win_dir)}],
    )
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.WINDOWS)
    [p] = projects_module.list_projects("c1")
    assert not p["folder_available"]
    assert p["path"] == str(win_dir / "003 - Some")


# ------------------------------------------------------------------ nomes
@pytest.mark.parametrize(
    "nome, esperado",
    [
        ("Entrevista: parte 2", "Entrevista parte 2"),
        ('Vídeo "final"?', "Vídeo final"),
        ("Projeto.", "Projeto"),
        ("CON", "_CON"),
        ("a/../b", "ab"),
        ("Arraiá 2026", "Arraiá 2026"),
    ],
)
def test_nome_de_pasta_valido_nos_dois_sistemas(nome, esperado):
    assert paths.sanitize_folder_component(nome) == esperado


# ------------------------------------------------------------------ data/
def test_data_dir_por_sistema(tmp_path, monkeypatch):
    cfg = tmp_path / "data_dir.json"
    cfg.write_text(json.dumps({"mac": str(tmp_path / "drive-mac"), "windows": "G:/x"}), encoding="utf-8")
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.resolve_data_dir(env={}, config_file=cfg) == tmp_path / "drive-mac"
    # A variável de ambiente ganha do arquivo.
    assert paths.resolve_data_dir(env={"CLIENTELA_DATA_DIR": str(tmp_path / "env")}, config_file=cfg) == tmp_path / "env"
    # Sem chave para este sistema, nem arquivo: data/ ao lado do app.
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.resolve_data_dir(env={}, config_file=cfg) == paths.BASE_DIR / "data"
    assert paths.resolve_data_dir(env={}, config_file=tmp_path / "nao-existe.json") == paths.BASE_DIR / "data"


# ------------------------------------------------------------------ editor
def test_job_de_outra_maquina_nao_e_tocado_no_boot(on_windows, monkeypatch):
    """O pid de um job do Mac não significa nada no Windows (e lá
    os.kill(pid, 0) mataria o processo de mesmo número)."""
    job = editor._new_job(tipo="legendas", project_id="c:001")
    editor._update_job(job["id"], status=editor.STATUS_RODANDO, pid=4242, host="MacBook-Pro")
    testados = []
    monkeypatch.setattr(editor, "_pid_alive", lambda pid: testados.append(pid) or False)
    editor.resume_jobs()
    assert testados == []
    assert editor.load_job(job["id"])["status"] == editor.STATUS_RODANDO
    with pytest.raises(editor.EditorError, match="outra máquina"):
        editor.cancel_job(job["id"])


def test_job_novo_guarda_a_maquina(on_windows):
    job = editor._new_job(tipo="legendas", project_id="c:001")
    assert job["host"] == "Desktop-Main"


# ------------------------------------------------------------------ API
def test_api_sistema_e_cliente_com_dois_caminhos(tmp_path, on_mac):
    client = create_app().test_client()
    info = client.get("/api/sistema").get_json()
    assert info["os"] == "mac" and info["host"] == "MacBook-Pro" and info["path_key"] == "path_mac"

    d = tmp_path / "Novo"
    r = client.post("/api/clients", json={"name": "Novo", "path_mac": str(d), "path_windows": r"G:\Novo"})
    assert r.status_code == 201
    body = r.get_json()
    assert body["path"] == str(d) and d.is_dir()
    listado = client.get("/api/clients").get_json()[0]
    assert listado["compartilhado"] is True
    assert client.post("/api/clients", json={"name": "Sem"}).status_code == 400
