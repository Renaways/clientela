"""Fase 2 do estúdio: a sequência multipista num arquivo .clientela da pasta do projeto (formato,
takes, clipes por trilha), o grafo do ffmpeg que empilha V1…Vn e soma A1…An, e o export como job."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app import create_app
from clientela import clients as clients_module
from clientela import editor, history
from clientela import projects as projects_module
from clientela import sequencia as sq

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="sem ffmpeg")


def _doc(**cfg):
    doc = sq.nova("Teste", "completo", **cfg)
    doc["midias"] = [
        {"id": "a", "caminho": "Brutos/a.mp4", "duracao": 4, "largura": 640, "altura": 360, "audios": 1},
        {"id": "b", "caminho": "/abs/b.mp4", "duracao": 3, "largura": 360, "altura": 640, "audios": 1},
        {"id": "m", "caminho": "trilha.wav", "duracao": 10, "video": False, "audios": 1},
    ]
    doc["clipes"] = [
        {"id": "c1", "midia": "a", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2, "vinculo": "l1"},
        {"id": "c2", "midia": "a", "tipo": "audio", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2, "vinculo": "l1"},
        {"id": "c3", "midia": "b", "tipo": "video", "trilha": 1, "inicio": 2, "entrada": 0.5, "saida": 2.5},
        {"id": "c4", "midia": "a", "tipo": "video", "trilha": 2, "inicio": 3, "entrada": 1, "saida": 2, "ajuste": "preencher"},
        {"id": "c5", "midia": "m", "tipo": "audio", "trilha": 2, "inicio": 0, "entrada": 0, "saida": 4, "ganho": -12},
    ]
    return sq.normalizar(doc)


# ------------------------------------------------------------------ documento
def test_presets_e_formato_personalizado():
    d = sq.nova("Reels", "vertical")
    assert (d["config"]["largura"], d["config"]["altura"], d["config"]["preset"]) == (1080, 1920, "vertical")
    d = sq.nova("Custom", "completo", largura=1001, altura=777, fps=29)
    # dimensão ímpar vira par (H.264 yuv420p), fps vai para o válido mais perto
    assert (d["config"]["largura"], d["config"]["altura"], d["config"]["fps"], d["config"]["preset"]) == (1002, 778, 29.97, "personalizado")
    assert d["trilhas"] == {"video": 2, "audio": 2} and d["clipes"] == [] and d["formato"] == sq.FORMATO


def test_normalizar_corrige_o_que_e_arrumacao_e_recusa_o_resto():
    d = _doc()
    assert sq.duracao(d) == 4.0
    assert [c["id"] for c in d["clipes"]] == ["c2", "c5", "c1", "c3", "c4"]   # áudio, depois vídeo; por trilha e tempo
    assert next(c for c in d["clipes"] if c["id"] == "c3")["ajuste"] == "conter"
    # passar do fim do take vira o fim do take
    raw = json.loads(json.dumps(d))
    raw["clipes"][2]["saida"] = 99
    assert sq.normalizar(raw)["clipes"][2]["saida"] == 4
    ruins = [
        lambda r: r["clipes"][0].update(trilha=3),                    # A3 não existe
        lambda r: r["clipes"][0].update(midia="x"),                   # mídia que não está na lista
        lambda r: r["clipes"].append({**r["clipes"][0], "id": "v", "tipo": "video", "midia": "m"}),   # trilha.wav não tem vídeo
        lambda r: r["clipes"][0].update(entrada=2, saida=2),          # sem duração
        lambda r: r["midias"].append(dict(r["midias"][0])),           # id repetido
        lambda r: r["config"].update(fps="x"),
    ]
    for estraga in ruins:
        r = json.loads(json.dumps(d))
        estraga(r)
        with pytest.raises(ValueError):
            sq.normalizar(r)


def test_sobreposicao_na_mesma_trilha_e_avisada():
    d = _doc()
    assert sq.sobreposicoes(d) == []
    r = json.loads(json.dumps(d))
    next(c for c in r["clipes"] if c["id"] == "c3")["inicio"] = 1.5
    assert sq.sobreposicoes(sq.normalizar(r)) == [("c1", "c3")]


def test_grafo_empilha_video_e_soma_audio():
    d = _doc()
    ordem = {cid: i for i, cid in enumerate(["c1", "c3", "c4", "c2", "c5"])}   # uma entrada por clipe
    g = sq.grafo(d, ordem)
    assert g.startswith("color=c=black:s=1920x1080:r=30:d=4,format=yuv420p[base]")
    # V1 antes de V2; cada clipe deslocado para o seu início e ligado só no seu intervalo (a entrada já vem cortada)
    assert g.index("[1:v:0]trim=duration=2,setpts=PTS-STARTPTS+2/TB") < g.index("[2:v:0]trim=duration=1,setpts=PTS-STARTPTS+3/TB")
    assert "force_original_aspect_ratio=decrease,setsar=1" in g                       # conter
    assert "force_original_aspect_ratio=increase,crop=1920:1080" in g                 # preencher
    assert "enable='between(t,3,4)'" in g
    assert "[4:a:0]atrim=duration=4" in g and "volume=-12.00dB,adelay=0:all=1" in g
    assert "amix=inputs=2:normalize=0:duration=longest,apad=whole_dur=4,atrim=0:4[aout]" in g
    # mudo sai do mix; sem áudio nenhum, silêncio do tamanho da sequência
    r = json.loads(json.dumps(d))
    for c in r["clipes"]:
        if c["tipo"] == "audio":
            c["mudo"] = True
    assert "anullsrc=r=48000:cl=stereo,atrim=0:4[aout]" in sq.grafo(sq.normalizar(r), ordem)
    caminhos = {"a": "/x/a.mp4", "b": "/x/b.mp4", "m": "/x/m.wav"}
    cmd = sq.comando(d, caminhos, "/x/out.mp4", audio="/t/mix.wav")
    # 2ª passada: as 3 entradas visuais (cortadas na leitura) + o WAV da mixagem
    assert cmd.count("-i") == 4 and cmd[cmd.index("/t/mix.wav") - 1] == "-i" and cmd[cmd.index("-r") + 1] == "30" and cmd[-1] == "/x/out.mp4"
    assert ["-ss", "0.5", "-t", "2", "-i", "/x/b.mp4"] == cmd[cmd.index("/x/b.mp4") - 5:cmd.index("/x/b.mp4") + 1]
    ca = sq.comando_audio(d, caminhos, "/t/mix.wav")
    assert ca.count("-i") == 2 and "-c:a" in ca and ca[ca.index("-c:a") + 1] == "pcm_s16le"


def _gerar(path, cor, w, h, dur, freq):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={cor}:s={w}x{h}:r=25:d={dur}",
                    "-f", "lavfi", "-i", f"sine=f={freq}:d={dur}", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(path)], check=True)


def _cor_em(video, t, tmp):
    raw = tmp / f"q{t}.raw"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", "-vf", "scale=8:4",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)], check=True)
    b = raw.read_bytes()
    return list(b[3 * 12:3 * 13]), list(b[:3])   # centro e canto


@needs_ffmpeg
def test_render_de_verdade_empilha_e_entrega_inteiro(tmp_path):
    _gerar(tmp_path / "red.mp4", "red", 640, 360, 4, 440)
    _gerar(tmp_path / "blue.mp4", "blue", 360, 640, 3, 880)
    d = sq.nova("Final", "completo", largura=640, altura=360, fps=25)
    d["midias"] = [{"id": "r", "caminho": "red.mp4", "duracao": 4, "audios": 1},
                   {"id": "b", "caminho": "blue.mp4", "duracao": 3, "audios": 1}]
    d["clipes"] = [
        {"id": "c1", "midia": "r", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2},
        {"id": "c2", "midia": "b", "tipo": "video", "trilha": 1, "inicio": 2, "entrada": 0, "saida": 2},
        {"id": "c3", "midia": "r", "tipo": "video", "trilha": 2, "inicio": 3, "entrada": 0, "saida": 1, "ajuste": "preencher"},
        {"id": "c4", "midia": "r", "tipo": "audio", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2},
    ]
    entrega = tmp_path / "Sequência - Final v1"
    msg = sq.renderizar({"doc": d, "entrega": str(entrega), "nome": "Final.mp4",
                         "midias": {"r": str(tmp_path / "red.mp4"), "b": str(tmp_path / "blue.mp4")}})
    assert "Final.mp4" in msg and not list(tmp_path.glob(".*montando"))
    out = entrega / "Final.mp4"
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                                      "-of", "json", str(out)], capture_output=True, text=True, check=True).stdout)
    assert float(info["format"]["duration"]) == pytest.approx(4.0, abs=0.1)
    assert [s["codec_type"] for s in info["streams"]] == ["video", "audio"] and info["streams"][0]["width"] == 640
    centro, canto = _cor_em(out, 1.0, tmp_path)
    assert centro[0] > 200 and centro[2] < 50                       # vermelho em V1
    centro, canto = _cor_em(out, 2.5, tmp_path)
    assert centro[2] > 150 and canto == [0, 0, 0]                    # o take vertical cabe inteiro, com faixa preta
    centro, canto = _cor_em(out, 3.5, tmp_path)
    assert centro[0] > 200 and canto[0] > 200                        # V2 por cima, preenchendo
    assert json.loads((entrega / "Final.clientela").read_text())["nome"] == "Final"
    # mídia que sumiu: o job falha antes do ffmpeg, sem deixar pasta pela metade
    with pytest.raises(sq.Falha):
        sq.renderizar({"doc": d, "entrega": str(tmp_path / "Sequência - Final v2"), "nome": "Final.mp4",
                       "midias": {"r": str(tmp_path / "sumiu.mp4"), "b": str(tmp_path / "blue.mp4")}})
    assert not (tmp_path / "Sequência - Final v2").exists()


# ------------------------------------------------------------------ app
def _project(tmp_path):
    d = tmp_path / "Bruno"
    d.mkdir()
    client = clients_module.create_client(name="Bruno", path=str(d))
    project = projects_module.create_project(client["id"], "Edição longa")
    folder = Path(project["path"])
    (folder / "Brutos").mkdir(exist_ok=True)
    return client, project, folder


def test_arquivo_clientela_na_pasta_do_projeto(tmp_path):
    client, project, folder = _project(tmp_path)
    c = create_app().test_client()
    q = {"project_id": project["id"]}
    assert c.get("/api/sequencias", query_string=q).get_json() == []
    r = c.post("/api/sequencias", json={**q, "nome": "Corte: final?", "preset": "vertical"})
    assert r.status_code == 200
    v = r.get_json()
    assert v["arquivo"] == "Corte final.clientela" and (folder / v["arquivo"]).is_file()   # nome limpo para Mac e Windows
    assert v["doc"]["config"]["largura"] == 1080
    assert c.post("/api/sequencias", json={**q, "nome": "Corte: final?"}).status_code == 409
    assert c.post("/api/sequencias", json={**q, "nome": "  "}).status_code == 400
    lista = c.get("/api/sequencias", query_string=q).get_json()
    assert [s["arquivo"] for s in lista] == ["Corte final.clientela"]
    # arquivo fora do padrão nunca é lido nem gravado
    for ruim in ("../x.clientela", ".oculto.clientela", "x.json"):
        assert c.get("/api/sequencia", query_string={**q, "arquivo": ruim}).status_code == 400

    (folder / "Brutos" / "take.mp4").write_bytes(b"x")
    doc = v["doc"]
    doc["midias"] = [{"id": "m1", "caminho": "Brutos/take.mp4", "duracao": 5, "audios": 1, "largura": 1920, "altura": 1080},
                     {"id": "m2", "caminho": str(tmp_path / "longe.mp4"), "duracao": 5, "audios": 1}]
    doc["clipes"] = [{"id": "c1", "midia": "m1", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 5}]
    r = c.put("/api/sequencia", json={**q, "arquivo": v["arquivo"], "doc": doc})
    assert r.status_code == 200 and r.get_json()["offline"] == ["m2"]
    salvo = json.loads((folder / v["arquivo"]).read_text())
    assert salvo["midias"][0]["caminho"] == "Brutos/take.mp4" and salvo["host"]
    doc["clipes"][0]["trilha"] = 9
    assert c.put("/api/sequencia", json={**q, "arquivo": v["arquivo"], "doc": doc}).status_code == 400
    got = c.get("/api/sequencia", query_string={**q, "arquivo": v["arquivo"]}).get_json()
    assert got["doc"]["clipes"][0]["trilha"] == 1 and got["duracao"] == 5


@needs_ffmpeg
def test_midia_de_video_e_audio_entra_relativa_ou_absoluta(tmp_path):
    client, project, folder = _project(tmp_path)
    _gerar(folder / "Brutos" / "take.mp4", "red", 320, 180, 2, 440)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=3", str(tmp_path / "musica.wav")], check=True)
    base = Path(project["path"])
    m = editor.sequence_media(base, "Brutos/take.mp4")
    assert m["caminho"] == "Brutos/take.mp4" and m["video"] and m["audios"] == 1 and m["largura"] == 320
    assert m["duracao"] == pytest.approx(2.0, abs=0.1) and m["reproduzivel"] is True
    a = editor.sequence_media(base, str(tmp_path / "musica.wav"))
    assert a["caminho"] == str(tmp_path / "musica.wav") and a["video"] is False and a["audios"] == 1
    # o estúdio toca e desenha a onda do áudio também
    c = create_app().test_client()
    assert c.get(f"/api/editor/video?video={tmp_path / 'musica.wav'}").mimetype == "audio/wav"
    assert c.get(f"/api/editor/onda?video={tmp_path / 'musica.wav'}").status_code == 200
    with pytest.raises(editor.EditorError):
        editor.sequence_media(base, "../../etc/passwd")
    with pytest.raises(editor.EditorError):
        editor.sequence_media(base, "/etc/hosts")


def _wait(job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = editor.load_job(job_id)
        if job and job["status"] not in editor.ACTIVE_STATUSES:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} não terminou: {editor.load_job(job_id)}")


@needs_ffmpeg
def test_exportar_e_um_job_sem_ia(tmp_path):
    client, project, folder = _project(tmp_path)
    _gerar(folder / "Brutos" / "take.mp4", "red", 320, 180, 2, 440)
    base = Path(project["path"])
    v = editor.create_sequence(base, "Final", "completo", 320, 180, 25)
    with pytest.raises(editor.EditorError) as exc:
        editor.start_sequence_job(project["id"], None, v["arquivo"])
    assert exc.value.status == 409   # vazia
    doc = v["doc"]
    doc["midias"] = [{"id": "m1", **{k: x for k, x in editor.sequence_media(base, "Brutos/take.mp4").items()
                                       if k in ("caminho", "nome", "duracao", "largura", "altura", "video", "audios")}}]
    doc["clipes"] = [{"id": "c1", "midia": "m1", "tipo": "video", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2},
                     {"id": "c2", "midia": "m1", "tipo": "audio", "trilha": 1, "inicio": 0, "entrada": 0, "saida": 2}]
    editor.save_sequence(base, v["arquivo"], doc)
    r = create_app().test_client().post("/api/sequencia/exportar", json={"project_id": project["id"], "arquivo": v["arquivo"]})
    assert r.status_code == 200
    job = _wait(r.get_json()["id"])
    assert job["status"] == editor.STATUS_CONCLUIDO, job.get("erro")
    assert job["tipo"] == editor.TIPO_SEQUENCIA and job["pasta_base"] == "Sequência - Final" and job["versao"] == 1
    assert (folder / "Sequência - Final v1" / "Final.mp4").is_file()
    assert editor.next_version(folder, "Sequência - Final") == 2
    # a entrega aparece no painel do projeto e não volta como vídeo bruto nos seletores
    assert "Sequência - Final v1" in [x["pasta"] for x in editor.list_deliveries(project["id"])]
    assert not any("Sequência" in x["path"] for x in editor.list_candidates(project["id"])["videos"])
    # o status vira "concluído" um instante antes do evento ir para o histórico (_record_finish roda depois)
    deadline = time.time() + 5
    while time.time() < deadline and not any(e["action"] == "editor_job" for e in history.get_history(project["id"])):
        time.sleep(0.05)
    assert any(e["action"] == "editor_job" and e["details"]["entrega"] == "Sequência - Final v1"
               for e in history.get_history(project["id"]))
    # mídia que sumiu: 409 antes de começar
    (folder / "Brutos" / "take.mp4").unlink()
    with pytest.raises(editor.EditorError) as exc:
        editor.start_sequence_job(project["id"], None, v["arquivo"])
    assert exc.value.status == 409 and "offline" in str(exc.value)


@needs_ffmpeg
def test_miniaturas_iguais_pedidas_juntas_nao_quebram(tmp_path):
    """A tira do estúdio e a da sequência pedem o mesmo quadro ao mesmo tempo: cada pedido tem o seu
    temporário, e a limpeza do cache não tropeça no temporário dos outros (antes: 500 e FileNotFoundError)."""
    import threading
    video = tmp_path / "v.mp4"
    _gerar(video, "red", 320, 180, 2, 440)
    erros = []

    def pede(t):
        try:
            editor.frame_path(None, str(video), t, 160)
        except Exception as exc:  # o teste quer ver qualquer falha
            erros.append(repr(exc))

    ts = [threading.Thread(target=pede, args=(0.0 if i % 2 else 1.0,)) for i in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    from clientela import paths
    assert erros == [] and not [p for p in (paths.EDITOR_DIR / editor.FRAMES_DIR).iterdir() if p.name.startswith(".")]
