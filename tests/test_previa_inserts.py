"""Prévia dos inserts em HTML antes do export (clientela/previa_inserts.py), os templates universais do kit
(kit/universais.py) e os jobs que saem dela: exportar só os aprovados (sem IA) e ajustar um insert na prévia."""

import importlib.util
import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from clientela import clients as clients_module
from clientela import editor, paths, previa_inserts
from clientela import projects as projects_module

FAKE_AUTO = r'''import json, os, sys
modo = sys.argv[1]
a = json.load(open(sys.argv[sys.argv.index("--args") + 1]))
json.dump({"modo": modo, "args": a, "cwd": os.getcwd()}, open(os.path.join(os.environ["FAKE_OUT"], "auto.json"), "w"))
if modo == "motion":
    os.chdir(a["projeto"]); os.execv(a["claude"][0], a["claude"])
sys.stdout.write(json.dumps({"type": "result", "is_error": False, "result": "pronto", "total_cost_usd": 0}) + "\n")
'''
FAKE_CLAUDE = r'''import json, os, sys
json.dump({"prompt": sys.stdin.read(), "argv": sys.argv[1:]}, open(os.path.join(os.environ["FAKE_OUT"], "claude.json"), "w"))
sys.stdout.write(json.dumps({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0.1}) + "\n")
'''


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """Kit de verdade (universais/common rodam no teste) com um auto.py falso; claude falso."""
    kit = tmp_path / "kit"
    kit.mkdir()
    for f in editor.KIT_DIR.iterdir():
        if f.is_file():
            (kit / f.name).write_bytes(f.read_bytes())
    (kit / "auto.py").write_text(FAKE_AUTO)
    monkeypatch.setattr(editor, "KIT_DIR", kit)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("FAKE_OUT", str(out))
    script = tmp_path / "bin" / "fake_claude.py"
    script.parent.mkdir()
    script.write_text(FAKE_CLAUDE)
    binary = tmp_path / "bin" / "claude"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    paths.EDITOR_DIR.mkdir(parents=True, exist_ok=True)
    editor.save_config({"claude_path": str(binary)})
    editor._version_cache.clear()
    yield out
    for job in editor.list_jobs():
        if job.get("status") in editor.ACTIVE_STATUSES and job.get("pid"):
            try:
                os.killpg(os.getpgid(job["pid"]), signal.SIGKILL)
            except OSError:
                pass


MARCA = {
    "cores": {"texto": "#FFFFFF", "acento": "#FFB800", "fundo": "#101010"},
    "tipografia": {"fontes": [{"familia": "Hando", "arquivo": "Hando-Black.otf", "peso": 900}],
                   "papeis": {"destaque": {"familia": "Hando", "peso": 900}}},
    "componentes": {"realce": {"tipo": "pilula", "fundo": "acento", "texto": "fundo"}},
}


def _comp(nome, dur):
    return (f'<!doctype html>\n<html>\n  <head>\n    <title>{nome}</title>\n  </head>\n  <body>\n'
            f'    <div id="root" data-composition-id="{nome}" data-duration="{dur}"></div>\n  </body>\n</html>\n')


def _bancada(tmp_path):
    d = tmp_path / "Cliente"
    d.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(d))
    project = projects_module.create_project(client["id"], "Teste")
    folder = Path(project["path"])
    (folder / "Brutos").mkdir(exist_ok=True)
    (folder / "Brutos" / "bruto.mp4").write_bytes(b"video")
    b = folder / ".estagiario" / "inserts v1"
    (b / "hf" / "assets" / "fonts").mkdir(parents=True)
    (b / "hf" / "assets" / "fonts" / "Hando-Black.otf").write_bytes(b"x")
    (b / "hf" / "assets" / "fonts" / "Outra-BoldItalic.otf").write_bytes(b"x")  # não está no marca.json
    (b / "job.json").write_text(json.dumps({
        "entrega": str(folder / "Motion - Inserts v1"), "trabalho": str(b),
        "video": {"caminho": str(folder / "Brutos" / "bruto.mp4"), "largura": 1080, "altura": 1920, "fps": 30, "duracao": 60.0}}))
    (b / "marca.json").write_text(json.dumps(MARCA))
    (b / "palavras.json").write_text(json.dumps({"palavras": [
        {"texto": "o", "t": 20.0, "fim": 20.1}, {"texto": "que", "t": 20.1, "fim": 20.3},
        {"texto": "Estratégia,", "t": 21.2, "fim": 21.8}]}))
    jobs = [
        {"comp": "ins-1-gancho-atras", "inicio": 2.0, "nome": "1 Gancho (camada ATRAS)"},
        {"comp": "ins-1-gancho-frente", "inicio": 2.0, "nome": "1 Gancho (camada FRENTE)"},
        {"comp": "ins-2-antes", "inicio": 10.0, "nome": "2", "partes": [[0.0, 3.0, "2a ANTES"], [3.0, 6.0, "2b AGORA (com transicao)"]]},
    ]
    (b / "jobs.json").write_text(json.dumps(jobs))
    for comp, dur in (("ins-1-gancho-atras", 4.4), ("ins-1-gancho-frente", 4.4), ("ins-2-antes", 6.0)):
        (b / "hf" / f"{comp}.html").write_text(_comp(comp, dur))
    (b / "hf" / "_check").mkdir()
    (b / "hf" / "_check" / "segredo.html").write_text("x")
    return project, folder, b


