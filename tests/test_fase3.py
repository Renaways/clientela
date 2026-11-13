"""Fase 3 do estúdio: Auto-SFX pela pasta de efeitos, painel de IDV do cliente, templates de gráfico com
expressões (desenhados pelo estúdio, quadros PNG no export), imagem/escala/posição/opacidade/cor por clipe
e o proxy com alfa dos inserts de motion."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app import create_app
from clientela import clients as clients_module
from clientela import editor, paths
from clientela import projects as projects_module
from clientela import sequencia as sq

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="sem ffmpeg")
RAIZ = Path(__file__).resolve().parent.parent


def _doc_cortes():
    d = sq.nova("Sonorizar")
    d["midias"] = [{"id": "a", "caminho": "a.mp4", "duracao": 20, "audios": 1},
                   {"id": "l", "caminho": "logo.png", "imagem": True}]
    d["clipes"] = [
        {"id": "1", "midia": "a", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 3},
        {"id": "2", "midia": "a", "tipo": "video", "trilha": 1, "inicio": 3, "entrada": 5, "saida": 7},
        {"id": "3", "midia": "a", "tipo": "video", "trilha": 1, "inicio": 5.3, "entrada": 8, "saida": 12},   # vão: não é corte
        {"id": "a1", "midia": "a", "tipo": "audio", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 9},
        {"id": "4", "midia": "l", "tipo": "video", "trilha": 2, "inicio": 1.2, "entrada": 0, "saida": 4},
        {"id": "5", "midia": "l", "tipo": "video", "trilha": 2, "inicio": 3.4, "entrada": 0, "saida": 1},   # perto demais do corte em 3
    ]
    return sq.normalizar(d)


# ------------------------------------------------------------------ modelo
def test_imagem_grafico_e_visual_por_clipe():
    d = sq.nova("V")
    d["midias"] = [{"id": "l", "caminho": "logo.png", "imagem": True, "duracao": 9, "audios": 2}]
    d["clipes"] = [
        {"id": "i", "midia": "l", "tipo": "video", "trilha": 2, "inicio": 1, "entrada": 0, "saida": 30,
         "escala": 9, "pos_x": 0.85, "opacidade": 1.7, "cor": {"brilho": 1.2}},
        {"id": "g", "tipo": "grafico", "trilha": 1, "inicio": 0, "entrada": 3, "saida": 4, "render": "ab" * 10,
         "grafico": {"template": "titulo", "params": {"texto": "Oi", "obj": {"x": 1}}, "def": {"nome": "Título", "camadas": []}}},
    ]
    n = sq.normalizar(d)
    m = n["midias"][0]
    assert (m["imagem"], m["duracao"], m["audios"], m["video"]) == (True, 0.0, 0, True)   # imagem não tem duração nem som
    i = next(c for c in n["clipes"] if c["id"] == "i")
    assert i["saida"] == 30 and i["escala"] == 4.0 and i["opacidade"] == 1.0 and i["pos_x"] == 0.85
    assert i["cor"] == {"brilho": 1.2, "contraste": 1.0, "saturacao": 1.0}
    g = next(c for c in n["clipes"] if c["id"] == "g")
    assert g["entrada"] == 0 and g["saida"] == 4 and "midia" not in g and g["render"] == "ab" * 10
    assert g["grafico"]["params"] == {"texto": "Oi"}   # só valores simples
    grande = {"camadas": [], "x": "y" * (sq.MAX_GRAFICO + 1)}
    for estraga in (lambda c: c.pop("grafico"), lambda c: c.update(trilha=3),
                    lambda c: c["grafico"].__setitem__("def", grande)):
        r = json.loads(json.dumps(d))
        estraga(r["clipes"][1])
        with pytest.raises(ValueError):
            sq.normalizar(r)


def test_grafo_com_escala_posicao_cor_opacidade_imagem_e_grafico():
    d = sq.nova("V", largura=1280, altura=720, fps=25)
    d["midias"] = [{"id": "v", "caminho": "v.mp4", "duracao": 5, "audios": 0}, {"id": "l", "caminho": "l.png", "imagem": True}]
    d["clipes"] = [
        {"id": "a", "midia": "v", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 5, "cor": {"brilho": 1.2, "saturacao": 0}},
        {"id": "b", "midia": "l", "tipo": "video", "trilha": 2, "inicio": 1, "entrada": 0, "saida": 2, "escala": 0.25, "pos_x": 0.9, "pos_y": 0.1, "opacidade": 0.5},
        {"id": "g", "tipo": "grafico", "trilha": 3, "inicio": 2, "entrada": 0, "saida": 1, "grafico": {"def": {"camadas": []}}},
    ]
    d["trilhas"]["video"] = 3
    d = sq.normalizar(d)
    cmd = sq.comando(d, {"v": "/m/v.mp4", "l": "/m/l.png"}, "/o.mp4", {"g": "/q/g"})
    entradas = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-i"]
    assert entradas == ["/m/v.mp4", "/m/l.png", "/q/g/%05d.png"]
    assert cmd[cmd.index("/m/l.png") - 7:cmd.index("/m/l.png")] == ["-loop", "1", "-framerate", "25", "-t", "2", "-i"]
    g = cmd[cmd.index("-filter_complex") + 1]
    assert "colorchannelmixer=rr=1.200:gg=1.200:bb=1.200,eq=contrast=1.000:saturation=0.000" in g
    assert "[1:v:0]trim=duration=2,setpts=PTS-STARTPTS+1/TB,scale=320:180:force_original_aspect_ratio=decrease" in g
    assert "colorchannelmixer=aa=0.500" in g and "overlay=x='(0.9)*W-w/2':y='(0.1)*H-h/2'" in g
    assert "[2:v:0]trim=duration=1,setpts=PTS-STARTPTS+2/TB" in g


def _png(path, cor, w, h, alpha=False):
    src = f"color=c={cor}:s={w}x{h}" + (",format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lt(X,W/2),255,0)'" if alpha else "")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-frames:v", "1", str(path)], check=True)


def _cor(video, t, x, y, w, h, tmp):
    raw = tmp / "px.raw"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                    "-vf", f"format=rgb24,crop=1:1:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)], check=True)
    return list(raw.read_bytes()[:3])


@needs_ffmpeg
def test_render_de_logo_com_alfa_e_grafico_pre_renderizado(tmp_path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25:d=3",
                    "-c:v", "libx264", "-preset", "ultrafast", str(tmp_path / "fundo.mp4")], check=True)
    _png(tmp_path / "logo.png", "yellow", 200, 100, alpha=True)        # metade esquerda opaca, direita transparente
    quadros = tmp_path / "graf"
    quadros.mkdir()
    for n in range(26):
        _png(quadros / f"{n:05d}.png", "red", 320, 180)
    (quadros / "pronto.json").write_text("{}")
    d = sq.nova("Alfa", largura=320, altura=180, fps=25)
    d["trilhas"]["video"] = 3
    d["midias"] = [{"id": "f", "caminho": "fundo.mp4", "duracao": 3, "audios": 0}, {"id": "l", "caminho": "logo.png", "imagem": True}]
    d["clipes"] = [
        {"id": "a", "midia": "f", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 3},
        # logo a 50% do quadro (160x90 cabendo 200x100 → 160x80), centro em (0.5, 0.5)
        {"id": "b", "midia": "l", "tipo": "video", "trilha": 2, "inicio": 0, "entrada": 0, "saida": 2, "escala": 0.5},
        {"id": "g", "tipo": "grafico", "trilha": 3, "inicio": 2, "entrada": 0, "saida": 1, "escala": 0.5, "pos_x": 0.25, "pos_y": 0.25,
         "grafico": {"def": {"nome": "Bloco", "camadas": []}}},
    ]
    entrega = tmp_path / "Sequência - Alfa v1"
    sq.renderizar({"doc": d, "entrega": str(entrega), "nome": "Alfa.mp4", "graficos": {"g": str(quadros)},
                   "midias": {"f": str(tmp_path / "fundo.mp4"), "l": str(tmp_path / "logo.png")}})
    out = entrega / "Alfa.mp4"
    amarelo, azul = _cor(out, 1, 120, 90, 320, 180, tmp_path), _cor(out, 1, 200, 90, 320, 180, tmp_path)
    assert amarelo[0] > 200 and amarelo[2] < 80          # metade opaca do logo
    assert azul[2] > 180 and azul[0] < 60                # metade transparente: o fundo aparece
    assert _cor(out, 2.5, 80, 45, 320, 180, tmp_path)[0] > 200      # gráfico no quadrante de cima à esquerda
    assert _cor(out, 2.5, 240, 135, 320, 180, tmp_path)[2] > 180    # e só lá
    with pytest.raises(sq.Falha):   # gráfico sem quadros prontos não renderiza
        sq.renderizar({"doc": d, "entrega": str(tmp_path / "Sequência - Alfa v2"), "nome": "Alfa.mp4", "graficos": {},
                       "midias": {"f": str(tmp_path / "fundo.mp4"), "l": str(tmp_path / "logo.png")}})


# ------------------------------------------------------------------ Auto-SFX
def test_categorias_pelo_nome_do_arquivo_e_da_pasta():
    casos = {"Whip (3).mp3": "whoosh", "EPIC Swish (6).wav": "whoosh", "transition Swoosh/deep-whoosh-1.wav": "whoosh",
             "Risers/Riser 4.mp3": "riser", "Reverse riser.wav": "riser", "click 03.wav": "clique",
             "Technology Electronic Button Beep Digital Camera 04.wav": "clique", "Nikon Shutter Click.mp3": "clique",
             "Cinematic Impact Boom.wav": "impacto", "Glitch 2.wav": "glitch", "Notification Ding.wav": "ding",
             "Technology Electronic Video Camera Eject Tape 01.wav": "outros", "Transição Rápida.wav": "whoosh"}
    assert {k: sq.categoria_sfx(k) for k in casos} == casos


def test_eventos_sao_cortes_secos_e_entradas_por_cima_com_intervalo():
    ev = sq.eventos_sfx(_doc_cortes(), {"minimo": 0.8})
    assert [(e["t"], e["motivo"], e["categoria"]) for e in ev] == [(1.2, "entrada", "clique"), (3.0, "corte", "whoosh")]
    assert sq.eventos_sfx(_doc_cortes(), {"corte": "", "entrada": ""}) == []


def _lib():
    return [{"caminho": f"/fx/Whip ({k}).mp3", "nome": f"Whip ({k}).mp3", "categoria": "whoosh", "duracao": 0.5} for k in range(1, 4)] + [
        {"caminho": "/fx/longo.wav", "nome": "longo.wav", "categoria": "whoosh", "duracao": 11.0},
        {"caminho": "/fx/click.wav", "nome": "click.wav", "categoria": "clique", "duracao": 0.12},
        {"caminho": "/fx/Riser.wav", "nome": "Riser.wav", "categoria": "riser", "duracao": 3.0}]


def test_plano_ancora_cada_categoria_e_cria_trilha_so_de_efeitos():
    plano = sq.planejar_sfx(_doc_cortes(), _lib(), {"minimo": 0.8, "ganho": -12})
    assert plano["trilha"] == 2 and plano["trilhas_audio"] == 2          # A2 estava vazia
    cl = {c["motivo"]: c for c in plano["clipes"]}
    assert cl["entrada · clique"]["inicio"] == 1.2                          # clique começa na entrada
    w = cl["corte · whoosh"]
    assert w["inicio"] == pytest.approx(2.75) and w["saida"] == 0.5          # whoosh centrado no corte (3 s)
    assert all(c["sfx"] and c["ganho"] == -12 and c["tipo"] == "audio" for c in plano["clipes"])
    usados = {m["caminho"] for m in plano["midias"]}
    assert "/fx/longo.wav" not in usados and all(m["sfx"] for m in plano["midias"])   # coleção longa fica de fora
    # riser termina no evento; a cabeça que passaria do zero é cortada
    r = sq.planejar_sfx(_doc_cortes(), _lib(), {"corte": "riser", "entrada": ""})["clipes"][0]
    assert r["inicio"] == 0.0 and r["entrada"] == 0.0 and r["saida"] == 3.0
    # só tem o arquivo longo: entra o meio dele (o pico do whoosh), centrado no corte
    so_longo = [a for a in _lib() if a["nome"] == "longo.wav"]
    w = sq.planejar_sfx(_doc_cortes(), so_longo, {"entrada": "", "max_dur": 1.5})["clipes"][0]
    assert (w["inicio"], w["entrada"], w["saida"]) == (2.25, 4.75, 6.25)
    # o resultado, aplicado, é uma sequência válida (normalizar aceita) e gerar de novo não empilha
    d = _doc_cortes()
    d["midias"] += plano["midias"]
    d["clipes"] += plano["clipes"]
    d = sq.normalizar(d)
    de_novo = sq.planejar_sfx(d, _lib(), {"minimo": 0.8})
    assert de_novo["trilha"] == 2 and len(de_novo["clipes"]) == len(plano["clipes"]) and de_novo["midias"] == []


def test_plano_usa_trilha_nova_quando_todas_tem_dialogo_e_nao_repete_seguido():
    d = _doc_cortes()
    d["clipes"].append({**next(c for c in d["clipes"] if c["id"] == "a1"), "id": "a2", "trilha": 2})
    d = sq.normalizar(d)
    p = sq.planejar_sfx(d, _lib(), {"entrada": "whoosh", "minimo": 0.1})
    assert p["trilha"] == 3 and p["trilhas_audio"] == 3
    nomes = [next(m["nome"] for m in p["midias"] if m["id"] == c["midia"]) for c in p["clipes"]]
    assert all(a != b for a, b in zip(nomes, nomes[1:]))
    # dois efeitos não se atropelam na trilha
    for a, b in zip(p["clipes"], p["clipes"][1:]):
        assert a["inicio"] + a["saida"] - a["entrada"] <= b["inicio"] + 1e-6
    assert sq.planejar_sfx(d, [], {})["faltou"] == ["clique", "whoosh"]


def test_biblioteca_de_efeitos_pela_config(tmp_path):
    pasta = tmp_path / "02 - FX"
    (pasta / "Risers").mkdir(parents=True)
    for nome in ("Whip (1).mp3", "Risers/Riser 1.wav", ".DS_Store", "leia.txt"):
        (pasta / nome).write_bytes(b"x")
    editor.save_config({"sfx": {"pasta_mac": str(pasta), "pasta_windows": str(pasta), "ganho": -9}})
    lib = editor.sfx_library()
    assert lib["existe"] and [a["rel"] for a in lib["arquivos"]] == ["Risers/Riser 1.wav", "Whip (1).mp3"]
    assert lib["categorias"] == {"riser": 1, "whoosh": 1}
    assert (paths.EDITOR_DIR / editor.SFX_INDEX).is_file()      # duração fica em cache
    editor.save_config({"sfx": {"pasta_mac": str(tmp_path / "nao"), "pasta_windows": str(tmp_path / "nao"), "ganho": -10}})
    assert editor.sfx_library()["existe"] is False
    with pytest.raises(editor.EditorError) as exc:
        editor.sfx_plan(_doc_cortes())
    assert exc.value.status == 409


# ------------------------------------------------------------------ IDV, templates, quadros
def _cliente_com_perfil(tmp_path):
    d = tmp_path / "Cliente"
    d.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(d))
    perfil = d / "_Estagiário"
    (perfil / "assets" / "logos").mkdir(parents=True)
    (perfil / "assets" / "fonts").mkdir(parents=True)
    (perfil / "assets" / "logos" / "logo-claro.png").write_bytes(b"png")
    (perfil / "assets" / "fonts" / "Marca-Bold.otf").write_bytes(b"otf")
    (perfil / "assets" / "fonts" / "solta.ttf").write_bytes(b"ttf")
    (perfil / "marca.json").write_text(json.dumps({"cores": {"acento": "#FFB800"}, "logos": {"claro": "logo-claro.png"},
                                                   "tipografia": {"fontes": [{"familia": "Marca", "peso": 700, "arquivo": "Marca-Bold.otf"}]}}))
    (d / "00 - Projeto" / "Elementos").mkdir(parents=True)
    (d / "00 - Projeto" / "Elementos" / "seta.png").write_bytes(b"png")
    (d / "00 - Projeto" / "notas.txt").write_text("x")
    project = projects_module.create_project(client["id"], "Vídeo")
    pdir = Path(project["path"])
    (pdir / "Motion - Inserts v1").mkdir()
    (pdir / "Motion - Inserts v1" / "00-05-00 - 1 Dado (camada FRENTE).mov").write_bytes(b"mov")
    return client, project, perfil


def test_idv_do_projeto_junta_perfil_pastas_00_e_motion(tmp_path):
    client, project, perfil = _cliente_com_perfil(tmp_path)
    c = create_app().test_client()
    v = c.get(f"/api/sequencia/idv?project_id={project['id']}").get_json()
    assert v["client_id"] == client["id"] and v["marca"]["cores"]["acento"] == "#FFB800"
    grupos = {g["nome"]: [i["nome"] for i in g["itens"]] for g in v["grupos"]}
    assert grupos == {"Logos": ["logo-claro.png"], "00 - Projeto": ["seta.png"],
                      "Motion - Inserts v1": ["00-05-00 - 1 Dado (camada FRENTE).mov"]}
    assert [(f["familia"], f["peso"]) for f in v["fontes"]] == [("Marca", 700), ("solta", 400)]
    r = c.get(f"/api/sequencia/fonte?client_id={client['id']}&arquivo=Marca-Bold.otf")
    assert r.status_code == 200 and r.data == b"otf"
    assert c.get(f"/api/sequencia/fonte?client_id={client['id']}&arquivo=../marca.json").status_code == 404
    assert c.get("/api/sequencia/idv").get_json()["grupos"] == []      # Downloads: sem cliente
    # pacote de templates do cliente
    t = {"nome": "Título da casa", "dur": 3, "params": [], "camadas": [{"tipo": "texto", "texto": "Oi", "x": "W/2"}]}
    salvo = c.post("/api/sequencia/templates", json={"client_id": client["id"], "template": t}).get_json()
    assert salvo["id"] == "cliente:Título da casa" and (perfil / "templates" / "sequencia" / "Título da casa.json").is_file()
    assert [x["nome"] for x in c.get(f"/api/sequencia/templates?client_id={client['id']}").get_json()] == ["Título da casa"]
    assert c.post("/api/sequencia/templates", json={"client_id": client["id"], "template": {"nome": "x"}}).status_code == 400


def test_quadros_do_grafico(tmp_path):
    c = create_app().test_client()
    key = "0123456789abcdef0123"
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    assert c.get(f"/api/sequencia/grafico/{key}").get_json() == {"pronto": False, "quadros": 0}
    assert c.put(f"/api/sequencia/grafico/{key}/0", data=b"GIF89a").status_code == 400
    assert c.put("/api/sequencia/grafico/..%2Fx/0", data=png).status_code in (400, 404)
    for n in range(2):
        assert c.put(f"/api/sequencia/grafico/{key}/{n}", data=png).status_code == 200
    assert c.post(f"/api/sequencia/grafico/{key}/pronto", json={"quadros": 3}).status_code == 409   # falta o 2
    assert c.post(f"/api/sequencia/grafico/{key}/pronto", json={"quadros": 2}).get_json() == {"pronto": True, "quadros": 2}
    assert not [p for p in (paths.EDITOR_DIR / editor.GRAFICOS_DIR / key).iterdir() if p.name.startswith(".")]


def test_export_recusa_grafico_sem_quadros(tmp_path):
    client, project, perfil = _cliente_com_perfil(tmp_path)
    base = Path(project["path"])
    v = editor.create_sequence(base, "Com gráfico")
    doc = v["doc"]
    doc["clipes"] = [{"id": "g", "tipo": "grafico", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2,
                      "grafico": {"def": {"nome": "Título", "camadas": []}}}]
    editor.save_sequence(base, v["arquivo"], doc)
    with pytest.raises(editor.EditorError) as exc:
        editor.start_sequence_job(project["id"], None, v["arquivo"])
    assert exc.value.status == 409 and "Título" in str(exc.value)


@needs_ffmpeg
def test_insert_com_alfa_ganha_proxy_webm_com_alfa(tmp_path):
    mov = tmp_path / "insert.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red@0.5:s=320x180:d=1,format=yuva444p10le",
                    "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(mov)], check=True)
    plano = tmp_path / "plano.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x180:d=1", "-c:v", "libx264",
                    "-preset", "ultrafast", str(plano)], check=True)
    assert editor.has_alpha(mov) and not editor.has_alpha(plano)
    assert editor.request_proxy(None, str(mov))["estado"] == "gerando"
    deadline = time.time() + 60
    while editor.proxy_status(mov)["estado"] == "gerando" and time.time() < deadline:
        time.sleep(0.2)
    alvo, mime = editor.video_file(None, str(mov), proxy=True)
    assert alvo.suffix == ".webm" and mime == "video/webm"
    fmt = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name:stream_tags=alpha_mode", "-of", "json", str(alvo)],
                         capture_output=True, text=True, check=True).stdout
    assert '"vp9"' in fmt and '"alpha_mode": "1"' in fmt


def test_expressoes_do_estudio_no_node():
    """O motor de expressões dos templates (index.html): contas, precedência, funções e — o que importa num
    arquivo que vem do Drive — nada além de números, variáveis e as funções liberadas."""
    node = shutil.which("node")
    if not node:
        pytest.skip("sem node")
    html = (RAIZ / "templates" / "index.html").read_text(encoding="utf-8")
    js = html[html.index("/* ---------- expressões"):html.index("/* ---------- templates de gráfico")]
    prog = js + r"""
