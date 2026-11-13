"""Conversor de entrega: argumentos do ffmpeg por preset, nomes livres, progresso e o job com ffmpeg falso."""
import sys
import time
from pathlib import Path

import pytest

from app import create_app
from clientela import clients as clients_module, conversor, paths
from clientela import projects as projects_module

FFPROBE = '''#!PY
print('{"streams": [{"width": 1920, "height": 1080}], "format": {"duration": "10.0"}}')
'''
# Escreve o progresso como o ffmpeg e cria a saída (último argumento). LENTO=1: demora para dar tempo de cancelar.
FFMPEG = '''#!PY
import os, sys, time
saida = sys.argv[-1]
if os.path.exists(saida):
    sys.exit("File exists")
open(saida, "wb").write(b"parcial")
for i in range(1, 11):
    print("out_time_us=%d" % (i * 1000000), flush=True)
    print("progress=continue", flush=True)
    time.sleep(0.3 if os.environ.get("LENTO") else 0)
open(saida, "wb").write(b"x" * 1000)
print("progress=end", flush=True)
'''


@pytest.fixture
def falsos(tmp_path, monkeypatch):
    bins = {}
    for nome, corpo in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        p = tmp_path / f"fake-{nome}"
        p.write_text(corpo.replace("#!PY", "#!" + sys.executable))
        p.chmod(0o755)
        bins[nome] = str(p)
    monkeypatch.setattr(conversor, "_bin", bins.get)
    return bins


def _esperar(job_id, t=10):
    fim = time.time() + t
    while time.time() < fim:
        j = conversor.get_status(job_id)
        if j["status"] in conversor.FINISHED:
            return j
        time.sleep(0.05)
    raise AssertionError("job não terminou")


def _v(args, flag):
    return args[args.index(flag) + 1]


def test_build_args_de_cada_preset():
    info = {"largura": 1920, "altura": 1080}
    cli = conversor.build_args("cliente", "/v/a.mov", "/v/a - Cliente.mp4")
    assert _v(cli, "-crf") == "20" and _v(cli, "-c:v") == "libx264" and _v(cli, "-b:a") == "192k"
    assert _v(cli, "-movflags") == "+faststart" and _v(cli, "-pix_fmt") == "yuv420p"
    assert "-n" in cli and "-y" not in cli and cli[-1] == "/v/a - Cliente.mp4"
    wa = conversor.build_args("whatsapp", "/v/a.mov", "/v/b.mp4")
    assert _v(wa, "-crf") == "26" and _v(wa, "-profile:v") == "main" and "1280" in _v(wa, "-vf")
    rec = conversor.build_args("reenquadrar", "/v/a.mov", "/v/c.mp4", {"formato": "9x16", "posicao": 0}, info)
    fc = _v(rec, "-filter_complex")
    assert "crop=606:1080:0:0" in fc and "scale=1080:1920" in fc  # 9:16 da altura toda, encostado à esquerda
    dir_ = _v(conversor.build_args("reenquadrar", "/v/a.mov", "/v/c.mp4", {"posicao": 1}, info), "-filter_complex")
    assert "crop=606:1080:1313:0" in dir_  # _caixa trunca: 1 px antes da borda (1314 = encostado)
    blur = _v(conversor.build_args("reenquadrar", "/v/a.mov", "/v/c.mp4", {"formato": "1x1", "modo": "desfocado"},
                                   info), "-filter_complex")
    assert "boxblur" in blur and "1080:1080" in blur
    mp3 = conversor.build_args("audio", "/v/a.mov", "/v/a.mp3")
    assert "-vn" in mp3 and _v(mp3, "-b:a") == "320k"
    wav = conversor.build_args("audio", "/v/a.mov", "/v/a.wav", {"audio": "wav"})
    assert _v(wav, "-ar") == "48000" and _v(wav, "-c:a") == "pcm_s16le"
    with pytest.raises(conversor.OptionError):
        conversor.build_args("cliente", "/v/a.mp4", "/v/a.mp4")  # saída = entrada
    with pytest.raises(conversor.OptionError):
        conversor.build_args("prores", "/v/a.mp4", "/v/b.mp4")


def test_nomes_de_saida():
    o = conversor.normalizar("reenquadrar", {"formato": "1x1"})
    assert conversor.nome_saida("/x/final.mov", "reenquadrar", o) == "final - 1x1.mp4"
    assert conversor.nome_saida("/x/final.mov", "whatsapp", o) == "final - WhatsApp.mp4"
    assert conversor.nome_saida("/x/final.mov", "audio", conversor.normalizar("audio", {"audio": "wav"})) == "final - Áudio.wav"


def test_ler_progresso():
    assert conversor.ler_progresso("out_time_us=5000000\n", 10.0) == 50.0
    assert conversor.ler_progresso("out_time_ms=2500000", 10.0) == 25.0
    assert conversor.ler_progresso("out_time_us=N/A", 10.0) is None
    assert conversor.ler_progresso("progress=end", None) == 100.0
    assert conversor.ler_progresso("frame=12", 10.0) is None


def test_job_completo_com_nome_livre(tmp_path, falsos):
    src = tmp_path / "final.mp4"
    src.write_bytes(b"origem")
    (tmp_path / "final - WhatsApp.mp4").write_bytes(b"antigo")
    j = _esperar(conversor.start(str(src), "whatsapp")["id"])
    assert j["status"] == "done", j
    assert j["nome"] == "final - WhatsApp (2).mp4" and j["tamanho"] == 1000 and j["pct"] == 100.0
    assert (tmp_path / "final - WhatsApp.mp4").read_bytes() == b"antigo"
    assert src.read_bytes() == b"origem"
    j2 = _esperar(conversor.start(str(src), "cliente", destino="downloads")["id"])
    assert Path(j2["saida"]).parent == paths.USER_DOWNLOADS_DIR


def test_cancelar_apaga_so_o_parcial(tmp_path, falsos, monkeypatch):
    monkeypatch.setenv("LENTO", "1")
    src = tmp_path / "final.mp4"
    src.write_bytes(b"origem")
    job = conversor.start(str(src), "cliente")
    fim = time.time() + 5
    while not (conversor.get_status(job["id"])["pct"] > 0) and time.time() < fim:
        time.sleep(0.05)
    assert conversor.cancel(job["id"])
    j = _esperar(job["id"])
    time.sleep(0.2)
    assert j["status"] == "cancelled" and not Path(j["saida"]).exists()
    assert src.read_bytes() == b"origem" and sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".mp4") == ["final.mp4"]


def test_rota_resolve_o_video_do_projeto_e_409_sem_pasta(tmp_path, falsos):
    d = tmp_path / "Cliente"
    d.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(d))
    project = projects_module.create_project(client["id"], "Teste")
    (Path(project["path"]) / "final.mp4").write_bytes(b"origem")
    c = create_app().test_client()
    r = c.post("/api/conversor", json={"project_id": project["id"], "arquivo": "final.mp4", "preset": "cliente"})
    assert r.status_code == 200, r.json
    assert _esperar(r.json["job"]["id"])["status"] == "done"
    assert c.post("/api/conversor", json={"project_id": project["id"], "arquivo": "../x.mp4",
                                          "preset": "cliente"}).status_code == 400
    import shutil
    shutil.rmtree(project["path"])
    r = c.post("/api/conversor", json={"project_id": project["id"], "arquivo": "final.mp4", "preset": "cliente"})
    assert r.status_code == 409
