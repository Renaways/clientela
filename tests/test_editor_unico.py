"""Etapa A do editor único: a sequência ganha o que um editor de vídeo tem no modelo (marcadores, estado
das trilhas, clipe desativado, enquadramento por clipe) e o Corte vertical vira uma sequência 9:16."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import create_app
from clientela import clients as clients_module
from clientela import editor
from clientela import projects as projects_module
from clientela import sequencia as sq

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="sem ffmpeg")


def _doc():
    d = sq.nova("A", "vertical")
    d["trilhas"] = {"video": 2, "audio": 2}
    d["midias"] = [{"id": "m", "caminho": "m.mp4", "duracao": 10, "largura": 1920, "altura": 1080, "audios": 1}]
    d["clipes"] = [
        {"id": "v1", "midia": "m", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 4},
        {"id": "v2", "midia": "m", "tipo": "video", "trilha": 2, "inicio": 0, "entrada": 0, "saida": 4},
        {"id": "a1", "midia": "m", "tipo": "audio", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 4},
        {"id": "a2", "midia": "m", "tipo": "audio", "trilha": 2, "inicio": 0, "entrada": 0, "saida": 4},
    ]
    return d


def test_marcadores_estado_das_trilhas_e_clipe_desativado():
    d = _doc()
    d["marcadores"] = [{"id": "x", "t": 3, "nome": "virada", "cor": "azul"}, {"id": "y", "t": 1, "cor": "rosa"},
                       {"id": "x", "t": 2}, {"t": -1}, "lixo"]
    d["trilhas_estado"] = {"V2": {"oculto": True, "travado": False}, "A1": {"mudo": True}, "A9": {"solo": True}, "V1": {"solo": True}}
    d["clipes"][0]["ativo"] = False
    n = sq.normalizar(d)
    assert [(m["id"], m["t"], m["cor"]) for m in n["marcadores"]] == [("y", 1.0, "verde"), ("x", 3.0, "azul")]
    assert n["trilhas_estado"] == {"V2": {"oculto": True}, "A1": {"mudo": True}}   # só trilhas que existem e chaves do tipo
    assert n["clipes"][[c["id"] for c in n["clipes"]].index("v1")]["ativo"] is False
    # nada de V1 (desativado), V2 (oculta) nem A1 (muda) no render
    assert sorted(c["id"] for c in sq.clipes_no_render(n)) == ["a2"]
    n["trilhas_estado"] = {"A1": {"solo": True}}
    assert sorted(c["id"] for c in sq.clipes_no_render(n)) == ["a1", "v2"]   # solo deixa só a A em solo
    g = sq.grafo(n, {c["id"]: i for i, c in enumerate(n["clipes"])})
    assert ":a:0]" in g and g.count("overlay") == 1


def test_enquadramento_e_normalizado_e_vai_para_o_grafo():
    d = _doc()
    d["clipes"][0]["enquadramento"] = {"modo": "dividido", "divisao": 0.9, "camera": {"x": 0.7, "y": 0, "w": 0.3, "h": 0.3},
                                       "principal": {"x": 0.2, "y": 0, "w": 9, "h": 1}, "capa": {"tipo": "quadro"}}
    d["clipes"][1]["enquadramento"] = {"modo": "desfocado", "escala": 1.5, "posicao": 2}
    n = sq.normalizar(d)
    e = next(c for c in n["clipes"] if c["id"] == "v1")["enquadramento"]
    assert e["modo"] == "dividido" and e["divisao"] == 0.8 and "principal" not in e and e["camera"]["w"] == 0.3
    e2 = next(c for c in n["clipes"] if c["id"] == "v2")["enquadramento"]
    assert e2 == {"modo": "desfocado", "divisao": 0.35, "escala": 1.5, "posicao": 1.0}
    g = sq.grafo(n, {c["id"]: i for i, c in enumerate(n["clipes"])})
    assert "vstack=inputs=2" in g and "[cam0]" in g and "boxblur" in g and "[fundo1]" in g
    # o enquadramento manda o quadro inteiro: overlay centrado, a escala/posição do clipe não valem
    assert "overlay=x='(0.5)*W-w/2':y='(0.5)*H-h/2'" in g


def _gerar_lados(path, dur=2):
    """640x360: metade esquerda vermelha, direita azul."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:s=320x360:r=25:d={dur}",
                    "-f", "lavfi", "-i", f"color=c=blue:s=320x360:r=25:d={dur}", "-f", "lavfi", "-i", f"sine=f=440:d={dur}",
                    "-filter_complex", "[0:v][1:v]hstack=inputs=2[v]", "-map", "[v]", "-map", "2:a", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)], check=True)


