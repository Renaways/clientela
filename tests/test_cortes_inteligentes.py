"""Fase 1 do estúdio: cortes inteligentes sem LLM (silêncio pela onda, gaguejo/repetição/hesitação pelas
palavras), legenda que não quebra nome composto, a trava "Serão usados tokens do Claude" e a revisão do
corte que a IA fez sozinha de um clipe de live."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app import create_app
from clientela import autocorte as ac
from clientela import clients as clients_module
from clientela import editor, history, paths
from clientela import projects as projects_module

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="sem ffmpeg")


def _w(texto, t, fim, locutor="A", **kw):
    return {"texto": texto, "t": t, "fim": fim, "locutor": locutor, **kw}


# ------------------------------------------------------------------ silêncio
def test_silencios_pelo_envelope():
    env = [-60] * 30 + [-12] * 20 + [-60] * 10 + [-12] * 5 + [-60] * 40   # passo 0,02 s
    assert ac.silencios(env, 0.02, -38, 0.5) == [(0.0, 0.6), (1.3, 2.1)]
    # o de 0,2 s no meio é curto demais
    assert (1.0, 1.2) not in ac.silencios(env, 0.02, -38, 0.5)


def test_silencio_deixa_respiro_e_nunca_corta_palavra():
    env = [-12] * 50 + [-60] * 100 + [-12] * 50          # som 0–1 s, silêncio 1–3 s, som 3–4 s
    r = ac.cortes_inteligentes(4.0, env, 0.02, params={"respiro": 0.1})
    assert [(x["inicio"], x["fim"], x["tipo"]) for x in r["removidos"]] == [(1.1, 2.9, "silêncio")]
    assert r["segmentos"] == [{"inicio": 0.0, "fim": 1.1, "origem": 0}, {"inicio": 2.9, "fim": 4.0, "origem": 0}]
    assert r["antes"] == 4.0 and r["depois"] == pytest.approx(2.2)
    # uma palavra falada baixinho (-44 dB, abaixo do limiar) no meio do "silêncio" fica, com respiro
    baixinho = env[:90] + [-44] * 15 + env[105:]
    r = ac.cortes_inteligentes(4.0, baixinho, 0.02, palavras=[_w("oi", 1.8, 2.1)], params={"respiro": 0.1})
    assert [(x["inicio"], x["fim"]) for x in r["removidos"]] == [(1.1, 1.7), (2.2, 2.9)]
    # mas a palavra que o whisper esticou para dentro da pausa (silêncio de verdade) não segura o corte
    r = ac.cortes_inteligentes(4.0, env, 0.02, palavras=[_w("Hoje", 1.2, 3.1)], params={"respiro": 0.1})
    assert [(x["inicio"], x["fim"]) for x in r["removidos"]] == [(1.1, 2.9)]
    # palavra duvidosa (música) não protege nada
    r = ac.cortes_inteligentes(4.0, env, 0.02, palavras=[_w("lá", 1.8, 2.1, duvidosa=True)], params={"respiro": 0.1})
    assert len(r["removidos"]) == 1


def test_cortes_respeitam_os_trechos_da_timeline_e_o_ignorar():
    env = [-12] * 50 + [-60] * 100 + [-12] * 50
    segs = [{"inicio": 0.5, "fim": 2.0}, {"inicio": 2.5, "fim": 4.0}]
    r = ac.cortes_inteligentes(4.0, env, 0.02, segmentos=segs, params={"respiro": 0.1})
    assert [(x["inicio"], x["fim"]) for x in r["removidos"]] == [(1.1, 2.0), (2.5, 2.9)]
    assert r["segmentos"] == [{"inicio": 0.5, "fim": 1.1, "origem": 0}, {"inicio": 2.9, "fim": 4.0, "origem": 1}]
    # "manter este trecho" na trilha IA: a proposta some e o trecho fica
    r = ac.cortes_inteligentes(4.0, env, 0.02, segmentos=segs, params={"respiro": 0.1},
                               ignorar=[{"inicio": 1.1, "fim": 2.0}])
    assert [(x["inicio"], x["fim"]) for x in r["removidos"]] == [(2.5, 2.9)]
    # pedaço que sobraria menor que o mínimo vai junto, para não virar um piscar
    r = ac.cortes_inteligentes(4.0, env, 0.02, segmentos=[{"inicio": 1.0, "fim": 3.0}], params={"respiro": 0.1})
    assert r["segmentos"] == [] or all(s["fim"] - s["inicio"] >= 0.2 for s in r["segmentos"])


def test_pausa_da_fala_corta_mesmo_com_som():
    env = [-12] * 200   # jogo alto o tempo todo: o silêncio não acha nada
    ws = [_w("vamos", 0.2, 0.6), _w("lá", 2.5, 2.8)]
    assert ac.cortes_inteligentes(4.0, env, 0.02, ws)["removidos"] == []
    r = ac.cortes_inteligentes(4.0, env, 0.02, ws, params={"pausa_fala": 1.0, "respiro": 0.1})
    assert [(x["inicio"], x["fim"], x["tipo"]) for x in r["removidos"]] == [(0.7, 2.4, "pausa")]


# ------------------------------------------------------------------ fala
def test_gaguejo_repeticao_e_hesitacao():
    ws = [_w("pro-", 0, 0.2), _w("problema", 0.3, 0.8), _w("eu", 0.9, 1.0), _w("acho", 1.05, 1.3),
          _w("eu", 1.4, 1.5), _w("acho", 1.55, 1.8), _w("que", 1.85, 2.0), _w("hã", 2.1, 2.5), _w("sim", 2.6, 2.9),
          _w("ca", 3.0, 3.1), _w("casa", 3.2, 3.5)]
    d = ac.disfluencias(ws)
    assert [(x["tipo"], x["texto"], x["inicio"], x["fim"]) for x in d] == [
        ("gaguejo", "pro-", 0, 0.3),
        ("repetição", "eu acho", 0.9, 1.4),   # fica a última vez, a corrigida
        ("hesitação", "hã", 2.1, 2.6),        # corta até a próxima palavra: a emenda cai na fala
        ("gaguejo", "ca", 3.0, 3.2),
    ]
    # desligados, nada
    assert ac.disfluencias(ws, gaguejos=False, repeticoes=False, hesitacoes=False) == []


def test_palavra_curta_comum_nao_vira_gaguejo():
    ws = [_w("a", 0, 0.1), _w("aula", 0.15, 0.5), _w("de", 0.6, 0.7), _w("depois", 0.75, 1.1),
          _w("é", 1.2, 1.3), _w("isso", 1.35, 1.6)]
    assert ac.disfluencias(ws) == []
    # "ééé" esticado é hesitação; o "é" verbo, curto, não
    assert [x["texto"] for x in ac.disfluencias([_w("ééé", 0, 0.9), _w("é", 1.0, 1.1), _w("bom", 1.2, 1.4)])] == ["ééé"]


def test_parametros_desconhecidos_e_tipos_sao_normalizados():
    p = ac.cortes_padrao({"silencio_db": "-30", "gaguejos": 0, "rm -rf": 1})
    assert p["silencio_db"] == -30.0 and p["gaguejos"] is False and "rm -rf" not in p


# ------------------------------------------------------------------ legenda semântica
def test_nome_composto_e_numero_nao_quebram_de_tela():
    ws = [_w("Eu", 0, .2), _w("moro", .25, .5), _w("em", .55, .6), _w("São", .65, .9), _w("Paulo", .95, 1.3),
          _w("faz", 1.35, 1.5), _w("10", 1.55, 1.7), _w("mil", 1.75, 1.9), _w("anos.", 1.95, 2.2),
          _w("O", 2.5, 2.6), _w("Rio", 2.65, 2.8), _w("de", 2.85, 2.9), _w("Janeiro", 2.95, 3.3)]
    cues = ac.blocos(ws, max_palavras=4)
    assert [[w["texto"] for w in c["palavras"]] for c in cues] == [
        ["Eu", "moro", "em", "São", "Paulo"], ["faz", "10", "mil", "anos."], ["O", "Rio", "de", "Janeiro"]]
    # "O" é artigo: não começa nome; "Rio de Janeiro" é um termo só
    assert ac.termos(ws)[9:] == [None, 10, 10, 10]
    # na .ass o termo acende inteiro e não quebra de linha
    ass = ac.gerar_ass(cues, ["A"], {})
    assert "SÃO\\hPAULO" in ass and "10\\hMIL" in ass and "RIO\\hDE\\hJANEIRO" in ass


def test_ligacao_nao_fica_no_fim_da_tela():
    ws = [_w("vamos", 0, .3), _w("falar", .35, .6), _w("hoje", .65, .9), _w("de", .95, 1.0), _w("política", 1.05, 1.5)]
    cues = ac.blocos(ws, max_palavras=4)
    assert [[w["texto"] for w in c["palavras"]] for c in cues] == [["vamos", "falar", "hoje"], ["de", "política"]]


def test_espelho_js_bate_com_o_python():
    """stuTermos/stuBlocos do index.html = autocorte.termos/blocos."""
    node = shutil.which("node")
    if not node:
        pytest.skip("sem node")
    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text(encoding="utf-8")
    js = html[html.index("// = autocorte.LIGACOES"):html.index("function stuCues(")]
    ws = [_w("Eu", 0, .2), _w("moro", .25, .5), _w("em", .55, .6), _w("São", .65, .9), _w("Paulo", .95, 1.3),
          _w("tem", 1.35, 1.5), _w("50", 1.55, 1.7), _w("por", 1.75, 1.8), _w("cento", 1.85, 2.0), _w("de", 2.05, 2.1),
          _w("chance", 2.15, 2.4), _w("com", 2.45, 2.5), _w("você", 2.55, 2.7), _w("R$", 2.8, 2.9), _w("50", 2.95, 3.1),
          _w("Maria", 3.5, 3.7, "B"), _w("e", 3.75, 3.8, "B"), _w("José", 3.85, 4.1, "B"),
          # marcas da passada semântica: "quebra" abre tela nova, "junto" prende na próxima
          _w("então", 4.2, 4.4, "B"), _w("vamos", 4.45, 4.6, "B", quebra=True), _w("lá", 4.65, 4.8, "B"),
          _w("Copa", 4.85, 5.0, "B", junto=True), _w("América", 5.05, 5.3, "B"), _w("hoje", 5.35, 5.5, "B")]
    prog = js + f"\nconst W={json.dumps(ws)};\nconsole.log(JSON.stringify(stuBlocos(W).map(c=>c.palavras.map(w=>[w.texto,w.grupo]))));"
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True).stdout
    py = [[[w["texto"], w["grupo"]] for w in c["palavras"]] for c in ac.blocos([dict(w) for w in ws])]
    assert json.loads(out) == py


# ------------------------------------------------------------------ app
def _project(tmp_path):
    d = tmp_path / "Bruno"
    d.mkdir()
    client = clients_module.create_client(name="Bruno", path=str(d))
    project = projects_module.create_project(client["id"], "Clipe live")
    folder = Path(project["path"])
    (folder / "Brutos").mkdir(exist_ok=True)
    return client, project, folder


def _clip(path):
    """6 s, faixa 1 com tom até 3 s e silêncio depois."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6,volume='if(lt(t,3),1,0)':eval=frame",
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path),
    ], check=True)


