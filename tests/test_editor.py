"""Função Editor: ciclo de vida do job do estagiário com o `claude` mockado
por um script que fala stream-json, mais o agrupamento das entregas."""

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from clientela import clients as clients_module
from clientela import editor, history, identidades, paths
from clientela import projects as projects_module

# CLI falso: lê o prompt do stdin, guarda argv/prompt/env pra inspeção e
# emite o stream conforme FAKE_CLAUDE_MODE.
FAKE = r'''import json, os, re, signal, sys, time
prompt = sys.stdin.read()
out = os.environ["FAKE_CLAUDE_OUT"]
json.dump({{"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd(),
           "env_claude": sorted(k for k in os.environ if k.startswith("CLAUDE"))}},
          open(os.path.join(out, "chamada.json"), "w"))
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
emit({{"type": "system", "subtype": "init", "session_id": "s1", "model": "fake-model",
      "permissionMode": "auto", "skills": ["estagiario-editor"]}})
emit({{"type": "assistant", "parent_tool_use_id": None, "message": {{"content": [
    {{"type": "text", "text": "Lendo o perfil do cliente."}},
    {{"type": "tool_use", "name": "Bash", "input": {{"command": "npx hyperframes render", "description": "Renderiza o insert 1"}}}}]}}}})
if mode == "slow":
    signal.signal(signal.SIGINT, lambda *a: sys.exit(130))
    time.sleep(30)
if mode == "neto_travado":   # um filho que ignora SIGINT/SIGTERM, como um ffmpeg em livelock
    import subprocess
    filho = subprocess.Popen([sys.executable, "-c",
        "import signal,time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)"])
    open(os.path.join(os.environ["FAKE_CLAUDE_OUT"], "neto.pid"), "w").write(str(filho.pid))
    signal.signal(signal.SIGINT, lambda *a: sys.exit(130))
    time.sleep(30)
if mode == "auth":
    emit({{"type": "result", "subtype": "success", "is_error": True,
          "result": "Failed to authenticate: OAuth session expired", "total_cost_usd": 0}})
    sys.exit(1)
if mode == "crash":
    sys.stderr.write("boom: algo quebrou\n"); sys.exit(2)
m = re.search(r"entregue os arquivos finais em (.+?)/ ", prompt)
if m:
    d = m.group(1); os.makedirs(d, exist_ok=True)
    for n in ["00-00-00 - 1 Gancho (camada ATRAS).mov", "00-00-00 - 1 Gancho (camada FRENTE).mov",
              "00-00-00 - Recorte Ana (alpha, video inteiro).mov",
              "00-29-06 - 5b AGORA (tela cheia, com transicao).mov"]:
        open(os.path.join(d, n), "wb").write(b"mov")
emit({{"type": "result", "subtype": "success", "is_error": False, "result": "Entreguei 3 inserts.",
      "total_cost_usd": 1.23, "duration_ms": 4000, "num_turns": 7, "session_id": "s1"}})
'''


# Kit falso: o auto.py de verdade roda ffprobe/whisper/hyperframes. Este fala
# o mesmo stream-json, guarda os args e, no modo motion, faz exec do claude
# como o real (o pipeline de verdade é testado em test_kit_auto.py).
FAKE_AUTO = r'''import json, os, sys
modo = sys.argv[1]
a = json.load(open(sys.argv[sys.argv.index("--args") + 1]))
json.dump({"modo": modo, "args": a, "cwd": os.getcwd()}, open(os.path.join(os.environ["FAKE_CLAUDE_OUT"], "auto.json"), "w"))
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
emit({"type": "system", "subtype": "auto", "modo": modo})
if modo == "motion":
    os.chdir(a["projeto"])
    os.execv(a["claude"][0], a["claude"])
if os.environ.get("FAKE_AUTO_FAIL"):
    emit({"type": "result", "is_error": True, "result": "Este cliente ainda não tem o template", "total_cost_usd": 0})
    sys.exit(1)
d = a.get("entrega") or a.get("saida")
os.makedirs(d, exist_ok=True)
nome = {"legendas": "00-00-00 - Legendas (video inteiro).mov", "gc": "00-01-15 - 1 GC Ana.mov", "amostras": "GC.mp4"}[modo]
open(os.path.join(d, nome), "wb").write(b"mov")
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"description": "Renderizando"}}]}})
emit({"type": "result", "is_error": False, "result": "pronto", "total_cost_usd": 0})
'''


@pytest.fixture
def fake_kit(tmp_path, monkeypatch):
    kit = tmp_path / "fake_kit"
    kit.mkdir()
    (kit / "auto.py").write_text(FAKE_AUTO)
    monkeypatch.setattr(editor, "KIT_DIR", kit)
    return kit