def _pixel(video, t, x, y, tmp):
    raw = tmp / f"p{t}-{x}-{y}.raw"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                    "-vf", f"crop=2:2:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)], check=True)
    return list(raw.read_bytes()[:3])


@needs_ffmpeg
def test_enquadramento_de_verdade_no_formato_da_sequencia(tmp_path):
    _gerar_lados(tmp_path / "lados.mp4")
    d = sq.nova("Enq", "personalizado", largura=180, altura=320, fps=25)
    d["midias"] = [{"id": "m", "caminho": "lados.mp4", "duracao": 2, "largura": 640, "altura": 360, "audios": 1}]
    d["clipes"] = [
        # câmera = o canto esquerdo (vermelho) em cima; principal = a direita (azul) embaixo
        {"id": "a", "midia": "m", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 1,
         "enquadramento": {"modo": "dividido", "divisao": 0.4, "camera": {"x": 0, "y": 0, "w": 0.2, "h": 0.5},
                           "principal": {"x": 0.7, "y": 0, "w": 0.3, "h": 1}}},
        # inteiro na esquerda: tudo vermelho
        {"id": "b", "midia": "m", "tipo": "video", "trilha": 1, "inicio": 1, "entrada": 1, "saida": 2,
         "enquadramento": {"modo": "inteiro", "principal": {"x": 0, "y": 0, "w": 0.2, "h": 1}}},
    ]
    entrega = tmp_path / "Sequência - Enq v1"
    sq.renderizar({"doc": d, "entrega": str(entrega), "nome": "Enq.mp4", "midias": {"m": str(tmp_path / "lados.mp4")}})
    out = entrega / "Enq.mp4"
    topo, baixo = _pixel(out, 0.5, 90, 40, tmp_path), _pixel(out, 0.5, 90, 260, tmp_path)
    assert topo[0] > 200 and topo[2] < 60 and baixo[2] > 200 and baixo[0] < 60
    inteiro = _pixel(out, 1.5, 90, 260, tmp_path)
    assert inteiro[0] > 200 and inteiro[2] < 60


# ------------------------------------------------------------------ corte → sequência
def _projeto_com_corte(tmp_path, edicao=True):
    d = tmp_path / "Bruno"
    d.mkdir()
    client = clients_module.create_client(name="Bruno", path=str(d))
    project = projects_module.create_project(client["id"], "Live")
    base = Path(project["path"])
    (base / "Brutos").mkdir(exist_ok=True)
    bruto = base / "Brutos" / "live.mp4"
    _gerar_lados(bruto, dur=6)
    corte = base / "Corte 9x16 v2"
    corte.mkdir()
    dados = {"titulo": "Teste", "video": str(bruto), "segmentos": [{"inicio": 0.5, "fim": 2.0}, {"inicio": 3.0, "fim": 5.0}],
             "layout_editor": {"modo": "inteiro", "principal": {"x": 0.6, "y": 0, "w": 0.3, "h": 1}}, "gancho": "OLHA ISSO"}
    if edicao:
        dados["edicao"] = {"segmentos": [{"inicio": 1.0, "fim": 2.0, "layout": {"modo": "desfocado", "escala": 1.2}},
                                         {"inicio": 3.0, "fim": 4.5}],
                           "palavras": [{"texto": "oi", "t": 1.1, "fim": 1.4}, {"texto": "gente", "t": 3.2, "fim": 3.6, "junto": True}],
                           "estilo": {"tamanho": 90, "cores": ["#FF00AA"], "estatica": True}, "cor": {"brilho": 1.1, "contraste": 1, "saturacao": 1},
                           "gancho": "VEJA", "gancho_duracao": 2.5}
    (corte / "corte.json").write_text(json.dumps(dados))
    return project, base, bruto


