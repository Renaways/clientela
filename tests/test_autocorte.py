"""Corte vertical automático (autocorte.py): as peças puras do pipeline, uma
rodada de verdade com ffmpeg (vídeo de teste, duas faixas de áudio, LLM falso)
e a ligação com os jobs do editor e com o vigia de clipes."""

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clientela import autocorte as ac
from clientela import clients as clients_module
from clientela import clips, editor, paths
from clientela import projects as projects_module


def _w(texto, t, fim, loc=None):
    w = {"texto": texto, "t": t, "fim": fim}
    if loc:
        w["locutor"] = loc
    return w


needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="sem ffmpeg")

# Palavras de 0,4 s a cada 0,5 s: "p0" em 0,5 s … "p8" em 4,5 s.
WORDS = [_w(f"p{i}", round(0.5 + i * 0.5, 3), round(0.9 + i * 0.5, 3)) for i in range(9)]


# ------------------------------------------------------------------ transcrição
def test_palavras_de_json_aceita_hyperframes_e_whisper():
    hf = [{"text": " olá ", "start": 0.2, "end": 0.5}, {"text": "", "start": 1, "end": 2}]
    assert ac.palavras_de_json(hf) == [{"texto": "olá", "t": 0.2, "fim": 0.5}]
    whisper = {"segments": [{"words": [{"word": "a", "start": 1.0, "end": 1.2}, {"text": "b", "start": 0.1, "duration": 0.3}]}]}
    assert [(w["texto"], w["t"], w["fim"]) for w in ac.palavras_de_json(whisper)] == [("b", 0.1, 0.4), ("a", 1.0, 1.2)]


def test_pedacos_de_10_s_com_folga():
    assert ac.pedacos(25) == [(0.0, 11.5, 0, 10), (8.5, 21.5, 10, 20), (18.5, 25, 20, 25)]


def test_juntar_pedacos_vizinho_cobre_o_que_o_dono_nao_ouviu():
    resultados = [
        # pedaço 0 (miolo 0–10): só a letra da música no começo; perdeu a fala de 8,2 s
        (0.0, 0, 10, [(0.0, 0.5, "pra"), (0.5, 1.0, "matar")]),
        # pedaço 1 (8,5–21,5, miolo 10–20): ouviu a fala de 8,2 s na folga, e "Ué" no miolo
        (8.5, 10, 20, [(0.0, 0.4, "Acho"), (0.45, 0.8, "que"), (0.85, 1.2, "não"), (2.5, 2.8, "Ué")]),
        # pedaço 2: a folga repete "Ué", que o dono (pedaço 1) já tem
        (18.5, 20, 30, [(-7.5 + 0.0, -7.5 + 0.3, "Ué"), (3.0, 3.4, "bora")]),
    ]
    words = ac.juntar_pedacos(resultados)
    assert [w["texto"] for w in words] == ["pra", "matar", "Acho", "que", "não", "Ué", "bora"]
    assert words[2]["t"] == 8.5 and words[-1]["t"] == 21.5


def test_palavras_do_whisper_cli_sem_marcacao_e_com_fim_limitado():
    d = {"transcription": [
        {"text": " [Música]", "offsets": {"from": 0, "to": 900}},
        {"text": " ele", "offsets": {"from": 1000, "to": 6700}},   # o fim estica até a próxima fala
        {"text": " ", "offsets": {"from": 6700, "to": 6700}},
        {"text": " pulou", "offsets": {"from": 6800, "to": 7100}},
    ]}
    assert ac.palavras_whisper_cli(d) == [(1.0, 2.2, "ele"), (6.8, 7.1, "pulou")]


FAKE_WHISPER = r"""import json, os, sys
a = sys.argv[1:]
json.dump(a, open(os.environ["FAKE_WHISPER_OUT"], "w"))
arquivos = [a[i + 1] for i, x in enumerate(a) if x == "-f"]
for k, f in enumerate(arquivos):
    segs = [{"text": " oi" if k == 0 else " bora", "offsets": {"from": 2000, "to": 2400}}]
    json.dump({"transcription": segs}, open(f + ".json", "w"))
"""


@needs_ffmpeg
def test_transcricao_em_pedacos_numa_chamada_so_sem_contexto(tmp_path, monkeypatch):
    script = tmp_path / "fake_whisper.py"
    script.write_text(FAKE_WHISPER)
    cli = tmp_path / "whisper-cli"
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "hf"
    (home / "models").mkdir(parents=True)
    (home / "models" / "ggml-small.bin").write_bytes(b"modelo")
    monkeypatch.setattr(ac, "WHISPER_HOME", home)
    monkeypatch.setenv("HYPERFRAMES_WHISPER_PATH", str(cli))
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_WHISPER_OUT", str(out))
    audio = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=15", "-ar", "16000", str(audio)], check=True)
    trab = tmp_path / "b"
    trab.mkdir()
    words = ac.transcrever(audio, trab, "small", "pt", 15.0)
    assert [(w["texto"], w["t"]) for w in words] == [("oi", 2.0), ("bora", 10.5)]
    argv = json.loads(out.read_text())
    assert argv[argv.index("-mc") + 1] == "0" and argv[argv.index("-l") + 1] == "pt"
    assert argv.count("-f") == 2 and argv[argv.index("-m") + 1].endswith("ggml-small.bin")
    out.unlink()
    assert ac.transcrever(audio, trab, "small", "pt", 15.0) == words  # cache da bancada: não roda de novo
    assert not out.exists()


def test_frase_tipica_de_alucinacao_fica_duvidosa():
    words = ac.marcar_duvidosas([_w("Legendas", 1, 1.4), _w("pela", 1.45, 1.6), _w("comunidade", 1.65, 2.0),
                                 _w("Amara.org", 2.05, 2.5), _w("bora", 5, 5.3)])
    assert [w["duvidosa"] for w in words] == [True, True, True, True, False]


# ------------------------------------------------------------------ trechos
def test_trechos_encaixam_nas_palavras_com_respiro_sem_invadir_a_vizinha():
    raw = {"segmentos": [{"inicio": 0.42, "fim": 2.3, "motivo": "gancho"}]}
    segs = ac.normalizar_segmentos(raw, WORDS, duracao=10)
    # começo encaixa em p0 (0,5) e ganha 0,12 de respiro; o fim encaixa no fim
    # de p3 (2,4) e o respiro para no começo de p4 (2,5)
    assert segs == [{"inicio": 0.38, "fim": 2.5, "motivo": "gancho"}]


