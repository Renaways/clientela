"""Fase 4: checklist de ambiente (fontes instaladas, pastas, mídias linkadas em .clientela e .prproj),
calendário gerencial (entrada, horas, liberação) e a lista/decisão de revisões que a tela do celular usa."""

import gzip
import json
import struct
from datetime import datetime, timedelta
from pathlib import Path

from app import create_app
from clientela import ambiente, dashboard, editor, feed, files_native, store
from clientela import clients as clients_module
from clientela import projects as projects_module


def _fonte(path: Path, familia: str) -> Path:
    """Um sfnt mínimo, só com a tabela name (nameID 1, Windows/UTF-16BE): o que familias_da_fonte lê."""
    texto = familia.encode("utf-16-be")
    nome = struct.pack(">HHH", 0, 1, 6 + 12) + struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(texto), 0) + texto
    cab = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0) + struct.pack(">4sIII", b"name", 0, 12 + 16, len(nome))
    path.write_bytes(cab + nome)
    return path


def _prproj(path: Path, medias: list[tuple[str, str]]) -> Path:
    corpo = "".join(f"<Media ObjectUID=\"{k}\"><ActualMediaFilePath>{a}</ActualMediaFilePath>"
                    f"<RelativePath>{r}</RelativePath></Media>" for k, (a, r) in enumerate(medias))
    with gzip.open(path, "wb") as f:
        f.write(f"<?xml version=\"1.0\"?><PremiereData>{corpo}</PremiereData>".encode())
    return path


def _cliente(tmp_path):
    d = tmp_path / "Cliente"
    d.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(d))
    return client, d


# ------------------------------------------------------------------ fontes
def test_familia_vem_da_tabela_name_do_arquivo(tmp_path):
    f = _fonte(tmp_path / "HandoTrial-Bold.otf", "Hando Trial")
    assert ambiente.familias_da_fonte(f) == {"Hando Trial"}
    assert ambiente.familias_da_fonte(tmp_path / "nao-existe.ttf") == set()
    (tmp_path / "lixo.ttf").write_bytes(b"\x00\x01")
    assert ambiente.familias_da_fonte(tmp_path / "lixo.ttf") == set()


def test_fonte_do_cliente_falta_e_instala(tmp_path):
    client, d = _cliente(tmp_path)
    fontes = d / "_Estagiário" / "assets" / "fonts"
    fontes.mkdir(parents=True)
    _fonte(fontes / "MarcaDisplay-Black.otf", "Marca Display")
    _fonte(fontes / "MarcaDisplay-Regular.otf", "Marca Display")
    (d / "_Estagiário" / "marca.json").write_text(json.dumps({"tipografia": {"fontes": [
        {"familia": "Marca", "arquivo": "MarcaDisplay-Black.otf"}, {"familia": "Sumida", "arquivo": "sumida.otf"}]}}))
    rel = ambiente.checar_cliente(client["id"])
    fontes_itens = {i["titulo"]: i for i in rel["itens"] if i["grupo"] == "fontes"}
    # "Marca" do marca.json é a "Marca Display" do arquivo; "Sumida" não tem arquivo nem está instalada
    assert set(fontes_itens) == {"Marca Display", "Sumida"}
    assert fontes_itens["Marca Display"]["estado"] == "falta"
    assert fontes_itens["Marca Display"]["acao"] == {"tipo": "instalar_fonte", "familia": "Marca Display"}
    assert fontes_itens["Sumida"]["estado"] == "falta" and "acao" not in fontes_itens["Sumida"]
    c = create_app().test_client()
    r = c.post("/api/ambiente/fonte", json={"client_id": client["id"], "familia": "Marca Display"}).get_json()
    assert sorted(r["copiados"]) == ["MarcaDisplay-Black.otf", "MarcaDisplay-Regular.otf"]
    destino = ambiente.pasta_de_fontes_do_usuario()
    assert (destino / "MarcaDisplay-Black.otf").is_file()
    novo = {i["titulo"]: i["estado"] for i in r["relatorio"]["itens"] if i["grupo"] == "fontes"}
    assert novo["Marca Display"] == "ok"
    # de novo: nada é sobrescrito
    assert c.post("/api/ambiente/fonte", json={"client_id": client["id"], "familia": "Marca Display"}).get_json()["copiados"] == []
    assert c.post("/api/ambiente/fonte", json={"client_id": client["id"], "familia": "Outra"}).status_code == 400


