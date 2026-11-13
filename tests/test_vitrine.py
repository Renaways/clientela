"""Vitrine de identidade do cliente (vitrine.py): fatos por script, pastas escolhidas por
categoria, arquivos servidos só de dentro delas e o resumo pela OpenRouter (falso aqui)."""

import json
from pathlib import Path

from clientela import autocorte, editor, identidades, vitrine
from clientela import clients as clients_module


def _client(tmp_path):
    d = tmp_path / "Cliente"
    d.mkdir()
    return clients_module.create_client(name="Cliente", path=str(d))


def _perfil_com_marca(client):
    d = identidades.ensure_profile(client["id"])
    (d / "assets/logos/logo.png").write_bytes(b"\x89PNG")
    (d / "marca.json").write_text(json.dumps({
        "cores": {"fundo": "#101010", "preto": "#101010", "acento": "#ffb800", "ruim": "amarelo"},
        "degrades": {"acento": "linear-gradient(90deg, #FFC126, #FF9800)"},
        "tipografia": {"papeis": {"destaque": {"familia": "Hando", "peso": 900}}},
    }), encoding="utf-8")
    return d


def test_fatos_do_perfil_e_o_que_falta(tmp_path):
    client = _client(tmp_path)
    _perfil_com_marca(client)
    v = vitrine.coletar(client["id"])
    assert v["cores"] == [{"hex": "#101010", "nomes": ["fundo", "preto"]}, {"hex": "#FFB800", "nomes": ["acento"]}]
    assert v["degrades"][0]["nome"] == "acento"
    assert v["papeis"][0]["familia"] == "Hando"
    assert [a["nome"] for a in v["categorias"]["logos"]["arquivos"]] == ["logo.png"]
    assert "Nenhum logo." not in v["faltando"]
    assert "Nenhuma pasta de ícones escolhida." in v["faltando"]
    assert v["resumo"] is None and v["ia_disponivel"] is False


def test_sem_marca_json_as_cores_vem_dos_textos(tmp_path):
    client = _client(tmp_path)
    d = identidades.ensure_profile(client["id"])
    (d / "marca.md").write_text("## Cores\n- Azul principal: #1A2B3C\n", encoding="utf-8")
    assert vitrine.coletar(client["id"])["cores"] == [{"hex": "#1A2B3C", "nomes": ["Azul principal"]}]


def test_pasta_de_icones_relativa_e_arquivo_validado(tmp_path):
    client = _client(tmp_path)
    icones = Path(client["path"]) / "Arte" / "Icones"
    (icones / "sub").mkdir(parents=True)
    (icones / "sub" / "seta.svg").write_text("<svg/>")
    (icones / "leia.txt").write_text("x")
    (icones / ".oculto.png").write_bytes(b"x")
    vitrine.escolher_pasta(client["id"], "icones", str(icones))
    meta = identidades._load_meta(client["id"])
    assert meta["vitrine_pastas"]["icones"] == "Arte/Icones"   # relativo: abre no Mac e no Windows
    v = vitrine.coletar(client["id"])
    assert v["categorias"]["icones"]["arquivos"] == [{"nome": "sub/seta.svg", "tipo": "imagem", "origem": "pasta"}]
    assert vitrine.arquivo(client["id"], "icones", "pasta", "sub/seta.svg") == (icones / "sub" / "seta.svg").resolve()
    assert vitrine.arquivo(client["id"], "icones", "pasta", "leia.txt") is None
    assert vitrine.arquivo(client["id"], "icones", "pasta", "../../Cliente/x.png") is None
    vitrine.escolher_pasta(client["id"], "icones", None)
    assert "icones" not in identidades._load_meta(client["id"])["vitrine_pastas"]


def test_resumo_pela_openrouter_e_guardado(tmp_path, monkeypatch):
    client = _client(tmp_path)
    _perfil_com_marca(client)
    editor.save_config({"corte": {"openrouter_api_key": "sk-or-1"}})
    pedidos = []

    def falso(prompt, cfg, timeout=300, imagens=()):
        pedidos.append(cfg)
        return {"descricao": "Escuro com amarelo.", "faltando": ["Ícones"]}, 0.0

    monkeypatch.setattr(autocorte, "llm_openrouter", falso)
    r = vitrine.gerar_resumo(client["id"])
    assert pedidos[0]["openrouter_modelo"] == vitrine.MODELO
    v = vitrine.coletar(client["id"])
    assert v["resumo"]["descricao"] == "Escuro com amarelo." and v["resumo_atual"] is True
    assert r["assinatura"] == v["assinatura"]
    (identidades.profile_dir(client["id"]) / "assets/logos/outro.png").write_bytes(b"x")
    assert vitrine.coletar(client["id"])["resumo_atual"] is False   # os fatos mudaram


def test_rotas(tmp_path):
    from app import create_app
    client = _client(tmp_path)
    _perfil_com_marca(client)
    c = create_app().test_client()
    base = f"/api/clients/{client['id']}/vitrine"
    assert c.get(base).get_json()["cores"][0]["hex"] == "#101010"
    assert c.get(f"{base}/arquivo?cat=logos&origem=perfil&nome=logo.png").status_code == 200
    assert c.get(f"{base}/arquivo?cat=logos&origem=perfil&nome=../marca.json").status_code == 404
    assert c.put(f"{base}/pasta", json={"categoria": "x", "pasta": str(tmp_path)}).status_code == 400
    assert c.post(f"{base}/resumo").status_code == 503   # sem chave
    assert c.get("/api/clients/nao-existe/vitrine").status_code == 404
