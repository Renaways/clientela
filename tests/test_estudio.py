"""Estúdio de Edição IA: a edição feita na timeline vira um corte vertical sem
passar pelo whisper nem pelo LLM (autocorte.py modo edição), e o app serve o
que a janela precisa — vídeo com Range, forma de onda, cortes para reabrir."""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import create_app
from clientela import autocorte as ac
from clientela import clients as clients_module
from clientela import editor, paths
from clientela import projects as projects_module

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="sem ffmpeg")

DIVIDIDO = {"modo": "dividido", "divisao": 0.3, "camera": {"x": 0.75, "y": 0.05, "w": 0.22, "h": 0.25},
            "principal": {"x": 0.25, "y": 0, "w": 0.5, "h": 1}}


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


# ------------------------------------------------------------------ grafo
def test_enquadramento_por_trecho_nao_colide_rotulos():
    segs = [{"inicio": 0.0, "fim": 1.0}, {"inicio": 2.0, "fim": 3.0, "transicao": "fade"},
            {"inicio": 4.0, "fim": 5.0, "transicao": "flash"}]
    cad = ac.cadeia_video(1920, 1080, layout=DIVIDIDO)
    g = ac.grafo_corte(segs, "IGNORADA", cadeias=[cad, cad, "crop=10:10:0:0"], cor="eq=contrast=1.2,format=yuv420p")
    assert "[cam_t0]" in g and "[cam_t1]" in g and "[cam]" not in g
    assert "IGNORADA" not in g and g.endswith("[vc]eq=contrast=1.2,format=yuv420p[vout]")
    # o fade do trecho 2 escurece o fim do 1 e o começo do 2; o flash entra do branco
    fim = {i: next(p for p in g.split(";") if p.endswith(f"[v{i}]")) for i in range(3)}
    assert fim[0].endswith("format=yuv420p,fade=t=out:st=0.850:d=0.150[v0]")
    assert fim[1].endswith("format=yuv420p,fade=t=in:st=0:d=0.150[v1]")
    assert fim[2] == "[0:v:0]trim=start=4.0:end=5.0,setpts=PTS-STARTPTS,crop=10:10:0:0,fade=t=in:st=0:d=0.200:color=white[v2]"
    # sem nada de novo, o grafo é o de sempre
    assert ac.grafo_corte(segs[:1], "CAD").endswith("[vc]CAD[vout]")


def test_cor_neutra_nao_filtra_e_brilho_bate_com_o_css():
    assert ac.cadeia_cor(None) == "" and ac.cadeia_cor({"brilho": 1, "contraste": 1, "saturacao": 1}) == ""
    assert ac.cadeia_cor({"brilho": 1.2}) == "colorchannelmixer=rr=1.200:gg=1.200:bb=1.200,format=yuv420p"
    assert ac.cadeia_cor({"saturacao": 0}) == "eq=contrast=1.000:saturation=0.000,format=yuv420p"