def test_trechos_fora_de_ordem_sobrepostos_e_lixo():
    raw = {"segmentos": [
        {"inicio": 3.0, "fim": 4.4},
        {"inicio": 0.5, "fim": 3.1},      # encosta no de cima: viram um só
        {"inicio": "x", "fim": 2},        # lixo
        {"inicio": 9.0, "fim": 9.1},      # curto demais
        {"inicio": -5, "fim": 0.2},       # antes da primeira palavra
    ]}
    segs = ac.normalizar_segmentos(raw, WORDS, duracao=10)
    assert len(segs) == 1
    assert segs[0]["inicio"] == pytest.approx(0.38) and segs[0]["fim"] == pytest.approx(4.5)  # o respiro para no começo de p8


def test_sem_trechos_aproveitaveis():
    assert ac.normalizar_segmentos({"segmentos": []}, WORDS, 10) == []
    assert ac.normalizar_segmentos(None, WORDS, 10) == []


def test_remapear_leva_as_palavras_para_o_tempo_do_corte():
    segs = [{"inicio": 0.4, "fim": 1.5}, {"inicio": 3.4, "fim": 4.5}]
    out = ac.remapear(WORDS, segs)
    assert [w["texto"] for w in out] == ["p0", "p1", "p6", "p7"]
    assert out[0]["t"] == pytest.approx(0.1)
    # p6 começa em 3,5 no bruto = 1,1 (primeiro trecho) + 0,1
    assert out[2]["t"] == pytest.approx(1.2) and out[2]["fim"] == pytest.approx(1.6)
    assert ac.duracao_total(segs) == pytest.approx(2.2)


# ------------------------------------------------------------------ locutores
def test_locutor_por_turnos_e_nomes_pela_ordem_de_fala():
    words = [_w("a", 0, 1), _w("b", 1.1, 2), _w("c", 5, 6), _w("d", 3.05, 3.1)]
    turnos = [(0, 1.05, "SPEAKER_01"), (1.05, 3, "SPEAKER_00"), (4.5, 7, "SPEAKER_01")]
    ac.nomear_locutores(ac.atribuir_por_turnos(words, turnos))
    assert [w["locutor"] for w in words] == ["Locutor 1", "Locutor 2", "Locutor 1", "Locutor 2"]


def test_locutor_pela_faixa_mais_alta():
    words = [_w("a", 0, 1), _w("b", 1, 2)]
    ac.por_energia(words, {"faixa2": [900, 10], "faixa3": [5, 700]})
    ac.nomear_locutores(words, {"faixa2": "Bruno", "faixa3": "Convidado"})
    assert [w["locutor"] for w in words] == ["Bruno", "Convidado"]


# ------------------------------------------------------------------ legendas
def test_blocos_quebram_por_locutor_pontuacao_e_tamanho():
    words = [_w("Oi,", 0, 0.3, "A"), _w("tudo", 0.35, 0.6, "A"), _w("bem", 0.65, 0.9, "A"),
             _w("com", 0.95, 1.1, "A"), _w("você", 1.15, 1.4, "A"), _w("hoje", 1.45, 1.7, "A"),
             _w("sim", 1.8, 2.0, "B")]
    cues = ac.blocos(words, max_palavras=3)
    # "com" não fica pendurado no fim da tela: desce com "você hoje"
    assert [[w["texto"] for w in c["palavras"]] for c in cues] == [["Oi,"], ["tudo", "bem"], ["com", "você", "hoje"], ["sim"]]
    assert [c["locutor"] for c in cues] == ["A", "A", "A", "B"]
    # "Oi," dura 0,3 s: fica na tela até o próximo bloco entrar
    assert cues[0]["fim"] == pytest.approx(0.35)


def test_bloco_nao_pisca_no_vao_curto():
    # o whisper deixa 0,18 s entre "Gente," e "vocês": o bloco fica até o próximo entrar
    cues = ac.blocos([_w("Gente,", 0.0, 0.48), _w("vocês", 0.69, 1.0), _w("depois", 3.0, 3.4)])
    assert [c["fim"] for c in cues] == [0.69, 1.3, 3.7]


def test_ass_karaoke_um_estilo_por_locutor():
    cues = ac.blocos([_w("bom", 1.0, 1.3, "Bruno"), _w("dia", 1.35, 1.7, "Bruno"), _w("oi", 2.5, 2.8, "Convidado")])
    ass = ac.gerar_ass(cues, ["Bruno", "Convidado"], {"legenda_maiusculas": True})
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Style: Bruno,Arial Black,84,&H000AD6FF," in ass        # #FFD60A em &HAABBGGRR
    assert "Style: Convidado,Arial Black,84,&H00F0C94C," in ass   # #4CC9F0
    dialogos = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert dialogos[0].startswith("Dialogue: 0,0:00:01.00,0:00:02.00,Bruno,Bruno,")
    assert "{\\k35}BOM {\\k35}DIA" in dialogos[0]
    assert ",Convidado,Convidado," in dialogos[1]


def test_srt_geral_com_locutor_e_tempo():
    cues = ac.blocos([_w("bom", 61.0, 61.3, "Bruno"), _w("dia.", 61.35, 61.7, "Bruno")])
    srt = ac.gerar_srt(cues, com_locutor=True)
    assert srt.startswith("1\n00:01:01,000 --> 00:01:02,000\n[Bruno] bom dia.\n")


def test_token_do_hugging_face_vem_do_ambiente(monkeypatch):
    # o app tira o token do auto-args.json (fica no Drive) e manda por HF_TOKEN
    usado = {}

    def fake_pyannote(audio, token, modelo):
        usado["token"] = token
        return [(0, 10, "SPEAKER_00")]

    monkeypatch.setattr(ac, "diarizar_pyannote", fake_pyannote)
    monkeypatch.setenv("HF_TOKEN", "hf_env")
    words = [_w("oi", 1, 2)]
    _, origem, _ = ac.locutores(words, "v.mp4", "a.wav", {"audios": [{"faixa": 1}]}, dict(ac.DEFAULTS))
    assert usado["token"] == "hf_env" and origem == "pyannote"
    monkeypatch.delenv("HF_TOKEN")
    usado.clear()
    _, origem, _ = ac.locutores([_w("oi", 1, 2)], "v.mp4", "a.wav", {"audios": [{"faixa": 1}]}, dict(ac.DEFAULTS))
    assert usado == {} and origem == "um locutor"


def test_entrega_so_aparece_inteira(tmp_path, monkeypatch):
    final = tmp_path / "Corte 9x16 v1"

    def quebra_no_meio(a, entrega, nome):
        entrega.mkdir()
        (entrega / "Corte 9x16.mp4").write_bytes(b"meio mp4")
        raise ac.Falha("ffmpeg falhou")

    monkeypatch.setattr(ac, "_executar", quebra_no_meio)
    with pytest.raises(ac.Falha):
        ac.executar({"entrega": str(final)})
    assert list(tmp_path.iterdir()) == []  # nem a entrega nem a pasta oculta de montagem

    def ok(a, entrega, nome):
        entrega.mkdir()
        (entrega / "Corte 9x16.mp4").write_bytes(b"mp4")
        return f"pronto em {nome}", 0.0

    monkeypatch.setattr(ac, "_executar", ok)
    assert ac.executar({"entrega": str(final)})[0] == "pronto em Corte 9x16 v1"
    assert [p.name for p in tmp_path.iterdir()] == ["Corte 9x16 v1"]
    with pytest.raises(ac.Falha, match="já existe"):
        ac.executar({"entrega": str(final)})