@needs_ffmpeg
def test_corte_vira_sequencia_vertical_com_enquadramento_e_legenda(tmp_path):
    project, base, bruto = _projeto_com_corte(tmp_path)
    c = create_app().test_client()
    r = c.post("/api/sequencia/de-corte", json={"project_id": project["id"], "pasta": "Corte 9x16 v2"})
    assert r.status_code == 200, r.get_json()
    v = r.get_json()
    assert v["arquivo"] == "Corte 9x16 v2.clientela" and (base / v["arquivo"]).is_file() and v["novo"]
    doc = v["doc"]
    assert (doc["config"]["largura"], doc["config"]["altura"]) == (1080, 1920)
    [m] = doc["midias"]
    assert m["caminho"] == "Brutos/live.mp4" and [w["texto"] for w in m["palavras"]] == ["oi", "gente"] and m["palavras"][1]["junto"]
    vs = [cl for cl in doc["clipes"] if cl["tipo"] == "video"]
    assert [(cl["inicio"], cl["entrada"], cl["saida"]) for cl in vs] == [(0.0, 1.0, 2.0), (1.0, 3.0, 4.5)]
    assert vs[0]["enquadramento"]["modo"] == "desfocado"                       # o do trecho
    assert vs[1]["enquadramento"]["principal"]["x"] == 0.6                     # o padrão do corte
    assert vs[0]["cor"]["brilho"] == 1.1 and all(cl.get("vinculo") for cl in doc["clipes"])
    assert doc["legenda"]["ativa"] and doc["legenda"]["modo"] == "fixa" and doc["legenda"]["tamanho"] == 90
    assert doc["legenda"]["cor_destaque"] == "#FF00AA"
    assert v["gancho"] == "VEJA" and v["gancho_duracao"] == 2.5
    # de novo: abre o que já existe, sem sobrescrever o ajuste
    doc["nome"] = "Ajustado"
    editor.save_sequence(base, v["arquivo"], doc)
    again = c.post("/api/sequencia/de-corte", json={"project_id": project["id"], "pasta": "Corte 9x16 v2"}).get_json()
    assert again["doc"]["nome"] == "Ajustado" and again["novo"] is False and again["gancho"] == ""
    assert c.post("/api/sequencia/de-corte", json={"project_id": project["id"], "pasta": "../x"}).status_code == 400


@needs_ffmpeg
def test_corte_antigo_sem_edicao_e_sem_bruto(tmp_path):
    project, base, bruto = _projeto_com_corte(tmp_path, edicao=False)
    v = editor.sequence_from_cut(base, "Corte 9x16 v2")
    vs = [cl for cl in v["doc"]["clipes"] if cl["tipo"] == "video"]
    assert [(cl["entrada"], cl["saida"]) for cl in vs] == [(0.5, 2.0), (3.0, 5.0)]
    assert v["gancho"] == "OLHA ISSO" and not v["doc"]["legenda"]["ativa"]   # sem palavras, sem legenda
    (base / v["arquivo"]).unlink()
    bruto.unlink()
    with pytest.raises(editor.EditorError) as exc:
        editor.sequence_from_cut(base, "Corte 9x16 v2")
    assert exc.value.status == 409


def test_enquadramento_do_monitor_bate_com_o_render():
    """seqEnqFit/seqEnqGeom do index.html = autocorte._caixa no tamanho da sequência (sequencia.cadeia_enquadramento):
    as caixas em pixels que o ffmpeg recorta são as que o monitor desenha, a menos do arredondamento para par."""
    node = shutil.which("node")
    if not node:
        pytest.skip("sem node")
    from clientela import autocorte
    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text(encoding="utf-8")
    js = html[html.index("const SEQ_ENQ_CAMERA"):html.index("function seqEnqCamadas(")]
    casos = [
        ({"modo": "inteiro", "principal": {"x": 0.6, "y": 0, "w": 0.3, "h": 1}}, 1920, 1080, 1080, 1920),
        ({"modo": "inteiro"}, 1280, 720, 1080, 1350),
        ({"modo": "dividido", "divisao": 0.4, "camera": {"x": 0.7, "y": 0.1, "w": 0.25, "h": 0.3},
          "principal": {"x": 0.1, "y": 0, "w": 0.5, "h": 1}}, 1920, 1080, 1080, 1920),
    ]
    prog = js + "\nconst seq = {doc: {config: {}}};\nconst out = [];\n" + "".join(
        f"seq.doc.config = {{largura: {W}, altura: {H}}}; out.push(seqEnqGeom({json.dumps(e)}, {sw}, {sh}));\n"
        for e, sw, sh, W, H in casos) + "console.log(JSON.stringify(out));"
    geo = json.loads(subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True).stdout)
    for (e, sw, sh, W, H), g in zip(casos, geo):
        if e["modo"] == "dividido":
            topo = autocorte._par(H * e["divisao"])
            pares = [("camera", e["camera"], W / topo), ("principal", e["principal"], W / (H - topo))]
        else:
            pares = [("principal", e.get("principal"), W / H)]
        for nome, box, aspecto in pares:
            px = autocorte._caixa(box, sw, sh, aspecto)
            b = g[nome]
            assert abs(b["w"] * sw - px["w"]) <= 2 and abs(b["h"] * sh - px["h"]) <= 2, (nome, b, px)
            assert abs(b["x"] * sw - px["x"]) <= 2 and abs(b["y"] * sh - px["y"]) <= 2, (nome, b, px)
