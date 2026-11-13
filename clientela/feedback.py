"""Feedback do uso das ferramentas, guardado em data/feedback.json para um diagnóstico depois.

Cada entrada: qual ferramenta, como foi (boa | ok | ruim), o tipo (bug | melhoria | elogio | outro),
o texto livre e onde estava (tela, projeto). Só acrescenta; o diagnóstico lê o arquivo inteiro.

Enviar ao desenvolvedor é opcional e sai do navegador, não daqui: um POST no ``formResponse`` de um
Google Forms (as respostas caem numa planilha de quem criou o formulário) ou um link de issue do GitHub
já preenchido. Vazios = sem envio.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit
from typing import Any

from . import paths
from .persistence import safe_read_json, safe_write_json

# Preencher antes de publicar. GOOGLE_FORM_LINK: o "link pré-preenchido" do formulário (⋮ → Gerar link
# pré-preenchido), com cada pergunta respondida com o nome do campo: ferramenta, nota, tipo, texto, sistema,
# email. GITHUB_REPO: "dono/repositório".
GOOGLE_FORM_LINK = "https://docs.google.com/forms/d/e/1FAIpQLSctqAnyqUpzl6Y3d5yHiaRoCe6IT8WomPQ3kYuaAlDyrQdSFQ/viewform?usp=pp_url&entry.1876683296=ferramenta&entry.2090509639=nota&entry.983751811=tipo&entry.981653022=texto&entry.473318665=sistema&entry.755433350=email"
GITHUB_REPO = "renaways/clientela"
CAMPOS_FORM = ("ferramenta", "nota", "tipo", "texto", "sistema", "email")

NOTAS = ("boa", "ok", "ruim")
TIPOS = ("bug", "melhoria", "elogio", "outro")


def _arquivo():
    return paths.DATA_DIR / "feedback.json"  # lido na hora: os testes trocam o DATA_DIR


def listar() -> list[dict[str, Any]]:
    dados = safe_read_json(_arquivo(), [])
    return dados if isinstance(dados, list) else []


def registrar(dados: dict[str, Any]) -> dict[str, Any]:
    ferramenta = str(dados.get("ferramenta") or "").strip()[:80]
    texto = str(dados.get("texto") or "").strip()[:4000]
    nota = dados.get("nota")
    if not ferramenta:
        raise ValueError("diga de qual ferramenta é o feedback")
    if nota not in NOTAS:
        raise ValueError("escolha como a ferramenta foi: boa, ok ou ruim")
    tipo = dados.get("tipo") if dados.get("tipo") in TIPOS else "outro"
    entrada = {
        "quando": datetime.now().isoformat(timespec="seconds"),
        "ferramenta": ferramenta, "nota": nota, "tipo": tipo, "texto": texto,
        "tela": str(dados.get("tela") or "")[:200], "projeto": str(dados.get("projeto") or "")[:120] or None,
    }
    todos = listar()
    todos.append(entrada)
    safe_write_json(_arquivo(), todos)
    return entrada


def resumo() -> list[dict[str, Any]]:
    """Por ferramenta: quantas boas/ok/ruins, a pior primeiro. É o começo do diagnóstico."""
    por: dict[str, dict[str, Any]] = {}
    for e in listar():
        r = por.setdefault(e["ferramenta"], {"ferramenta": e["ferramenta"], "boa": 0, "ok": 0, "ruim": 0, "total": 0})
        r[e["nota"]] += 1
        r["total"] += 1
    return sorted(por.values(), key=lambda r: (-(r["ruim"] / r["total"]), -r["total"]))


def ler_link_do_form(link: str) -> dict[str, Any] | None:
    """Do link pré-preenchido tira o endereço que recebe as respostas e o entry.N de cada campo.

    None se o link não for de um Google Forms ou não tiver ao menos ferramenta e texto.
    """
    partes = urlsplit((link or "").strip())
    m = re.match(r"^(/forms/d/e/[\w-]+)/", partes.path)
    if partes.scheme != "https" or partes.netloc != "docs.google.com" or not m:
        return None
    campos = {v.strip().lower(): k for k, v in parse_qsl(partes.query)
              if k.startswith("entry.") and v.strip().lower() in CAMPOS_FORM}
    if not {"ferramenta", "texto"} <= campos.keys():
        return None
    return {"acao": f"https://docs.google.com{m.group(1)}/formResponse", "campos": campos}


def destino() -> dict[str, Any]:
    """Para onde o navegador pode mandar o feedback (as variáveis de ambiente servem para testar)."""
    return {
        "google_form": ler_link_do_form(os.environ.get("CLIENTELA_FEEDBACK_FORM", GOOGLE_FORM_LINK)),
        "github_repo": os.environ.get("CLIENTELA_FEEDBACK_REPO", GITHUB_REPO),
    }
