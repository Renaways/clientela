"""Revisão da descrição (briefing do cliente → roteiro por momento) e os marcadores para o Premiere, sem rede."""

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app import create_app
from clientela import autocorte, clients as clients_module, editor, projeto_ia, store, vitrine
from clientela import projects as projects_module

RESPOSTA = {
    "resumo": "Vídeo sobre anúncios políticos no Ceará.", "legenda_padrao": "simples",
    "itens": [
        {"inicio": "02:38", "tipo": "motion_tela_cheia", "cena": "palavra ATENÇÃO sozinha", "efeito": "fade in"},
        {"inicio": "00:33", "tipo": "gancho", "cena": "abertura", "texto_tela": "o ceará voltou a se destacar"},
        {"inicio": "03:05", "fim": "03:17", "tipo": "motion_tela_cheia", "cena": "campanha eleitoral",
         "texto_tela": "73% MAIS CHANCE"},
        {"inicio": "03:26", "tipo": "inventado", "cena": "cursor no balão de comentários"},
        {"inicio": "x", "tipo": "gc", "cena": ""},  # sem cena: some
    ],
    "duvidas": ["Onde está o anexo COMPARATIVO?", " "],
}


def _projeto(tmp_path):
    d = tmp_path / "Cliente"
    d.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(d))
    return projects_module.create_project(client["id"], "Teste")


def test_segundos():
    assert projeto_ia._segundos("02:07") == 127 and projeto_ia._segundos("1:02:03") == 3723
    assert projeto_ia._segundos("0:05,5") == 5.5 and projeto_ia._segundos("agora") is None


def test_normaliza_ordena_e_soma_o_motion():
    r = projeto_ia.normalizar_revisao(RESPOSTA)
    assert [i["inicio"] for i in r["itens"]] == ["00:33", "02:38", "03:05", "03:26"]
    assert r["itens"][-1]["tipo"] == "outro" and r["duvidas"] == ["Onde está o anexo COMPARATIVO?"]
    # 02:38 vai até o próximo momento (03:05) = 27 s, mais 03:05–03:17 = 12 s: 39 s, abaixo do limite
    assert r["alertas"] == []
    mais = projeto_ia.normalizar_revisao({**RESPOSTA, "itens": RESPOSTA["itens"] + [
        {"inicio": "03:30", "fim": "03:40", "tipo": "motion_tela_cheia", "cena": "fim"}]})
    assert "49 s de motion" in mais["alertas"][0]
    solto = projeto_ia.normalizar_revisao({"itens": [{"inicio": "1:00", "tipo": "motion_tela_cheia", "cena": "a"}]})
    assert "sem fim claro" in solto["alertas"][0]


def test_revisao_gratuita_guarda_e_avancada_pede_tokens(tmp_path, monkeypatch):
    project = _projeto(tmp_path)
    prompts = []
    monkeypatch.setattr(vitrine, "_chave", lambda: "chave")
    monkeypatch.setattr(autocorte, "llm_openrouter", lambda p, cfg, **k: (prompts.append(p), (RESPOSTA, 0))[1])
    with pytest.raises(LookupError):
        projeto_ia.revisar_descricao(project["id"])
    projects_module.set_notes(project["id"], "* 02:07 inserir GC - João")
    r = projeto_ia.revisar_descricao(project["id"])
    assert "inserir GC - João" in prompts[-1] and not r["avancada"]
    assert projeto_ia.revisao_salva(project["id"])["atual"]
    projects_module.set_notes(project["id"], "outra coisa")
    assert not projeto_ia.revisao_salva(project["id"])["atual"]

    client = create_app().test_client()
    monkeypatch.setattr(editor, "find_claude", lambda: "/bin/claude")
    resp = client.post("/api/projects/descricao-revisada", json={"project_id": project["id"], "avancada": True})
    assert resp.status_code == 428 and resp.get_json()["confirmar_tokens"]
    monkeypatch.setattr(autocorte, "llm_claude", lambda p, cfg, b, **k: (RESPOSTA, 0.1))
    resp = client.post("/api/projects/descricao-revisada",
                       json={"project_id": project["id"], "avancada": True, "confirmar_tokens": True, "descricao": "x"})
    assert resp.status_code == 200 and resp.get_json()["avancada"]


