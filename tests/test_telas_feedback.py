"""Telas de insert em cards (nível de animação, tela fixa primeiro, animar só as aprovadas), o feedback das
ferramentas em data/feedback.json e a IA gratuita lendo o projeto (resumo + sugestões de telas), sem rede."""

import json
from pathlib import Path

import pytest

from app import create_app
from clientela import aprendizado, autocorte, editor, feedback, identidades, paths, previa_inserts, projeto_ia, store, vitrine
from clientela import projects as projects_module
from tests.test_previa_inserts import _bancada, _comp, _wait, fakes  # noqa: F401  (fixture)


def test_telas_e_tela_fixa_vao_para_o_pedido_e_para_a_bancada(tmp_path, fakes):  # noqa: F811
    project, folder, b = _bancada(tmp_path)
    telas = [{"titulo": "Orçamento", "descricao": '"R$ 2 milhões" quando fala do orçamento', "nivel": 9},
             {"titulo": "vazia", "descricao": "   "},
             {"descricao": "Lista dos 3 passos", "nivel": 0}]
    job = editor.start_project_job(project["id"], "inserts", "Brutos/bruto.mp4", confirmar_tokens=True,
                                   opcoes={"estatico": True, "telas": telas})
    _wait(job["id"])
    assert job["inserts"]["telas"] == [
        {"titulo": "Orçamento", "descricao": '"R$ 2 milhões" quando fala do orçamento', "nivel": 3},
        {"titulo": "", "descricao": "Lista dos 3 passos", "nivel": 0}]
    p = json.loads((fakes / "claude.json").read_text())["prompt"]
    assert "Tela 1 — Orçamento" in p and "nível 3" in p and "Tela 2: Lista dos 3 passos" in p and "nível 0" in p
    assert "tela-<n>" in p and "TELA FIXA PRIMEIRO" in p
    salvo = json.loads((b / editor.TELAS_JSON).read_text())
    assert salvo["estatico"] and len(salvo["telas"]) == 2 and salvo["animar"] == []
    # sem prévia (avulso) não existe tela fixa: ninguém aprovaria
    assert not editor.inserts_opcoes({"estatico": True}, previa=False)["estatico"]


def _com_telas(b):
    (b / editor.TELAS_JSON).write_text(json.dumps({"estatico": True, "animar": [], "telas": [
        {"titulo": "A", "descricao": "primeira", "nivel": 1}, {"titulo": "B", "descricao": "segunda", "nivel": 3}]}))
    jobs = json.loads((b / "jobs.json").read_text())
    jobs.append({"comp": "tela-1", "inicio": 30.0, "nome": "Tela 1 A"})
    (b / "jobs.json").write_text(json.dumps(jobs))
    (b / "hf" / "tela-1.html").write_text(_comp("tela-1", 3))
    # a tela 2 já está no hf/ mas a IA ainda não escreveu o jobs.json: o card já mostra
    (b / "hf" / "tela-2-atras.html").write_text(_comp("tela-2-atras", 3))
    (b / "hf" / "tela-2-frente.html").write_text(_comp("tela-2-frente", 3))


def test_estado_casa_card_com_composicao(tmp_path):
    project, folder, b = _bancada(tmp_path)
    _com_telas(b)
    e = previa_inserts.estado(project["id"], "inserts v1")
    assert e["estatico"] and len(e["telas"]) == 2
    assert e["telas_prontas"] == {"1": ["tela-1"], "2": ["tela-2-atras", "tela-2-frente"]}
    t1 = next(i for i in e["itens"] if i["chave"] == "tela-1")
    assert t1["tela"]["numero"] == 1 and t1["tela"]["nivel"] == 1 and not t1["animada"]
    assert next(i for i in e["itens"] if i["chave"] == "ins-1-gancho")["tela"] is None


