"""Etapa B do editor único: keyframes (escala, posição, rotação, opacidade, volume), fades nas pontas dos
clipes, transições no corte (dissolver, mergulho no preto/branco, deslizar, cruzada de áudio) e a
ferramenta de texto — no modelo, no grafo do ffmpeg (render de verdade, amostrando pixels e volume) e no
espelho em JS da prévia."""

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from clientela import sequencia as sq

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="sem ffmpeg")
HTML = Path(__file__).resolve().parent.parent / "templates" / "index.html"


def _doc():
    d = sq.nova("B", "personalizado", largura=320, altura=180, fps=25)
    d["midias"] = [{"id": c, "caminho": f"{c}.mp4", "duracao": 4, "largura": 320, "altura": 180, "audios": 1}
                   for c in ("red", "blue", "lime")]
    d["trilhas"] = {"video": 2, "audio": 1}
    d["clipes"] = [
        {"id": "a", "midia": "red", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2},
        {"id": "b", "midia": "blue", "tipo": "video", "trilha": 1, "inicio": 2, "entrada": 1, "saida": 2,
         "transicao": {"tipo": "dissolver", "dur": 1}},
        {"id": "c", "midia": "red", "tipo": "video", "trilha": 1, "inicio": 3, "entrada": 2, "saida": 3,
         "transicao": {"tipo": "preto", "dur": 0.6}},
        {"id": "x", "midia": "red", "tipo": "audio", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2},
        {"id": "y", "midia": "blue", "tipo": "audio", "trilha": 1, "inicio": 2, "entrada": 1, "saida": 3,
         "transicao": {"tipo": "cruzado", "dur": 0.8}},
    ]
    return d


# ------------------------------------------------------------------ modelo
def test_keyframes_fades_rotacao_e_transicao_sao_normalizados():
    d = _doc()
    d["clipes"][0].update(rotacao=12, fade_in=0.5, fade_out=99, keyframes={
        "escala": [{"t": 3, "v": 2}, {"t": -1, "v": 1, "ease": "suave"}, {"t": 3, "v": 9}, {"t": "x", "v": 1}],
        "ganho": [{"t": 0, "v": -5}],          # propriedade de áudio num clipe de vídeo: fora
        "opacidade": [{"t": 1, "v": 0.5, "ease": "pulo"}]})
    d["clipes"][3]["keyframes"] = {"ganho": [{"t": 0, "v": -20}], "escala": [{"t": 0, "v": 2}]}
    d["clipes"][1]["transicao"] = {"tipo": "cruzado", "dur": 9}   # tipo de áudio num vídeo: fora
    d["clipes"][4]["transicao"] = {"tipo": "cruzado", "dur": 9}
    n = {c["id"]: c for c in sq.normalizar(d)["clipes"]}
    a = n["a"]
    assert a["keyframes"]["escala"] == [{"t": 0.0, "v": 1.0, "ease": "suave"}, {"t": 2.0, "v": 2.0, "ease": "linear"}]
    assert a["keyframes"]["opacidade"][0]["ease"] == "linear" and "ganho" not in a["keyframes"]
    assert a["rotacao"] == 12 and a["fade_in"] == 0.5 and a["fade_out"] == 2.0   # fade limitado à duração
    assert n["x"]["keyframes"] == {"ganho": [{"t": 0.0, "v": -20.0, "ease": "linear"}]}
    assert "transicao" not in n["b"] and n["y"]["transicao"] == {"tipo": "cruzado", "dur": sq.TRANSICAO_MAX}


