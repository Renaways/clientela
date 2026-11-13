"""Treino do estagiário dentro do Clientela: a avaliação depois de cada geração.

Cada avaliação (nota 1–5, o que ficou bom, o que ficou ruim, o que fazer diferente) vai para
`data/editor/avaliacoes.json` e o app reescreve `data/editor/APRENDIZADOS.md`, a documentação legível
do que funciona e do que não funciona, por tarefa. As avaliações recentes da mesma tarefa (do mesmo
cliente primeiro) voltam no próximo pedido à IA como LIÇÕES: é assim que o resultado melhora sem
gastar mais tokens em tentativa e erro.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from . import paths
from .persistence import safe_read_json, safe_write_json, safe_write_text

MAX_LICOES = 8


def _arquivo():
    return paths.EDITOR_DIR / "avaliacoes.json"  # lido na hora: os testes trocam o EDITOR_DIR


def listar() -> list[dict[str, Any]]:
    dados = safe_read_json(_arquivo(), []) if _arquivo().exists() else []
    return dados if isinstance(dados, list) else []


def _texto(v: Any, limite: int = 1500) -> str:
    return str(v or "").strip()[:limite]


def avaliar(job: dict[str, Any], dados: dict[str, Any]) -> dict[str, Any]:
    try:
        nota = int(dados.get("nota"))
    except (TypeError, ValueError):
        nota = 0
    if not 1 <= nota <= 5:
        raise ValueError("dê uma nota de 1 a 5")
    entrada = {
        "quando": datetime.now().isoformat(timespec="seconds"),
        "job": job["id"], "tipo": job.get("tipo"), "tarefa": job.get("tarefa"),
        "tarefa_rotulo": job.get("tarefa_rotulo"), "client_id": job.get("client_id"),
        "cliente": job.get("cliente_nome"), "projeto": job.get("projeto_nome"), "versao": job.get("versao"),
        "nota": nota, "bom": _texto(dados.get("bom")), "ruim": _texto(dados.get("ruim")),
        "fazer_diferente": _texto(dados.get("fazer_diferente")),
        "custo_usd": (job.get("resultado") or {}).get("custo_usd"),
    }
    todas = [a for a in listar() if a.get("job") != job["id"]]  # reavaliar troca a anterior
    todas.append(entrada)
    paths.EDITOR_DIR.mkdir(parents=True, exist_ok=True)
    safe_write_json(_arquivo(), todas)
    safe_write_text(paths.EDITOR_DIR / "APRENDIZADOS.md", documento(todas))
    return entrada


def da_job(job_id: str) -> dict[str, Any] | None:
    return next((a for a in reversed(listar()) if a.get("job") == job_id), None)


def licoes(tarefa: str, client_id: str | None) -> str:
    """As avaliações recentes desta tarefa, as do cliente primeiro, como texto para o pedido. Vazio sem nenhuma."""
    da_tarefa = [a for a in listar() if a.get("tarefa") == tarefa and (a.get("ruim") or a.get("fazer_diferente") or a.get("bom"))]
    if not da_tarefa:
        return ""
    escolhidas = sorted(da_tarefa, key=lambda a: (a.get("client_id") == client_id, a.get("quando", "")), reverse=True)[:MAX_LICOES]
    linhas = []
    for a in escolhidas:
        partes = [f"nota {a['nota']}/5" + (" (deste cliente)" if client_id and a.get("client_id") == client_id else "")]
        if a.get("bom"):
            partes.append(f"funcionou: {a['bom']}")
        if a.get("ruim"):
            partes.append(f"ficou ruim: {a['ruim']}")
        if a.get("fazer_diferente"):
            partes.append(f"fazer diferente: {a['fazer_diferente']}")
        linhas.append("- " + "; ".join(partes))
    return ("LIÇÕES das avaliações do editor sobre entregas anteriores desta tarefa (siga o que funcionou, "
            "não repita o que ficou ruim):\n" + "\n".join(linhas))


def documento(todas: list[dict[str, Any]] | None = None) -> str:
    todas = listar() if todas is None else todas
    por: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in todas:
        por[a.get("tarefa_rotulo") or a.get("tarefa") or "?"].append(a)
    out = ["# Aprendizados do estagiário", "",
           "Gerado pelo Clientela a cada avaliação pós-geração (data/editor/avaliacoes.json). "
           "As avaliações recentes de cada tarefa entram no pedido seguinte como LIÇÕES.", ""]
    for rotulo, lista in sorted(por.items()):
        media = sum(a["nota"] for a in lista) / len(lista)
        out += [f"## {rotulo}", "", f"{len(lista)} avaliações · média {media:.1f}/5", ""]
        for a in sorted(lista, key=lambda a: a.get("quando", ""), reverse=True):
            out.append(f"### {a.get('quando', '')[:10]} · {a.get('cliente') or 'sem cliente'} · "
                       f"{a.get('projeto') or ''} v{a.get('versao') or '?'} · nota {a['nota']}/5")
            for chave, nome in (("bom", "Funcionou"), ("ruim", "Ficou ruim"), ("fazer_diferente", "Fazer diferente")):
                if a.get(chave):
                    out.append(f"- **{nome}:** {a[chave]}")
            out.append("")
    return "\n".join(out)