def test_animar_so_as_aprovadas_uma_vez(tmp_path, fakes):  # noqa: F811
    project, folder, b = _bancada(tmp_path)
    _com_telas(b)
    with pytest.raises(editor.EditorError) as exc:
        editor.start_animate_job(project["id"], "inserts v1", confirmar_tokens=True)
    assert exc.value.status == 409
    previa_inserts.salvar(project["id"], "inserts v1", {"itens": {"tela-1": {"aprovado": True}}})
    with pytest.raises(editor.EditorError) as exc:  # gasta tokens: pede o sim
        editor.start_animate_job(project["id"], "inserts v1")
    assert exc.value.status == 428
    job = editor.start_animate_job(project["id"], "inserts v1", confirmar_tokens=True)
    _wait(job["id"])
    p = json.loads((fakes / "claude.json").read_text())["prompt"]
    assert "aprovadas paradas" in p and "hf/tela-1.html" in p and "nível 1" in p and "ins-1-gancho" not in p
    assert json.loads((b / editor.TELAS_JSON).read_text())["animar"] == ["tela-1"]
    assert next(i for i in previa_inserts.estado(project["id"], "inserts v1")["itens"] if i["chave"] == "tela-1")["animada"]
    with pytest.raises(editor.EditorError):
        editor.start_animate_job(project["id"], "inserts v1", confirmar_tokens=True)


# ------------------------------------------------------------------ feedback
def test_feedback_vai_para_o_arquivo_e_resume(tmp_path):
    feedback.registrar({"ferramenta": "Inserts", "nota": "ruim", "tipo": "bug", "texto": "demorou", "tela": "#/"})
    feedback.registrar({"ferramenta": "Inserts", "nota": "boa"})
    feedback.registrar({"ferramenta": "GC", "nota": "boa", "tipo": "xpto"})
    salvo = json.loads((paths.DATA_DIR / "feedback.json").read_text())
    assert [e["ferramenta"] for e in salvo] == ["Inserts", "Inserts", "GC"] and salvo[2]["tipo"] == "outro"
    assert feedback.resumo()[0] == {"ferramenta": "Inserts", "boa": 1, "ok": 0, "ruim": 1, "total": 2}
    for ruim in ({"nota": "boa"}, {"ferramenta": "x", "nota": "ótima"}):
        with pytest.raises(ValueError):
            feedback.registrar(ruim)


def test_api_do_feedback(tmp_path):
    c = create_app().test_client()
    assert c.post("/api/feedback", json={"ferramenta": "Timeline", "nota": "ok"}).status_code == 201
    assert c.post("/api/feedback", json={"nota": "ok"}).status_code == 400
    assert c.get("/api/feedback").get_json()["resumo"][0]["total"] == 1


# ------------------------------------------------------------------ IA do projeto
@pytest.fixture
def llm(monkeypatch):
    chamadas = []

    def falso(prompt, cfg, timeout=300, imagens=()):
        chamadas.append(prompt)
        if '"resposta"' in prompt:
            return {"resposta": "O cliente pediu 30 s."}, 0
        if "transcrição" in prompt.lower() and "telas" in prompt:
            return {"telas": [{"titulo": "Dois milhões", "descricao": '"R$ 2 mi" em destaque', "momento": "0:21", "nivel": 7},
                              {"titulo": "sem descrição"}]}, 0
        return {"sobre": "Vídeo institucional.", "esperado": "Um reel de 30 s.", "sugestoes": ["Pedir a logo", ""],
                "broll": [{"momento": "0:20", "ideia": "planilha de orçamento"}, {"ideia": ""}]}, 0

    monkeypatch.setattr(autocorte, "llm_openrouter", falso)
    monkeypatch.setattr(vitrine, "_chave", lambda: "chave")
    return chamadas


def test_resumo_do_projeto_guarda_e_sabe_quando_ficou_velho(tmp_path, llm):
    project, folder, b = _bancada(tmp_path)
    with pytest.raises(LookupError):  # sem descrição não há de onde tirar contexto
        projeto_ia.gerar_resumo(project["id"])
    projects_module.set_notes(project["id"], "Reel de 30 s sobre o orçamento da campanha")
    assert projeto_ia.estado(project["id"])["resumo"] is None
    r = projeto_ia.gerar_resumo(project["id"])
    assert r["sobre"] == "Vídeo institucional." and r["sugestoes"] == ["Pedir a logo"]
    assert "orçamento da campanha" in llm[-1]
    assert store.load(project["id"])["ia_resumo"]["sobre"] == "Vídeo institucional."
    assert projeto_ia.estado(project["id"])["atual"]
    projects_module.set_notes(project["id"], "Agora são dois reels")
    assert not projeto_ia.estado(project["id"])["atual"]