def test_xml_do_premiere_tem_um_marcador_por_momento(tmp_path):
    itens = projeto_ia.normalizar_revisao(RESPOSTA)["itens"]
    video = tmp_path / "bruto & cia.mp4"
    xml = projeto_ia.xml_premiere(video, {"largura": 1920, "altura": 1080, "fps": 29.97, "duracao": 240.0}, itens, "B")
    raiz = ET.fromstring(xml.split("\n", 2)[2])
    seq = raiz.find("sequence")
    assert seq.find("rate/ntsc").text == "TRUE" and seq.find("rate/timebase").text == "30"
    ms = seq.findall("marker")
    assert [m.find("name").text for m in ms] == ["Gancho", "Motion tela cheia", "Motion tela cheia", "Outro"]
    assert ms[0].find("in").text == str(round(33 * 29.97)) and ms[0].find("out").text == "-1"
    assert ms[2].find("out").text == str(round(197 * 29.97))
    assert "Texto: 73% MAIS CHANCE" in ms[2].find("comment").text
    assert seq.find(".//pathurl").text == video.as_uri()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="sem ffmpeg")
def test_marcadores_gravam_xml_na_pasta_sem_tocar_no_bruto(tmp_path, monkeypatch):
    project = _projeto(tmp_path)
    folder = Path(project["path"])
    (folder / "Brutos").mkdir(exist_ok=True)
    bruto = folder / "Brutos" / "bruto.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=320x240:r=25:d=2", str(bruto)], check=True)
    antes = bruto.read_bytes()
    with pytest.raises(LookupError):  # sem revisão não há marcadores
        projeto_ia.marcadores_premiere(project["id"])
    store.update(project["id"], lambda r: r.__setitem__("descricao_revisada", projeto_ia.normalizar_revisao(
        {"itens": [{"inicio": "0:01", "tipo": "gc", "cena": "GC do João"}]})))
    r = projeto_ia.marcadores_premiere(project["id"])
    xml = Path(r["arquivo"]).read_text()
    assert r["marcadores"] == 1 and "<in>25</in>" in xml and "<timebase>25</timebase>" in xml
    assert bruto.read_bytes() == antes


def test_modelos_de_ia_configuraveis(tmp_path, monkeypatch):
    assert editor.get_config()["ia"]["openrouter_modelo"] == "openrouter/free"
    with pytest.raises(editor.EditorError):
        editor.save_config({"ia": {"temperatura": "quente"}})
    cfg = editor.save_config({"ia": {"openrouter_modelo": " google/gemini-2.5-flash ", "temperatura": 5,
                                     "timeout": "30", "claude_modelo": "haiku", "lixo": 1}})
    assert cfg["ia"] == {"openrouter_modelo": "google/gemini-2.5-flash", "temperatura": 2.0, "timeout": 30,
                         "claude_modelo": "haiku"}
    assert editor.save_config({"ia": {"openrouter_modelo": ""}})["ia"]["openrouter_modelo"] == "openrouter/free"

    editor.save_config({"ia": {"openrouter_modelo": "x/y", "temperatura": 0.7}})
    vistos = []
    monkeypatch.setattr(vitrine, "_chave", lambda: "chave")
    monkeypatch.setattr(autocorte, "llm_openrouter",
                        lambda p, cfg, **k: (vistos.append((cfg, k)), ({"resposta": "sou o Y", **RESPOSTA}, 0))[1])
    project = _projeto(tmp_path)
    assert projeto_ia.revisar_descricao(project["id"], "02:07 GC")["modelo"] in ("x/y", *projeto_ia.RESERVAS_REVISAO)
    pedido = next(c for c, k in vistos if c["openrouter_modelo"] == "x/y")  # o escolhido entra sempre na corrida
    assert pedido["temperatura"] == 0.7 and all(k["timeout"] == 30 for _, k in vistos)

    client = create_app().test_client()
    r = client.post("/api/ia/testar", json={"modelo": "outro/modelo"}).get_json()
    assert r["modelo"] == "outro/modelo" and r["resposta"] == "sou o Y"
    vistos_claude = []
    monkeypatch.setattr(editor, "find_claude", lambda: "/bin/claude")
    monkeypatch.setattr(autocorte, "llm_claude", lambda p, cfg, b, **k: (vistos_claude.append(cfg), (RESPOSTA, 0))[1])
    rev = projeto_ia.revisar_descricao(project["id"], "x", avancada=True, confirmar_tokens=True)
    assert vistos_claude[-1]["modelo_llm"] == "haiku" and rev["modelo"] == "claude · haiku"