def test_valor_e_expressao_dos_keyframes_batem():
    c = {"keyframes": {"escala": [{"t": 0, "v": 1, "ease": "suave"}, {"t": 2, "v": 2, "ease": "segurar"},
                                  {"t": 3, "v": 0.5, "ease": "linear"}, {"t": 4, "v": 1, "ease": "linear"}]}}
    assert [round(sq.valor(c, "escala", t), 4) for t in (-1, 0, 1, 2, 2.5, 3, 3.5, 9)] == [1, 1, 1.5, 2, 2, 0.5, 0.75, 1]
    assert sq.valor({"escala": 0.7}, "escala", 1) == 0.7          # sem keyframes, o valor fixo
    expr = sq.expr_kf(c["keyframes"]["escala"], "(T)")
    # avalia a expressão do ffmpeg em Python (if/lt → condicional) e compara com valor()
    py = expr.replace("if(", "_if(").replace("lt(", "_lt(")
    for t in (0, 0.3, 1, 1.7, 2, 2.6, 3, 3.2, 3.9, 5):
        got = eval(py.replace("(T)", f"({t})"), {"_if": lambda a, b, c: b if a else c, "_lt": lambda a, b: a < b})
        assert got == pytest.approx(sq.valor(c, "escala", t), abs=1e-6), t


def test_plano_das_transicoes():
    p = sq.plano_transicoes(sq.normalizar(_doc()))
    # dissolver de 1 s centrado no corte em 2: a alça de "a" (2 s depois da saída) e de "b" (1 s antes da entrada)
    assert p["a"]["depois"] == 0.5 and p["b"]["antes"] == 0.5
    assert p["b"]["trans"][0] == {"tipo": "dissolver", "a": 1.5, "b": 2.5, "lado": "entra"}
    # mergulho no preto não usa alça: escurece até o corte (3) e clareia depois
    assert {"tipo": "preto", "lado": "sai", "a": 2.7, "b": 3.0} in p["b"]["trans"]
    assert p["c"]["trans"] == [{"tipo": "preto", "lado": "entra", "a": 3.0, "b": 3.3}] and p["c"]["antes"] == 0
    # cruzado: "x" tem 2 s de sobra, "y" só 1 s antes da entrada → janela 1.6–2.4
    assert p["y"]["trans"][0]["a"] == 1.6 and p["x"]["depois"] == 0.4
    # sem alça nenhuma (gráfico/começo do take), a transição some; sem clipe encostado antes, também
    d = _doc()
    d["clipes"][1]["entrada"] = 0                                  # "b" começa no início do take: sem sobra antes
    d["clipes"][0].update(entrada=2, saida=4)                      # "a" acaba no fim do take: sem sobra depois
    assert [x for x in sq.plano_transicoes(sq.normalizar(d))["b"]["trans"] if x["lado"] == "entra"] == []
    d = _doc()
    d["clipes"][1]["inicio"] = 2.5                                 # não encosta no anterior
    d["clipes"].pop(2)
    assert sq.plano_transicoes(sq.normalizar(d))["b"]["trans"] == []