def test_sugerir_telas_le_a_transcricao_da_pasta(tmp_path, llm):
    project, folder, b = _bancada(tmp_path)
    with pytest.raises(LookupError):
        projeto_ia.sugerir_telas(project["id"])
    (folder / "Brutos" / "bruto.json").write_text(json.dumps({"segments": [{"words": [
        {"text": "o", "start": 20.0}, {"text": "orçamento", "start": 20.4}, {"text": "subiu", "start": 30.5}]}]}))
    r = projeto_ia.sugerir_telas(project["id"])
    assert r["transcricao"] == "Brutos/bruto.json"
    assert r["telas"] == [{"titulo": "Dois milhões", "descricao": '[0:21] "R$ 2 mi" em destaque', "nivel": 3}]
    assert "[0:20] o orçamento\n[0:30] subiu" in llm[-1]


def test_api_da_ia_do_projeto(tmp_path, llm):
    project, folder, b = _bancada(tmp_path)
    c = create_app().test_client()
    q = f"?project_id={project['id']}"
    assert c.post("/api/projects/ia" + q).status_code == 409
    projects_module.set_notes(project["id"], "Pedido")
    assert c.post("/api/projects/ia" + q).get_json()["sobre"]
    assert c.get("/api/projects/ia" + q).get_json()["atual"]
    assert c.post("/api/editor/inserts-sugestoes", json={"project_id": project["id"]}).status_code == 409
    assert c.get("/api/projects/ia?project_id=nada").status_code == 409


def _transcricao(folder):
    (folder / "Brutos" / "bruto.json").write_text(json.dumps({"segments": [{"words": [
        {"text": "o", "start": 20.0}, {"text": "orçamento", "start": 20.4}]}]}))


def test_transcricao_nova_refaz_a_analise_com_broll(tmp_path, llm):
    project, folder, b = _bancada(tmp_path)
    projects_module.set_notes(project["id"], "Reel")
    r = projeto_ia.gerar_resumo(project["id"])
    assert r["broll"] == [] and r["transcricao_id"] == ""  # sem transcrição, B-roll seria chute
    assert "TRANSCRIÇÃO" not in llm[-1] and not projeto_ia.estado(project["id"])["nova_transcricao"]
    _transcricao(folder)
    e = projeto_ia.estado(project["id"])
    assert e["nova_transcricao"] and not e["atual"]
    r = projeto_ia.gerar_resumo(project["id"])
    assert "[0:20] o orçamento" in llm[-1] and r["broll"] == [{"momento": "0:20", "ideia": "planilha de orçamento"}]
    e = projeto_ia.estado(project["id"])
    assert e["atual"] and not e["nova_transcricao"]


def test_chat_do_projeto_guarda_a_conversa(tmp_path, llm):
    project, folder, b = _bancada(tmp_path)
    projects_module.set_notes(project["id"], "Reel de 30 s")
    _transcricao(folder)
    c = create_app().test_client()
    q = f"?project_id={project['id']}"
    r = c.post("/api/projects/chat" + q, json={"mensagem": "Qual a duração?", "modo": "mensagem"}).get_json()
    assert r["texto"] == "O cliente pediu 30 s." and r["modo"] == "mensagem"
    assert "Reel de 30 s" in llm[-1] and "orçamento" in llm[-1] and "mensagem pronta" in llm[-1]
    r = c.post("/api/projects/chat" + q, json={"mensagem": "Gere a copy", "modo": "copy"}).get_json()
    assert r["modo"] == "copy" and "hashtags" in llm[-1]
    c.post("/api/projects/chat" + q, json={"mensagem": "E o formato?"})
    assert "editor: Qual a duração?" in llm[-1]  # a conversa volta no pedido seguinte
    assert [m["papel"] for m in c.get("/api/projects/chat" + q).get_json()] == ["editor", "ia"] * 3
    assert c.post("/api/projects/chat" + q, json={"mensagem": " "}).status_code == 409
    c.delete("/api/projects/chat" + q)
    assert c.get("/api/projects/chat" + q).get_json() == []