def test_estado_agrupa_camadas_e_partes(tmp_path):
    project, folder, b = _bancada(tmp_path)
    e = previa_inserts.estado(project["id"], "inserts v1")
    assert [i["chave"] for i in e["itens"]] == ["ins-1-gancho", "ins-2-antes"]
    g, a = e["itens"]
    assert g["nome"] == "1 Gancho" and [c["camada"] for c in g["camadas"]] == ["atras", "frente"] and g["dur"] == 4.4
    assert g["arquivos"] == ["00-02-00 - 1 Gancho (camada ATRAS).mov", "00-02-00 - 1 Gancho (camada FRENTE).mov"]
    assert a["nome"] == "2a ANTES / 2b AGORA"
    assert a["arquivos"] == ["00-10-00 - 2a ANTES.mov", "00-13-00 - 2b AGORA (com transicao).mov"]
    assert e["video"].endswith("bruto.mp4") and e["largura"] == 1080 and not e["recorte"]
    familias = {f["familia"]: f for f in e["fontes"]}
    assert familias["Hando"]["origem"] == "marca"
    assert familias["Outra"]["arquivos"] == ["Outra-BoldItalic.otf"]
    assert previa_inserts.listar(project["id"])[0]["total"] == 2


def test_aprovar_e_trocar_a_fonte_reescreve_so_o_html_do_insert(tmp_path):
    project, folder, b = _bancada(tmp_path)
    e = previa_inserts.salvar(project["id"], "inserts v1", {"itens": {"ins-1-gancho": {"aprovado": True, "fonte": "Outra"},
                                                                     "nao-existe": {"aprovado": True}}})
    assert e["aprovados"] == 1 and e["pendentes"] == 1
    atras = (b / "hf" / "ins-1-gancho-atras.html").read_text()
    assert '<style id="clientela-fonte">' in atras and "font-weight: 700; font-style: italic" in atras
    assert 'src: url("assets/fonts/Outra-BoldItalic.otf")' in atras and '"Outra", "Helvetica' in atras
    assert "clientela-fonte" in (b / "hf" / "ins-1-gancho-frente.html").read_text()
    assert "clientela-fonte" not in (b / "hf" / "ins-2-antes.html").read_text()
    # fonte geral: vale para quem não tem a sua; a do marca.json não ganha @font-face (já está declarada)
    previa_inserts.salvar(project["id"], "inserts v1", {"fonte": "Hando"})
    antes = (b / "hf" / "ins-2-antes.html").read_text()
    assert '"Hando", "Helvetica' in antes and "@font-face" not in antes
    assert "Outra-BoldItalic" in (b / "hf" / "ins-1-gancho-atras.html").read_text()
    # tirar a fonte tira o bloco (uma vez só, sem acumular)
    previa_inserts.salvar(project["id"], "inserts v1", {"fonte": None, "itens": {"ins-1-gancho": {"fonte": None}}})
    assert "clientela-fonte" not in (b / "hf" / "ins-1-gancho-atras.html").read_text()
    with pytest.raises(editor.EditorError):
        previa_inserts.salvar(project["id"], "inserts v1", {"fonte": 'x"; } body { color: red'})


def test_exportado_fica_velho_quando_o_html_muda(tmp_path):
    project, folder, b = _bancada(tmp_path)
    previa_inserts.salvar(project["id"], "inserts v1", {"itens": {"ins-1-gancho": {"aprovado": True}}})
    entrega = folder / "Motion - Inserts v1"
    entrega.mkdir()
    for n in ("00-02-00 - 1 Gancho (camada ATRAS).mov", "00-02-00 - 1 Gancho (camada FRENTE).mov"):
        (entrega / n).write_bytes(b"mov")
    e = previa_inserts.estado(project["id"], "inserts v1")
    assert e["itens"][0]["exportado"] and e["pendentes"] == 0
    time.sleep(0.02)
    previa_inserts.salvar(project["id"], "inserts v1", {"itens": {"ins-1-gancho": {"fonte": "Outra"}}})
    assert previa_inserts.estado(project["id"], "inserts v1")["pendentes"] == 1