# ------------------------------------------------------------------ mídias linkadas
def test_prproj_religa_pelo_relativo_e_pela_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Drive" / "Trilhas").mkdir(parents=True)
    (home / "Drive" / "Trilhas" / "musica.mp3").write_bytes(b"x")
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    (proj / "Brutos").mkdir(parents=True)
    (proj / "Brutos" / "a.mp4").write_bytes(b"x")
    (proj / "Brutos" / "b.mp4").write_bytes(b"x")
    pr = _prproj(proj / "edicao.prproj", [
        (str(proj / "Brutos" / "a.mp4"), ""),                                   # existe
        ("/Users/outro/Projetos/x/Brutos/b.mp4", "Brutos/b.mp4"),              # religa pelo relativo
        ("/Users/outra_pessoa/Drive/Trilhas/musica.mp3", "../../nada.mp3"),          # mesma coisa na home deste usuário
        ("/Users/outra_pessoa/Documents/MGT/abc/GC.aegraphic", ""),                   # template do Premiere: ignora
        ("/Users/outro/sumiu.wav", "sumiu.wav"),                               # falta de verdade
        ("G:\\Meu Drive\\clipe.mov", "nada/clipe.mov"),                        # outro sistema
        ("1112293707", ""),                                                    # não é caminho
        ("/Users/outro/a &amp; b.mp4", ""),                                    # entidade XML decodificada
    ])
    m = {x["caminho"]: x for x in ambiente.midias_do_prproj(pr)}
    assert set(m) == {str(proj / "Brutos" / "a.mp4"), "/Users/outro/Projetos/x/Brutos/b.mp4", "/Users/outra_pessoa/Drive/Trilhas/musica.mp3",
                      "/Users/outro/sumiu.wav", "G:\\Meu Drive\\clipe.mov", "/Users/outro/a & b.mp4"}
    assert [k for k, v in m.items() if not v["existe"]] == ["/Users/outro/sumiu.wav", "G:\\Meu Drive\\clipe.mov", "/Users/outro/a & b.mp4"]
    assert m["G:\\Meu Drive\\clipe.mov"]["outro_sistema"] and not m["/Users/outro/sumiu.wav"]["outro_sistema"]
    (tmp_path / "quebrado.prproj").write_bytes(b"isto nao e gzip")
    assert ambiente.midias_do_prproj(tmp_path / "quebrado.prproj") == []


def test_checagem_do_cliente_junta_pastas_e_midias_dos_projetos_abertos(tmp_path):
    client, d = _cliente(tmp_path)
    aberto = projects_module.create_project(client["id"], "Vídeo")
    pdir = Path(aberto["path"])
    (pdir / aberto["folder_name"]).mkdir(exist_ok=True)
    _prproj(pdir / aberto["folder_name"] / "Vídeo.prproj", [("/Users/outro/sumiu.wav", "")])
    v = editor.create_sequence(pdir, "Seq")
    doc = v["doc"]
    doc["midias"] = [{"id": "m", "caminho": "Brutos/fora.mp4", "duracao": 3, "audios": 1}]
    editor.save_sequence(pdir, v["arquivo"], doc)
    fechado = projects_module.create_project(client["id"], "Antigo")
    store.update(fechado["id"], lambda r: r.__setitem__("stage", "concluido"))
    _prproj(Path(fechado["path"]) / "Antigo.prproj", [("/Users/outro/sumiu.wav", "")])   # concluído não entra
    perdido = projects_module.create_project(client["id"], "Sumiu")
    import shutil
    shutil.rmtree(perdido["path"])
    clients_module.update_client(client["id"], template_path_premiere=str(tmp_path / "modelo.prproj"))
    rel = create_app().test_client().get(f"/api/ambiente/{client['id']}?forcar=1").get_json()
    por = {(i["grupo"], i["titulo"]): i for i in rel["itens"]}
    assert por[("pastas", "Pasta do cliente")]["estado"] == "ok"
    assert por[("pastas", "Perfil de IDV (_Estagiário)")]["estado"] == "aviso"
    assert por[("pastas", "Modelo do Premiere")]["estado"] == "falta"
    assert por[("pastas", f"{perdido['num']} · Sumiu")]["estado"] == "aviso"
    midias = [i for i in rel["itens"] if i["grupo"] == "midias"]
    assert sorted(i["titulo"] for i in midias) == [f"{aberto['num']} · Seq.clientela", f"{aberto['num']} · Vídeo.prproj"]
    assert all(i["estado"] == "falta" for i in midias)
    assert rel["resumo"]["falta"] == 3
    # o resumo geral (o chip do card) usa a última varredura
    geral = create_app().test_client().get("/api/ambiente").get_json()
    assert geral["clientes"][client["id"]]["resumo"]["falta"] == 3 and "app" in geral


def test_revelar_so_dentro_do_cliente(tmp_path, monkeypatch):
    client, d = _cliente(tmp_path)
    abertos = []
    monkeypatch.setattr(files_native, "reveal_in_finder", lambda p: abertos.append(p) or True)
    c = create_app().test_client()
    assert c.post("/api/ambiente/revelar", json={"client_id": client["id"], "caminho": str(d)}).status_code == 200
    fora = tmp_path / "fora.txt"
    fora.write_text("x")
    assert c.post("/api/ambiente/revelar", json={"client_id": client["id"], "caminho": str(fora)}).status_code == 404
    assert abertos == [str(d)]


# ------------------------------------------------------------------ calendário
def _iso(d: datetime) -> str:
    return d.isoformat()


