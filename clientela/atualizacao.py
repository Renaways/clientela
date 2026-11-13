"""Aviso de versão nova: compara VERSAO com o último release do repositório público no GitHub.

Só avisa; não baixa nem instala nada. A pergunta ao GitHub não leva dado nenhum do usuário
(API pública, sem chave; limite de 60 por hora por IP, por isso o cache). Desligável em
config.json → verificar_atualizacoes.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import feedback, paths
from .persistence import safe_read_json

# Suba a cada release; a tag no GitHub é "v" + isto (ex.: v0.1.0-pre-alpha).
VERSAO = "0.1.0-pre-alpha"
CACHE_S = 6 * 3600
TIMEOUT_S = 5

_lock = threading.Lock()
_cache: dict[str, Any] = {"em": 0.0, "dados": None}


def _numeros(v: str) -> tuple[int, ...]:
    """'v0.2.10-pre-alpha' → (0, 2, 10). shortcut: o sufixo (-pre-alpha, -beta) é ignorado na comparação;
    se um dia sair 0.2.0-beta e depois 0.2.0 final, compare o sufixo também."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def e_mais_nova(remota: str, local: str = VERSAO) -> bool:
    a, b = _numeros(remota), _numeros(local)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def ativa() -> bool:
    return safe_read_json(paths.CONFIG_JSON, {}).get("verificar_atualizacoes", True) is not False


def _buscar(repo: str) -> dict[str, Any]:
    """O release mais novo, pré-release incluído (/releases/latest pula os marcados como pré-release,
    e enquanto o app for pré-alpha todos são). Sem nenhum publicado = 404, como o /latest."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases?per_page=10",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Clientela"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        lista = json.loads(r.read().decode("utf-8"))
    for rel in lista if isinstance(lista, list) else []:
        if not rel.get("draft"):
            return rel
    raise urllib.error.HTTPError(req.full_url, 404, "sem release", {}, None)


def verificar(forcar: bool = False, buscar=None) -> dict[str, Any]:
    """{versao, repo, ativa, nova, ultima, url, notas, publicada, erro, verificado_em}."""
    buscar = buscar or _buscar  # resolvido aqui para o conftest poder trocar _buscar
    repo = feedback.destino()["github_repo"]
    base = {"versao": VERSAO, "repo": repo, "ativa": ativa(), "nova": False}
    if not repo or not base["ativa"]:
        return base
    with _lock:
        if not forcar and _cache["dados"] and time.time() - _cache["em"] < CACHE_S and _cache["dados"].get("repo") == repo:
            return {**_cache["dados"], "ativa": True}
    try:
        rel = buscar(repo)
        tag = str(rel.get("tag_name") or "")
        dados = {**base, "ultima": tag.lstrip("v"), "nova": e_mais_nova(tag), "url": rel.get("html_url") or "",
                 "notas": str(rel.get("body") or "")[:4000], "publicada": rel.get("published_at") or ""}
    except urllib.error.HTTPError as e:
        # 404 = o repositório ainda não tem release (ou não existe): não é erro para o usuário.
        dados = {**base, "erro": "" if e.code == 404 else f"GitHub respondeu {e.code}"}
    except Exception as e:  # sem internet, DNS, JSON quebrado: só não avisa
        dados = {**base, "erro": f"não consegui falar com o GitHub ({type(e).__name__})"}
    dados["verificado_em"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _lock:
        _cache.update(em=time.time(), dados=dados)
    return dados


def reset() -> None:
    with _lock:
        _cache.update(em=0.0, dados=None)
