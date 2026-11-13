"""Transcrição: opções → argumentos do whisper, progresso do tqdm e a fila,
com um "python" falso que finge ser o openai-whisper."""
import json
import sys
import time

import pytest

from app import create_app
from clientela import transcricao

FALSO = r'''#!PY
import json, sys, os, time
a = sys.argv[1:]
if a[0] == "-c":
    print(json.dumps({"versao": "20240930", "torch": "2.x", "idiomas": {"pt": "portuguese"}, "mps": False, "cuda": False}))
    sys.exit(0)
args = a[a.index("whisper") + 1:]
audio = args[0]
out = args[args.index("--output_dir") + 1]
if "QUEBRA" in audio:
    print("Skipping " + audio + " due to RuntimeError: boom"); sys.exit(0)
print("Detected language: Portuguese", flush=True)
for n in (0, 500, 1000):
    sys.stderr.write("\r%3d%%|##| %d/1000 [00:01<00:02, 300frames/s]" % (n // 10, n)); sys.stderr.flush()
stem = os.path.splitext(os.path.basename(audio))[0]
for ext in ("txt", "srt", "vtt", "tsv", "json"):
    open(os.path.join(out, stem + "." + ext), "w").write("ola mundo")
'''


@pytest.fixture
def falso(tmp_path):
    p = tmp_path / "fakepy"
    p.write_text(FALSO.replace("#!PY", "#!" + sys.executable))
    p.chmod(0o755)
    return str(p)


def _esperar(job_id, t=10):
    fim = time.time() + t
    while time.time() < fim:
        j = transcricao.get_status(job_id)
        if j["status"] in transcricao.FINISHED:
            return j
        time.sleep(0.05)
    raise AssertionError("job não terminou")


def test_build_args_traduz_as_opcoes():
    a = transcricao.build_args({"idioma": "pt", "formatos": ["srt"], "word_timestamps": True,
                                "max_line_width": 42, "patience": 2}, "v.mp4", "/o")
    assert a[:3] == ["v.mp4", "--model", "turbo"]
    assert a[a.index("--output_format") + 1] == "srt"
    assert a[a.index("--language") + 1] == "pt"
    assert a[a.index("--max_line_width") + 1] == "42" and "--patience" in a
    assert a[a.index("--fp16") + 1] == "False"  # cpu
    sem = transcricao.build_args({"formatos": ["txt", "srt"], "max_line_width": 42}, "v.mp4", "/o")
    assert "--max_line_width" not in sem and sem[sem.index("--output_format") + 1] == "all"
    with pytest.raises(transcricao.OptionError):
        transcricao.build_args({"formatos": []}, "v", "/o")
    with pytest.raises(transcricao.OptionError):
        transcricao.build_args({"temperatura": 3}, "v", "/o")


def test_progresso_do_tqdm():
    p = transcricao.ler_progresso(" 45%|####    | 4500/10000 [00:10<00:12, 450.00frames/s]")
    assert p["pct"] == 45.0 and p["total_s"] == 100 and p["eta"] == "00:12"
    d = transcricao.ler_progresso(" 10%|#  | 160M/1.51G [00:05<00:40, 30.0MiB/s]")
    assert d["etapa"] == "Baixando o modelo" and 10 <= d["pct"] <= 11
    assert transcricao.ler_progresso("Detected language: Portuguese") is None


def test_job_gera_os_arquivos_sem_apagar_os_antigos(tmp_path, falso):
    v = tmp_path / "bruto.mp4"
    v.write_bytes(b"x")
    (tmp_path / "bruto.srt").write_text("antigo")
    app = create_app().test_client()
    r = app.post("/api/transcricao", json={"arquivos": [str(v)], "opcoes": {"python": falso, "formatos": ["txt", "srt"]}})
    assert r.status_code == 200, r.get_json()
    j = _esperar(r.get_json()["jobs"][0]["id"])
    assert j["status"] == "done" and j["idioma_detectado"] == "Portuguese"
    assert sorted(j["arquivos"]) == ["bruto (2).srt", "bruto.txt"]
    assert (tmp_path / "bruto.srt").read_text() == "antigo"
    assert not (tmp_path / "bruto.vtt").exists()  # só o que foi pedido
    assert app.get(f"/api/transcricao/{j['id']}/texto").get_json()["texto"] == "ola mundo"


def test_falha_do_whisper_vira_erro(tmp_path, falso):
    v = tmp_path / "QUEBRA.wav"
    v.write_bytes(b"x")
    j = _esperar(transcricao.start(str(v), {"python": falso})["id"])
    assert j["status"] == "error" and "boom" in j["error"]


def test_recusa_arquivo_que_nao_e_midia(tmp_path, falso):
    t = tmp_path / "nota.pdf"
    t.write_bytes(b"x")
    r = create_app().test_client().post("/api/transcricao", json={"arquivos": [str(t)], "opcoes": {"python": falso}})
    assert r.status_code == 400
