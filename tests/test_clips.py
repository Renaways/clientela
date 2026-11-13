"""Clipes de live: a pasta de gravação vigiada vira projeto sozinha.

Clipe novo e parado (tamanho/mtime sem mudar por ``estavel_segundos``) →
projeto no cliente configurado, clipe movido para Brutos/ e marcado como
``midia_original``, evento ``clipe_pronto`` para os callbacks e para o
frontend. O tempo é passado explícito para ``scan_once``; nenhum teste
espera de verdade."""

import os
from datetime import datetime
from pathlib import Path

import pytest

from app import create_app
from clientela import clients as clients_module
from clientela import clips, history, machine, store
from clientela import projects as projects_module


@pytest.fixture
def setup(tmp_path):
    client_dir = tmp_path / "Twitch"
    client_dir.mkdir()
    client = clients_module.create_client(name="Twitch", path=str(client_dir))
    inbox = tmp_path / "Gravacoes"
    inbox.mkdir()
    clips.set_config(ativo=True, pasta=str(inbox), client_id=client["id"], estavel_segundos=5)
    # Primeiro ciclo: o vigia "liga" e fotografa a pasta (vazia).
    clips.scan_once(now=1000.0)
    return client, client_dir, inbox


def _write(path: Path, data: bytes = b"video", mtime: float | None = None) -> Path:
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_clipe_parado_vira_projeto_com_o_video_em_brutos(setup):
    client, client_dir, inbox = setup
    _write(inbox / "2026-10-20 14-33-12.mkv", b"x" * 100, mtime=1001.0)

    assert clips.scan_once(now=1002.0) == []      # entrou na fila
    assert clips.scan_once(now=1004.0) == []      # parado há 2 s < 5 s
    [event] = clips.scan_once(now=1008.0)

    assert event["tipo"] == clips.EVENT
    assert not (inbox / "2026-10-20 14-33-12.mkv").exists()
    assert list(inbox.iterdir()) == []
    project = projects_module.get_project(event["project_id"])
    assert project["client_id"] == client["id"]
    assert project["name"] == "Clipe 2026-10-20 14-33-12"
    assert project["stage"] == "backlog"
    dest = Path(project["path"]) / "Brutos" / "2026-10-20 14-33-12.mkv"
    assert dest.read_bytes() == b"x" * 100
    assert event["video"] == str(dest)
    assert project["midia_original"]["arquivo"] == "Brutos/2026-10-20 14-33-12.mkv"
    assert project["midia_original"]["nome_original"] == "2026-10-20 14-33-12.mkv"
    assert project["midia_original"]["tamanho"] == 100
    assert [e["action"] for e in history.get_history(project["id"])] == ["criado", "clipe_recebido"]
    # A estrutura de projeto de sempre, numerada como qualquer outro.
    assert (Path(project["path"]) / "Links").is_dir()
    assert Path(project["path"]).name.startswith("001 - ")


def test_arquivo_ainda_crescendo_nao_e_processado(setup):
    _, _, inbox = setup
    clip = inbox / "live.mp4"
    _write(clip, b"a", mtime=1001.0)
    clips.scan_once(now=1002.0)
    _write(clip, b"ab", mtime=1006.0)             # o gravador ainda escreve
    assert clips.scan_once(now=1007.0) == []
    _write(clip, b"abc", mtime=1011.0)
    assert clips.scan_once(now=1012.0) == []
    assert clip.exists()
    assert clips.scan_once(now=1016.0) == []      # parado há 4 s
    [event] = clips.scan_once(now=1018.0)
    assert Path(event["video"]).read_bytes() == b"abc"


def test_arquivo_vazio_espera(setup):
    _, _, inbox = setup
    _write(inbox / "live.mp4", b"", mtime=1001.0)
    clips.scan_once(now=1002.0)
    assert clips.scan_once(now=1100.0) == []
    assert (inbox / "live.mp4").exists()