def test_prazo_de_verdade_mesmo_com_modelo_lento(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(autocorte, "llm_openrouter", lambda p, cfg, **k: (time.sleep(3), (RESPOSTA, 0))[1])
    t0 = time.monotonic()
    with pytest.raises(autocorte.Falha, match="não respondeu em 1 s"):
        vitrine.com_prazo(lambda: autocorte.llm_openrouter("", {}), 1, "lento/modelo")
    assert time.monotonic() - t0 < 2


def test_editar_revisao_a_mao_mantem_o_modelo(tmp_path, monkeypatch):
    project = _projeto(tmp_path)
    client = create_app().test_client()
    url = "/api/projects/descricao-revisada"
    assert client.put(url, json={"project_id": project["id"], "revisao": {"itens": []}}).status_code == 409
    monkeypatch.setattr(vitrine, "_chave", lambda: "chave")
    monkeypatch.setattr(autocorte, "llm_openrouter", lambda p, cfg, **k: (RESPOSTA, 0))
    projects_module.set_notes(project["id"], "briefing")
    rev = projeto_ia.revisar_descricao(project["id"])
    rev["itens"][0]["cena"] = "abertura editada"
    rev["itens"].append({"inicio": "0:10", "fim": "1:00", "tipo": "motion_tela_cheia", "cena": "novo"})
    resp = client.put(url, json={"project_id": project["id"], "revisao": rev})
    assert resp.status_code == 200
    salvo = projeto_ia.revisao_salva(project["id"])
    assert salvo["atual"] and salvo["revisao"]["modelo"] == rev["modelo"] and salvo["revisao"]["editado_em"]
    assert [i["cena"] for i in salvo["revisao"]["itens"]][:2] == ["novo", "abertura editada"]
    assert "motion" in salvo["revisao"]["alertas"][0]  # 50 s do novo trecho: os alertas são recalculados


def test_revisao_fica_com_o_primeiro_modelo_que_responde(tmp_path, monkeypatch):
    import time
    project = _projeto(tmp_path)
    projects_module.set_notes(project["id"], "briefing")
    monkeypatch.setattr(vitrine, "_chave", lambda: "chave")
    monkeypatch.setattr(editor, "get_config", lambda: {"ia": {**editor.DEFAULT_CONFIG["ia"], "openrouter_modelo": "lento/x", "timeout": 5}})

    def falso(p, cfg, **k):
        m = cfg["openrouter_modelo"]
        if m == "lento/x":
            time.sleep(3)
        if m == "openrouter/free":
            raise autocorte.Falha("a OpenRouter recusou (429)")
        return (RESPOSTA if m != "lento/x" else {"itens": []}), 0
    monkeypatch.setattr(autocorte, "llm_openrouter", falso)
    t0 = time.monotonic()
    rev = projeto_ia.revisar_descricao(project["id"])
    assert rev["modelo"] == "nvidia/nemotron-3.5-super-vl:free" and time.monotonic() - t0 < 2

    monkeypatch.setattr(autocorte, "llm_openrouter", lambda p, cfg, **k: (_ for _ in ()).throw(autocorte.Falha("404")))
    with pytest.raises(autocorte.Falha, match="nenhum modelo gratuito"):
        projeto_ia.revisar_descricao(project["id"])


def test_boot_id_para_a_pagina_recarregar_sozinha():
    client = create_app().test_client()
    a = client.get("/api/boot").get_json()["id"]
    assert a and client.get("/api/boot").get_json()["id"] == a
