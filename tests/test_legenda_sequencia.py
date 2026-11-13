"""O que completou o plano do estúdio depois das quatro fases: a legenda da sequência (palavras dos takes
no tempo da sequência, telas, .ass no tamanho da sequência, queimada via PNGs do navegador), os estilos
de legenda do cliente, a passada semântica por LLM, o relatório de edições do pipeline e o Auto-SFX
nas trocas de cena."""

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from clientela import autocorte, editor
from clientela import clients as clients_module
from clientela import projects as projects_module
from clientela import sequencia as sq

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="sem ffmpeg")


def _doc_fala(**leg):
    d = sq.nova("Fala", "completo", largura=1920, altura=1080, fps=30)
    palavras = [{"texto": t, "t": i * 0.5, "fim": i * 0.5 + 0.4} for i, t in enumerate(
        "olá pessoal hoje vamos falar de São Paulo e do Rio de Janeiro".split())]
    d["midias"] = [{"id": "a", "caminho": "a.mp4", "duracao": 10, "audios": 1, "palavras": palavras},
                   {"id": "fx", "caminho": "fx.wav", "duracao": 1, "video": False, "audios": 1, "sfx": True,
                    "palavras": [{"texto": "não", "t": 0, "fim": 0.5}]}]
    d["clipes"] = [
        # o take entra em 2 s na sequência, usando de 1 s a 5 s dele
        {"id": "v", "midia": "a", "tipo": "video", "trilha": 1, "inicio": 2, "entrada": 1, "saida": 5, "vinculo": "l"},
        {"id": "au", "midia": "a", "tipo": "audio", "trilha": 1, "inicio": 2, "entrada": 1, "saida": 5, "vinculo": "l"},
        {"id": "s", "midia": "fx", "tipo": "audio", "trilha": 2, "inicio": 0, "entrada": 0, "saida": 1, "sfx": True},
    ]
    d["legenda"] = {"ativa": True, **leg}
    return sq.normalizar(d)


# ------------------------------------------------------------------ documento
def test_normalizar_guarda_pastas_legenda_e_palavras():
    d = sq.nova("X", "vertical")
    d["pastas"] = ["Entrevista", "Entrevista", "  ", "B-roll"]
    d["midias"] = [{"id": "m", "caminho": "m.mp4", "duracao": 3, "pasta": "Takes bons",
                    "palavras": [{"texto": "oi", "t": 1, "fim": 0.5, "quebra": True}, {"texto": " ", "t": 1, "fim": 2},
                                 {"t": "x"}]}]
    d["legenda"] = {"ativa": True, "modo": "neon", "tamanho": 9999, "margem": -50, "cor_destaque": "amarelo",
                    "palavras": 0, "render": "não é hash"}
    n = sq.normalizar(d)
    assert n["pastas"] == ["Entrevista", "B-roll", "Takes bons"]      # a pasta da mídia entra na lista do bin
    assert n["midias"][0]["palavras"] == [{"texto": "oi", "t": 1.0, "fim": 1.0, "quebra": True}]
    leg = n["legenda"]
    assert leg["modo"] == "animada" and leg["tamanho"] == 400 and leg["margem"] == -1 and leg["palavras"] == 1
    assert leg["cor_destaque"] == sq.LEGENDA_PADRAO["cor_destaque"] and "render" not in leg
    # sem legenda no arquivo: desligada, com os padrões
    assert sq.normalizar(sq.nova("Y", "completo"))["legenda"]["ativa"] is False


def test_tamanho_e_margem_automaticos_seguem_o_quadro():
    leg = sq.normalizar(sq.nova("Y", "completo"))["legenda"]
    assert sq.legenda_auto(leg, 1920, 1080) == (81, 313)
    assert sq.legenda_auto(leg, 1080, 1920) == (81, 557)     # o lado menor manda no tamanho da letra
    assert sq.legenda_auto({**leg, "tamanho": 60, "margem": 100}, 1080, 1920) == (60, 100)