def test_arquivo_so_dentro_de_hf_e_com_o_objeto_das_timelines(tmp_path):
    project, folder, b = _bancada(tmp_path)
    p = previa_inserts.arquivo(project["id"], "inserts v1", "ins-2-antes.html")
    html = previa_inserts.html_previa(p)
    assert html.index("window.__timelines = window.__timelines || {}") < html.index("<title>")
    assert previa_inserts.arquivo(project["id"], "inserts v1", "assets/fonts/Hando-Black.otf").name == "Hando-Black.otf"
    for rel in ("../job.json", "_check/segredo.html", "nada.html"):
        with pytest.raises(editor.EditorError):
            previa_inserts.arquivo(project["id"], "inserts v1", rel)
    with pytest.raises(editor.EditorError):
        previa_inserts.estado(project["id"], "../inserts v1")


def test_template_universal_entra_sai_e_respeita_os_outros(tmp_path, fakes):
    project, folder, b = _bancada(tmp_path)
    e = previa_inserts.salvar_universal(project["id"], "inserts v1", {
        "template": "frase", "estilo": "clean", "inicio": 20.0, "textos": {"apoio": "o que muda", "destaque": "estratégia"}})
    novo = next(i for i in e["itens"] if i["universal"])
    assert novo["chave"] == "u-3" and novo["nome"] == "3 o que muda estrategia"  # número seguinte aos da IA
    html = (b / "hf" / "u-3.html").read_text()
    assert "suave(" in html and "realceIn(" not in html.split("// clean")[1]  # clean: sem o realce do perfil
    assert 'suave("#k", 1.15)' in html  # "estratégia" é dita em 21,2 s: entra junto da fala
    jobs = json.loads((b / "jobs.json").read_text())
    assert [j["comp"] for j in jobs] == ["ins-1-gancho-atras", "ins-1-gancho-frente", "ins-2-antes", "u-3"]
    # editar o mesmo id substitui; template inválido não estraga o que havia
    previa_inserts.salvar_universal(project["id"], "inserts v1", {"id": "3", "template": "numero", "inicio": 30,
                                                                    "textos": {"numero": "1.200", "rotulo": "pontos"}})
    assert "conta(\"nv\", 0.15, 1200" in (b / "hf" / "u-3.html").read_text()
    with pytest.raises(editor.EditorError):
        previa_inserts.salvar_universal(project["id"], "inserts v1", {"template": "nao-existe", "textos": {"x": "y"}})
    assert len(json.loads((b / "inserts.json").read_text())["inserts"]) == 1
    e = previa_inserts.remover_universal(project["id"], "inserts v1", "3")
    assert not (b / "hf" / "u-3.html").exists() and [i["chave"] for i in e["itens"]] == ["ins-1-gancho", "ins-2-antes"]


def _wait(job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = editor.load_job(job_id)
        if job and job["status"] not in editor.ACTIVE_STATUSES:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} não terminou")


def test_exportar_so_os_aprovados_sem_ia(tmp_path, fakes):
    project, folder, b = _bancada(tmp_path)
    (b / "kit").mkdir()
    with pytest.raises(editor.EditorError) as exc:
        editor.start_inserts_export_job(project["id"], "inserts v1")
    assert exc.value.status == 409
    previa_inserts.salvar(project["id"], "inserts v1", {"itens": {"ins-1-gancho": {"aprovado": True}}})
    job = editor.start_inserts_export_job(project["id"], "inserts v1", recorte=True)
    assert _wait(job["id"])["status"] == editor.STATUS_CONCLUIDO
    chamada = json.loads((fakes / "auto.json").read_text())
    assert chamada["modo"] == "exportar" and chamada["cwd"] == str(b)
    assert chamada["args"]["comps"] == ["ins-1-gancho-atras", "ins-1-gancho-frente"]
    assert job["tipo"] == editor.TIPO_EXPORTAR and "prompt" in job and "claude" not in str(chamada["args"])


