"""IA gratuita da OpenRouter (o mesmo roteador da vitrine) lendo o projeto.

- `gerar_resumo`: do que o projeto trata e o que se espera dele, mais sugestões práticas. Lê a descrição
  (o principal), os ajustes pedidos, o checklist, as tags e os nomes dos arquivos. Guarda no registro do
  projeto (`ia_resumo`) com a assinatura dos fatos, para o app saber quando ficou velho.
- `sugerir_telas`: depois de escolher vídeo e transcrição para os Inserts, lê a transcrição da pasta e
  propõe quais telas valem a pena, com a descrição de cada uma e o nível de animação.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import autocorte, clients as clients_module, editor, store, vitrine
from . import projects as projects_module
from .persistence import safe_read_text

MAX_TRANSCRICAO = 24000


def _chamar(prompt: str) -> dict[str, Any]:
    cfg = vitrine.cfg_openrouter()
    if not cfg["openrouter_api_key"]:
        raise PermissionError("sem chave da OpenRouter (Configurações → Modelos de IA, ou OPENROUTER_API_KEY)")
    resp = vitrine.com_prazo(lambda: autocorte.llm_openrouter(prompt, cfg, timeout=cfg["timeout"])[0], cfg["timeout"],
                     cfg["openrouter_modelo"])
    return resp if isinstance(resp, dict) else {}



# Os gratuitos falham muito (429, 404, JSON quebrado, 1–2 min numa revisão longa): a revisão pergunta ao
# modelo escolhido e a estes ao mesmo tempo, e fica com a primeira resposta que tem roteiro.
RESERVAS_REVISAO = ("openrouter/free", "nvidia/nemotron-3.5-super-vl:free")


def _corrida(prompt: str, aceita: Any) -> tuple[dict[str, Any], str]:
    cfg = vitrine.cfg_openrouter()
    if not cfg["openrouter_api_key"]:
        raise PermissionError("sem chave da OpenRouter (Configurações → Modelos de IA, ou OPENROUTER_API_KEY)")
    modelos = list(dict.fromkeys([cfg["openrouter_modelo"], *RESERVAS_REVISAO]))
    futs = {vitrine._pool.submit(lambda m=m: autocorte.llm_openrouter(
        prompt, {**cfg, "openrouter_modelo": m}, timeout=cfg["timeout"])[0]): m for m in modelos}
    erros = []
    try:
        for fut in concurrent.futures.as_completed(futs, timeout=cfg["timeout"]):
            m = futs[fut]
            try:
                resp = fut.result()
            except Exception as exc:  # noqa: BLE001 — um modelo que falha só sai da corrida
                erros.append(f"{m}: {str(exc)[:120]}")
                continue
            if isinstance(resp, dict) and aceita(resp):
                return resp, m
            erros.append(f"{m}: resposta sem roteiro")
    except concurrent.futures.TimeoutError:
        erros.append(f"os outros não responderam em {cfg['timeout']} s")
    raise autocorte.Falha("nenhum modelo gratuito devolveu o roteiro. Tente de novo daqui a pouco, ou use a "
                          "Revisão avançada. Detalhes: " + " · ".join(erros))


def _projeto(project_id: str) -> dict[str, Any]:
    project = projects_module.get_project(project_id)
    if not project:
        raise LookupError("projeto não encontrado")
    return project


# ------------------------------------------------------------------ resumo do projeto
def _transcricao_atual(project: dict[str, Any]) -> tuple[str, str]:
    """(identidade, texto) da transcrição mais recente da pasta; ("", "") sem nenhuma."""
    if not project.get("folder_available"):
        return "", ""
    try:
        caminho = _transcricao_do_projeto(project, None)
        ident = f"{caminho.name}@{int(caminho.stat().st_mtime)}"
        return ident, texto_transcricao(caminho).strip()
    except (LookupError, OSError):
        return "", ""


def fatos(project_id: str, com_transcricao: bool = True) -> dict[str, Any]:
    project = _projeto(project_id)
    record = store.load(project_id) or {}
    client = clients_module.get_client(project["client_id"]) or {}
    arquivos: list[str] = []
    if project.get("folder_available"):
        try:
            arquivos = sorted(p.name for p in Path(project["path"]).iterdir() if not p.name.startswith("."))[:60]
        except OSError:
            pass
    return {
        "cliente": client.get("name"), "projeto": project.get("name"),
        "descricao": (record.get("notes") or "").strip()[:6000],
        "tags": record.get("tags") or [], "prazo": record.get("due_date"), "etapa": record.get("stage"),
        "ajustes_pedidos": [c.get("text") for c in record.get("comments") or [] if c.get("text")][-30:],
        "checklist": [c.get("text") for c in record.get("checklist") or [] if c.get("text")][:40],
        "arquivos": arquivos,
        **_grupo(project),
        **(dict(zip(("transcricao_id", "transcricao"), _transcricao_atual(project))) if com_transcricao else {}),
    }


def _grupo(project: dict[str, Any]) -> dict[str, Any]:
    """Subprojetos são interligados: o filho lê a descrição do pai, e o pai a
    lista dos filhos com as descrições. Sem grupo nada entra, então a
    assinatura dos projetos comuns não muda."""
    if project.get("pai"):
        pai = store.load(project["pai"]) or {}
        return {"projeto_principal": {"nome": pai.get("name"),
                                      "descricao": (pai.get("notes") or "").strip()[:3000]}}
    prefix = f"{project['num']}."
    filhos = [r for r in store.list_for_client(project["client_id"]) if r.get("num", "").startswith(prefix)]
    if not filhos:
        return {}
    return {"subprojetos": [{"nome": r.get("name"), "descricao": (r.get("notes") or "").strip()[:1500]}
                            for r in filhos]}


def assinatura(f: dict[str, Any]) -> str:
    # a etapa e o prazo mudam o tempo todo e não mudam do que o projeto trata
    base = {k: v for k, v in f.items() if k not in ("etapa", "prazo", "transcricao")}  # o id já muda com ela
    return hashlib.sha256(json.dumps(base, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def estado(project_id: str) -> dict[str, Any]:
    f = fatos(project_id)
    resumo = (store.load(project_id) or {}).get("ia_resumo")
    return {"resumo": resumo, "atual": bool(resumo) and resumo.get("assinatura") == assinatura(f),
            "tem_descricao": bool(f["descricao"]),
            "tem_transcricao": bool(f.get("transcricao_id")),
            # transcrição nova desde o último resumo: o app refaz a análise sozinho
            "nova_transcricao": bool(resumo) and bool(f.get("transcricao_id"))
            and resumo.get("transcricao_id") != f["transcricao_id"]}


def gerar_resumo(project_id: str) -> dict[str, Any]:
    f = fatos(project_id)
    if not f["descricao"] and not f["ajustes_pedidos"] and not f["checklist"] and not f.get("transcricao"):
        raise LookupError("escreva a descrição do projeto primeiro: é dela que a IA tira o contexto")
    transcricao = f.pop("transcricao", "")
    tid = f.pop("transcricao_id", "")
    prompt = (
        "Você é o produtor de um estúdio de edição de vídeo e motion design. Abaixo estão os dados de um "
        "projeto (a descrição costuma ser o pedido do cliente, colado do ClickUp)"
        + (" e a transcrição do vídeo bruto" if transcricao else "") + ". Responda SÓ com JSON "
        '{"sobre": "...", "esperado": "...", "sugestoes": ["..."], "broll": [{"momento": "m:ss", "ideia": "..."}]}, '
        "em português do Brasil:\n"
        "- sobre: 1 a 3 frases sobre do que o projeto trata.\n"
        "- esperado: 1 a 3 frases sobre o que o cliente espera receber (formato, duração, tom, prazo), "
        "só o que os dados dizem ou deixam claro.\n"
        "- sugestoes: até 6 sugestões curtas e práticas para o editor (o que fazer primeiro, o que "
        "perguntar ao cliente, o que falta, ideias de edição/motion que combinam com o pedido).\n"
        "- broll: " + ("até 8 ideias de B-roll (imagem de apoio: o que filmar ou buscar em banco de imagens) "
                       "para trechos da transcrição, com o momento pelo tempo dela."
                       if transcricao else "lista vazia (ainda não há transcrição).") + "\n"
        "Não invente fatos que não estão nos dados.\n\n"
        f"DADOS:\n{json.dumps(f, ensure_ascii=False, indent=1)}\n"
        + (f"\nTRANSCRIÇÃO:\n{transcricao[:MAX_TRANSCRICAO]}\n" if transcricao else "")
    )
    resp = _chamar(prompt)
    sobre = str(resp.get("sobre") or "").strip()
    if not sobre:
        raise autocorte.Falha("o modelo não devolveu o resumo; tente de novo")
    resumo = {
        "sobre": sobre[:1200], "esperado": str(resp.get("esperado") or "").strip()[:1200],
        "sugestoes": [str(s).strip()[:400] for s in resp.get("sugestoes") or [] if str(s).strip()][:8],
        "broll": [{"momento": str(b.get("momento") or "").strip()[:8], "ideia": str(b.get("ideia") or "").strip()[:400]}
                  for b in (resp.get("broll") or [] if tid else [])  # sem fala, B-roll seria chute
                  if isinstance(b, dict) and str(b.get("ideia") or "").strip()][:10],
        "transcricao_id": tid,
        "modelo": vitrine.cfg_openrouter()["openrouter_modelo"], "assinatura": assinatura({**f, "transcricao_id": tid}),
        "gerado_em": datetime.now(timezone.utc).isoformat(),
    }
    store.update(project_id, lambda r: r.__setitem__("ia_resumo", resumo))
    return resumo


# ------------------------------------------------------------------ telas de insert
def _mmss(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def texto_transcricao(path: Path) -> str:
    """Texto legível de uma transcrição: SRT/VTT/TXT como estão; JSON (whisper, hyperframes, Premiere) vira
    linhas "[m:ss] frase", juntando as palavras a cada ~8 s."""
    bruto = safe_read_text(path, "")
    if path.suffix.lower() != ".json":
        return bruto
    try:
        dados = json.loads(bruto)
    except ValueError:
        return bruto
    palavras: list[tuple[float, str]] = []

    def andar(x: Any) -> None:
        if isinstance(x, dict):
            txt = x.get("text") or x.get("word")
            ini = x.get("start", x.get("t"))
            filhos = [v for v in x.values() if isinstance(v, (list, dict))]
            if isinstance(txt, str) and isinstance(ini, (int, float)) and not any(
                    isinstance(v, list) and v and isinstance(v[0], dict) for v in filhos):
                palavras.append((float(ini), txt.strip()))
                return
            for v in filhos:
                andar(v)
        elif isinstance(x, list):
            for v in x:
                andar(v)

    andar(dados)
    if not palavras:
        return bruto
    linhas, atual, t0 = [], [], palavras[0][0]
    for t, w in palavras:
        if atual and t - t0 >= 8:
            linhas.append(f"[{_mmss(t0)}] {' '.join(atual)}")
            atual, t0 = [], t
        atual.append(w)
    if atual:
        linhas.append(f"[{_mmss(t0)}] {' '.join(atual)}")
    return "\n".join(linhas)


def _transcricao_do_projeto(project: dict[str, Any], transcricao: str | None) -> Path:
    base = Path(project["path"])
    if transcricao:
        alvo = editor._resolve_media(base, transcricao, editor.TRANSCRIPT_EXTS)
        if not alvo:
            raise LookupError("transcrição não encontrada")
        return alvo
    cands = (editor.list_candidates(project["id"]) or {}).get("transcricoes") or []
    if not cands:
        raise LookupError("nenhuma transcrição na pasta do projeto (exporte do Premiere ou rode a transcrição)")
    return base / cands[0]["path"]


def _nivel(v: Any) -> int:
    try:
        return min(3, max(0, int(v)))
    except (TypeError, ValueError):
        return 2


def sugerir_telas(project_id: str, transcricao: str | None = None, quantas: int = 6) -> dict[str, Any]:
    project = _projeto(project_id)
    if not project.get("folder_available"):
        raise LookupError("pasta do projeto não encontrada no disco")
    caminho = _transcricao_do_projeto(project, transcricao)
    texto = texto_transcricao(caminho).strip()
    if not texto:
        raise LookupError("a transcrição está vazia")
    descricao = ((store.load(project_id) or {}).get("notes") or "").strip()[:3000]
    prompt = (
        "Você é diretor de motion de um estúdio de edição. Abaixo está a transcrição de um vídeo com "
        "apresentador e o pedido do projeto. Escolha os momentos que ganham um insert de motion na tela "
        "(texto/número/lista animados por cima do vídeo): números, dados, frases de efeito, listas, nomes "
        "importantes. Responda SÓ com JSON "
        '{"telas": [{"titulo": "...", "descricao": "...", "momento": "m:ss", "nivel": 0}]}, em português '
        f"do Brasil, no máximo {quantas} telas, em ordem de tempo:\n"
        "- titulo: 2 a 5 palavras.\n"
        "- descricao: o que a tela mostra (o texto exato que aparece, entre aspas) e como, em 1 a 2 frases.\n"
        "- momento: quando entra, pelo tempo da transcrição.\n"
        "- nivel: animação de 0 (tela parada, só aparece) a 3 (bem animada); use 3 só no ponto alto.\n\n"
        f"PEDIDO DO PROJETO:\n{descricao or '(sem descrição)'}\n\n"
        f"TRANSCRIÇÃO ({caminho.name}):\n{texto[:MAX_TRANSCRICAO]}\n"
    )
    resp = _chamar(prompt)
    telas = []
    for t in resp.get("telas") or []:
        if not isinstance(t, dict) or not str(t.get("descricao") or "").strip():
            continue
        momento = str(t.get("momento") or "").strip()
        telas.append({
            "titulo": str(t.get("titulo") or "").strip()[:60],
            "descricao": (f"[{momento}] " if re.match(r"^\d+:\d\d", momento) else "")
            + str(t["descricao"]).strip()[:600],
            "nivel": _nivel(t.get("nivel")),
        })
    if not telas:
        raise autocorte.Falha("o modelo não sugeriu nenhuma tela; tente de novo")
    rel = caminho.relative_to(Path(project["path"])).as_posix() if Path(project["path"]) in caminho.parents else caminho.name
    return {"telas": telas[:quantas], "transcricao": rel}


# ------------------------------------------------------------------ tirar dúvidas (chat)
MAX_CHAT = 40
MODOS_CHAT = {
    "duvida": "Responda a dúvida do editor sobre o projeto, direto e prático, citando de onde tirou (descrição, "
              "ajustes, transcrição). Se os dados não respondem, diga isso e sugira o que perguntar ao cliente.",
    "mensagem": "Escreva a mensagem pronta para o editor mandar ao cliente (WhatsApp) tirando a dúvida: cordial, "
                "curta, objetiva, com as perguntas numeradas se forem várias. Só a mensagem, sem comentários.",
    "copy": "Escreva a copy (legenda do post) para publicar este vídeo nas redes do cliente: primeira linha que "
            "prende, corpo curto no tom do cliente, chamada para ação e 3 a 8 hashtags. Baseie-se na transcrição "
            "e na descrição; se o editor pedir ajustes (tom, tamanho, rede), siga. Só a copy, sem comentários.",
}


def conversa(project_id: str) -> list[dict[str, Any]]:
    _projeto(project_id)
    return list((store.load(project_id) or {}).get("chat") or [])


def limpar_conversa(project_id: str) -> None:
    _projeto(project_id)
    store.update(project_id, lambda r: r.__setitem__("chat", []))


def perguntar(project_id: str, mensagem: str, modo: str = "duvida") -> dict[str, Any]:
    mensagem = (mensagem or "").strip()[:4000]
    if not mensagem:
        raise LookupError("escreva a dúvida")
    if modo not in MODOS_CHAT:
        modo = "duvida"
    f = fatos(project_id)
    transcricao = f.pop("transcricao", "")
    f.pop("transcricao_id", None)
    resumo = (store.load(project_id) or {}).get("ia_resumo") or {}
    historico = conversa(project_id)[-12:]
    prompt = (
        "Você é o assistente de um editor de vídeo/motion dentro do app dele. Você conhece o projeto pelos dados "
        "abaixo. " + MODOS_CHAT[modo] + " Responda SÓ com JSON {\"resposta\": \"...\"}, em português do Brasil. "
        "Não invente fatos.\n\n"
        f"DADOS DO PROJETO:\n{json.dumps(f, ensure_ascii=False, indent=1)}\n"
        + (f"\nRESUMO JÁ FEITO: {resumo.get('sobre', '')} {resumo.get('esperado', '')}\n" if resumo else "")
        + (f"\nTRANSCRIÇÃO:\n{transcricao[:12000]}\n" if transcricao else "")
        + ("\nCONVERSA ATÉ AQUI:\n" + "\n".join(f"{m['papel']}: {m['texto']}" for m in historico) if historico else "")
        + f"\n\neditor: {mensagem}\n"
    )
    resp = _chamar(prompt)
    texto = str(resp.get("resposta") or "").strip()
    if not texto:
        raise autocorte.Falha("o modelo não respondeu; tente de novo")
    agora = datetime.now(timezone.utc).isoformat()
    novas = [{"papel": "editor", "texto": mensagem, "modo": modo, "quando": agora},
             {"papel": "ia", "texto": texto[:6000], "modo": modo, "quando": agora}]
    store.update(project_id, lambda r: r.__setitem__("chat", ((r.get("chat") or []) + novas)[-MAX_CHAT:]))
    return novas[1]


# ------------------------------------------------------------------ revisão da descrição (briefing)
# O briefing chega em texto livre (tempos e pedidos juntos). A IA devolve um roteiro de edição: uma linha
# por momento, com o tipo de tratamento. Gratuita pela OpenRouter, ou "avançada" pelo Claude (trava de tokens).
TIPOS_CENA = {
    "gancho": "Gancho", "fala": "Fala / apresentador", "legenda": "Legenda", "gc": "GC",
    "texto_destaque": "Texto em destaque", "motion_tela_cheia": "Motion tela cheia", "imagem": "Imagem",
    "video": "Vídeo / tela de fundo", "mockup": "Mockup", "efeito": "Efeito", "cta": "CTA", "outro": "Outro",
}
LIMITE_MOTION_S = 40  # um cliente pediu: mais que isso de motion, falar com ele antes de editar
REVISAO_SCHEMA = {
    "type": "object", "required": ["resumo", "itens"],
    "properties": {
        "resumo": {"type": "string"},
        "legenda_padrao": {"type": "string"},
        "itens": {"type": "array", "items": {
            "type": "object", "required": ["inicio", "tipo", "cena"],
            "properties": {
                "inicio": {"type": "string"}, "fim": {"type": "string"},
                "tipo": {"type": "string", "enum": list(TIPOS_CENA)},
                "cena": {"type": "string"}, "texto_tela": {"type": "string"},
                "midia": {"type": "string"}, "efeito": {"type": "string"},
            }}},
        "duvidas": {"type": "array", "items": {"type": "string"}},
    },
}


def _segundos(tc: Any) -> float | None:
    """"m:ss", "mm:ss", "h:mm:ss" (com ou sem fração) → segundos; o resto é None."""
    m = re.match(r"^\s*(?:(\d+):)?(\d{1,3}):(\d{2}(?:[.,]\d+)?)\s*$", str(tc or ""))
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3).replace(",", "."))


def _prompt_revisao(descricao: str, extra: dict[str, Any]) -> str:
    return (
        "Você é o produtor de um estúdio de edição e motion design. O briefing abaixo veio do cliente em texto "
        "livre: tempos e pedidos podem vir juntos na mesma linha ou fora de ordem. Reorganize em um "
        "roteiro de edição claro, sem perder nenhum pedido e sem inventar nenhum. Responda SÓ com JSON "
        '{"resumo": "...", "legenda_padrao": "...", "itens": [{"inicio": "m:ss", "fim": "m:ss", "tipo": "...", '
        '"cena": "...", "texto_tela": "...", "midia": "...", "efeito": "..."}], "duvidas": ["..."]}, '
        "em português do Brasil:\n"
        "- resumo: 1 a 2 frases sobre o vídeo (tema, estrutura gancho → desenvolvimento → CTA, duração).\n"
        "- legenda_padrao: o estilo de legenda que vale para o vídeo todo, se o briefing disser.\n"
        "- itens: um por momento, em ordem de tempo. Um pedido com dois tratamentos (ex.: imagem de fundo + "
        "texto grande) vira um item só, com os campos preenchidos. inicio é o tempo do briefing; fim só se o "
        "briefing disser ou se o próximo momento deixar claro.\n"
        f"- tipo, um destes: {', '.join(TIPOS_CENA)}. motion_tela_cheia = a tela inteira vira animação e o "
        "apresentador some; texto_destaque = texto grande por cima, mas a pessoa continua aparecendo. Na dúvida "
        "entre os dois, escolha texto_destaque e pergunte em duvidas.\n"
        "- cena: o que acontece na tela, em uma frase direta para o editor.\n"
        "- texto_tela: o texto EXATO que aparece na tela (copiado do briefing), se houver.\n"
        "- midia: qual imagem, vídeo, anexo, link ou mockup entra, se houver (copie links como estão).\n"
        "- efeito: animação/transição pedida (fade in, digitando, cursor clicando…), se houver.\n"
        "- duvidas: o que está ambíguo ou faltando e vale perguntar ao cliente antes de editar (anexo que não "
        "veio, tempo de fim que não está claro, item que pode virar motion demais).\n\n"
        f"CONTEXTO: {json.dumps(extra, ensure_ascii=False)}\n\n"
        f"BRIEFING:\n{descricao}\n"
    )


def normalizar_revisao(resp: dict[str, Any]) -> dict[str, Any]:
    itens = []
    for it in resp.get("itens") or []:
        if not isinstance(it, dict) or not str(it.get("cena") or "").strip():
            continue
        tipo = it.get("tipo") if it.get("tipo") in TIPOS_CENA else "outro"
        item = {"inicio": str(it.get("inicio") or "").strip()[:12], "fim": str(it.get("fim") or "").strip()[:12],
                "tipo": tipo, **{k: str(it.get(k) or "").strip()[:800] for k in ("cena", "texto_tela", "midia", "efeito")}}
        item["inicio_s"], item["fim_s"] = _segundos(item["inicio"]), _segundos(item["fim"])
        itens.append(item)
    itens.sort(key=lambda i: (i["inicio_s"] is None, i["inicio_s"] or 0))
    return {"resumo": str(resp.get("resumo") or "").strip()[:1200],
            "legenda_padrao": str(resp.get("legenda_padrao") or "").strip()[:400],
            "itens": itens[:80],
            "duvidas": [str(d).strip()[:400] for d in resp.get("duvidas") or [] if str(d).strip()][:12],
            "alertas": alertas_revisao(itens)}


def alertas_revisao(itens: list[dict[str, Any]]) -> list[str]:
    """Soma o tempo de motion tela cheia (fim, ou o começo do próximo momento) e avisa acima do limite."""
    total, sem_fim = 0.0, 0
    for k, it in enumerate(itens):
        if it["tipo"] != "motion_tela_cheia" or it["inicio_s"] is None:
            continue
        fim = it["fim_s"]
        if fim is None:
            fim = next((o["inicio_s"] for o in itens[k + 1:] if o["inicio_s"] is not None and o["inicio_s"] > it["inicio_s"]), None)
        if fim is None or fim <= it["inicio_s"]:
            sem_fim += 1
            continue
        total += fim - it["inicio_s"]
    alertas = []
    if total > LIMITE_MOTION_S:
        alertas.append(f"~{round(total)} s de motion tela cheia (acima de {LIMITE_MOTION_S} s): alinhe com o cliente "
                       "antes de editar; o apresentador some da tela nesses trechos.")
    if sem_fim:
        alertas.append(f"{sem_fim} trecho(s) de motion tela cheia sem fim claro: confirme até onde vão.")
    return alertas


def revisar_descricao(project_id: str, descricao: str | None = None, avancada: bool = False,
                      confirmar_tokens: bool = False) -> dict[str, Any]:
    project = _projeto(project_id)
    record = store.load(project_id) or {}
    texto = (descricao if descricao is not None else record.get("notes") or "").strip()[:12000]
    if not texto:
        raise LookupError("escreva (ou cole) a descrição do projeto primeiro")
    client = clients_module.get_client(project["client_id"]) or {}
    prompt = _prompt_revisao(texto, {"cliente": client.get("name"), "projeto": project.get("name"),
                                     "tags": record.get("tags") or []})
    if avancada:
        binario = editor.find_claude()
        if not binario:
            raise editor.EditorError(editor.availability()["motivo"], 503)
        editor.check_tokens(confirmar_tokens, "A revisão avançada da descrição")
        resp, _ = autocorte.llm_claude(prompt, {"modelo_llm": editor.get_config()["ia"]["claude_modelo"]}, binario, timeout=600,
                                       schema=REVISAO_SCHEMA, env=editor.child_env())
        resp = resp if isinstance(resp, dict) else {}
        usado = "claude" + (f" · {editor.get_config()['ia']['claude_modelo']}" if editor.get_config()["ia"]["claude_modelo"] else "")
    else:
        resp, usado = _corrida(prompt, lambda r: bool(normalizar_revisao(r)["itens"]))
    rev = normalizar_revisao(resp)
    if not rev["itens"]:
        raise autocorte.Falha("o modelo não devolveu o roteiro; tente de novo")
    rev.update(avancada=bool(avancada), modelo=usado,
               assinatura=hashlib.sha256(texto.encode()).hexdigest()[:16],
               gerado_em=datetime.now(timezone.utc).isoformat())
    store.update(project_id, lambda r: r.__setitem__("descricao_revisada", rev))
    return rev


def revisao_salva(project_id: str) -> dict[str, Any]:
    _projeto(project_id)
    record = store.load(project_id) or {}
    rev = record.get("descricao_revisada")
    atual = bool(rev) and rev.get("assinatura") == hashlib.sha256(
        (record.get("notes") or "").strip()[:12000].encode()).hexdigest()[:16]
    return {"revisao": rev, "atual": atual}


def editar_revisao(project_id: str, revisao: dict[str, Any]) -> dict[str, Any]:
    """Grava a revisão editada à mão (mesma normalização da IA; guarda modelo e assinatura da que existia)."""
    _projeto(project_id)
    if not isinstance(revisao, dict):
        raise LookupError("revisão inválida")
    atual = (store.load(project_id) or {}).get("descricao_revisada")
    if not atual:
        raise LookupError("este projeto ainda não tem descrição revisada")
    rev = normalizar_revisao(revisao)
    rev.update({k: atual.get(k) for k in ("avancada", "modelo", "assinatura", "gerado_em")},
               editado_em=datetime.now(timezone.utc).isoformat())
    store.update(project_id, lambda r: r.__setitem__("descricao_revisada", rev))
    return rev


# ------------------------------------------------------------------ marcadores para o Premiere
def _rate(fps: float) -> tuple[int, bool]:
    """FCP XML: timebase inteiro + ntsc (29,97 = 30 NTSC)."""
    base = round(fps)
    return base or 30, abs(fps - base) > 0.01


def xml_premiere(video: Path, info: dict[str, Any], itens: list[dict[str, Any]], nome: str) -> str:
    """FCP 7 XML (xmeml v4), que o Premiere importa (Arquivo → Importar): uma sequência com o vídeo bruto em
    V1/A1 e um marcador por momento do roteiro, com a cena, o texto e a mídia no comentário."""
    from xml.sax.saxutils import escape
    tb, ntsc = _rate(info["fps"])
    fps_real = info["fps"] or tb
    dur = max(1, round(info["duracao"] * fps_real))
    rate = f"<rate><timebase>{tb}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"
    amostra = (f"<samplecharacteristics>{rate}<width>{info['largura']}</width>"
               f"<height>{info['altura']}</height></samplecharacteristics>")
    arquivo = (f'<file id="file-1"><name>{escape(video.name)}</name><pathurl>{escape(video.as_uri())}</pathurl>'
               f"{rate}<duration>{dur}</duration><media><video>{amostra}</video>"
               "<audio><channelcount>2</channelcount></audio></media></file>")

    def clip(cid: str, corpo: str) -> str:
        return (f'<clipitem id="{cid}"><name>{escape(video.name)}</name><duration>{dur}</duration>{rate}'
                f"<start>0</start><end>{dur}</end><in>0</in><out>{dur}</out>{corpo}</clipitem>")

    audio = ('<file id="file-1"/><sourcetrack><mediatype>audio</mediatype>'
             "<trackindex>1</trackindex></sourcetrack>")
    marcadores = []
    for it in itens:
        if it.get("inicio_s") is None:
            continue
        ini = min(dur - 1, round(it["inicio_s"] * fps_real))
        fim = round(it["fim_s"] * fps_real) if it.get("fim_s") and it["fim_s"] > it["inicio_s"] else -1
        partes = [it["cena"]] + [f"{rot}: {it[k]}" for k, rot in (("texto_tela", "Texto"), ("midia", "Mídia"),
                                                                    ("efeito", "Efeito")) if it.get(k)]
        marcadores.append(f"<marker><name>{escape(TIPOS_CENA[it['tipo']])}</name>"
                          f"<comment>{escape(' | '.join(partes))}</comment><in>{ini}</in><out>{fim}</out></marker>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
            f'<sequence id="seq-1"><name>{escape(nome)}</name><duration>{dur}</duration>{rate}'
            f"<media><video><format>{amostra}</format><track>{clip('clip-v', arquivo)}</track></video>"
            f"<audio><track>{clip('clip-a', audio)}</track></audio></media>"
            + "".join(marcadores) + "</sequence></xmeml>\n")


def _info_video(path: Path) -> dict[str, Any]:
    import shutil
    import subprocess
    ffprobe = shutil.which("ffprobe", path=editor._search_path())
    if not ffprobe:
        raise PermissionError("ffprobe não encontrado (instale o ffmpeg)")
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=30).stdout
        d = json.loads(out or "{}")
        s = (d.get("streams") or [{}])[0]
        num, _, den = str(s.get("r_frame_rate") or "30/1").partition("/")
        return {"largura": int(s["width"]), "altura": int(s["height"]),
                "fps": float(num) / float(den or 1), "duracao": float(d["format"]["duration"])}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, ZeroDivisionError):
        raise LookupError("não consegui ler o vídeo (ffprobe)") from None


def marcadores_premiere(project_id: str, video: str | None = None) -> dict[str, Any]:
    """Grava `Marcadores - <vídeo>.xml` na pasta do projeto. Não mexe no vídeo bruto (está no Drive e pode
    ser enorme): o Premiere importa o XML e abre a sequência já com os marcadores."""
    project = _projeto(project_id)
    if not project.get("folder_available"):
        raise LookupError("pasta do projeto não encontrada no disco")
    rev = ((store.load(project_id) or {}).get("descricao_revisada") or {})
    if not rev.get("itens"):
        raise LookupError("revise a descrição primeiro: os marcadores saem do roteiro revisado")
    base = Path(project["path"])
    if not video:
        orig = ((store.load(project_id) or {}).get("midia_original") or {}).get("arquivo")
        cands = (editor.list_candidates(project_id) or {}).get("videos") or []
        video = orig or (cands[0]["path"] if cands else None)
    alvo = editor._resolve_media(base, video, editor.VIDEO_EXTS) if video else None
    if not alvo:
        raise LookupError("nenhum vídeo bruto na pasta do projeto")
    xml = xml_premiere(alvo, _info_video(alvo), rev["itens"], f"Briefing - {alvo.stem}")
    from .persistence import safe_write_text
    destino = base / f"Marcadores - {alvo.stem}.xml"
    safe_write_text(destino, xml)
    return {"arquivo": str(destino), "video": alvo.name,
            "marcadores": sum(1 for i in rev["itens"] if i.get("inicio_s") is not None)}


# ------------------------------------------------------------------ Configurações → Modelos de IA
_modelos_cache: dict[str, Any] = {}


def listar_modelos_openrouter() -> list[dict[str, Any]]:
    """Catálogo público da OpenRouter (sem chave), em memória por 1 h."""
    import time
    import urllib.error
    import urllib.request
    if _modelos_cache.get("ate", 0) > time.time():
        return _modelos_cache["lista"]
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as r:
            dados = json.loads(r.read().decode("utf-8")).get("data") or []
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise autocorte.Falha(f"não consegui listar os modelos da OpenRouter: {exc}") from None
    lista = []
    for m in dados:
        preco = m.get("pricing") or {}
        try:
            gratis = float(preco.get("prompt") or 0) == 0 and float(preco.get("completion") or 0) == 0
        except (TypeError, ValueError):
            gratis = False
        lista.append({"id": m.get("id"), "nome": m.get("name") or m.get("id"), "gratis": gratis,
                      "contexto": m.get("context_length")})
    lista = sorted((m for m in lista if m["id"]), key=lambda m: (not m["gratis"], m["id"]))
    _modelos_cache.update(lista=lista, ate=time.time() + 3600)
    return lista


def testar_modelo(modelo: str | None = None, temperatura: Any = None) -> dict[str, Any]:
    """Uma pergunta curta ao modelo (o do formulário, mesmo sem salvar) para ver se responde e quanto demora."""
    import time
    cfg = vitrine.cfg_openrouter()
    if modelo:
        cfg["openrouter_modelo"] = str(modelo).strip()
    if temperatura not in (None, ""):
        cfg["temperatura"] = float(temperatura)
    if not cfg["openrouter_api_key"]:
        raise PermissionError("sem chave da OpenRouter")
    t0 = time.monotonic()
    resp, custo = vitrine.com_prazo(lambda: autocorte.llm_openrouter(
        'Responda SÓ com JSON {"resposta": "..."}: em uma frase, quem é você (modelo e empresa)?',
        cfg, timeout=cfg["timeout"]), cfg["timeout"], cfg["openrouter_modelo"])
    return {"modelo": cfg["openrouter_modelo"], "segundos": round(time.monotonic() - t0, 1),
            "resposta": str((resp or {}).get("resposta") or resp)[:400] if isinstance(resp, dict) else str(resp)[:400],
            "custo": custo}