def test_palavras_no_tempo_da_sequencia_telas_e_ass():
    d = _doc_fala()
    ws = sq.palavras_da_sequencia(d)
    # só as palavras cujo meio cai em 1–5 s do take ("pessoal", 0,5–0,9, fica de fora), deslocadas +1 s;
    # o efeito sonoro não fala
    assert [w["texto"] for w in ws][:2] == ["hoje", "vamos"]
    assert ws[0]["t"] == 2.0 and ws[0]["midia"] == "a" and ws[0]["i"] == 2
    assert "não" not in [w["texto"] for w in ws] and ws[-1]["fim"] <= 6
    cues = sq.cues_da_sequencia(d)
    textos = [" ".join(w["texto"] for w in c["palavras"]) for c in cues]
    assert any("São Paulo" in t for t in textos)             # termo composto não quebra
    ass = sq.ass_da_sequencia(d)
    assert "PlayResX: 1920" in ass and "PlayResY: 1080" in ass and "\\k" in ass
    fixa = sq.ass_da_sequencia(_doc_fala(modo="fixa"))
    assert "\\k" not in fixa and "Dialogue:" in fixa


# ------------------------------------------------------------------ legenda queimada
def test_estados_da_legenda_viram_lista_do_concat(tmp_path):
    key = "ab" * 16
    d = editor._graphic_dir(key)
    d.mkdir(parents=True)
    for n in range(3):
        (d / f"{n:05d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(editor.EditorError) as exc:
        editor.finish_graphic(key, 4)
    assert exc.value.status == 409                           # falta o quadro 3
    for ruim in ([], [{"n": 5, "duracao": 1}], [{"n": 0, "duracao": 0}], [{"n": "x"}]):
        with pytest.raises(editor.EditorError):
            editor.finish_graphic(key, 3, ruim)
    st = editor.finish_graphic(key, 3, [{"n": 0, "duracao": 0.5}, {"n": 2, "duracao": 1.25}])
    assert st["pronto"] and st["quadros"] == 3
    linhas = (d / "lista.ffconcat").read_text().splitlines()
    assert linhas == ["ffconcat version 1.0", "file '00000.png'", "duration 0.5000", "file '00002.png'",
                      "duration 1.2500", "file '00002.png'"]


def _gerar(path, cor, w, h, dur):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={cor}:s={w}x{h}:r=25:d={dur}",
                    "-f", "lavfi", "-i", f"sine=f=440:d={dur}", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(path)], check=True)


def _pixel(video, t, x, y, tmp):
    raw = tmp / f"p{t}.raw"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                    "-vf", f"crop=2:2:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)], check=True)
    return list(raw.read_bytes()[:3])