const c = {time: 1, dur: 4, W: 1920, H: 1080, fps: 30, frame: 30, progress: .25, entra: 1, sai: 1, de: 0, ate: 1000};
const ok = [['1+2*3', 7], ['2^3^2', 512], ['10-4-3', 3], ['W/2', 960], ['time > 0.5 ? 10 : 20', 10], ['lerp(de, ate, 0.5)', 500],
  ['easeOut(0.5)', 0.875], ['linear(time, 0, 2, 0, 100)', 50], ['ease(time, 0, 2, 0, 10)', 5], ['1/0', 0], ['mod(-1, 3)', 2]]
  .map(([e, v]) => Math.abs(exprCompilar(e, ['de', 'ate'])(c) - v) < 1e-9);
const ruins = ['alert(1)', 'constructor(1)', 'toString()', 'this.x', 'window', '__proto__', 'W[0]', "'x'", 'fetch("/api")', '1 +', 'x = 1']
  .map(e => !!exprErro(e));
console.log(JSON.stringify({ok, ruins, wig: [exprCompilar('wiggle(2, 10)')({...c, time: 1.3}), exprCompilar('wiggle(2, 10)')({...c, time: 1.3})]}));
"""
    out = json.loads(subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True).stdout)
    assert all(out["ok"]) and all(out["ruins"])
    assert out["wig"][0] == out["wig"][1] and abs(out["wig"][0]) <= 10   # determinístico: o export bate com a prévia


@needs_ffmpeg
def test_miniatura_de_jpeg_e_png(tmp_path):
    """O painel IDV pede miniatura de logos JPEG: com "-ss 0" antes de um JPEG único o ffmpeg não escrevia nada."""
    for nome in ("logo.jpg", "logo.png"):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=orange:s=640x360", "-frames:v", "1",
                        str(tmp_path / nome)], check=True)
        alvo = editor.frame_path(None, str(tmp_path / nome), 0, 160)
        assert alvo.is_file() and alvo.stat().st_size > 0