@needs_ffmpeg
def test_envelope_e_cortes_pela_api(tmp_path):
    video = tmp_path / "clip.mp4"
    _clip(video)
    env = editor.audio_envelope(None, str(video), 1)
    assert env["passo"] == 0.02 and len(env["db"]) == pytest.approx(300, abs=4)
    assert max(env["db"][20:140]) > -30 and max(env["db"][170:290]) == -90.0
    c = create_app().test_client()
    r = c.post("/api/editor/cortes-inteligentes", json={"video": str(video), "params": {"respiro": 0.1}}).get_json()
    assert len(r["removidos"]) == 1 and r["removidos"][0]["tipo"] == "silêncio"
    assert r["removidos"][0]["inicio"] == pytest.approx(3.1, abs=0.05) and r["removidos"][0]["fim"] == pytest.approx(6.0, abs=0.05)
    assert r["segmentos"][0]["inicio"] < 0.1 and r["depois"] == pytest.approx(3.1, abs=0.1)
    assert c.post("/api/editor/cortes-inteligentes", json={"video": "/etc/passwd"}).status_code == 404
    bad = c.post("/api/editor/cortes-inteligentes", json={"video": str(video), "segmentos": [{"inicio": "x"}]})
    assert bad.status_code == 400


FAKE_TRANSCREVE = r'''
import json, sys, time
from pathlib import Path
assert sys.argv[1] == "--transcrever"
time.sleep(1.5)   # tempo de sobra para o segundo pedido chegar enquanto este ainda roda
Path(sys.argv[3]).write_text(json.dumps([{"texto": "oi", "t": 0.2, "fim": 0.5, "locutor": "Locutor 1", "duvidosa": False}]))
'''