def test_filhos_nao_herdam_o_stdin_do_job(tmp_path, monkeypatch):
    """O stdin do job é o .prompt.txt, que começa com "(corte". Para o ffmpeg "c" é a tecla
    de comando de filtro: ele para em "Enter command:" e gira a 100% de CPU para sempre
    (aconteceu no app, num clipe de 23 s). O clipe dos testes é curto demais para o ffmpeg
    chegar a ler o teclado, então a garantia é testada aqui, no `rodar`."""
    prompt = tmp_path / "job.prompt.txt"
    prompt.write_text("(corte vertical: whisper → LLM → ffmpeg) clip.mp4")
    salvo = os.dup(0)  # o descritor 0 de verdade, que é o que o filho herda
    try:
        with open(prompt) as f:
            os.dup2(f.fileno(), 0)
        r = ac.rodar([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], text=True)
    finally:
        os.dup2(salvo, 0)
        os.close(salvo)
    assert r.stdout.strip() == "''"
    chamadas = []
    monkeypatch.setattr(ac.subprocess, "run", lambda cmd, **kw: chamadas.append(cmd) or subprocess.CompletedProcess(cmd, 0, b"", b""))
    ac.rodar(["ffmpeg", "-i", "x"])
    ac.rodar(["ffprobe", "-i", "x"])
    assert chamadas == [["ffmpeg", "-nostdin", "-i", "x"], ["ffprobe", "-i", "x"]]


# ------------------------------------------------------------------ ffmpeg
def test_cadeia_recorta_9x16_no_enquadramento():
    assert ac.cadeia_video(1920, 1080, "corte", 0.5).startswith("crop=606:1080:657:0,scale=1080:1920")
    assert ac.cadeia_video(1920, 1080, "corte", 0).startswith("crop=606:1080:0:0,")
    assert ac.cadeia_video(1920, 1080, "corte", 1).startswith("crop=606:1080:1314:0,")
    # já mais alto que 9:16: corta em cima e embaixo
    assert ac.cadeia_video(1080, 2400, "corte").startswith("crop=1080:1920:0:240,")
    assert "boxblur" in ac.cadeia_video(1920, 1080, "desfocado")
    assert ac.cadeia_video(1920, 1080).endswith("fps=30,format=yuv420p")


def test_grafos_emendam_os_trechos():
    segs = [{"inicio": 1.0, "fim": 2.0}, {"inicio": 5.0, "fim": 6.5}]
    g = ac.grafo_corte(segs, "CADEIA")
    assert "[0:v:0]trim=start=1.0:end=2.0" in g and "[0:a:0]atrim=start=5.0:end=6.5" in g
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vc][aout]" in g and g.endswith("[vc]CADEIA[vout]")
    ga = ac.grafo_audio(segs, 3)
    assert "[0:a:2]atrim" in ga and ga.endswith("concat=n=2:v=0:a=1[aout]")
    iso = ac.grafo_audio(segs, 1, [(0.0, 0.5), (1.2, 2.0)])
    assert iso.endswith("[ac]volume=volume='if(between(t,0.000,0.500)+between(t,1.200,2.000),1,0)':eval=frame[aout]")


def test_regioes_do_locutor_juntam_blocos_vizinhos():
    cues = [{"t": 0.0, "fim": 1.0, "locutor": "A"}, {"t": 1.1, "fim": 2.0, "locutor": "A"},
            {"t": 2.1, "fim": 3.0, "locutor": "B"}, {"t": 5.0, "fim": 6.0, "locutor": "A"}]
    assert ac.regioes_locutor(cues, "A") == [(0.0, 2.12), (4.88, 6.12)]


# ------------------------------------------------------------------ LLM
def test_extrair_json_tolera_cerca_e_texto():
    assert ac.extrair_json('Aqui:\n```json\n{"segmentos": []}\n```') == {"segmentos": []}
    with pytest.raises(ac.Falha):
        ac.extrair_json("sem json")


FAKE_LLM = r'''import json, os, sys
prompt = sys.stdin.read()
json.dump({"argv": sys.argv[1:], "prompt": prompt}, open(os.environ["FAKE_LLM_OUT"], "w"))
modo = os.environ.get("FAKE_LLM_MODE", "ok")
if modo == "auth":
    print(json.dumps({"type": "result", "is_error": True, "result": "Failed to authenticate. Run /login"}))
else:
    print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.02,
                      "result": "", "structured_output": json.loads(os.environ["FAKE_LLM_RESPOSTA"])}))
'''


@pytest.fixture
def fake_llm(tmp_path, monkeypatch):
    script = tmp_path / "fake_llm.py"
    script.write_text(FAKE_LLM)
    binary = tmp_path / "claude"
    # wrapper em shell: o caminho do venv tem espaço e um shebang com espaço não executa
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "llm.json"
    monkeypatch.setenv("FAKE_LLM_OUT", str(out))
    monkeypatch.setenv("FAKE_LLM_RESPOSTA", json.dumps({"titulo": "T", "segmentos": [{"inicio": 1, "fim": 2}]}))
    return binary, out


def test_llm_claude_manda_as_folhas_dentro_da_mensagem_sem_ferramentas(fake_llm, tmp_path):
    binary, out = fake_llm
    folha = tmp_path / "folha-01.jpg"
    folha.write_bytes(b"\xff\xd8jpeg")
    resposta, _ = ac.llm_claude("PROMPT", {}, str(binary), imagens=[folha], cwd=tmp_path)
    assert resposta["titulo"] == "T" and (tmp_path / "llm-uso.json").exists()
    d = json.loads(out.read_text())
    argv = d["argv"]
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--input-format") + 1] == "stream-json"
    conteudo = json.loads(d["prompt"])["message"]["content"]
    assert conteudo[0] == {"type": "text", "text": "PROMPT"}
    assert conteudo[1]["type"] == "image" and conteudo[1]["source"]["media_type"] == "image/jpeg"


