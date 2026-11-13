"""Aviso de versão nova (atualizacao.py): compara VERSAO com o último release do GitHub, sem rede nos testes."""

import urllib.error

from clientela import atualizacao, paths
from clientela.persistence import safe_write_json


def _release(tag):
    return lambda repo: {"tag_name": tag, "html_url": f"https://github.com/{repo}/releases/tag/{tag}", "body": "Novidades", "published_at": "2026-12-01T10:00:00Z"}


def test_compara_versoes():
    assert atualizacao.e_mais_nova("v0.2.0-pre-alpha", "0.1.0-pre-alpha")
    assert atualizacao.e_mais_nova("v0.1.10", "0.1.9")
    assert atualizacao.e_mais_nova("v1", "0.9.9")
    assert not atualizacao.e_mais_nova("v0.1.0", "0.1.0-pre-alpha")
    assert not atualizacao.e_mais_nova("v0.0.9", "0.1.0")
    assert not atualizacao.e_mais_nova("lixo", "0.1.0")


def test_avisa_versao_nova_e_guarda_em_cache(monkeypatch):
    monkeypatch.setattr(atualizacao.feedback, "GITHUB_REPO", "dono/repo")
    monkeypatch.delenv("CLIENTELA_FEEDBACK_REPO", raising=False)
    chamadas = []
    def buscar(repo):
        chamadas.append(repo)
        return _release("v9.0.0")(repo)
    r = atualizacao.verificar(buscar=buscar)
    assert r["nova"] and r["ultima"] == "9.0.0" and r["notas"] == "Novidades" and "dono/repo" in r["url"]
    atualizacao.verificar(buscar=buscar)
    assert chamadas == ["dono/repo"]  # a segunda veio do cache
    atualizacao.verificar(forcar=True, buscar=buscar)
    assert len(chamadas) == 2


def test_sem_repo_desligado_ou_sem_release_nao_avisa(monkeypatch):
    monkeypatch.delenv("CLIENTELA_FEEDBACK_REPO", raising=False)
    monkeypatch.setattr(atualizacao.feedback, "GITHUB_REPO", "")
    assert atualizacao.verificar(buscar=_release("v9.0.0"))["nova"] is False
    monkeypatch.setattr(atualizacao.feedback, "GITHUB_REPO", "dono/repo")
    safe_write_json(paths.CONFIG_JSON, {"verificar_atualizacoes": False})
    r = atualizacao.verificar(buscar=_release("v9.0.0"))
    assert r["nova"] is False and r["ativa"] is False
    safe_write_json(paths.CONFIG_JSON, {})

    def sem_release(repo):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    r = atualizacao.verificar(forcar=True, buscar=sem_release)
    assert r["nova"] is False and r["erro"] == ""
    r = atualizacao.verificar(forcar=True)  # conftest: sem rede
    assert r["nova"] is False and "GitHub" in r["erro"]


def test_rota(monkeypatch):
    import app as app_module
    monkeypatch.setattr(atualizacao.feedback, "GITHUB_REPO", "dono/repo")
    monkeypatch.delenv("CLIENTELA_FEEDBACK_REPO", raising=False)
    monkeypatch.setattr(atualizacao, "_buscar", _release("v9.0.0"))
    r = app_module.create_app().test_client().get("/api/atualizacao?forcar=1").get_json()
    assert r["nova"] and r["versao"] == atualizacao.VERSAO


def test_buscar_pega_o_mais_novo_mesmo_pre_release_e_pula_rascunho(monkeypatch):
    import io
    import json as _json

    import pytest

    respostas = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def abrir(req, timeout):
        respostas.append(req.full_url)
        return Resp(_json.dumps(lista).encode())

    monkeypatch.setattr(atualizacao.urllib.request, "urlopen", abrir)
    lista = [{"tag_name": "v0.3.0", "draft": True}, {"tag_name": "v0.2.0-pre-alpha", "prerelease": True, "draft": False}]
    assert _ORIGINAL("dono/repo")["tag_name"] == "v0.2.0-pre-alpha"
    assert "releases?per_page" in respostas[0]
    lista = []
    with pytest.raises(urllib.error.HTTPError):
        _ORIGINAL("dono/repo")


_ORIGINAL = atualizacao._buscar  # importado antes do conftest trocar (a coleta vem antes das fixtures)