def test_ignora_ocultos_e_outras_extensoes(setup):
    _, _, inbox = setup
    _write(inbox / ".live.mp4", mtime=1001.0)
    _write(inbox / "notas.txt", mtime=1001.0)
    _write(inbox / "live.mp4.part", mtime=1001.0)
    clips.scan_once(now=1002.0)
    assert clips.scan_once(now=1100.0) == []
    assert len(list(inbox.iterdir())) == 3


def test_videos_antigos_da_pasta_ficam_de_fora(tmp_path):
    client_dir = tmp_path / "Twitch"
    client_dir.mkdir()
    client = clients_module.create_client(name="Twitch", path=str(client_dir))
    inbox = tmp_path / "Gravacoes"
    inbox.mkdir()
    _write(inbox / "antigo.mp4", mtime=500.0)
    clips.set_config(ativo=True, pasta=str(inbox), client_id=client["id"], estavel_segundos=5)

    clips.scan_once(now=1000.0)
    assert clips.scan_once(now=1100.0) == []
    assert (inbox / "antigo.mp4").exists()

    clips.set_config(processar_existentes=True)
    clips.scan_once(now=1200.0)
    [event] = clips.scan_once(now=1210.0)
    assert event["video"].endswith("antigo.mp4")


def test_desligado_nao_faz_nada(setup):
    _, _, inbox = setup
    clips.set_config(ativo=False)
    _write(inbox / "live.mp4", mtime=1001.0)
    clips.scan_once(now=1002.0)
    assert clips.scan_once(now=1100.0) == []
    assert (inbox / "live.mp4").exists()
    assert clips.status()["motivo"] == "desligado"


def test_callback_recebe_o_evento_e_um_callback_quebrado_nao_atrapalha(setup):
    _, _, inbox = setup
    recebidos = []

    @clips.on_clip_ready
    def quebra(event):
        raise RuntimeError("boom")

    clips.on_clip_ready(recebidos.append)
    _write(inbox / "live.mp4", mtime=1001.0)
    clips.scan_once(now=1002.0)
    clips.scan_once(now=1010.0)
    assert len(recebidos) == 1
    assert recebidos[0]["project_id"]
    assert clips.events_after(0)["ultimo"] == 1
    assert clips.events_after(1)["eventos"] == []


def test_falha_na_criacao_devolve_o_clipe_e_nao_deixa_projeto(setup, monkeypatch):
    client, client_dir, inbox = setup
    real_move = clips.shutil.move

    def move_quebrado(src, dst):
        raise OSError("disco cheio")

    monkeypatch.setattr(clips.shutil, "move", move_quebrado)
    _write(inbox / "live.mp4", b"abc", mtime=1001.0)
    clips.scan_once(now=1002.0)
    assert clips.scan_once(now=1010.0) == []

    assert (inbox / "live.mp4").read_bytes() == b"abc"
    assert [p.name for p in inbox.iterdir()] == ["live.mp4"]
    assert list(client_dir.iterdir()) == []
    assert store.list_for_client(client["id"]) == []
    [falha] = clips.status()["falhas"]
    assert "disco cheio" in falha["erro"]

    # A mesma falha não se repete a cada ciclo; "verificar" tenta de novo.
    monkeypatch.setattr(clips.shutil, "move", real_move)
    assert clips.scan_once(now=1020.0) == []
    clips.retry_failures()
    clips.scan_once(now=1030.0)
    [event] = clips.scan_once(now=1040.0)
    assert Path(event["video"]).read_bytes() == b"abc"