def test_transcricao_local_em_segundo_plano(tmp_path, monkeypatch):
    script = tmp_path / "fake_autocorte.py"
    script.write_text(FAKE_TRANSCREVE)
    monkeypatch.setattr(editor, "AUTOCORTE_SCRIPT", script)
    monkeypatch.setattr(editor, "_transcricoes", {})
    video = tmp_path / "bruto.mp4"
    video.write_bytes(b"video")
    c = create_app().test_client()
    assert c.get(f"/api/editor/transcricao?video={video}").get_json() == {"estado": "nenhuma"}
    assert c.post("/api/editor/transcricao", json={"video": str(video)}).get_json() == {"estado": "transcrevendo"}
    # uma de cada vez
    outro = tmp_path / "outro.mp4"
    outro.write_bytes(b"x")
    assert c.post("/api/editor/transcricao", json={"video": str(outro)}).status_code == 409
    deadline = time.time() + 10
    while time.time() < deadline:
        st = c.get(f"/api/editor/transcricao?video={video}").get_json()
        if st["estado"] != "transcrevendo":
            break
        time.sleep(0.1)
    assert st["estado"] == "pronta" and st["palavras"][0]["texto"] == "oi"
    # a pasta de trabalho do whisper não fica para trás
    assert [p.name for p in (paths.EDITOR_DIR / editor.TRANSCRICOES_DIR).iterdir() if p.is_dir()] == []