@pytest.fixture
def fake_claude(tmp_path, monkeypatch, fake_kit):
    out = tmp_path / "fake_out"
    out.mkdir()
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir()
    script = tmp_path / "bin" / "fake_claude.py"
    script.write_text(FAKE.format())
    # wrapper em shell: o caminho do venv tem espaço ("Clientela V5") e um
    # shebang com espaço não executa.
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    paths.EDITOR_DIR.mkdir(parents=True, exist_ok=True)
    editor.save_config({"claude_path": str(binary)})
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(out))
    # simula o app aberto de dentro de uma sessão do Claude Code
    monkeypatch.setenv("CLAUDE_CODE_SIMPLE", "1")
    monkeypatch.setenv("CLAUDECODE", "1")
    editor._version_cache.clear()
    yield out
    for job in editor.list_jobs():
        if job.get("status") in editor.ACTIVE_STATUSES and job.get("pid"):
            try:
                os.killpg(os.getpgid(job["pid"]), signal.SIGKILL)
            except OSError:
                pass


def _project(tmp_path):
    d = tmp_path / "Aurora"
    d.mkdir()
    client = clients_module.create_client(name="Aurora", path=str(d))
    project = projects_module.create_project(client["id"], "Análise Pós-debate")
    folder = Path(project["path"])
    (folder / "Brutos").mkdir(exist_ok=True)
    (folder / "Brutos" / "IMG_7369.MP4").write_bytes(b"video")
    (folder / "IMG_7369.txt").write_text("transcrição")
    return client, project, folder


def _wait(job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = editor.load_job(job_id)
        if job and job["status"] not in editor.ACTIVE_STATUSES:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} não terminou: {editor.load_job(job_id)}")


def test_indisponivel_sem_o_cli(monkeypatch):
    monkeypatch.setattr(editor, "find_claude", lambda: None)
    st = editor.availability()
    assert st["disponivel"] is False and "claude" in st["motivo"]
    with pytest.raises(editor.EditorError) as exc:
        editor.start_analysis_job("x", confirmar_tokens=True)
    assert exc.value.status == 503


def test_job_ok_ciclo_completo(tmp_path, fake_claude):
    client, project, folder = _project(tmp_path)
    perfil = identidades.ensure_profile(client["id"])
    job = editor.start_project_job(project["id"], "inserts", "Brutos/IMG_7369.MP4", "IMG_7369.txt", "só os 3 primeiros", confirmar_tokens=True)
    assert job["status"] == editor.STATUS_RODANDO and job["pid"]
    done = _wait(job["id"])
    assert done["status"] == editor.STATUS_CONCLUIDO
    assert done["resultado"]["custo_usd"] == 1.23
    assert done["entrega"] == str(folder / "Motion - Inserts v1")
    assert (folder / ".estagiario" / "inserts v1").is_dir()

    chamada = json.loads((fake_claude / "chamada.json").read_text())
    argv = chamada["argv"]
    assert argv[:1] == ["-p"]
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[-2:] == ["--add-dir", str(perfil)]
    assert str(perfil).endswith("Aurora/_Estagiário")
    assert chamada["cwd"] == str(folder.resolve()) or chamada["cwd"] == str(folder)
    # o prompt chega pelo stdin com os placeholders trocados
    p = chamada["prompt"]
    assert "Use a skill estagiario-editor" in p
    assert "IMG_7369.MP4" in p and "IMG_7369.txt" in p and "só os 3 primeiros" in p
    assert "{" not in p
    # o ambiente do filho não herda o modo --bare da sessão que abriu o app
    assert "CLAUDE_CODE_SIMPLE" not in chamada["env_claude"]
    assert "CLAUDECODE" not in chamada["env_claude"]

    view = editor.job_view(job["id"])
    textos = [e["texto"] for e in view["eventos"]]
    assert "Lendo o perfil do cliente." in textos
    assert "Bash · Renderiza o insert 1" in textos
    assert view["passos"] == 1 and view["skill_carregada"] is True

    # o status vira "concluído" um instante antes do evento ir para o histórico (_record_finish roda depois)
    deadline = time.time() + 5
    while time.time() < deadline and history.get_history(project["id"])[-1]["action"] != "editor_job":
        time.sleep(0.05)
    events = history.get_history(project["id"])
    assert events[-1]["action"] == "editor_job"
    assert events[-1]["details"]["entrega"] == "Motion - Inserts v1"


def test_versao_seguinte_e_entregas_agrupadas(tmp_path, fake_claude):
    client, project, folder = _project(tmp_path)
    (folder / "Motion - Inserts v3").mkdir()
    job = editor.start_project_job(project["id"], "inserts", "Brutos/IMG_7369.MP4", confirmar_tokens=True)
    done = _wait(job["id"])
    assert done["versao"] == 4
    assert "nenhuma (transcreva o vídeo)" in done["prompt"]
    entregas = editor.list_deliveries(project["id"])
    v4 = next(v for v in entregas if v["versao"] == 4)
    assert [i["titulo"] for i in v4["inserts"]] == ["Gancho", "AGORA"]
    gancho, agora = v4["inserts"]
    assert gancho["tipo"] == "ATRÁS + FRENTE" and gancho["timecode"] == "00:00:00"
    assert [a["camada"] for a in gancho["arquivos"]] == ["ATRÁS", "FRENTE"]
    assert agora["tipo"] == "tela cheia, com transição" and agora["numero"] == "5b"
    assert v4["recortes"][0]["name"].startswith("00-00-00 - Recorte Ana")
    # um MOV entregue não aparece como vídeo bruto
    cands = editor.list_candidates(project["id"])
    assert [v["path"] for v in cands["videos"]] == ["Brutos/IMG_7369.MP4"]
    assert [t["path"] for t in cands["transcricoes"]] == ["IMG_7369.txt"]