def test_opcoes_dos_inserts_no_pedido(tmp_path, fakes):
    project, folder, b = _bancada(tmp_path)
    job = editor.start_project_job(project["id"], "inserts", "Brutos/bruto.mp4", confirmar_tokens=True,
                                   opcoes={"estilo": "clean", "recorte": False})
    _wait(job["id"])
    p = json.loads((fakes / "claude.json").read_text())["prompt"]
    assert "ESTILO: clean" in p and "não recorte a pessoa" in p and "PARE" in p and "universais" in p
    assert job["inserts"] == {"estilo": "clean", "recorte": False, "manual": False, "previa": True, "estatico": False, "telas": []}
    # no avulso (~/Downloads) não há prévia: o estagiário entrega como antes
    job = editor.start_downloads_job("inserts", str(folder / "Brutos" / "bruto.mp4"), confirmar_tokens=True,
                                     opcoes={"recorte": True})
    _wait(job["id"])
    p = json.loads((fakes / "claude.json").read_text())["prompt"]
    assert "PRÉVIA" not in p and "recorte a pessoa (remove-background)" in p


def test_montar_a_mao_nao_usa_ia(tmp_path, fakes):
    client_dir = tmp_path / "Cliente"
    project, folder, b = _bancada(tmp_path)
    perfil = client_dir / "_Estagiário"
    perfil.mkdir()
    (perfil / "marca.json").write_text(json.dumps(MARCA))
    job = editor.start_project_job(project["id"], "inserts", "Brutos/bruto.mp4", opcoes={"manual": True})  # sem confirmar tokens
    _wait(job["id"])
    chamada = json.loads((fakes / "auto.json").read_text())
    assert chamada["modo"] == "bancada" and job["modo"] == editor.MODO_AUTO
    assert not (fakes / "claude.json").exists()


def test_api_da_previa(tmp_path, fakes):
    from app import create_app
    project, folder, b = _bancada(tmp_path)
    c = create_app().test_client()
    q = {"project_id": project["id"]}
    r = c.get("/api/editor/inserts/inserts v1", query_string=q)
    assert r.status_code == 200 and r.get_json()["total"] == 2
    r = c.put("/api/editor/inserts/inserts v1", json={**q, "itens": {"ins-2-antes": {"aprovado": True}}})
    assert r.get_json()["aprovados"] == 1
    r = c.get(f"/api/editor/inserts-hf/{project['id']}/inserts v1/ins-2-antes.html")
    assert r.status_code == 200 and b"__timelines" in r.data and r.headers["Cache-Control"] == "no-store"
    assert c.get(f"/api/editor/inserts-hf/{project['id']}/inserts v1/assets/fonts/Hando-Black.otf").status_code == 200
    assert c.get(f"/api/editor/inserts-hf/{project['id']}/inserts v1/%2E%2E/job.json").status_code == 404
    r = c.post("/api/editor/inserts/inserts v1/ajuste", json={**q, "chave": "ins-2-antes", "pedido": "maior"})
    assert r.status_code == 428 and r.get_json()["confirmar_tokens"]
    r = c.post("/api/editor/inserts/inserts v1/ajuste", json={**q, "chave": "ins-2-antes", "pedido": "maior", "confirmar_tokens": True})
    assert r.status_code == 200
    _wait(r.get_json()["id"])
    p = json.loads((fakes / "claude.json").read_text())["prompt"]
    assert "hf/ins-2-antes.html" in p and "Não renderize" in p and "maior" in p


def _universais():
    spec = importlib.util.spec_from_file_location("universais_kit", editor.KIT_DIR / "universais.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tempos_seguem_a_fala_e_nunca_voltam():
    u = _universais()
    fala = [{"texto": "Sessenta", "t": 12.4}, {"texto": "milhões,", "t": 12.9}, {"texto": "de", "t": 13.3}]
    spec = {"template": "numero", "inicio": 12.0, "dur": 4.0, "textos": {"numero": "sessenta", "rotulo": "Milhões", "apoio": "xyz"}}
    assert u.tempos(spec, fala) == {"numero": 0.35, "rotulo": 0.85, "apoio": 1.3}
    lista = {"template": "lista", "inicio": 0, "dur": 3, "textos": {"itens": "um\ndois\ntrês"}, "tempos": {"itens1": 2.5}}
    assert u.tempos(lista, []) == {"itens0": 0.15, "itens1": 2.5, "itens2": 2.7}
    assert u.nome_arquivo({"id": "4", "textos": {"numero": "145", "rotulo": "milhões: já?"}}) == "4 145 milhoes ja"
    with pytest.raises(ValueError):
        u.validar({"id": "../x", "template": "frase"})


def test_peso_pelo_nome_do_arquivo():
    f = previa_inserts._peso_estilo
    assert f("built titling bd it") == (700, "italic")
    assert f("HandoTrial-BlackOblique") == (900, "italic")
    assert f("Montserrat-ExtraBold") == (800, "normal")
    assert f("Inter-Regular") == (400, "normal")