def test_grafo_tem_as_expressoes_entradas_cortadas_e_apad_com_fim():
    d = _doc()
    d["clipes"].append({"id": "g", "midia": "lime", "tipo": "video", "trilha": 2, "inicio": 0, "entrada": 0, "saida": 4,
                        "fade_in": 0.5, "keyframes": {"escala": [{"t": 0, "v": 0.2}, {"t": 4, "v": 0.6}],
                                                      "pos_x": [{"t": 0, "v": 0.2}, {"t": 4, "v": 0.8}],
                                                      "rotacao": [{"t": 0, "v": 0}, {"t": 4, "v": 90}],
                                                      "opacidade": [{"t": 0, "v": 1}, {"t": 4, "v": 0.5}]}})
    d["clipes"][3]["keyframes"] = {"ganho": [{"t": 0, "v": -20}, {"t": 2, "v": 0}]}
    d["clipes"][3]["fade_out"] = 0.3
    n = sq.normalizar(d)
    cmd = sq.comando(n, {c: f"/m/{c}.mp4" for c in ("red", "blue", "lime")}, "/o.mp4", audio="/t/a.wav")
    entradas = [cmd[i - 4:i + 2] for i, x in enumerate(cmd) if x == "-i"]
    # uma entrada por clipe visual, já cortada com as alças: "a" vai até 2.5, "b" começa 0.5 antes
    assert ["-ss", "0", "-t", "2.5", "-i", "/m/red.mp4"] in entradas and ["-ss", "0.5", "-t", "1.5", "-i", "/m/blue.mp4"] in entradas
    assert cmd[cmd.index("-map") + 3] == "4:a"   # o WAV da passada de áudio
    g = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=w='max(2,trunc(320*(if(lt((t-0)" in g and ":eval=frame" in g
    assert "rotate=a='(if(" in g and ":c=black@0" in g
    assert "alphaextract,geq=lum='p(X,Y)*clip(if(lt((T-0)" in g
    assert "fade=t=in:st=1.5:d=1:alpha=1" in g                              # dissolver em "b"
    assert "fade=t=out:st=2.7:d=0.3:color=black" in g and "fade=t=in:st=3:d=0.3:color=black" in g
    assert "fade=t=in:st=0:d=0.5:alpha=1" in g                              # fade de entrada do "g"
    assert "x='(if(lt((t-0),0),0.2," in g
    ga = sq.grafo_audio(n, {"x": 0, "y": 1})
    assert "volume='pow(10,(if(lt((t-0),0),-20," in ga and ":eval=frame" in ga
    assert "afade=t=out:st=1.7:d=0.3" in ga                                  # fade de saída do "x"
    assert "afade=t=out:st=1.6:d=0.8:curve=qsin" in ga and "afade=t=in:st=0:d=0.8:curve=qsin" in ga
    assert "adelay=1600:all=1" in ga
    assert re.search(r"apad=whole_dur=[\d.]+,atrim=0:", ga) and ",apad," not in ga


# ------------------------------------------------------------------ render de verdade
def _clipe(path, cor, freq):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={cor}:s=320x180:r=25:d=4",
                    "-f", "lavfi", "-i", f"sine=f={freq}:d=4", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(path)], check=True)


def _pixel(video, t, x, y, tmp):
    raw = tmp / f"p{t}-{x}-{y}.raw"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                    "-vf", f"crop=2:2:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)], check=True)
    return list(raw.read_bytes()[:3])


def _volume(video, t, tmp):
    r = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(t), "-t", "0.1", "-i", str(video), "-vn", "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else -99.0


@needs_ffmpeg
def test_render_com_transicoes_keyframes_e_fades(tmp_path):
    for cor, f in (("red", 440), ("blue", 660), ("lime", 880)):
        _clipe(tmp_path / f"{cor}.mp4", cor, f)
    d = _doc()
    d["clipes"].append({"id": "g", "midia": "lime", "tipo": "video", "trilha": 2, "inicio": 0, "entrada": 0, "saida": 4,
                        "keyframes": {"escala": [{"t": 0, "v": 0.2}, {"t": 4, "v": 0.6}],
                                      "pos_x": [{"t": 0, "v": 0.2}, {"t": 4, "v": 0.8}]}})
    d["clipes"][3]["keyframes"] = {"ganho": [{"t": 0, "v": -30}, {"t": 1, "v": 0}]}
    entrega = tmp_path / "Sequência - B v1"
    args = {"doc": d, "entrega": str(entrega), "nome": "B.mp4", "midias": {c: str(tmp_path / f"{c}.mp4") for c in ("red", "blue", "lime")}}
    out = {}
    th = threading.Thread(target=lambda: out.update(m=sq.renderizar(args)), daemon=True)
    th.start()
    th.join(120)
    if th.is_alive():
        subprocess.run(["pkill", "-9", "-f", str(entrega)], check=False)
        pytest.fail("o render travou")
    v = entrega / "B.mp4"
    assert _pixel(v, 0.5, 300, 170, tmp_path)[0] > 200                 # vermelho puro antes da transição
    meio = _pixel(v, 2.0, 300, 170, tmp_path)                           # dissolver: metade vermelho, metade azul
    assert 70 < meio[0] < 190 and 70 < meio[2] < 190
    assert _pixel(v, 2.6, 300, 170, tmp_path)[2] > 200                  # azul depois do dissolver
    assert 120 < _pixel(v, 2.8, 300, 170, tmp_path)[2] < 220            # o mergulho começa em 2.7
    preto = _pixel(v, 2.98, 300, 170, tmp_path)
    assert max(preto) < 40                                               # mergulho no preto no corte
    assert _pixel(v, 3.6, 300, 170, tmp_path)[0] > 200                  # o vermelho volta
    # V2 anda da esquerda (0.2) para a direita (0.8) e cresce: no começo à esquerda, no fim à direita
    assert _pixel(v, 0.3, 64, 90, tmp_path)[1] > 200 and _pixel(v, 0.3, 256, 90, tmp_path)[1] < 100
    assert _pixel(v, 3.7, 250, 90, tmp_path)[1] > 200 and _pixel(v, 3.7, 40, 90, tmp_path)[1] < 100
    # volume com keyframe: -30 dB no começo, cheio em 1.5 s
    assert _volume(v, 0.0, tmp_path) < _volume(v, 1.5, tmp_path) - 15