def test_llm_claude_sem_ferramentas_com_schema(fake_llm, monkeypatch):
    binary, out = fake_llm
    resposta, custo = ac.llm_claude("PROMPT", {"modelo_llm": "sonnet"}, str(binary))
    assert resposta["titulo"] == "T" and custo == 0.02
    argv = json.loads(out.read_text())["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == ["segmentos"]
    monkeypatch.setenv("FAKE_LLM_MODE", "auth")
    with pytest.raises(ac.Falha, match="não está logado"):
        ac.llm_claude("PROMPT", {}, str(binary))


def test_openrouter_sem_chave_ou_modelo(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ac.Falha, match="chave"):
        ac.llm_openrouter("x", {"openrouter_modelo": "m"})
    with pytest.raises(ac.Falha, match="modelo"):
        ac.llm_openrouter("x", {"openrouter_api_key": "k"})


def test_prompt_leva_frases_com_tempo_locutor_e_pedido():
    words = [_w("Olha", 1.0, 1.2, "Bruno"), _w("isso.", 1.25, 1.5, "Bruno"), _w("Nossa", 2.5, 2.8, "Ana")]
    p = ac.montar_prompt(words, 30.0, {"duracao_min": 15, "duracao_max": 40}, "só a parte do boss")
    assert "[1.00–1.50] Bruno: Olha isso." in p and "[2.50–2.80] Ana: Nossa" in p
    assert "entre 15 e 40 s" in p and "só a parte do boss" in p and "30.0 s" in p


def test_prompt_mostra_folhas_linha_do_tempo_e_fala_duvidosa():
    words = ac.marcar_duvidosas([_w("Epra", 30.0, 35.0), _w("matar", 35.0, 41.4)])
    midia = {"passo": 2, "folhas": [{"arquivo": "folhas/folha-01.jpg", "tempos": [0, 2, 4]}],
             "linha_do_tempo": [{"t": 0, "fim": 2, "volume_db": -21.4, "movimento": 3.25, "cena": True},
                                {"t": 2, "fim": 4, "volume_db": None, "movimento": None, "cena": False}]}
    p = ac.montar_prompt(words, 66.0, {}, "", midia, {"largura": 2560, "altura": 1080})
    assert "folhas/folha-01.jpg: quadros em 0, 2, 4 s" in p and "anexadas" in p
    assert "[0–2] volume -21 dB · movimento 3.2 · cena" in p and "[2–4] volume silêncio · movimento —" in p
    assert "Locutor 1 (duvidosa): Epra matar" in p and "2560x1080" in p
    assert "(sem fala)" in ac.montar_prompt([], 66.0, {}, "", midia)


# ------------------------------------------------------------------ fala duvidosa e corte longe da fala
def test_whisper_em_cima_de_musica_fica_duvidoso():
    musica = [_w("Epra", 30.0, 33.0), _w("matar", 33.0, 36.0), _w("pro", 36.0, 41.4)]   # 3 palavras em 11 s
    refrao = [_w(t, 50 + i * 0.3 + k * 5, 50.25 + i * 0.3 + k * 5) for k in range(3)
              for i, t in enumerate(["meu", "tudo", "ou", "nada,"])]                    # o mesmo refrão 3 vezes
    fala = [_w("olha", 70.0, 70.3), _w("isso", 70.35, 70.6), _w("aqui", 70.7, 71.0)]
    words = ac.marcar_duvidosas(musica + refrao + fala)
    assert all(w["duvidosa"] for w in musica + refrao)
    assert not any(w["duvidosa"] for w in fala)


def test_corte_longe_da_fala_fica_onde_o_llm_pediu():
    # o lobby vai de 0 a 20 s, a primeira fala é em 30 s: o corte em 20 s não pode ir parar em 30 s
    words = [_w("bora", 30.0, 30.4), _w("lá", 30.45, 30.8)]
    segs = ac.normalizar_segmentos({"segmentos": [{"inicio": 20.0, "fim": 27.0}]}, words, duracao=66)
    assert segs == [{"inicio": 19.88, "fim": 27.25, "motivo": ""}]
    # dentro de uma palavra, vai para a borda dela; perto (até 0,4 s), encaixa
    segs = ac.normalizar_segmentos({"segmentos": [{"inicio": 30.2, "fim": 31.1}]}, words, duracao=66)
    assert segs == [{"inicio": 29.88, "fim": 31.05, "motivo": ""}]


def test_palavra_duvidosa_nao_segura_o_corte():
    words = ac.marcar_duvidosas([_w("Epra", 30.02, 41.4)])
    segs = ac.normalizar_segmentos({"segmentos": [{"inicio": 34.0, "fim": 40.0}]}, words, duracao=66)
    assert segs[0]["inicio"] == pytest.approx(33.88) and segs[0]["fim"] == pytest.approx(40.25)


# ------------------------------------------------------------------ imagem e som
def test_metadata_e_linha_do_tempo():
    mov = ac.ler_metadata("frame:0 pts:0 pts_time:0\nlavfi.scd.mafd=0.000\nlavfi.scd.score=0.000\n"
                          "frame:1 pts:1 pts_time:0.5\nlavfi.scd.mafd=4.000\nlavfi.scd.score=30.0\n"
                          "frame:2 pts:2 pts_time:1.2\nlavfi.scd.mafd=2.000\nlavfi.scd.score=0.5\n")
    vol = ac.ler_metadata("frame:0 pts:0 pts_time:0\nlavfi.astats.Overall.RMS_level=-20.0\n"
                          "frame:1 pts:1 pts_time:0.5\nlavfi.astats.Overall.RMS_level=-inf\n"
                          "frame:2 pts:2 pts_time:1.5\nlavfi.astats.Overall.RMS_level=-30.0\n")
    tl = ac.linha_do_tempo(mov, vol, 2.5, 1)
    assert [(x["t"], x["fim"]) for x in tl] == [(0, 1), (1, 2), (2, 2.5)]
    assert tl[0] == {"t": 0, "fim": 1, "volume_db": -20.0, "movimento": 2.0, "cena": True}
    assert tl[1]["cena"] is False and tl[1]["volume_db"] == -30.0
    assert tl[2]["volume_db"] is None and tl[2]["movimento"] is None


def test_intervalo_e_folhas_de_contato():
    assert ac.intervalo_quadros(66) == 1 and ac.intervalo_quadros(600) == 5
    assert ac.folhas_de_contato(list(range(14))) == [list(range(12)), [12, 13]]


def test_gancho_no_alto_da_tela_nos_3_primeiros_segundos():
    ass = ac.gerar_ass([], [], {}, gancho="ele não viu o tanque")
    assert "Style: Gancho," in ass
    assert "Dialogue: 1,0:00:00.00,0:00:03.00,Gancho,Gancho" in ass and "ELE NÃO VIU O TANQUE" in ass
    assert "Gancho" not in ac.gerar_ass([], [], {})
    assert "PILOTAR O HELICÓPTERO}" not in ac.gerar_ass([], [], {}, "pilotar o helicóptero 💥")
    assert ac.gerar_ass([], [], {}, "pilotar o helicóptero 💥").rstrip().endswith("PILOTAR O HELICÓPTERO")


# ------------------------------------------------------------------ rodada de verdade


def _clip(path):
    """10 s em 1280x720 com duas faixas: a 1 só soa até 5 s, a 2 só depois."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10,volume='if(lt(t,5),1,0)':eval=frame",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=10,volume='if(gte(t,5),1,0)':eval=frame",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True)


def _stream_info(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,r_frame_rate"
                          ":format=duration", "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@needs_ffmpeg
def test_analisar_midia_tira_quadros_folhas_e_movimento(tmp_path):
    video = tmp_path / "clip.mp4"
    _clip(video)
    info = ac.probe(str(video))
    trab = tmp_path / "b"
    trab.mkdir()
    m = ac.analisar_midia(str(video), info, trab)
    assert m["passo"] == 1 and [q["t"] for q in m["quadros"]] == list(range(10))
    assert all((trab / q["arquivo"]).exists() for q in m["quadros"]) and m["quadros"][3]["arquivo"] == "quadros/q-00003s.jpg"
    assert [f["arquivo"] for f in m["folhas"]] == ["folhas/folha-01.jpg"] and m["folhas"][0]["tempos"] == list(range(10))
    assert len(m["linha_do_tempo"]) == 10 and m["linha_do_tempo"][0]["volume_db"] is not None
    assert all(x["movimento"] is not None for x in m["linha_do_tempo"])
    # a faixa 1 do clipe de teste só soa até 5 s
    assert m["linha_do_tempo"][8]["volume_db"] is None or m["linha_do_tempo"][8]["volume_db"] < -60


@needs_ffmpeg
def test_pipeline_completo_com_duas_faixas(tmp_path, fake_llm, monkeypatch):
    binary, llm_out = fake_llm
    video = tmp_path / "clip.mp4"
    _clip(video)
    falas = [("Olha", 0.5), ("só", 1.0), ("isso", 1.5), ("aqui.", 2.0), ("Que", 2.5), ("coisa.", 3.0),
             ("Nossa", 5.5), ("eu", 6.0), ("não", 6.5), ("acredito.", 7.0), ("Sério", 7.5), ("mesmo.", 8.0)]
    transcript = tmp_path / "clip.json"
    transcript.write_text(json.dumps([{"text": t, "start": s, "end": s + 0.4} for t, s in falas]))
    monkeypatch.setenv("FAKE_LLM_RESPOSTA", json.dumps({"titulo": "Reação", "segmentos": [
        {"inicio": 0.4, "fim": 2.3, "motivo": "gancho"}, {"inicio": 5.6, "fim": 7.9, "motivo": "reação"}]}))
    trab = tmp_path / "bancada"
    trab.mkdir()
    entrega = tmp_path / "proj" / "Corte 9x16 v1"
    args = trab / "auto-args.json"
    args.write_text(json.dumps({
        "video": str(video), "transcricao": str(transcript), "entrega": str(entrega), "versao": "v1",
        "observacoes": "foque na reação", "claude": str(binary),
        "corte": {"faixas_locutores": [{"faixa": 1, "nome": "Ana"}, {"faixa": 2, "nome": "Beto"}]},
    }))
    r = subprocess.run([sys.executable, ac.__file__, "--args", str(args)], cwd=trab,
                       capture_output=True, text=True, timeout=180)
    linhas = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
    resultado = linhas[-1]
    assert resultado["type"] == "result" and resultado["is_error"] is False, resultado
    assert resultado["total_cost_usd"] == 0.02

    nomes = sorted(p.name for p in entrega.iterdir())
    assert nomes == sorted([
        "Corte 9x16.mp4", "Legenda animada.ass", "Legenda.srt", "Legenda - Ana.srt", "Legenda - Beto.srt",
        "Transcrição.txt", "Áudio - Faixa 1 (Ana).wav", "Áudio - Faixa 2 (Beto).wav", "corte.json",
    ])
    info = _stream_info(entrega / "Corte 9x16.mp4")
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["r_frame_rate"]) == (1080, 1920, "30/1")
    corte = json.loads((entrega / "corte.json").read_text())
    # 0,38–2,5 (para antes de "Que") + 5,38–8,0 (para antes de "mesmo.")
    assert [(s["inicio"], s["fim"]) for s in corte["segmentos"]] == [(0.38, 2.5), (5.38, 8.0)]
    assert corte["locutores"] == ["Ana", "Beto"] and corte["origem_locutores"] == "faixas de áudio"
    assert float(info["format"]["duration"]) == pytest.approx(4.74, abs=0.15)
    wav = _stream_info(entrega / "Áudio - Faixa 2 (Beto).wav")
    assert float(wav["format"]["duration"]) == pytest.approx(4.74, abs=0.1)
    assert "[Ana] OLHA SÓ ISSO AQUI." in (entrega / "Legenda.srt").read_text()
    assert "Beto" not in (entrega / "Legenda - Ana.srt").read_text()
    # o LLM recebeu as frases com locutor e o pedido do editor
    prompt = json.loads(llm_out.read_text())["prompt"]
    assert "Ana: Olha só isso aqui." in prompt and "Beto: Nossa eu não acredito." in prompt
    assert "foque na reação" in prompt
    assert "folhas/folha-01.jpg" in prompt and "Linha do tempo:" in prompt
    assert (trab / "folhas" / "folha-01.jpg").exists() and (trab / "linha-do-tempo.json").exists()
    argv = json.loads(llm_out.read_text())["argv"]
    assert argv[argv.index("--input-format") + 1] == "stream-json" and argv[argv.index("--tools") + 1] == ""
    assert (trab / "llm-resposta.json").exists() and (trab / "palavras.json").exists()


@needs_ffmpeg
def test_gameplay_sem_fala_sai_pela_imagem_sem_legenda(tmp_path, fake_llm, monkeypatch):
    binary, llm_out = fake_llm
    video = tmp_path / "clip.mp4"
    _clip(video)
    transcript = tmp_path / "clip.json"
    transcript.write_text("[]")  # o whisper não achou fala
    monkeypatch.setenv("FAKE_LLM_RESPOSTA", json.dumps({
        "titulo": "Tanque", "resumo": "lobby e depois a explosão", "gancho": "ninguém esperava", "legenda": False,
        "segmentos": [{"inicio": 3.0, "fim": 7.5, "motivo": "a explosão"}],
        "descartado": [{"inicio": 0, "fim": 3.0, "motivo": "lobby"}]}))
    trab = tmp_path / "bancada"
    trab.mkdir()
    entrega = tmp_path / "proj" / "Corte 9x16 v1"
    args = trab / "auto-args.json"
    args.write_text(json.dumps({"video": str(video), "transcricao": str(transcript), "entrega": str(entrega),
                                "claude": str(binary)}))
    r = subprocess.run([sys.executable, ac.__file__, "--args", str(args)], cwd=trab,
                       capture_output=True, text=True, timeout=180)
    linhas = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
    assert linhas[-1]["is_error"] is False, linhas[-1]
    falas = " ".join(c.get("text", "") for ln in linhas if ln["type"] == "assistant" for c in ln["message"]["content"])
    assert "lobby e depois a explosão" in falas and "0.0–3.0 s: lobby" in falas
    nomes = sorted(p.name for p in entrega.iterdir())
    assert nomes == sorted(["Corte 9x16.mp4", "Gancho.ass", "Áudio - Faixa 1.wav", "Áudio - Faixa 2.wav", "corte.json"])
    corte = json.loads((entrega / "corte.json").read_text())
    # sem fala, o corte fica onde o LLM pediu (+ o respiro)
    assert [(s["inicio"], s["fim"]) for s in corte["segmentos"]] == [(2.88, 7.75)]
    assert corte["legenda"] is False and corte["gancho"] == "ninguém esperava" and corte["descartado"][0]["motivo"] == "lobby"
    assert "(sem fala)" in json.loads(llm_out.read_text())["prompt"]


@needs_ffmpeg
def test_pipeline_falha_com_linha_result_quando_o_llm_nao_ajuda(tmp_path, fake_llm, monkeypatch):
    binary, _ = fake_llm
    video = tmp_path / "clip.mp4"
    _clip(video)
    transcript = tmp_path / "clip.json"
    transcript.write_text(json.dumps([{"text": "oi", "start": 1, "end": 1.3}]))
    monkeypatch.setenv("FAKE_LLM_RESPOSTA", json.dumps({"segmentos": []}))
    trab = tmp_path / "b"
    trab.mkdir()
    args = trab / "a.json"
    args.write_text(json.dumps({"video": str(video), "transcricao": str(transcript),
                                "entrega": str(tmp_path / "e"), "claude": str(binary)}))
    r = subprocess.run([sys.executable, ac.__file__, "--args", str(args)], cwd=trab, capture_output=True, text=True, timeout=120)
    resultado = json.loads(r.stdout.splitlines()[-1])
    assert r.returncode == 1 and resultado["is_error"] is True
    assert "nenhum trecho" in resultado["result"]
    assert not (tmp_path / "e").exists()


# ------------------------------------------------------------------ editor
FAKE_SCRIPT = r'''import json, os, sys
a = json.load(open(sys.argv[sys.argv.index("--args") + 1]))
json.dump({"args": a, "cwd": os.getcwd(), "key": os.environ.get("OPENROUTER_API_KEY"), "hf": os.environ.get("HF_TOKEN")},
          open(os.environ["FAKE_CORTE_OUT"], "w"))
os.makedirs(a["entrega"], exist_ok=True)
open(os.path.join(a["entrega"], "Corte 9x16.mp4"), "wb").write(b"mp4")
open(os.path.join(a["entrega"], "Legenda.srt"), "w").write("1")
print(json.dumps({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0.01}))
'''


def _project(tmp_path):
    d = tmp_path / "Bruno"
    d.mkdir()
    client = clients_module.create_client(name="Bruno", path=str(d))
    project = projects_module.create_project(client["id"], "Clipe live")
    folder = Path(project["path"])
    (folder / "Brutos").mkdir(exist_ok=True)
    (folder / "Brutos" / "live.mkv").write_bytes(b"video")
    return client, project, folder


def _wait(job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = editor.load_job(job_id)
        if job and job["status"] not in editor.ACTIVE_STATUSES:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} não terminou: {editor.load_job(job_id)}")


@pytest.fixture
def fake_corte(tmp_path, monkeypatch):
    script = tmp_path / "fake_autocorte.py"
    script.write_text(FAKE_SCRIPT)
    out = tmp_path / "corte_out.json"
    monkeypatch.setattr(editor, "AUTOCORTE_SCRIPT", script)
    monkeypatch.setenv("FAKE_CORTE_OUT", str(out))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    paths.EDITOR_DIR.mkdir(parents=True, exist_ok=True)
    return out


def test_config_esconde_as_chaves_e_vazio_mantem(fake_corte):
    editor.save_config({"corte": {"provedor_llm": "openrouter", "openrouter_api_key": "sk-or-1",
                                  "openrouter_modelo": "x/y", "faixas_locutores": "2=Bruno, 3=Ana"}})
    pub = editor.public_config()["corte"]
    assert pub["openrouter_api_key"] == "" and pub["openrouter_api_key_definida"] is True
    assert pub["faixas_locutores"] == [{"faixa": 2, "nome": "Bruno"}, {"faixa": 3, "nome": "Ana"}]
    editor.save_config({"corte": {"openrouter_api_key": "", "duracao_max": "45"}})
    cfg = editor.get_config()["corte"]
    assert cfg["openrouter_api_key"] == "sk-or-1" and cfg["duracao_max"] == 45
    # restaurar os padrões não leva a chave junto
    editor.reset_config()
    assert editor.get_config()["corte"]["openrouter_api_key"] == "sk-or-1"
    assert editor.get_config()["corte"]["provedor_llm"] == "claude"
    editor.save_config({"corte": {"openrouter_api_key": None}})
    assert editor.get_config()["corte"]["openrouter_api_key"] == ""
    with pytest.raises(editor.EditorError):
        editor.save_config({"corte": {"duracao_min": 90, "duracao_max": 30}})
    with pytest.raises(editor.EditorError):
        editor.save_config({"corte": {"faixas_locutores": "dois=Bruno"}})


def test_job_de_corte_roda_o_script_com_a_chave_so_no_ambiente(tmp_path, fake_corte, monkeypatch):
    monkeypatch.setattr(editor, "find_claude", lambda: None)
    client, project, folder = _project(tmp_path)
    with pytest.raises(editor.EditorError) as exc:  # LLM = claude, e não há claude
        editor.start_project_job(project["id"], editor.TAREFA_CORTE, "Brutos/live.mkv", confirmar_tokens=True)
    assert exc.value.status == 503
    editor.save_config({"corte": {"provedor_llm": "openrouter", "openrouter_api_key": "sk-or-segredo",
                                  "openrouter_modelo": "x/y", "hf_token": "hf_abc"}})
    assert editor.availability()["corte_disponivel"] is True
    job = editor.start_project_job(project["id"], editor.TAREFA_CORTE, "Brutos/live.mkv", observacoes="só o fim", confirmar_tokens=True)
    done = _wait(job["id"])
    assert done["status"] == editor.STATUS_CONCLUIDO and done["modo"] == editor.MODO_CORTE
    assert done["entrega"] == str(folder / "Corte 9x16 v1")
    chamada = json.loads(fake_corte.read_text())
    assert chamada["key"] == "sk-or-segredo" and chamada["hf"] == "hf_abc"
    assert chamada["cwd"].endswith(".estagiario/corte_vertical v1")
    assert chamada["args"]["observacoes"] == "só o fim"
    assert chamada["args"]["video"] == str(folder / "Brutos" / "live.mkv")
    args_txt = (folder / ".estagiario" / "corte_vertical v1" / "auto-args.json").read_text()
    assert "sk-or-segredo" not in args_txt and "hf_abc" not in args_txt
    assert not (folder / ".estagiario" / "corte_vertical v1" / "kit").exists()
    # a entrega aparece no painel e a próxima versão é a 2
    entregas = editor.list_deliveries(project["id"])
    assert [(e["pasta"], e["total_arquivos"]) for e in entregas] == [("Corte 9x16 v1", 2)]
    assert editor.next_version(folder, "Corte 9x16") == 2
    # o vídeo cortado não vira candidato a vídeo bruto
    assert [v["path"] for v in editor.list_candidates(project["id"])["videos"]] == ["Brutos/live.mkv"]


def test_clipe_novo_entra_no_corte_quando_ligado(monkeypatch):
    chamadas = []
    ocupado = {"id": None}
    monkeypatch.setattr(editor, "start_project_job", lambda pid, tarefa, video, **kw: chamadas.append((pid, tarefa, video)))
    monkeypatch.setattr(editor, "active_job_id", lambda: ocupado["id"])
    ev = {"project_id": "c:001", "video_rel": "Brutos/a.mkv"}
    editor.autocut_from_clip(ev)
    assert chamadas == []  # desligado por padrão
    clips.set_config(autocorte=True)
    editor.autocut_from_clip(ev)
    assert chamadas == [("c:001", editor.TAREFA_CORTE, "Brutos/a.mkv")]
    ocupado["id"] = "outro-job"
    editor.autocut_from_clip({"project_id": "c:002", "video_rel": "Brutos/b.mkv"})
    assert len(chamadas) == 1 and editor.clip_queue() == ["c:002"]
    ocupado["id"] = None
    editor._after_finish({"tipo": "amostras", "status": "concluido"})  # um job terminou: a fila anda
    assert chamadas[-1] == ("c:002", editor.TAREFA_CORTE, "Brutos/b.mkv") and editor.clip_queue() == []


# ------------------------------------------------------------------ enquadramento (editor visual)
def test_layout_inteiro_trava_9x16_e_cabe_no_quadro():
    px = ac.normalizar_layout({"modo": "inteiro", "principal": {"x": 0.9, "y": 0, "w": 0.4, "h": 1}}, 1920, 1080)
    # 0,4 da largura = 768 px pediria 1365 de altura: encolhe para 1080 de altura, 606 de largura,
    # e a caixa que passava da borda direita é empurrada para dentro
    assert px["principal"] == {"w": 606, "h": 1080, "x": 1314, "y": 0}


def test_layout_dividido_camera_em_cima_com_a_proporcao_de_cada_espaco():
    layout = {"modo": "dividido", "divisao": 0.35,
              "camera": {"x": 0.78, "y": 0.38, "w": 0.23, "h": 0.25},
              "principal": {"x": 0.26, "y": 0, "w": 0.47, "h": 1}}
    px = ac.normalizar_layout(layout, 1920, 1080)
    assert px["altura_camera"] == 672                       # 35 % de 1920, par
    cam, jogo = px["camera"], px["principal"]
    assert cam["w"] / cam["h"] == pytest.approx(1080 / 672, rel=0.02)
    assert jogo["w"] / jogo["h"] == pytest.approx(1080 / 1248, rel=0.02)
    assert cam["x"] + cam["w"] <= 1920 and jogo["y"] + jogo["h"] <= 1080
    cadeia = ac.cadeia_video(1920, 1080, layout=layout)
    assert cadeia.startswith("split[cam][jogo];[cam]crop=")
    assert "scale=1080:672" in cadeia and "scale=1080:1248" in cadeia and "vstack=inputs=2" in cadeia
    # divisão fora da faixa é presa em 20–80 %
    assert ac.normalizar_layout({"modo": "dividido", "divisao": 0.95}, 1920, 1080)["divisao"] == 0.8


def test_layout_desfocado_e_sem_layout():
    assert "boxblur" in ac.cadeia_video(1920, 1080, layout={"modo": "desfocado"})
    assert ac.cadeia_video(1920, 1080, "corte", 0.5, None).startswith("crop=606:1080:657:0,")


@needs_ffmpeg
def test_grafo_dividido_roda_no_ffmpeg(tmp_path):
    video = tmp_path / "clip.mp4"
    _clip(video)
    segs = [{"inicio": 1.0, "fim": 2.0}, {"inicio": 6.0, "fim": 7.5}]
    cadeia = ac.cadeia_video(1280, 720, layout={"modo": "dividido", "divisao": 0.3,
                                                 "camera": {"x": 0.75, "y": 0.05, "w": 0.22, "h": 0.25}})
    out = tmp_path / "out.mp4"
    ac.rodar(["ffmpeg", "-v", "error", "-y", "-i", video, "-filter_complex", ac.grafo_corte(segs, cadeia),
              "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "ultrafast", out], timeout=120)
    info = _stream_info(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["r_frame_rate"]) == (1080, 1920, "30/1")
    assert float(info["format"]["duration"]) == pytest.approx(2.5, abs=0.1)


def test_layout_validado_na_config_e_no_job(tmp_path, fake_corte, monkeypatch):
    with pytest.raises(editor.EditorError):
        editor.clean_layout({"modo": "torto"})
    with pytest.raises(editor.EditorError):
        editor.clean_layout({"modo": "dividido", "camera": {"x": 0, "y": 0, "w": 0.001, "h": 0.5}})
    assert editor.clean_layout(None) is None
    assert editor.clean_layout({"modo": "dividido", "divisao": 3, "principal": {"x": 0.9, "y": -1, "w": 0.4, "h": 1}}) == {
        "modo": "dividido", "divisao": 0.8, "principal": {"x": 0.6, "y": 0.0, "w": 0.4, "h": 1.0}}
    padrao = {"modo": "dividido", "divisao": 0.35, "camera": {"x": 0.7, "y": 0.1, "w": 0.25, "h": 0.25}}
    editor.save_config({"corte": {"layout": padrao, "provedor_llm": "openrouter",
                                  "openrouter_api_key": "k", "openrouter_modelo": "x/y"}})
    assert editor.public_config()["corte"]["layout"] == padrao
    # salvar o resto da configuração (o modal não manda o layout) não apaga o padrão
    editor.save_config({"corte": {"duracao_max": 50}})
    assert editor.get_config()["corte"]["layout"] == padrao

    client, project, folder = _project(tmp_path)
    job = editor.start_project_job(project["id"], editor.TAREFA_CORTE, "Brutos/live.mkv", confirmar_tokens=True)
    _wait(job["id"])
    assert json.loads(fake_corte.read_text())["args"]["layout"] == padrao   # sem layout no job: o padrão
    deste = {"modo": "inteiro", "principal": {"x": 0.1, "y": 0, "w": 0.3, "h": 1}}
    job = editor.start_project_job(project["id"], editor.TAREFA_CORTE, "Brutos/live.mkv", layout=deste, confirmar_tokens=True)
    _wait(job["id"])
    assert json.loads(fake_corte.read_text())["args"]["layout"] == deste


@needs_ffmpeg
def test_quadro_e_midia_para_o_editor_de_enquadramento(tmp_path):
    from app import create_app

    client, project, folder = _project(tmp_path)
    _clip(folder / "Brutos" / "live.mp4")
    c = create_app().test_client()
    info = c.get("/api/editor/midia", query_string={"project_id": project["id"], "video": "Brutos/live.mp4"}).get_json()
    assert (info["largura"], info["altura"]) == (1280, 720) and info["duracao"] == pytest.approx(10, abs=0.2)
    r = c.get("/api/editor/quadro", query_string={"project_id": project["id"], "video": "Brutos/live.mp4", "t": "2.5"})
    assert r.status_code == 200 and r.mimetype == "image/jpeg" and r.data[:2] == b"\xff\xd8"
    assert len(list((paths.EDITOR_DIR / "quadros").glob("*.jpg"))) == 1   # cache fora da pasta do projeto
    assert c.get("/api/editor/quadro", query_string={"project_id": project["id"], "video": "../../etc/passwd"}).status_code == 404
    assert c.get("/api/editor/quadro", query_string={"project_id": project["id"], "video": "Brutos/live.mp4", "t": "x"}).status_code == 400


# ------------------------------------------------------------------ desfocado com escala e capa
def test_desfocado_escala_e_posicao():
    assert ac.escala_maxima(1920, 1080) == pytest.approx(3.16, abs=0.01)
    assert ac.escala_maxima(1080, 1920) == 1.0   # já vertical: não há o que ampliar
    # escala 1 = cabe na largura, nada cortado (o mesmo de antes)
    assert "[frente]scale=1080:608:flags=lanczos,crop=1080:608:0:0[frentes]" in ac.cadeia_video(
        1920, 1080, layout={"modo": "desfocado"})
    # escala 2: 2160 de largura, 1080 aparecem; posição 0,3 escolhe o pedaço
    c = ac.cadeia_video(1920, 1080, layout={"modo": "desfocado", "escala": 2, "posicao": 0.3})
    assert "[frente]scale=2160:1216:flags=lanczos,crop=1080:1216:324:0[frentes]" in c
    # acima do máximo é preso: preenche a altura, sem passar de 1920
    px = ac.normalizar_layout({"modo": "desfocado", "escala": 9}, 1920, 1080)
    assert px["escala"] == pytest.approx(3.1605, abs=1e-3)
    assert "crop=1080:1920:" in ac.cadeia_video(1920, 1080, layout={"modo": "desfocado", "escala": 9})


@needs_ffmpeg
def test_capa_so_no_primeiro_quadro_sem_mudar_a_duracao(tmp_path):
    video = tmp_path / "clip.mp4"
    _clip(video)
    capa = tmp_path / "Capa.jpg"
    # imagem da capa: vermelho puro, para achar no vídeo
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=640x480",
                    "-frames:v", "1", str(tmp_path / "vermelho.png")], check=True)
    ac.rodar(ac.comando_capa_imagem(tmp_path / "vermelho.png", capa))
    info = _stream_info(capa)
    assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (1080, 1920)
    segs = [{"inicio": 1.0, "fim": 3.0}]
    cadeia = ac.cadeia_video(1280, 720, layout={"modo": "desfocado", "escala": 1.5})
    out = tmp_path / "out.mp4"
    ac.rodar(["ffmpeg", "-v", "error", "-y", "-i", video, "-i", capa, "-filter_complex",
              ac.grafo_corte(segs, cadeia, capa=True), "-map", "[vout]", "-map", "[aout]",
              "-c:v", "libx264", "-preset", "ultrafast", out], timeout=120)
    assert float(_stream_info(out)["format"]["duration"]) == pytest.approx(2.0, abs=0.1)

    def cor_media(n):
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-vf",
                            f"select=eq(n\\,{n}),scale=1:1", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True, check=True)
        return tuple(r.stdout[:3])

    r0, g0, b0 = cor_media(0)
    assert r0 > 200 and g0 < 60 and b0 < 60          # quadro 0 = a capa
    r1, g1, b1 = cor_media(1)
    assert not (r1 > 200 and g1 < 60 and b1 < 60)     # quadro 1 já é o vídeo
    # quadro do vídeo como capa, com o mesmo enquadramento
    ac.rodar(ac.comando_capa_quadro(video, 2.0, cadeia, tmp_path / "q.jpg"))
    info = _stream_info(tmp_path / "q.jpg")
    assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (1080, 1920)


def test_capa_validada(tmp_path):
    assert editor.clean_cover({"tipo": "nenhuma"}) is None and editor.clean_cover(None) is None
    assert editor.clean_cover({"tipo": "quadro", "t": "12.345"}) == {"tipo": "quadro", "t": 12.35}
    assert editor.clean_cover({"tipo": "imagem", "imagem": "/x/capa.PNG"}) == {"tipo": "imagem", "imagem": "/x/capa.PNG"}
    for ruim in ({"tipo": "imagem", "imagem": "capa.png"}, {"tipo": "imagem", "imagem": "/x/capa.mp4"},
                 {"tipo": "gif"}, {"tipo": "quadro", "t": "x"}):
        with pytest.raises(editor.EditorError):
            editor.clean_cover(ruim)
    layout = editor.clean_layout({"modo": "desfocado", "escala": 9, "posicao": -1, "capa": {"tipo": "quadro", "t": 3}})
    assert layout == {"modo": "desfocado", "escala": 4.0, "posicao": 0.0, "capa": {"tipo": "quadro", "t": 3.0}}


def test_imagem_da_capa_servida_so_se_for_imagem(tmp_path):
    from app import create_app

    img = tmp_path / "capa.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (tmp_path / "segredo.txt").write_text("não")
    c = create_app().test_client()
    assert c.get("/api/editor/capa-imagem", query_string={"caminho": str(img)}).status_code == 200
    assert c.get("/api/editor/capa-imagem", query_string={"caminho": str(tmp_path / "segredo.txt")}).status_code == 404
    assert c.get("/api/editor/capa-imagem", query_string={"caminho": "capa.png"}).status_code == 404