@needs_ffmpeg
def test_legenda_queimada_de_verdade_sem_travar(tmp_path):
    """Os PNGs com alfa de cada estado viram um .mov à parte antes do render: com o concat direto no grafo
    grande o ffmpeg 8.1 travava em livelock (100% de CPU, sem progresso) na maioria das vezes."""
    _gerar(tmp_path / "azul.mp4", "blue", 320, 180, 3)
    key = "cd" * 16
    d = editor._graphic_dir(key)
    d.mkdir(parents=True)
    # estado 0: nada (transparente); estado 1: uma faixa branca opaca na metade de baixo
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black@0.0:s=320x180,format=rgba",
                    "-frames:v", "1", str(d / "00000.png")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-filter_complex",
                    "color=c=black@0.0:s=320x180,format=rgba[a];color=c=white:s=320x40,format=rgba[b];[a][b]overlay=0:120",
                    "-frames:v", "1", str(d / "00001.png")], check=True)
    editor.finish_graphic(key, 2, [{"n": 0, "duracao": 1.0}, {"n": 1, "duracao": 2.0}])
    doc = sq.nova("Leg", "completo", largura=320, altura=180, fps=25)
    doc["midias"] = [{"id": "a", "caminho": "azul.mp4", "duracao": 3, "audios": 1}]
    doc["clipes"] = [{"id": "v", "midia": "a", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 3},
                     {"id": "au", "midia": "a", "tipo": "audio", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 3}]
    doc["legenda"] = {"ativa": True, "queimar": True}
    entrega = tmp_path / "Sequência - Leg v1"
    args = {"doc": doc, "entrega": str(entrega), "nome": "Leg.mp4", "midias": {"a": str(tmp_path / "azul.mp4")},
            "legenda_dir": str(d)}
    resultado = {}
    th = threading.Thread(target=lambda: resultado.update(msg=sq.renderizar(args)), daemon=True)
    th.start()
    th.join(90)
    if th.is_alive():
        subprocess.run(["pkill", "-9", "-f", str(entrega)], check=False)
        pytest.fail("o render com legenda queimada travou")
    assert "Leg.mp4" in resultado["msg"]
    out = entrega / "Leg.mp4"
    assert _pixel(out, 0.5, 160, 140, tmp_path)[2] > 150      # antes: o azul do take
    branco = _pixel(out, 2.0, 160, 140, tmp_path)
    assert min(branco) > 200                                  # depois: a faixa da legenda por cima
    assert _pixel(out, 2.0, 160, 40, tmp_path)[2] > 150       # e só onde ela foi desenhada
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True, check=True).stdout)
    assert dur == pytest.approx(3.0, abs=0.15)
    # sem a lista o job recusa antes do ffmpeg
    (d / "lista.ffconcat").unlink()
    with pytest.raises(sq.Falha):
        sq.renderizar({**args, "entrega": str(tmp_path / "Sequência - Leg v2")})
    assert not (tmp_path / "Sequência - Leg v2").exists()


# ------------------------------------------------------------------ estilos e passada semântica
def _cliente(tmp_path):
    d = tmp_path / "Aurora"
    d.mkdir()
    return clients_module.create_client(name="Aurora", path=str(d)), d


def test_estilos_de_legenda_no_pacote_do_cliente(tmp_path):
    client, d = _cliente(tmp_path)
    assert editor.list_caption_styles(client["id"]) == []
    with pytest.raises(editor.EditorError):
        editor.save_caption_style(client["id"], {"nome": "  "})
    with pytest.raises(editor.EditorError) as exc:
        editor.save_caption_style(None, {"nome": "X"})
    assert exc.value.status == 409
    salvo = editor.save_caption_style(client["id"], {"nome": "Reels: amarelo", "fonte": "Hando", "cor_destaque": "#FFAA00",
                                                     "palavras": 3, "lixo": "fora"})
    assert salvo["id"].startswith("cliente:") and "lixo" not in salvo
    arquivos = list((d / "_Estagiário" / "templates" / "legendas").glob("*.json"))
    assert len(arquivos) == 1 and ":" not in arquivos[0].name
    [e] = editor.list_caption_styles(client["id"])
    assert e["nome"] == "Reels: amarelo" and e["fonte"] == "Hando" and e["palavras"] == 3


def _palavras():
    return [{"texto": t} for t in "o Banco Central subiu os juros mas o mercado".split()]


def test_passada_semantica_pela_openrouter_sem_trava(monkeypatch):
    editor.save_config({"corte": {"provedor_llm": "openrouter"}})
    vistos = []

    def falso(prompt, cfg, timeout=300, imagens=()):
        vistos.append(prompt)
        return {"juntos": [1, 1, 99, "x"], "quebras": [2, 6]}, 0.002

    monkeypatch.setattr(autocorte, "llm_openrouter", falso)
    r = editor.caption_semantics(_palavras(), 3)
    # número fora da lista é ignorado; quebra logo depois de um "junto" perde para o junto
    assert r == {"juntos": [1], "quebras": [6], "custo": 0.002, "provedor": "openrouter"}
    assert "1:Banco" in vistos[0] and "~3 palavras" in vistos[0]
    with pytest.raises(editor.EditorError):
        editor.caption_semantics([{"texto": " "}])