def test_calendario_horas_entradas_e_liberacoes(tmp_path):
    client, d = _cliente(tmp_path)
    p = projects_module.create_project(client["id"], "Clipe")
    q = projects_module.create_project(client["id"], "Aberto")
    base = datetime(2026, 9, 10, 10, 0)
    sessoes = [{"start": _iso(base), "end": _iso(base + timedelta(hours=2)), "seconds": 7200, "business_seconds": 7200},
               {"start": _iso(base + timedelta(days=1)), "end": _iso(base + timedelta(days=1, minutes=5)), "seconds": 300, "business_seconds": 300},  # < 15 min: não conta
               {"start": _iso(base + timedelta(days=2)), "seconds": 3600, "manual": True}]

    def prepara(r):
        r["created_at"] = datetime(2026, 9, 20).timestamp()   # importado depois: a entrada vem do histórico
        r["history"] = [{"action": "criado", "at": "2026-09-08T12:00:00+00:00", "details": {}},
                        {"action": "etapa_alterada", "at": "2026-09-14T12:00:00+00:00", "details": {"de": "producao", "para": "liberado"}}]
        r["stage"] = "liberado"
        r["timer"]["sessions"] = sessoes
    store.update(p["id"], prepara)
    store.update(q["id"], lambda r: r.update(created_at=datetime(2026, 9, 25, 9).timestamp(), history=[]))
    cal = create_app().test_client().get("/api/calendario?mes=2026-09").get_json()
    assert cal["inicio"] == "2026-09-01" and cal["fim"] == "2026-09-30" and len(cal["dias"]) == 30
    assert cal["dias"]["2026-09-10"]["segundos"] == 7200 and cal["dias"]["2026-09-11"]["segundos"] == 0
    assert cal["dias"]["2026-09-12"]["segundos"] == 3600
    assert cal["dias"]["2026-09-08"]["entradas"] == [p["id"]] and cal["dias"]["2026-09-14"]["liberacoes"] == [p["id"]]
    pp = next(x for x in cal["projetos"] if x["id"] == p["id"])
    assert (pp["entrada"], pp["liberado"], pp["lead_dias"], pp["segundos_mes"]) == ("2026-09-08", "2026-09-14", 6, 10800)
    qq = next(x for x in cal["projetos"] if x["id"] == q["id"])
    assert qq["liberado"] is None and qq["entrada"] == "2026-09-25"
    assert cal["resumo"] == {"segundos": 10800, "entradas": 2, "liberacoes": 1, "lead_medio": 6.0, "dias_trabalhados": 2}
    # outro mês: as horas de setembro não vazam
    out = dashboard.calendario("2026-08")
    assert out["resumo"]["segundos"] == 0 and not any(x["id"] == p["id"] for x in out["projetos"])
    assert dashboard.calendario("lixo")["mes"] == datetime.now().strftime("%Y-%m")


def test_calendario_deixa_de_fora_pasta_de_assets(tmp_path):
    client, d = _cliente(tmp_path)
    (d / "00 - Assets").mkdir()
    projects_module.list_projects(client["id"])   # registra a pasta
    cal = dashboard.calendario()
    assert not any(x["num"] == "00" for x in cal["projetos"])


# ------------------------------------------------------------------ revisão (tela do celular)
def test_lista_de_revisoes_e_nota_vai_para_ajustes(tmp_path):
    client, d = _cliente(tmp_path)
    p = projects_module.create_project(client["id"], "Live")
    q = projects_module.create_project(client["id"], "Outra live")
    for proj in (p, q):
        store.update(proj["id"], lambda r: r.__setitem__("revisao_corte", {"pasta": "Corte 9x16 v1", "estado": "pendente",
                                                                          "desde": datetime.now().isoformat()}))
    c = create_app().test_client()
    lista = c.get("/api/revisoes").get_json()
    assert sorted(r["project_id"] for r in lista) == sorted([p["id"], q["id"]]) and all(r["estado"] == "pendente" for r in lista)
    r = c.post("/api/editor/revisao", json={"project_id": p["id"], "acao": "ajustar", "nota": "  tirar o começo  "}).get_json()
    assert r["estado"] == "em_ajuste" and r["nota"] == "tirar o começo"
    assert [i["text"] for i in feed.list_items(p["id"], "comments")] == ["Revisão do Corte 9x16 v1: tirar o começo"]
    lista = c.get("/api/revisoes").get_json()
    assert [x["estado"] for x in lista] == ["pendente", "em_ajuste"]     # pendentes primeiro
    c.post("/api/editor/revisao", json={"project_id": q["id"], "acao": "aprovar", "nota": "ignorada"})
    assert feed.list_items(q["id"], "comments") == []


def test_pagina_renderiza():
    """O index.html passa pelo Jinja: um "{{" dentro do JS (objeto literal logo depois do cifrão de um
    template string) derrubava a página inteira com 500."""
    r = create_app().test_client().get("/")
    assert r.status_code == 200 and b'id="view-calendario"' in r.data and b'id="view-revisao"' in r.data