def test_arquivo_preso_pelo_gravador_volta_para_a_fila(setup, monkeypatch):
    _, client_dir, inbox = setup
    real_rename = Path.rename
    preso = {"ativo": True}

    def rename(self, target):
        if preso["ativo"] and self.name == "live.mp4":
            raise PermissionError("em uso por outro processo")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    _write(inbox / "live.mp4", mtime=1001.0)
    clips.scan_once(now=1002.0)
    assert clips.scan_once(now=1010.0) == []
    assert list(client_dir.iterdir()) == []       # nenhum projeto criado
    assert clips.status()["falhas"] == []
    assert [p["arquivo"] for p in clips.status()["pendentes"]] == ["live.mp4"]

    preso["ativo"] = False
    assert clips.scan_once(now=1012.0) == []      # voltou à fila do zero
    [event] = clips.scan_once(now=1020.0)
    assert event["project_id"]


def test_nome_modelo_e_etapa_e_tags(setup):
    _, _, inbox = setup
    clips.set_config(nome_modelo="Live {data} {hora} {desconhecido}", etapa_inicial="producao", tags="clipe, live")
    clips.scan_once(now=1000.0)
    _write(inbox / "x.mp4", mtime=datetime(2026, 10, 20, 14, 33).timestamp())
    clips.scan_once(now=1002.0)
    [event] = clips.scan_once(now=1010.0)
    project = projects_module.get_project(event["project_id"])
    # Placeholder desconhecido fica como está, em vez de quebrar a criação.
    assert project["name"] == "Live 2026-10-20 14h33 {desconhecido}"
    assert project["stage"] == "producao"
    assert project["tags"] == ["clipe", "live"]


def test_pasta_e_por_sistema(tmp_path, monkeypatch):
    clips.set_config(pasta=str(tmp_path / "mac"))
    monkeypatch.setattr(machine, "current_os", lambda platform=None: machine.WINDOWS)
    assert clips.get_config()["pasta"] == ""
    clips.set_config(pasta=str(tmp_path / "win"))
    cfg = clips.get_config()
    assert cfg["pasta"] == str(tmp_path / "win")
    assert cfg["pasta_mac"] == str(tmp_path / "mac")


def test_config_valida(setup):
    with pytest.raises(clips.ClipError):
        clips.set_config(client_id="nao-existe")
    with pytest.raises(clips.ClipError):
        clips.set_config(etapa_inicial="xpto")
    with pytest.raises(clips.ClipError):
        clips.set_config(pasta="relativa/pasta")
    assert clips.set_config(extensoes="MP4, mkv")["extensoes"] == [".mp4", ".mkv"]
    assert clips.set_config(estavel_segundos=0)["estavel_segundos"] == clips.MIN_STABLE_SECONDS


def test_cliente_sem_pasta_neste_computador_nao_vigia(tmp_path):
    client = clients_module.create_client(name="Só Windows", path_windows="G:\\Twitch")
    inbox = tmp_path / "Gravacoes"
    inbox.mkdir()
    clips.set_config(ativo=True, pasta=str(inbox), client_id=client["id"])
    clips.scan_once(now=1000.0)
    assert "não tem pasta" in clips.status()["motivo"]


def test_api(setup):
    client, _, inbox = setup
    app = create_app()
    http = app.test_client()
    assert http.get("/api/clipes/config").get_json()["client_id"] == client["id"]
    assert http.put("/api/clipes/config", json={"etapa_inicial": "nada"}).status_code == 400
    assert http.put("/api/clipes/config", json={"nome_modelo": "Corte {arquivo}"}).status_code == 200
    # Arquivo velho o bastante: o "verificar" de agora o enfileira, o próximo processa.
    _write(inbox / "live.mp4", mtime=1001.0)
    http.post("/api/clipes/verificar")
    clips.scan_once(now=10**10)
    data = http.get("/api/clipes/eventos?apos=0").get_json()
    [event] = data["eventos"]
    assert data["ultimo"] == event["seq"]
    assert event["projeto_nome"] == "Corte live"
    [listed] = http.get(f"/api/clients/{client['id']}/projects").get_json()
    assert listed["midia_original"]["arquivo"] == "Brutos/live.mp4"
    status = http.get("/api/clipes/status").get_json()
    assert status["recentes"][0]["project_id"] == event["project_id"]
