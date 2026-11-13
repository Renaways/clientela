import pytest

from clientela import acesso, ambiente, atualizacao, clips, conversor, paths, startup, transcricao


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own data/ directory so tests never touch the
    real app data or leak state between each other."""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "CLIENTS_JSON", tmp_path / "clients.json")
    monkeypatch.setattr(paths, "TAGS_JSON", tmp_path / "tags.json")
    monkeypatch.setattr(paths, "COST_SETTINGS_JSON", tmp_path / "cost_settings.json")
    monkeypatch.setattr(paths, "MESSAGE_SETTINGS_JSON", tmp_path / "message_settings.json")
    monkeypatch.setattr(paths, "MONTHLY_LEDGER_JSON", tmp_path / "monthly_ledger.json")
    monkeypatch.setattr(paths, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(paths, "TIMER_ACTIVE_JSON", tmp_path / "timer_active.json")
    monkeypatch.setattr(paths, "MIGRATIONS_JSON", tmp_path / "migrations.json")
    monkeypatch.setattr(paths, "DEPENDENCIES_JSON", tmp_path / "dependencies.json")
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(paths, "EDITOR_DIR", tmp_path / "editor")
    monkeypatch.setattr(paths, "EDITOR_JOBS_DIR", tmp_path / "editor" / "jobs")
    monkeypatch.setattr(paths, "EDITOR_CONFIG_JSON", tmp_path / "editor" / "config.json")
    monkeypatch.setattr(paths, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(paths, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(paths, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(paths, "LOGS_DIR", tmp_path / "logs")
    # Sem isto um teste de "destino: Downloads" escreveria na ~/Downloads de verdade.
    monkeypatch.setattr(paths, "USER_DOWNLOADS_DIR", tmp_path / "Downloads")
    # create_app() dispara a verificação de ambiente numa thread. Nos testes ela
    # rodaria contra o PATH real (e contra o PATH falso dos testes do editor) e
    # o relatório, global do módulo, bloquearia rotas de um teste para o outro.
    monkeypatch.setattr(startup, "start_background_check", lambda: None)
    startup.reset()
    clips.reset()
    ambiente.reset()
    transcricao.reset()
    conversor.reset()
    acesso.reset()   # bloqueios de PIN errado são do módulo
    # nenhum teste pergunta ao GitHub de verdade
    atualizacao.reset()

    def _sem_rede(repo):
        raise OSError("rede desligada nos testes")
    monkeypatch.setattr(atualizacao, "_buscar", _sem_rede)
    # nenhum teste lê (nem instala em) as pastas de fontes do sistema de verdade
    # (não cria a pasta: há teste que exige tmp_path vazio; quem instala cria)
    fontes = tmp_path / "fontes-sistema"
    monkeypatch.setattr(ambiente, "pastas_de_fontes", lambda: [fontes])
    yield tmp_path
    startup.reset()
    clips.reset()


@pytest.fixture
def servidor_de_video(tmp_path):
    """Um MP4 de 1 s servido por HTTP local: o yt-dlp de verdade baixa dele."""
    import subprocess
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    from clientela import downloads

    pytest.importorskip("yt_dlp")
    ffmpeg = downloads.find_tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("sem ffmpeg")
    site = tmp_path / "site"
    site.mkdir()
    subprocess.run([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-c:v", "libx264", "-c:a", "aac",
                    "-shortest", str(site / "clipe.mp4")], check=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(site)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/clipe.mp4"
    httpd.shutdown()