def test_passada_semantica_pelo_claude_pede_confirmacao(monkeypatch):
    editor.save_config({"corte": {"provedor_llm": "claude"}})
    monkeypatch.setattr(editor, "find_claude", lambda: "/bin/claude-falso")
    chamadas = []
    monkeypatch.setattr(autocorte, "llm_claude", lambda *a, **k: (chamadas.append(k), ({"juntos": [], "quebras": [3]}, 0.01))[1])
    with pytest.raises(editor.EditorError) as exc:
        editor.caption_semantics(_palavras())
    assert exc.value.status == 428 and not chamadas
    r = editor.caption_semantics(_palavras(), confirmar_tokens=True)
    assert r["quebras"] == [3] and r["provedor"] == "claude"
    assert chamadas[0]["schema"] == editor.SEMANTICA_SCHEMA and "CLAUDECODE" not in chamadas[0]["env"]


def test_lotes_grandes_viram_varias_chamadas(monkeypatch):
    editor.save_config({"corte": {"provedor_llm": "openrouter"}})
    n = []
    monkeypatch.setattr(autocorte, "llm_openrouter", lambda p, cfg, **k: (n.append(1), ({"juntos": [], "quebras": []}, 0))[1])
    editor.caption_semantics([{"texto": "a"}] * (editor.SEMANTICA_LOTE + 1))
    assert len(n) == 2


# ------------------------------------------------------------------ pipeline
def test_relatorio_de_edicoes_do_projeto(tmp_path):
    client, d = _cliente(tmp_path)
    project = projects_module.create_project(client["id"], "Reels")
    base = Path(project["path"])
    v = editor.create_sequence(base, "Final", "completo", 640, 360, 30)
    doc = v["doc"]
    doc["midias"] = [{"id": "m", "caminho": "Brutos/sumiu.mp4", "duracao": 4, "audios": 1}]
    doc["clipes"] = [{"id": "c", "midia": "m", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 4}]
    editor.save_sequence(base, v["arquivo"], doc)
    r = editor.project_editions(project["id"])
    [s] = r["sequencias"]
    assert s["arquivo"] == "Final.clientela" and s["offline"] == 1 and s["takes"] == 1 and s["exports"] == []
    assert r["cortes"] == [] and r["exports"] == 0
    with pytest.raises(editor.EditorError) as exc:
        editor.project_editions("nao:existe")
    assert exc.value.status == 404


# ------------------------------------------------------------------ Auto-SFX nas trocas de cena
def test_eventos_de_movimento_dentro_dos_takes_de_v1():
    d = sq.nova("S", "completo")
    d["midias"] = [{"id": "a", "caminho": "a.mp4", "duracao": 20, "audios": 1}]
    d["clipes"] = [{"id": "v", "midia": "a", "tipo": "video", "trilha": 1, "inicio": 10, "entrada": 2, "saida": 12}]
    d = sq.normalizar(d)
    mov = {"a": [1.0, 2.1, 5.0, 11.9, 15.0]}   # fora do trecho, colado nas bordas, dentro, colado, fora
    assert sq.eventos_sfx(d, {"corte": "", "entrada": "", "movimento": "impacto"}, mov) == [
        {"t": 13.0, "motivo": "movimento", "categoria": "impacto"}]
    assert sq.eventos_sfx(d, {"corte": "", "entrada": ""}, mov) == []   # regra desligada


@needs_ffmpeg
def test_trocas_de_cena_de_um_take_de_verdade(tmp_path):
    take = tmp_path / "take.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=25:d=2",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=2",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-c:v", "libx264", "-preset",
                    "ultrafast", str(take)], check=True)
    tempos = editor.scene_changes(take)
    assert len(tempos) == 1 and tempos[0] == pytest.approx(2.0, abs=0.25)
    cache = list((editor.paths.EDITOR_DIR / editor.MOVIMENTO_DIR).glob("*.json"))
    assert len(cache) == 1 and json.loads(cache[0].read_text()) == tempos
    assert editor.scene_changes(take) == tempos   # do cache