def _wait(job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = editor.load_job(job_id)
        if job and job["status"] not in editor.ACTIVE_STATUSES:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} não terminou: {editor.load_job(job_id)}")


FAKE_CORTE = r'''
import json, sys
print(json.dumps({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0}))
'''


def test_corte_com_claude_pede_confirmacao_de_tokens(tmp_path, monkeypatch):
    script = tmp_path / "fake_autocorte.py"
    script.write_text(FAKE_CORTE)
    monkeypatch.setattr(editor, "AUTOCORTE_SCRIPT", script)
    monkeypatch.setattr(editor, "find_claude", lambda: "/bin/echo")
    paths.EDITOR_DIR.mkdir(parents=True, exist_ok=True)
    client, project, folder = _project(tmp_path)
    (folder / "Brutos" / "live.mp4").write_bytes(b"video")
    with pytest.raises(editor.EditorError) as exc:
        editor.start_project_job(project["id"], editor.TAREFA_CORTE, "Brutos/live.mp4")
    assert exc.value.status == 428 and "tokens" in str(exc.value)
    assert not (folder / "Corte 9x16 v1").exists() and editor.active_job_id() is None   # nada começou

    c = create_app().test_client()
    body = {"project_id": project["id"], "tarefa": editor.TAREFA_CORTE, "video": "Brutos/live.mp4"}
    r = c.post("/api/editor/jobs", json=body)
    assert r.status_code == 428 and r.get_json()["confirmar_tokens"] is True
    r = c.post("/api/editor/jobs", json={**body, "confirmar_tokens": True})
    assert r.status_code == 200
    _wait(r.get_json()["id"])
    # render da timeline: só ffmpeg, sem pergunta
    edicao = {"segmentos": [{"inicio": 0, "fim": 1}]}
    _wait(editor.start_project_job(project["id"], editor.TAREFA_CORTE, "Brutos/live.mp4", edicao=edicao)["id"])
    # OpenRouter também não é Claude: sem pergunta
    editor.save_config({"corte": {"provedor_llm": "openrouter", "openrouter_api_key": "k", "openrouter_modelo": "m"}})
    _wait(editor.start_project_job(project["id"], editor.TAREFA_CORTE, "Brutos/live.mp4")["id"])