# ------------------------------------------------------------------ espelho em JS
def _js(trecho_ini, trecho_fim):
    html = HTML.read_text(encoding="utf-8")
    return html[html.index(trecho_ini):html.index(trecho_fim)]


def test_espelho_js_dos_keyframes_e_do_plano():
    node = shutil.which("node")
    if not node:
        pytest.skip("sem node")
    js = _js("const SEQ_KF_VISUAL", "// opacidade final de um clipe visual")
    doc = sq.normalizar(_doc())
    c = {"keyframes": {"escala": [{"t": 0, "v": 1, "ease": "suave"}, {"t": 2, "v": 2, "ease": "segurar"},
                                  {"t": 3, "v": 0.5, "ease": "linear"}, {"t": 4, "v": 1, "ease": "linear"}]}}
    ts = [-1, 0, 0.7, 2, 2.5, 3, 3.5, 9]
    prog = js + f"""
const doc = {json.dumps(doc)};
const seq = {{doc, ver: 1}};
const seqMidia = (id) => doc.midias.find(m => m.id === id);
const seqTT = (c) => (c.tipo === 'audio' ? 'audio' : 'video');
const seqDurC = (c) => c.saida - c.entrada, seqFimC = (c) => c.inicio + c.saida - c.entrada;
const seqNoRender = (c) => c.ativo !== false;
const c = {json.dumps(c)};
console.log(JSON.stringify({{v: {json.dumps(ts)}.map(t => seqValor(c, 'escala', t)), plano: seqPlano()}}));"""
    out = json.loads(subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True).stdout)
    assert out["v"] == pytest.approx([sq.valor(c, "escala", t) for t in ts])
    py = sq.plano_transicoes(doc)
    for cid, p in py.items():
        assert out["plano"][cid]["antes"] == pytest.approx(p["antes"]) and out["plano"][cid]["depois"] == pytest.approx(p["depois"])
        assert [(x["tipo"], x["lado"]) for x in out["plano"][cid]["trans"]] == [(x["tipo"], x["lado"]) for x in p["trans"]]
        for a, b in zip(out["plano"][cid]["trans"], p["trans"]):
            assert a["a"] == pytest.approx(b["a"]) and a["b"] == pytest.approx(b["b"])


def test_texto_da_ferramenta_e_um_grafico_valido():
    """O template que a ferramenta de texto monta passa pelo normalizar (o clipe vai para o arquivo e o render)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("sem node")
    js = _js("const SEQ_TEXTO_PADRAO", "function seqNovoTexto(")
    prog = js + """