def test_avaliacao_vira_documento_e_licao_no_proximo_pedido(tmp_path, fakes):  # noqa: F811
    project, folder, b = _bancada(tmp_path)
    job = editor.start_project_job(project["id"], "inserts", "Brutos/bruto.mp4", confirmar_tokens=True)
    job = _wait(job["id"])
    c = create_app().test_client()
    assert c.post(f"/api/editor/jobs/{job['id']}/avaliacao", json={"nota": 9}).status_code == 400
    r = c.post(f"/api/editor/jobs/{job['id']}/avaliacao", json={
        "nota": 2, "ruim": "todas as telas iguais", "fazer_diferente": "variar a composição"})
    assert r.status_code == 200
    c.post(f"/api/editor/jobs/{job['id']}/avaliacao", json={"nota": 3, "ruim": "todas as telas iguais"})
    assert len(aprendizado.listar()) == 1  # reavaliar troca, não duplica
    assert editor.job_view(job["id"])["avaliacao"]["nota"] == 3
    doc = (paths.EDITOR_DIR / "APRENDIZADOS.md").read_text()
    assert "## Inserts" in doc and "todas as telas iguais" in doc
    job2 = editor.start_project_job(project["id"], "inserts", "Brutos/bruto.mp4", confirmar_tokens=True)
    _wait(job2["id"])
    p = json.loads((fakes / "claude.json").read_text())["prompt"]
    assert "LIÇÕES" in p and "todas as telas iguais" in p and "(deste cliente)" in p and "CRIATIVIDADE" in p
    assert aprendizado.licoes("gc", None) == ""


def test_projetos_aprovados_como_referencia(tmp_path, fakes):  # noqa: F811
    project, folder, b = _bancada(tmp_path)
    ref = projects_module.create_project(project["client_id"], "Aprovado")
    c = create_app().test_client()
    r = c.put("/api/projects/referencia", json={"project_id": ref["id"], "ativo": True}).get_json()
    assert r["ativo"] and r["referencias"][0]["pasta"] == ref["path"]
    assert c.get(f"/api/projects/referencia?project_id={project['id']}").get_json()["ativo"] is False
    job = editor.start_project_job(project["id"], "inserts", "Brutos/bruto.mp4", confirmar_tokens=True)
    _wait(job["id"])
    chamada = json.loads((fakes / "claude.json").read_text())
    assert "REFERÊNCIAS APROVADAS" in chamada["prompt"] and ref["path"] in chamada["prompt"]
    assert ref["path"] in chamada["argv"]  # --add-dir: a pasta irmã fica legível
    texto, _ = identidades.referencias_texto(project["client_id"], exceto=ref["id"])
    assert texto == ""  # o próprio projeto não é referência de si mesmo
    c.put("/api/projects/referencia", json={"project_id": ref["id"], "ativo": False})
    assert identidades.referencias_projetos(project["client_id"]) == []


def test_destino_do_feedback_vazio_por_padrao_e_sobrescrito_pelo_ambiente(monkeypatch):
    from app import create_app
    from clientela import feedback
    monkeypatch.delenv("CLIENTELA_FEEDBACK_FORM", raising=False)
    monkeypatch.delenv("CLIENTELA_FEEDBACK_REPO", raising=False)
    monkeypatch.setattr(feedback, "GOOGLE_FORM_LINK", "")
    monkeypatch.setattr(feedback, "GITHUB_REPO", "")
    assert create_app().test_client().get("/api/feedback").get_json()["destino"] == {"google_form": None, "github_repo": ""}
    monkeypatch.setenv("CLIENTELA_FEEDBACK_REPO", "dono/repo")
    assert feedback.destino()["github_repo"] == "dono/repo"


def test_link_pre_preenchido_do_google_forms_vira_os_campos():
    from clientela.feedback import ler_link_do_form
    link = ("https://docs.google.com/forms/d/e/1FAIpQL-abc_123/viewform?usp=pp_url"
            "&entry.111=ferramenta&entry.222=Nota&entry.333=tipo&entry.444=texto&entry.555=sistema&entry.666=email")
    assert ler_link_do_form(link) == {
        "acao": "https://docs.google.com/forms/d/e/1FAIpQL-abc_123/formResponse",
        "campos": {"ferramenta": "entry.111", "nota": "entry.222", "tipo": "entry.333", "texto": "entry.444",
                   "sistema": "entry.555", "email": "entry.666"},
    }
    # sem os campos essenciais, de outro site ou vazio: sem envio
    assert ler_link_do_form("https://docs.google.com/forms/d/e/x/viewform?entry.1=ferramenta") is None
    assert ler_link_do_form("https://evil.example/forms/d/e/x/viewform?entry.1=ferramenta&entry.2=texto") is None
    assert ler_link_do_form("") is None