def test_mixagem_soma_as_faixas_com_ganho_e_fades():
    segs = [{"inicio": 0.0, "fim": 2.0}, {"inicio": 5.0, "fim": 7.0}]
    g = ac.grafo_mix(segs, [(1, 0.0), (2, -6.0)], fade_entrada=0.5, fade_saida=1.0, normalizar=True)
    assert "[0:a:1]atrim=start=5.0:end=7.0" in g and "volume=-6.00dB[mv2]" in g
    assert "[mv1][mv2]amix=inputs=2:normalize=0" in g
    assert g.endswith("[mx]afade=t=in:st=0:d=0.500,afade=t=out:st=3.000:d=1.000,loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    assert ac.grafo_mix(segs, []).startswith("anullsrc")
    info = {"audios": [{"faixa": 1}, {"faixa": 2}]}
    assert ac.mix_da_edicao(None, info) is None
    assert ac.mix_da_edicao({"audio": {"faixas": {"1": {"ganho": 0}}}}, info) is None  # = o áudio de sempre
    assert ac.mix_da_edicao({"audio": {"faixas": {"2": {"mudo": False}}}}, info)["faixas"] == [(1, 0.0), (2, 0.0)]


def test_trechos_da_edicao_ficam_como_a_pessoa_pos():
    segs = ac.segmentos_da_edicao({"segmentos": [
        {"inicio": 5, "fim": 7.333333, "transicao": "flash", "layout": DIVIDIDO},
        {"inicio": -1, "fim": 2}, {"inicio": 6.5, "fim": 8}, {"inicio": 9.95, "fim": 30},
        {"inicio": "x", "fim": 1}]}, 10.0)
    assert [(s["inicio"], s["fim"]) for s in segs] == [(0.0, 2.0), (5.0, 7.333), (7.333, 8.0)]
    assert segs[1]["transicao"] == "flash" and segs[1]["layout"] == DIVIDIDO
    assert "transicao" not in segs[0]


def test_estilo_da_edicao_vai_para_o_ass():
    cfg = {**ac.DEFAULTS, **ac.estilo_da_edicao({"estilo": {"margem": 300, "cores": ["#FF0000"], "tamanho": 100},
                                                 "gancho_duracao": 4.5})}
    cues = ac.blocos([{"texto": "oi", "t": 0, "fim": 0.4, "locutor": "Ana"}])
    ass = ac.gerar_ass(cues, ["Ana"], cfg, gancho="Olha isso")
    assert "Style: Ana,Arial Black,100,&H000000FF" in ass and ",80,80,300,1" in ass
    assert "Dialogue: 1,0:00:00.00,0:00:04.50,Gancho" in ass


def test_edicao_validada():
    with pytest.raises(editor.EditorError):
        editor.clean_edicao({"segmentos": []})
    with pytest.raises(editor.EditorError):
        editor.clean_edicao({"segmentos": [{"inicio": 0, "fim": 1}], "estilo": {"fonte": "Arial; rm -rf"}})
    ed = editor.clean_edicao({
        "segmentos": [{"inicio": 1, "fim": 2, "transicao": "girar", "layout": {"modo": "inteiro"}}, {"inicio": 3, "fim": 3.01}],
        "palavras": [{"texto": " oi ", "t": 1.1, "fim": 1.0, "locutor": ""}, {"texto": "", "t": 1, "fim": 2}],
        "cor": {"brilho": 9}, "estilo": {"tamanho": 5, "cores": ["#fff", "#00FF00"]},
        "audio": {"faixas": {"2": {"ganho": 99, "mudo": 0}}, "normalizar": 1},
    })
    assert ed["segmentos"] == [{"inicio": 1.0, "fim": 2.0, "motivo": "", "layout": {"modo": "inteiro"}}]
    assert ed["palavras"] == [{"texto": "oi", "t": 1.1, "fim": 1.1, "locutor": "Locutor 1", "duvidosa": False}]
    assert ed["cor"] == {"brilho": 2.0, "contraste": 1.0, "saturacao": 1.0}
    assert ed["estilo"] == {"tamanho": 30, "cores": ["#00FF00"]}
    assert ed["audio"]["faixas"] == {"2": {"ganho": 18.0, "mudo": False}} and ed["audio"]["normalizar"] is True


# ------------------------------------------------------------------ rodada de verdade
FALAS = [("Olha", 0.5), ("só", 1.0), ("isso", 1.5), ("aqui.", 2.0), ("Nossa", 5.5), ("eu", 6.0), ("não", 6.5),
         ("acredito.", 7.0)]


@needs_ffmpeg
def test_edicao_da_timeline_renderiza_sem_whisper_nem_llm(tmp_path):
    video = tmp_path / "clip.mp4"
    _clip(video)
    palavras = [{"texto": t, "t": s, "fim": s + 0.4, "locutor": "Ana" if s < 5 else "Beto"} for t, s in FALAS]
    palavras[2]["texto"] = "ISSO AÍ"   # corrigido na timeline
    trab = tmp_path / "bancada"
    trab.mkdir()
    entrega = tmp_path / "proj" / "Corte 9x16 v2"
    args = trab / "auto-args.json"
    args.write_text(json.dumps({
        "video": str(video), "entrega": str(entrega), "versao": "v2", "claude": "/nao/existe",
        "layout": {"modo": "inteiro", "principal": {"x": 0.3, "y": 0, "w": 0.3, "h": 1}},
        "edicao": {
            "segmentos": [{"inicio": 0.4, "fim": 2.5, "motivo": "gancho"},
                          {"inicio": 5.4, "fim": 7.6, "transicao": "fade", "layout": DIVIDIDO}],
            "palavras": palavras, "gancho": "Olha isso", "titulo": "Reação", "cor": {"brilho": 1.1, "saturacao": 1.3},
            "estilo": {"margem": 400}, "audio": {"faixas": {"1": {"ganho": 0}, "2": {"ganho": -3}}, "fade_saida": 0.5},
            "origem": "Corte 9x16 v1",
        },
    }))
    r = subprocess.run([sys.executable, ac.__file__, "--args", str(args)], cwd=trab,
                       capture_output=True, text=True, timeout=180)
    linhas = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
    assert linhas[-1]["type"] == "result" and linhas[-1]["is_error"] is False, linhas[-1]
    assert linhas[-1]["total_cost_usd"] == 0
    # nada de transcrição nem LLM: nem o áudio para o whisper foi extraído
    assert not (trab / "audio.wav").exists() and not (trab / "llm-resposta.json").exists()
    info = _stream_info(entrega / "Corte 9x16.mp4")
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert float(info["format"]["duration"]) == pytest.approx(4.3, abs=0.15)
    srt = (entrega / "Legenda.srt").read_text()
    assert "[Ana] OLHA SÓ ISSO AÍ AQUI." in srt and "[Beto] NOSSA" in srt
    assert ",80,80,400,1" in (entrega / "Legenda animada.ass").read_text()
    corte = json.loads((entrega / "corte.json").read_text())
    assert corte["edicao"]["origem"] == "Corte 9x16 v1" and "palavras" not in corte["edicao"]
    assert corte["edicao"]["segmentos"][1]["layout"] == DIVIDIDO and corte["llm"]["provedor"] == "edição na timeline"
    assert corte["layout_editor"]["modo"] == "inteiro" and corte["titulo"] == "Reação"
    assert json.loads((trab / "palavras.json").read_text())[2]["texto"] == "ISSO AÍ"


# ------------------------------------------------------------------ app: job, cortes, mídia
def _project(tmp_path):
    d = tmp_path / "Bruno"
    d.mkdir()
    client = clients_module.create_client(name="Bruno", path=str(d))
    project = projects_module.create_project(client["id"], "Clipe live")
    folder = Path(project["path"])
    (folder / "Brutos").mkdir(exist_ok=True)
    return client, project, folder


def _wait(job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = editor.load_job(job_id)
        if job and job["status"] not in editor.ACTIVE_STATUSES:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} não terminou: {editor.load_job(job_id)}")


FAKE_SCRIPT = r'''
import json, os, sys
from pathlib import Path
a = json.loads(Path(sys.argv[sys.argv.index("--args") + 1]).read_text())
Path(os.environ["FAKE_CORTE_OUT"]).write_text(json.dumps(a))
print(json.dumps({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0}))
'''


def test_job_da_timeline_nao_pede_llm_e_reabre_o_corte(tmp_path, monkeypatch):
    script = tmp_path / "fake_autocorte.py"
    script.write_text(FAKE_SCRIPT)
    out = tmp_path / "out.json"
    monkeypatch.setattr(editor, "AUTOCORTE_SCRIPT", script)
    monkeypatch.setenv("FAKE_CORTE_OUT", str(out))
    monkeypatch.setattr(editor, "find_claude", lambda: None)   # sem claude: o corte com IA não roda
    paths.EDITOR_DIR.mkdir(parents=True, exist_ok=True)
    client, project, folder = _project(tmp_path)
    (folder / "Brutos" / "live.mp4").write_bytes(b"video")
    with pytest.raises(editor.EditorError):
        editor.start_project_job(project["id"], editor.TAREFA_CORTE, "Brutos/live.mp4", confirmar_tokens=True)
    # um corte anterior, como o autocorte deixa: entrega + bancada com as palavras
    (folder / "Corte 9x16 v1").mkdir()
    (folder / "Corte 9x16 v1" / "corte.json").write_text(json.dumps({
        "titulo": "Primeiro", "duracao": 4.0, "video": str(folder / "Brutos" / "live.mp4"),
        "segmentos": [{"inicio": 1, "fim": 5, "motivo": "m"}], "gancho": "Ei", "layout_editor": DIVIDIDO}))
    trab1 = folder / ".estagiario" / "corte_vertical v1"
    trab1.mkdir(parents=True)
    (trab1 / "palavras.json").write_text(json.dumps([{"texto": "oi", "t": 1.2, "fim": 1.5, "locutor": "Locutor 1"}]))
    cortes = editor.list_cuts(folder)
    assert [(c["pasta"], c["com_palavras"], c["video_existe"]) for c in cortes] == [("Corte 9x16 v1", True, True)]
    cut = editor.load_cut(folder, "Corte 9x16 v1")
    assert cut["layout"] == DIVIDIDO and cut["palavras"][0]["texto"] == "oi" and cut["gancho"] == "Ei"
    with pytest.raises(editor.EditorError):
        editor.load_cut(folder, "../Corte 9x16 v1")

    edicao = {"segmentos": cut["segmentos"], "palavras": cut["palavras"], "layout": cut["layout"], "origem": cut["pasta"]}
    job = editor.start_project_job(project["id"], editor.TAREFA_CORTE, "Brutos/live.mp4", edicao=edicao, confirmar_tokens=True)
    done = _wait(job["id"])
    assert done["status"] == editor.STATUS_CONCLUIDO and done["versao"] == 2 and done["edicao_de"] == "Corte 9x16 v1"
    chamada = json.loads(out.read_text())
    assert chamada["edicao"]["segmentos"] == [{"inicio": 1.0, "fim": 5.0, "motivo": "m"}]
    assert chamada["layout"] == DIVIDIDO and chamada["edicao"]["palavras"][0]["texto"] == "oi"
    with pytest.raises(editor.EditorError):
        editor.start_project_job(project["id"], "inserts", "Brutos/live.mp4", edicao=edicao, confirmar_tokens=True)


@needs_ffmpeg
def test_estudio_serve_video_com_range_onda_e_miniatura(tmp_path):
    video = tmp_path / "clip.mp4"
    _clip(video)
    app = create_app()
    c = app.test_client()
    q = f"video={video}"
    info = c.get(f"/api/editor/midia?{q}").get_json()
    assert [a["faixa"] for a in info["audios"]] == [1, 2] and info["reproduzivel"] is True
    assert info["proxy"]["estado"] == "nenhum"
    r = c.get(f"/api/editor/video?{q}", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.data) == 100 and r.mimetype == "video/mp4"
    assert c.get("/api/editor/video?video=/etc/passwd").status_code == 404
    onda = c.get(f"/api/editor/onda?{q}&faixa=2").get_json()
    assert onda["por_segundo"] == 50 and len(onda["picos"]) == pytest.approx(500, abs=3)
    # a faixa 2 só soa depois dos 5 s
    assert max(onda["picos"][:200]) < 3 and max(onda["picos"][300:]) > 8  # o sine do lavfi tem amplitude 1/8
    assert c.get(f"/api/editor/onda?{q}&faixa=9").status_code == 404
    mini = c.get(f"/api/editor/quadro?{q}&t=1&w=160")
    assert mini.status_code == 200 and mini.mimetype == "image/jpeg"
    assert c.get("/api/editor/cortes?destino=downloads").get_json() == []


@needs_ffmpeg
def test_proxy_sai_em_h264_540p(tmp_path):
    video = tmp_path / "clip.mp4"
    _clip(video)
    assert editor.request_proxy(None, str(video))["estado"] == "gerando"
    deadline = time.time() + 60
    while editor.proxy_status(video)["estado"] == "gerando" and time.time() < deadline:
        time.sleep(0.2)
    assert editor.proxy_status(video)["estado"] == "pronto"
    target, mime = editor.video_file(None, str(video), proxy=True)
    v = next(s for s in _stream_info(target)["streams"] if s["codec_type"] == "video")
    assert v["height"] == 540 and mime == "video/mp4"
