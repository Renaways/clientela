"""O kit da skill (estagiario/skill/estagiario-editor/templates/kit) no que é
puro: alinhar a transcrição do Premiere ao ASR do bruto e quebrar a legenda
em blocos com destaque. O pipeline inteiro (whisper + hyperframes) foi
validado à mão no 049; aqui fica o que pode quebrar sem ninguém ver."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

from clientela import editor

KIT = editor.KIT_DIR


def _preparar():
    spec = importlib.util.spec_from_file_location("preparar_kit", KIT / "preparar.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _w(texto, t):
    return {"texto": texto, "t": t, "fim": round(t + 0.3, 3)}


def test_srt_e_txt_do_premiere_viram_blocos(tmp_path):
    p = _preparar()
    srt = tmp_path / "Base.srt"
    srt.write_text("﻿1\n00:00:00,033 --> 00:00:01,366\n<font color=#FFAE16FF>O debate da Band entre</font>\n\n"
                   "2\n00:00:01,366 --> 00:00:02,633\nLula e Flávio\n", encoding="utf-8")
    assert p.cues_srt(srt) == [(0.033, 1.366, "O debate da Band entre"), (1.366, 2.633, "Lula e Flávio")]
    txt = tmp_path / "IMG.txt"
    txt.write_text("00:00:00:01 - 00:00:11:14\nSpeaker 1\nO debate da Band.\n\n00:00:11:16 - 00:00:23:06\nSpeaker 1\nO primeiro.\n")
    cues = p.from_premiere_txt(txt)
    assert [c[2] for c in cues] == ["O debate da Band.", "O primeiro."] and round(cues[1][0], 2) == 11.53


def test_alinhar_corrige_o_asr_e_mantem_os_tempos():
    p = _preparar()
    asr = [_w("O", 0.1), _w("debate", 0.2), _w("da", 0.6), _w("Bundy", 0.7), _w("trouxe", 0.9), _w("um", 1.0),
           _w("mal-estar.", 1.2), _w("Eaqui", 2.0), _w("entra", 2.3), _w("o", 2.6), _w("problema", 2.7)]
    out, corr = p.alinhar(asr, "O debate da Band trouxe um mal estar. E aqui entra o problema", confiavel=True)
    assert [w["texto"] for w in out] == ["O", "debate", "da", "Band", "trouxe", "um", "mal-estar.", "E", "aqui",
                                         "entra", "o", "problema"]
    assert out[3]["t"] == 0.7  # o tempo continua sendo o do bruto
    assert {c["asr"] for c in corr} == {"Bundy", "Eaqui"}  # o hífen do ASR fica
    # roteiro comum (pode ter frase não dita): só troca o que é parecido
    out2, _ = p.alinhar(asr, "O debate da Band trouxe um mal estar. Frase que ninguém disse", confiavel=False)
    assert "ninguém" not in " ".join(w["texto"] for w in out2)
    # deriva: o bloco do Premiere em 0,0 s está em 0,1 s no bruto
    assert p.deriva(out, [(0.0, 1.0, "O debate da")]) == [pytest.approx(0.1)]


@pytest.mark.skipif(not shutil.which("python3"), reason="precisa do python3")
def test_blocos_da_legenda_nao_acabam_em_preposicao_e_destacam_o_sentido(tmp_path):
    trab = tmp_path / "legendas v1"
    shutil.copytree(KIT, trab / "kit")
    fala = "O debate da Band entre Lula e Flávio deixou uma pergunta importante. Cada candidato teve 15 minutos para falar necessariamente de dados"
    palavras = [_w(t, round(0.1 + i * 0.32, 3)) for i, t in enumerate(fala.split())]
    (trab / "palavras.json").write_text(json.dumps({"palavras": palavras}))
    (trab / "job.json").write_text(json.dumps({"video": {"largura": 1080, "altura": 1920, "duracao": 20}}))
    (trab / "marca.json").write_text(json.dumps({"cores": {"fundo": "#000", "texto": "#fff"}}))
    r = subprocess.run([sys.executable, str(trab / "kit" / "legendas.py"), "blocos"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    blocos = json.loads((trab / "legendas.json").read_text())
    textos = [[w["texto"] for w in b["palavras"]] for b in blocos]
    assert all(b[-1].lower() not in ("de", "da", "e", "o", "para") for b in textos[:-1]), textos
    destaques = {b["palavras"][b["destaque"]]["texto"].strip(".,") for b in blocos if b["destaque"] is not None}
    assert "15" in destaques and "necessariamente" not in destaques
    assert not destaques & {"O", "da", "e", "uma", "teve"}


def test_frase_que_o_asr_nao_ouviu_no_bruto_nao_entra_na_legenda():
    p = _preparar()
    asr = [_w("o", 0.1), _w("foco", 0.4), _w("embora", 3.0), _w("houvesse", 3.3)]
    out, corr = p.alinhar(asr, "o foco Mas vamos lá. embora houvesse", confiavel=True)
    assert [w["texto"] for w in out] == ["o", "foco", "embora", "houvesse"]
    assert "não entrou" in corr[0]["texto"]
    out, _ = p.alinhar([_w("uso", 0.1), _w("dados", 0.5)], "o uso de dados", confiavel=True)
    assert [w["texto"] for w in out] == ["o", "uso", "de", "dados"]  # palavrinha pulada entra


def test_rodar_do_kit_nao_passa_o_stdin_do_job(tmp_path, monkeypatch):
    """O stdin do auto.py é o .prompt.txt do app; um ffmpeg neto que o herdasse
    leria o "c" de "(modo automático" como tecla de comando e travaria."""
    spec = importlib.util.spec_from_file_location("auto_kit", KIT / "auto.py")
    auto = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auto)
    monkeypatch.setattr(auto, "TRAB", tmp_path)
    prompt = tmp_path / "job.prompt.txt"
    prompt.write_text("(modo automático, sem IA) Legendas")
    # o descritor 0 de verdade, que é o que o filho herda (sys.stdin não basta)
    salvo = os.dup(0)
    try:
        with open(prompt) as f:
            os.dup2(f.fileno(), 0)
        linhas = auto.rodar("lê o stdin", [sys.executable, "-c", "import sys; print('aviso: lido=' + repr(sys.stdin.read()))"])
    finally:
        os.dup2(salvo, 0)
        os.close(salvo)
    assert "aviso: lido=''" in linhas