def test_rotas_de_ia_respondem_428_sem_confirmacao(tmp_path, monkeypatch):
    monkeypatch.setattr(editor, "find_claude", lambda: "/bin/echo")
    client, project, folder = _project(tmp_path)
    c = create_app().test_client()
    for rota in (f"/api/clients/{client['id']}/identidade/analisar", f"/api/clients/{client['id']}/identidade/templates"):
        r = c.post(rota, json={})
        assert r.status_code == 428 and r.get_json()["confirmar_tokens"] is True, rota
    r = c.post("/api/editor/ajustes", json={"project_id": project["id"], "pasta": "Motion - Inserts v1",
                                            "chave": "x", "pedido": "y"})
    assert r.status_code == 428


def test_clipe_entra_no_corte_ja_confirmado_e_marcado(monkeypatch):
    chamadas, marcados = [], []
    monkeypatch.setattr(editor, "start_project_job", lambda *a, **kw: chamadas.append(kw) or {"id": "j1"})
    monkeypatch.setattr(editor, "_update_job", lambda job_id, **f: marcados.append((job_id, f)))
    monkeypatch.setattr(editor, "active_job_id", lambda: None)
    editor._clip_queue.append(("c:001", "Brutos/a.mkv"))
    editor._drain_clip_queue()
    assert chamadas == [{"confirmar_tokens": True}] and marcados == [("j1", {"clipe": True})]


def test_revisao_do_corte_automatico(tmp_path):
    client, project, folder = _project(tmp_path)
    entrega = folder / "Corte 9x16 v1"
    entrega.mkdir()
    (entrega / "Corte 9x16.mp4").write_bytes(b"mp4")
    job = {"id": "j1", "status": editor.STATUS_CONCLUIDO, "project_id": project["id"], "tarefa_rotulo": "Corte",
           "entrega": str(entrega)}
    editor._record_finish(job)   # corte pedido à mão: sem revisão
    assert projects_module.get_project(project["id"])["revisao_corte"] is None
    with pytest.raises(editor.EditorError) as exc:
        editor.review_cut(project["id"], "aprovar")
    assert exc.value.status == 409

    editor._record_finish({**job, "clipe": True})
    rev = projects_module.get_project(project["id"])["revisao_corte"]
    assert rev["estado"] == "pendente" and rev["pasta"] == "Corte 9x16 v1"
    with pytest.raises(editor.EditorError):
        editor.review_cut(project["id"], "apagar")
    c = create_app().test_client()
    r = c.post("/api/editor/revisao", json={"project_id": project["id"], "acao": "descartar"})
    assert r.status_code == 200 and r.get_json()["estado"] == "descartado"
    assert not entrega.exists()
    movido = folder / r.get_json()["descartado_em"]
    assert movido.parent == folder / ".estagiario" / "_descartados" and (movido / "Corte 9x16.mp4").read_bytes() == b"mp4"
    p = projects_module.get_project(project["id"])
    assert p["revisao_corte"]["estado"] == "descartado"
    assert [e["details"]["acao"] for e in history.get_history(project["id"]) if e["action"] == "revisao_corte"] == ["descartar"]
    # decidido uma vez, não decide de novo
    assert c.post("/api/editor/revisao", json={"project_id": project["id"], "acao": "aprovar"}).status_code == 409


def test_credito_de_legendador_e_alucinacao():
    """Num tom puro (sem fala) o whisper escreveu "Transcrição e Legendas Pedro Negri": crédito de legendador
    dos dados de treino. Fica marcado como duvidosa e sai da legenda."""
    ws = [_w("Transcrição", 0.5, 1.0), _w("e", 1.05, 1.1), _w("Legendas", 1.15, 1.6), _w("Pedro", 1.65, 1.9), _w("Negri", 1.95, 2.2)]
    assert all(w["duvidosa"] for w in ac.marcar_duvidosas(ws))
    assert not any(w["duvidosa"] for w in ac.marcar_duvidosas([_w("as", 0, .2), _w("legendas", .25, .6), _w("ficaram", .65, .9), _w("boas", .95, 1.2)]))