const p = {...SEQ_TEXTO_PADRAO, texto: 'Oi\\ngente', fundo: '#E64FC4', entrada: 'digitar', alinhar: 'esquerda', caixa_alta: true};
console.log(JSON.stringify({params: p, def: seqDefTexto(p)}));"""
    out = json.loads(subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True).stdout)
    cam = out["def"]["camadas"][0]
    assert cam["fundo"] == "#E64FC4" and cam["revelar"] == "clamp(time/0.8)" and cam["caixa"] == "alta" and cam["alinhar"] == "esquerda"
    d = sq.nova("T", "vertical")
    d["clipes"] = [{"id": "t", "tipo": "grafico", "trilha": 2, "inicio": 1, "entrada": 0, "saida": 4, "pos_x": 0.5, "pos_y": 0.25,
                    "grafico": {"template": "texto", "params": out["params"], "def": out["def"]}}]
    n = sq.normalizar(d)["clipes"][0]
    assert n["grafico"]["def"]["ferramenta_texto"] is True and n["grafico"]["params"]["texto"] == "Oi\ngente"


# ------------------------------------------------------------------ etapa C: temperatura de cor e volume da trilha
def test_temperatura_e_volume_da_trilha_no_modelo_e_no_grafo():
    d = _doc()
    d["clipes"][0]["cor"] = {"brilho": 1.2, "temperatura": 3}
    d["clipes"][1]["cor"] = {"temperatura": 0}
    d["trilhas_estado"] = {"A1": {"ganho": -6, "mudo": False}, "A2": {"ganho": 3}, "V1": {"ganho": 4}}
    n = sq.normalizar(d)
    cs = {c["id"]: c for c in n["clipes"]}
    assert cs["a"]["cor"]["temperatura"] == 1.0 and "temperatura" not in cs["b"]["cor"]
    assert n["trilhas_estado"] == {"A1": {"ganho": -6.0}}          # A2 não existe; V não tem volume
    assert sq.cadeia_cor(cs["a"]["cor"]) == [f"colorchannelmixer=rr={1.2 * 1.15:.3f}:gg=1.200:bb={1.2 * 0.85:.3f}"]
    assert sq.cadeia_cor({"temperatura": -0.5}) == ["colorchannelmixer=rr=0.925:gg=1.000:bb=1.075"]
    ga = sq.grafo_audio(n, {"x": 0, "y": 1})
    assert ga.count("volume=-6.00dB") == 2                          # o fader de A1 nos dois clipes da trilha


@needs_ffmpeg
def test_render_com_temperatura_e_volume_da_trilha(tmp_path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=25:d=2",
                    "-f", "lavfi", "-i", "sine=f=440:d=2", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(tmp_path / "cinza.mp4")], check=True)
    d = sq.nova("C", "personalizado", largura=320, altura=180, fps=25)
    d["midias"] = [{"id": "m", "caminho": "cinza.mp4", "duracao": 2, "largura": 320, "altura": 180, "audios": 1}]
    d["clipes"] = [{"id": "v", "midia": "m", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2, "cor": {"temperatura": 1}},
                   {"id": "a", "midia": "m", "tipo": "audio", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2}]
    caminhos = {"m": str(tmp_path / "cinza.mp4")}
    sq.renderizar({"doc": d, "entrega": str(tmp_path / "S - C v1"), "nome": "C.mp4", "midias": caminhos})
    d["clipes"][0]["cor"] = {}
    d["trilhas_estado"] = {"A1": {"ganho": -20}}
    sq.renderizar({"doc": d, "entrega": str(tmp_path / "S - C v2"), "nome": "C.mp4", "midias": caminhos})
    quente, neutro = _pixel(tmp_path / "S - C v1" / "C.mp4", 1, 160, 90, tmp_path), _pixel(tmp_path / "S - C v2" / "C.mp4", 1, 160, 90, tmp_path)
    assert quente[0] > quente[2] + 20 and abs(neutro[0] - neutro[2]) < 8   # quente = mais vermelho que azul
    assert _volume(tmp_path / "S - C v2" / "C.mp4", 1, tmp_path) < _volume(tmp_path / "S - C v1" / "C.mp4", 1, tmp_path) - 15