def test_um_job_por_vez_e_cancelar(tmp_path, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    monkeypatch.setattr(editor, "CANCEL_GRACE_SECONDS", 2)
    client, project, folder = _project(tmp_path)
    job = editor.start_project_job(project["id"], "inserts", "Brutos/IMG_7369.MP4", confirmar_tokens=True)
    with pytest.raises(editor.EditorError) as exc:
        editor.start_analysis_job(client["id"], confirmar_tokens=True)
    assert exc.value.status == 409
    # enquanto roda, a próxima versão continua reservada
    assert editor.next_version(folder, "Motion - Inserts") == 2
    time.sleep(0.5)
    editor.cancel_job(job["id"])
    done = _wait(job["id"])
    assert done["status"] == editor.STATUS_CANCELADO
    assert not editor._pid_alive(done["pid"]) or editor._process_done(job["id"], done["pid"])[0]
    # agora já pode outro
    assert editor.active_job_id() is None


def test_cancelar_mata_o_filho_que_ignora_sinais(tmp_path, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "neto_travado")
    monkeypatch.setattr(editor, "CANCEL_GRACE_SECONDS", 1)
    client, project, folder = _project(tmp_path)
    job = editor.start_project_job(project["id"], "inserts", "Brutos/IMG_7369.MP4", confirmar_tokens=True)
    pid_file = fake_claude / "neto.pid"
    deadline = time.time() + 10
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.1)
    neto = int(pid_file.read_text())
    editor.cancel_job(job["id"])
    assert _wait(job["id"])["status"] == editor.STATUS_CANCELADO
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(neto, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(neto, signal.SIGKILL)
        pytest.fail("o filho que ignora SIGINT/SIGTERM continuou vivo depois de cancelar")


def test_falha_de_login_vira_instrucao(tmp_path, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth")
    client, project, folder = _project(tmp_path)
    done = _wait(editor.start_project_job(project["id"], "inserts", "Brutos/IMG_7369.MP4", confirmar_tokens=True)["id"])
    assert done["status"] == editor.STATUS_ERRO
    assert "/login" in done["erro"]


def test_processo_que_morre_sem_result_usa_o_stderr(tmp_path, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    client, project, folder = _project(tmp_path)
    done = _wait(editor.start_project_job(project["id"], "inserts", "Brutos/IMG_7369.MP4", confirmar_tokens=True)["id"])
    assert done["status"] == editor.STATUS_ERRO
    assert "boom" in done["erro"]


def test_projeto_sem_perfil_nao_cria_pasta_nem_add_dir(tmp_path, fake_claude):
    client, project, folder = _project(tmp_path)
    _wait(editor.start_project_job(project["id"], "inserts", "Brutos/IMG_7369.MP4", confirmar_tokens=True)["id"])
    chamada = json.loads((fake_claude / "chamada.json").read_text())
    assert "--add-dir" not in chamada["argv"]
    assert not (Path(client["path"]) / "_Estagiário").exists()


def test_analise_de_identidade(tmp_path, fake_claude):
    client, project, folder = _project(tmp_path)
    referencias = tmp_path / "Teste" / "Renders aprovados"
    referencias.mkdir(parents=True)
    job = editor.start_analysis_job(client["id"], "foque nos inserts", [str(folder), str(referencias)], confirmar_tokens=True)
    assert identidades.get_status(client["id"]) == identidades.STATUS_EM_ANALISE
    done = _wait(job["id"])
    assert done["status"] == editor.STATUS_CONCLUIDO
    chamada = json.loads((fake_claude / "chamada.json").read_text())
    assert Path(chamada["cwd"]).resolve() == Path(client["path"]).resolve()
    assert str(Path(client["path"]) / "_Estagiário") in chamada["prompt"]
    # as duas pastas escolhidas vão no prompt; só a de fora do cliente vira --add-dir
    assert str(folder.resolve()) in chamada["prompt"] and str(referencias.resolve()) in chamada["prompt"]
    add_dirs = [a for i, a in enumerate(chamada["argv"]) if i and chamada["argv"][i - 1] == "--add-dir"]
    assert add_dirs == [str(referencias.resolve())]
    # a próxima análise já abre com as mesmas pastas
    assert identidades.get_profile(client["id"])["pastas_analise"] == [str(folder.resolve()), str(referencias.resolve())]
    # terminou bem → rascunho pra revisar, nunca "pronto" sozinho
    assert identidades.get_status(client["id"]) == identidades.STATUS_EM_REVISAO


def test_resume_fecha_job_orfao_pelo_stream(tmp_path):
    """App caiu com um job rodando e o processo também morreu: no boot, o
    job é fechado pela linha `result` que ficou no .ndjson."""
    paths.EDITOR_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job = editor._new_job(tipo="projeto", client_id="c", project_id=None, tarefa="inserts",
                          tarefa_rotulo="Inserts", cwd=str(tmp_path), prompt="x")
    editor._update_job(job["id"], status=editor.STATUS_RODANDO, pid=999999)
    editor._job_stream(job["id"]).write_text(json.dumps(
        {"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0.5}) + "\n")
    editor.resume_jobs()
    assert editor.load_job(job["id"])["status"] == editor.STATUS_CONCLUIDO


def test_resume_sem_result_vira_erro(tmp_path):
    paths.EDITOR_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job = editor._new_job(tipo="projeto", client_id="c", project_id=None, tarefa="inserts",
                          tarefa_rotulo="Inserts", cwd=str(tmp_path), prompt="x")
    editor._update_job(job["id"], status=editor.STATUS_RODANDO, pid=999999)
    editor.resume_jobs()
    assert editor.load_job(job["id"])["status"] == editor.STATUS_ERRO


def test_entradas_invalidas(tmp_path, fake_claude):
    client, project, folder = _project(tmp_path)
    with pytest.raises(editor.EditorError):
        editor.start_project_job(project["id"], "inserts", "../../etc/passwd", confirmar_tokens=True)
    with pytest.raises(editor.EditorError):
        editor.start_project_job(project["id"], "naoexiste", "Brutos/IMG_7369.MP4", confirmar_tokens=True)
    with pytest.raises(editor.EditorError):
        editor.start_project_job(project["id"], "inserts", "Brutos/IMG_7369.MP4", "nao.txt", confirmar_tokens=True)
    with pytest.raises(editor.EditorError):
        editor.start_analysis_job(client["id"], "", ["/nao/existe"], confirmar_tokens=True)
    assert editor.list_jobs() == []


def test_config_guarda_so_o_que_mudou_e_template_tolera_chaves():
    paths.EDITOR_DIR.mkdir(parents=True, exist_ok=True)
    editor.save_config({"modelo": "opus", "tarefas": {"gc": {"pasta": "Motion - GCs"}}})
    saved = json.loads(paths.EDITOR_CONFIG_JSON.read_text())
    assert saved == {"modelo": "opus", "tarefas": {"gc": {"pasta": "Motion - GCs"}}}
    cfg = editor.get_config()
    assert cfg["tarefas"]["gc"]["rotulo"] == "GC"  # o resto da tarefa vem do padrão
    assert cfg["prompt_projeto"] == editor.DEFAULT_PROMPT_PROJETO
    assert editor.fill_template('{"a": 1} {cliente} {x}', {"cliente": "Aurora"}) == '{"a": 1} Aurora {x}'
    assert editor.reset_config()["modelo"] == ""


def test_parse_delivery_name():
    p = editor.parse_delivery_name("00-11-09 - 3 Debate (camada ATRAS).mov")
    assert p == {"tipo": "insert", "timecode": "00-11-09", "numero": "3", "titulo": "Debate",
                 "camada": "ATRÁS", "tela_cheia": False, "com_transicao": False}
    assert editor.parse_delivery_name("00-49-27 - 7 Camera.mov")["camada"] is None
    assert editor.parse_delivery_name("render final.mov")["tipo"] == "outro"


def test_api_editor(tmp_path, fake_claude):
    from app import create_app

    client, project, folder = _project(tmp_path)
    c = create_app().test_client()
    assert c.get("/api/editor/status").json["disponivel"] is True
    r = c.post("/api/editor/jobs", json={"confirmar_tokens": True, "project_id": project["id"], "tarefa": "inserts",
                                         "video": "Brutos/IMG_7369.MP4"})
    assert r.status_code == 200, r.json
    _wait(r.json["id"])
    view = c.get(f"/api/editor/jobs/{r.json['id']}").json
    assert view["status"] == "concluido" and view["eventos"]
    assert c.get(f"/api/editor/jobs?project_id={project['id']}").json[0]["id"] == r.json["id"]
    entregas = c.get(f"/api/editor/entregas?project_id={project['id']}").json
    assert entregas[0]["pasta"] == "Motion - Inserts v1"
    assert c.post("/api/editor/revelar", json={"project_id": project["id"], "rel": "Brutos"}).status_code == 404
    bad = c.post("/api/editor/jobs", json={"confirmar_tokens": True, "project_id": project["id"], "tarefa": "inserts", "video": ""})
    assert bad.status_code == 400 and "vídeo" in bad.json["error"]


def _bancada(folder, pasta="Motion - Inserts v1", tarefa="inserts v1", video="Brutos/IMG_7369.MP4"):
    """Simula o que o kit/entregar.py deixa: a bancada com job.json e o
    marcador .estagiario-origem na entrega apontando para ela."""
    trabalho = folder / ".estagiario" / tarefa
    trabalho.mkdir(parents=True, exist_ok=True)
    (trabalho / "job.json").write_text(json.dumps({"video": {"caminho": str(folder / video)}}))
    entrega = folder / pasta
    entrega.mkdir(exist_ok=True)
    (entrega / ".estagiario-origem").write_text(str(trabalho))
    return trabalho, entrega


def test_entregas_trazem_chave_e_inicio_por_elemento(tmp_path):
    d = tmp_path / "Motion - Inserts v1"
    d.mkdir()
    for n in ["00-58-00 - 3 Sintomas.mov", "00-05-06 - 1 Gancho (camada ATRAS).mov",
              "00-05-06 - 1 Gancho (camada FRENTE).mov", "00-00-00 - Legendas (video inteiro).mov"]:
        (d / n).write_bytes(b"x")
    (d / "_anteriores").mkdir()  # o que o --substituir guarda não aparece no painel
    (d / "_anteriores" / "00-58-00 - 3 Sintomas.mov").write_bytes(b"x")
    items = editor.group_delivery(d)["inserts"]
    assert [i["chave"] for i in items] == ["00-00-00||Legendas", "00-05-06|1|Gancho", "00-58-00|3|Sintomas"]
    assert [i["inicio"] for i in items] == [0.0, 5.2, 58.0]
    layers = editor.preview_layers({"recortes": [{"name": "00-00-00 - Recorte R (alpha, video inteiro).mov"}]}, items[1], d)
    assert [(p.name.split(" (")[0][-6:], inteiro) for p, inteiro in layers] == [
        ("Gancho", False), ("orte R", True), ("Gancho", False)]
    assert "ATRAS" in layers[0][0].name and "FRENTE" in layers[2][0].name


def test_ajuste_de_um_elemento(tmp_path, fake_claude):
    client, project, folder = _project(tmp_path)
    identidades.ensure_profile(client["id"])
    entrega = folder / "Motion - Inserts v1"
    entrega.mkdir()
    (entrega / "00-58-00 - 3 Sintomas.mov").write_bytes(b"mov")
    v = editor.list_deliveries(project["id"])[0]
    assert v["ajustavel"] is False and v["previa_disponivel"] is False
    with pytest.raises(editor.EditorError) as exc:  # sem bancada não há como refazer um elemento só
        editor.start_adjust_job(project["id"], "Motion - Inserts v1", "00-58-00|3|Sintomas", "mais curto", confirmar_tokens=True)
    assert exc.value.status == 409

    trabalho, _ = _bancada(folder)
    v = editor.list_deliveries(project["id"])[0]
    assert v["ajustavel"] is True and v["previa_disponivel"] is True
    with pytest.raises(editor.EditorError):
        editor.start_adjust_job(project["id"], "Motion - Inserts v1", "00-58-00|3|Sintomas", "  ", confirmar_tokens=True)
    with pytest.raises(editor.EditorError) as exc:
        editor.start_adjust_job(project["id"], "Motion - Inserts v1", "00-58-00|9|Outro", "x", confirmar_tokens=True)
    assert exc.value.status == 404
    with pytest.raises(editor.EditorError):
        editor.start_adjust_job(project["id"], "../Brutos", "00-58-00|3|Sintomas", "x", confirmar_tokens=True)

    job = editor.start_adjust_job(project["id"], "Motion - Inserts v1", "00-58-00|3|Sintomas", "troque o emoji por um ícone", confirmar_tokens=True)
    done = _wait(job["id"])
    assert done["status"] == editor.STATUS_CONCLUIDO
    assert done["tipo"] == editor.TIPO_AJUSTE and done["tarefa"] == "inserts" and done["versao"] == 1
    assert done["trabalho"] == str(trabalho.resolve()) and done["entrega"] == str(entrega)
    assert done["tarefa_rotulo"] == "Ajuste · 3 Sintomas"
    p = json.loads((fake_claude / "chamada.json").read_text())["prompt"]
    assert "modo ajuste" in p and "troque o emoji por um ícone" in p
    assert "3 Sintomas (entra em 00:58:00, camada única)" in p and "- 00-58-00 - 3 Sintomas.mov" in p
    assert str(trabalho.resolve()) in p and "Inserts v1" in p and "{" not in p
    # nenhuma pasta de entrega nova: o ajuste mora na mesma
    assert sorted(x.name for x in folder.iterdir() if x.name.startswith("Motion")) == ["Motion - Inserts v1"]
    assert history.get_history(project["id"])[-1]["details"]["pedido"] == "troque o emoji por um ícone"


def test_bancada_fora_do_projeto_nao_conta(tmp_path):
    client, project, folder = _project(tmp_path)
    entrega = folder / "Motion - Inserts v1"
    entrega.mkdir()
    fora = tmp_path / "outro"
    fora.mkdir()
    (fora / "job.json").write_text("{}")
    (entrega / ".estagiario-origem").write_text(str(fora))
    assert editor._delivery_origin(folder, entrega) is None


def _ffmpeg():
    import shutil
    return shutil.which("ffmpeg", path=editor._search_path())


@pytest.mark.skipif(not _ffmpeg(), reason="precisa do ffmpeg")
def test_previa_do_elemento_sobre_o_video(tmp_path):
    import subprocess

    from app import create_app

    client, project, folder = _project(tmp_path)
    raw = folder / "Brutos" / "IMG_7369.MP4"
    run = lambda *a: subprocess.run([_ffmpeg(), "-v", "error", "-y", *a], check=True)  # noqa: E731
    run("-f", "lavfi", "-i", "testsrc=s=108x192:d=4:r=30", "-f", "lavfi", "-i", "sine=d=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-f", "mp4", str(raw))
    _, entrega = _bancada(folder)
    alpha = ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-r", "30"]
    caixa = "color=c=black@0.0:s=108x192:d={d},format=rgba,drawbox=x=10:y=10:w=40:h=40:color=yellow@1:t=fill"
    run("-f", "lavfi", "-i", caixa.format(d=1.5), *alpha, str(entrega / "00-01-00 - 1 Gancho (camada ATRAS).mov"))
    run("-f", "lavfi", "-i", caixa.format(d=1.5), *alpha, str(entrega / "00-01-00 - 1 Gancho (camada FRENTE).mov"))
    run("-f", "lavfi", "-i", caixa.format(d=4), *alpha, str(entrega / "00-00-00 - Recorte R (alpha, video inteiro).mov"))

    r = editor.build_preview(project["id"], "Motion - Inserts v1", "00-01-00|1|Gancho")
    assert r["inicio"] == 1.0 and 1.4 <= r["duracao"] <= 1.6
    assert r["camadas"][1].startswith("00-00-00 - Recorte R")
    out = editor.preview_path(project["id"], r["arquivo"])
    assert out and out.parent == folder / ".estagiario" / "_previas"
    info = editor._media_info(_ffmpeg().replace("ffmpeg", "ffprobe"), out)
    assert info["largura"] == 108 and info["altura"] == 192 and 1.3 <= info["duracao"] <= 1.7
    # mesma entrada → mesmo arquivo, sem ffmpeg de novo
    mtime = out.stat().st_mtime
    assert editor.build_preview(project["id"], "Motion - Inserts v1", "00-01-00|1|Gancho")["arquivo"] == r["arquivo"]
    assert out.stat().st_mtime == mtime
    assert editor.preview_path(project["id"], "../x.mp4") is None

    c = create_app().test_client()
    res = c.post("/api/editor/previa", json={"project_id": project["id"], "pasta": "Motion - Inserts v1",
                                             "chave": "00-01-00|1|Gancho"})
    assert res.status_code == 200 and res.json["arquivo"] == r["arquivo"]
    f = c.get(f"/api/editor/previa/arquivo?project_id={project['id']}&arquivo={r['arquivo']}",
              headers={"Range": "bytes=0-99"})
    assert f.status_code == 206 and f.mimetype == "video/mp4" and len(f.data) == 100
    f.close()
    bad = c.post("/api/editor/previa", json={"project_id": project["id"], "pasta": "Motion - Inserts v1", "chave": "x"})
    assert bad.status_code == 404


def _template(client_id):
    perfil = identidades.ensure_profile(client_id)
    (perfil / "marca.json").write_text(json.dumps({"cores": {"fundo": "#000", "texto": "#fff"}}))
    return perfil


def test_legendas_sao_automaticas_sem_claude(tmp_path, fake_claude, monkeypatch):
    client, project, folder = _project(tmp_path)
    monkeypatch.setattr(editor, "find_claude", lambda: None)  # nem precisa do CLI
    with pytest.raises(editor.EditorError) as exc:  # sem template, o automático não sabe o estilo
        editor.start_project_job(project["id"], "legendas", "Brutos/IMG_7369.MP4", "IMG_7369.txt", confirmar_tokens=True)
    assert exc.value.status == 409 and "templates" in str(exc.value)
    perfil = _template(client["id"])
    job = editor.start_project_job(project["id"], "legendas", "Brutos/IMG_7369.MP4", "IMG_7369.txt", confirmar_tokens=True)
    assert job["modo"] == editor.MODO_AUTO
    done = _wait(job["id"])
    assert done["status"] == editor.STATUS_CONCLUIDO and done["resultado"]["custo_usd"] == 0
    auto = json.loads((fake_claude / "auto.json").read_text())
    assert auto["modo"] == "legendas" and auto["args"]["asr_modelo"] == "large-v3"
    assert auto["args"]["perfil"] == str(perfil) and auto["args"]["transcricao"].endswith("IMG_7369.txt")
    assert auto["cwd"] == str((folder / ".estagiario" / "legendas v1").resolve()) or auto["cwd"].endswith("legendas v1")
    assert not (fake_claude / "chamada.json").exists()  # o claude não rodou
    assert (folder / ".estagiario" / "legendas v1" / "kit" / "auto.py").is_file()
    view = editor.job_view(job["id"])
    assert view["modelo"] == "automático (sem IA)" and view["skill_carregada"] is True
    assert editor.list_deliveries(project["id"])[0]["inserts"][0]["titulo"] == "Legendas"


def test_gc_automatico_precisa_de_pessoa_e_entrada(tmp_path, fake_claude):
    client, project, folder = _project(tmp_path)
    _template(client["id"])
    with pytest.raises(editor.EditorError):
        editor.start_project_job(project["id"], "gc", "Brutos/IMG_7369.MP4", gcs=[{"nome": " "}], confirmar_tokens=True)
    with pytest.raises(editor.EditorError):
        editor.start_project_job(project["id"], "gc", "Brutos/IMG_7369.MP4", gcs=[{"nome": "R", "inicio": "x"}], confirmar_tokens=True)
    job = editor.start_project_job(project["id"], "gc", "Brutos/IMG_7369.MP4",
                                   gcs=[{"nome": "Ana Souza", "inicio": "75.5"}, {"nome": ""}], confirmar_tokens=True)
    _wait(job["id"])
    auto = json.loads((fake_claude / "auto.json").read_text())
    assert auto["modo"] == "gc"
    assert auto["args"]["gcs"] == [{"nome": "Ana Souza", "cargo": None, "inicio": 75.5}]


def test_tarefa_com_ia_prepara_a_bancada_antes_do_claude(tmp_path, fake_claude):
    client, project, folder = _project(tmp_path)
    editor.save_config({"tarefas": {"legendas": {"modo": "ia"}}})  # o automático pode ser desligado
    assert editor.task_mode("legendas") == editor.MODO_IA and editor.task_mode("inserts") == editor.MODO_IA
    editor.save_config({"tarefas": {"inserts": {"modo": "auto"}}})  # inserts não tem automático
    assert editor.task_mode("inserts") == editor.MODO_IA
    job = editor.start_project_job(project["id"], "inserts", "Brutos/IMG_7369.MP4", "IMG_7369.txt", confirmar_tokens=True)
    assert _wait(job["id"])["status"] == editor.STATUS_CONCLUIDO
    auto = json.loads((fake_claude / "auto.json").read_text())
    assert auto["modo"] == "motion" and auto["args"]["asr_modelo"] == "small"
    assert auto["args"]["claude"][1] == "-p"
    p = json.loads((fake_claude / "chamada.json").read_text())["prompt"]
    assert "A bancada já foi preparada pelo app" in p
    textos = [e["texto"] for e in editor.job_view(job["id"])["eventos"]]
    assert "Lendo o perfil do cliente." in textos  # o stream do claude segue no mesmo log


def test_templates_e_amostras(tmp_path, fake_claude):
    client, project, folder = _project(tmp_path)
    with pytest.raises(editor.EditorError) as exc:
        editor.start_samples_job(client["id"])
    assert exc.value.status == 409
    with pytest.raises(editor.EditorError):  # sem análise não há de onde tirar o template
        editor.start_templates_job(client["id"], confirmar_tokens=True)
    perfil = identidades.ensure_profile(client["id"])
    (perfil / "marca.md").write_text("# Marca")
    info = identidades.get_profile(client["id"])["templates"]
    assert info == {"marca": False, "gc": False, "amostras": []}

    # a IA escreve o marca.json (aqui, o claude falso escreve no fim) e as amostras saem sozinhas, sem IA
    (perfil / "marca.json").write_text("{}")
    job = editor.start_templates_job(client["id"], confirmar_tokens=True)
    assert "modo templates" in job["prompt"] and str(perfil) in job["prompt"]
    assert _wait(job["id"])["status"] == editor.STATUS_CONCLUIDO
    deadline = time.time() + 10
    while time.time() < deadline and not (perfil / "amostras" / "GC.mp4").exists():
        time.sleep(0.1)
    amostra = [j for j in editor.list_jobs() if j["tipo"] == editor.TIPO_AMOSTRAS]
    assert amostra and _wait(amostra[0]["id"])["status"] == editor.STATUS_CONCLUIDO
    info = identidades.get_profile(client["id"])["templates"]
    assert info["marca"] is True and [a["name"] for a in info["amostras"]] == ["GC.mp4"]
    assert identidades.sample_path(client["id"], "GC.mp4")
    assert identidades.sample_path(client["id"], "../marca.md") is None

    from app import create_app
    c = create_app().test_client()
    r = c.get(f"/api/clients/{client['id']}/identidade/amostras/GC.mp4")
    assert r.status_code == 200 and r.mimetype == "video/mp4"
    r.close()
    r = c.post(f"/api/clients/{client['id']}/identidade/amostras")
    assert r.status_code == 200 and r.json["tipo"] == "amostras"
    _wait(r.json["id"])


def test_gc_por_api_leva_as_pessoas(tmp_path, fake_claude):
    from app import create_app

    client, project, folder = _project(tmp_path)
    _template(client["id"])
    c = create_app().test_client()
    r = c.post("/api/editor/jobs", json={"confirmar_tokens": True, "project_id": project["id"], "tarefa": "gc", "video": "Brutos/IMG_7369.MP4",
                                         "gcs": [{"nome": "Ana", "inicio": 2}]})
    assert r.status_code == 200, r.json
    _wait(r.json["id"])
    assert json.loads((fake_claude / "auto.json").read_text())["args"]["gcs"][0]["nome"] == "Ana"


def test_video_de_qualquer_pasta_entrega_na_downloads(tmp_path, fake_claude):
    """"Edição IA" do topo: vídeo escolhido no Finder, fora de qualquer projeto,
    e a entrega na ~/Downloads (paths.USER_DOWNLOADS_DIR, isolada no conftest)."""
    solto = tmp_path / "Mesa" / "gravacao.mov"
    solto.parent.mkdir()
    solto.write_bytes(b"video")
    (solto.parent / "senha.cfg").write_text("x")
    with pytest.raises(editor.EditorError):  # absoluto só vale para mídia
        editor.start_downloads_job("inserts", str(solto.parent / "senha.cfg"), confirmar_tokens=True)
    with pytest.raises(editor.EditorError) as exc:  # automático sem cliente não tem estilo
        editor.start_downloads_job("legendas", str(solto), confirmar_tokens=True)
    assert exc.value.status == 409

    job = editor.start_downloads_job("inserts", str(solto), observacoes="sem emoji", confirmar_tokens=True)
    done = _wait(job["id"])
    assert done["status"] == editor.STATUS_CONCLUIDO
    assert done["project_id"] is None and done["destino"] == "downloads"
    assert done["video"] == str(solto.resolve())
    base = paths.USER_DOWNLOADS_DIR
    assert (base / "Motion - Inserts v1" / "00-00-00 - 1 Gancho (camada ATRAS).mov").is_file()
    assert (base / ".estagiario" / "inserts v1" / "kit" / "auto.py").is_file()
    chamada = json.loads((fake_claude / "chamada.json").read_text())
    # o claude precisa poder ler a pasta do vídeo, que está fora da cwd
    assert str(solto.parent.resolve()) in chamada["argv"]
    assert "nenhum (trabalho avulso)" in chamada["prompt"] and "None" not in chamada["prompt"]
    assert editor.start_downloads_job("inserts", str(solto), confirmar_tokens=True)["versao"] == 2


def test_video_de_fora_com_entrega_no_projeto(tmp_path, fake_claude):
    client, project, folder = _project(tmp_path)
    perfil = _template(client["id"])
    solto = tmp_path / "Externo" / "bruto.mp4"
    solto.parent.mkdir()
    solto.write_bytes(b"video")
    job = editor.start_project_job(project["id"], "legendas", str(solto), confirmar_tokens=True)
    _wait(job["id"])
    auto = json.loads((fake_claude / "auto.json").read_text())
    assert auto["args"]["video"] == str(solto.resolve()) and auto["args"]["perfil"] == str(perfil)
    assert (folder / "Motion - Legendas v1").is_dir()
    # escolhendo identidade de cliente, o avulso também roda o automático
    job = editor.start_downloads_job("legendas", str(solto), client_id=client["id"], confirmar_tokens=True)
    _wait(job["id"])
    assert (paths.USER_DOWNLOADS_DIR / "Motion - Legendas v1").is_dir()


def test_api_editor_destino_downloads(tmp_path, fake_claude, monkeypatch):
    from app import create_app
    from clientela import files_native

    revelados = []
    monkeypatch.setattr(files_native, "reveal_in_finder", lambda p: revelados.append(p) or True)

    solto = tmp_path / "solto.mp4"
    solto.write_bytes(b"video")
    c = create_app().test_client()
    r = c.post("/api/editor/jobs", json={"confirmar_tokens": True, "destino": "downloads", "tarefa": "inserts", "video": str(solto)})
    assert r.status_code == 200, r.json
    _wait(r.json["id"])
    assert c.post(f"/api/editor/jobs/{r.json['id']}/revelar").status_code == 200
    assert revelados == [str(paths.USER_DOWNLOADS_DIR / "Motion - Inserts v1")]
    assert c.post("/api/editor/jobs/nao-existe/revelar").status_code == 404
    destinos = c.get("/api/destinos").json
    assert destinos["downloads"]["path"] == str(paths.USER_DOWNLOADS_DIR)
