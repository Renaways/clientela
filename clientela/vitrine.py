"""Vitrine de identidade visual do cliente: o que a tela do cliente mostra antes do pipeline.

Tudo aqui é script: lê o perfil `_Estagiário/` (marca.json, textos, assets) e as pastas que o
usuário escolheu por categoria (ícones, por exemplo, não têm lugar no perfil). A única parte com
IA é `gerar_resumo` — uma descrição curta e o que falta, pelo modelo gratuito da OpenRouter —,
guardada em perfil.json junto com a assinatura dos fatos, para saber quando ficou desatualizada.

As pastas escolhidas ficam em perfil.json (`vitrine_pastas`), que vai junto com a pasta do
cliente pelo Drive: dentro da pasta do cliente o caminho é relativo (posix), e abre no Mac e no
Windows; fora dela, absoluto (só vale no sistema que escolheu).
"""

import concurrent.futures
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ambiente, autocorte, editor, identidades
from . import clients as clients_module
from .persistence import safe_read_json, safe_read_text

# shortcut: modelo fixo (o roteador gratuito da OpenRouter escolhe um modelo :free); vira config se precisar trocar.
MODELO = "openrouter/free"

IMAGEM = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"}
VIDEO = {".mp4", ".mov", ".webm", ".m4v"}
FONTE = set(ambiente.FONT_FILE_EXTS)

# chave → (rótulo, extensões, pasta dentro do perfil ou None)
CATEGORIAS = {
    "logos": ("Logos", IMAGEM, identidades.ASSET_CATEGORIES["logos"]),
    "icones": ("Ícones", IMAGEM, None),
    "texturas": ("Texturas e elementos", IMAGEM | VIDEO, identidades.ASSET_CATEGORIES["texturas"]),
    "fontes": ("Fontes", FONTE, identidades.ASSET_CATEGORIES["fonts"]),
    "referencias": ("Referências", IMAGEM | VIDEO | {".pdf"}, identidades.ASSET_CATEGORIES["referencias"]),
}
MAX_ARQUIVOS = 240     # por categoria e origem: uma pasta de ícones com milhares não trava a tela
MAX_VISITADOS = 5000   # entradas lidas no disco por origem
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")


def _tipo(ext: str) -> str:
    return "imagem" if ext in IMAGEM else "video" if ext in VIDEO else "fonte" if ext in FONTE else "pdf"


def _pasta_escolhida(client: dict[str, Any], valor: str | None) -> Path | None:
    if not valor:
        return None
    p = Path(valor)
    if p.is_absolute():
        return p
    base = clients_module.client_dir(client)
    return base / p if base is not None else None


def _raizes(client_id: str, cat: str, meta: dict[str, Any] | None = None) -> dict[str, Path]:
    """Origem → pasta: "perfil" (dentro de _Estagiário) e "pasta" (a escolhida)."""
    client = clients_module.get_client(client_id)
    perfil = identidades.profile_dir(client_id)
    meta = identidades._load_meta(client_id) if meta is None else meta
    out = {}
    rel = CATEGORIAS[cat][2]
    if perfil and rel:
        out["perfil"] = perfil / rel
    escolhida = _pasta_escolhida(client or {}, (meta.get("vitrine_pastas") or {}).get(cat))
    if escolhida is not None:
        out["pasta"] = escolhida
    return out


def _listar(raiz: Path, exts: set[str]) -> tuple[list[dict[str, Any]], bool]:
    """Arquivos da pasta (recursivo, sem pastas ocultas). Devolve (itens, cortado)."""
    if not raiz.is_dir():
        return [], False
    itens, visitados = [], 0
    for dirpath, dirs, nomes in os.walk(raiz):
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_anteriores")))
        for n in sorted(nomes):
            visitados += 1
            if visitados > MAX_VISITADOS or len(itens) >= MAX_ARQUIVOS:
                return itens, True
            ext = Path(n).suffix.lower()
            if n.startswith(".") or ext not in exts:
                continue
            rel = (Path(dirpath) / n).relative_to(raiz).as_posix()
            itens.append({"nome": rel, "tipo": _tipo(ext)})
    return itens, False


def _fontes(arquivos: list[dict[str, Any]], raizes: dict[str, Path]) -> list[dict[str, Any]]:
    """Agrupa os arquivos de fonte por família (tabela `name` do próprio arquivo)."""
    instaladas = ambiente.familias_instaladas()
    por_familia: dict[str, list[dict[str, Any]]] = {}
    for a in arquivos:
        p = raizes[a["origem"]] / a["nome"]
        for fam in ambiente.familias_da_fonte(p) or {Path(a["nome"]).stem.split("-")[0]}:
            por_familia.setdefault(fam, []).append(a)
    out = []
    for fam, xs in sorted(por_familia.items(), key=lambda kv: kv[0].lower()):
        # a amostra usa o "Regular" quando há um; senão o nome mais curto (costuma ser o peso base)
        previa = next((x for x in xs if "regular" in x["nome"].lower()), min(xs, key=lambda x: len(x["nome"])))
        out.append({"familia": fam, "arquivos": len(xs), "instalada": ambiente.fonte_instalada(fam, instaladas),
                    "previa": {"origem": previa["origem"], "nome": previa["nome"]}})
    return out


def _cores(perfil: Path | None, marca: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cores do marca.json (nomes iguais com o mesmo hex viram uma cor só); sem ele, os hex dos textos."""
    cores: dict[str, list[str]] = {}
    degrades = []
    if marca:
        for nome, v in (marca.get("cores") or {}).items():
            if isinstance(v, str) and _HEX_RE.fullmatch(v.strip()):
                cores.setdefault(v.strip().upper(), []).append(nome)
        degrades = [{"nome": k, "css": v} for k, v in (marca.get("degrades") or {}).items()
                    if isinstance(v, str) and "gradient(" in v]
    elif perfil:
        for arq in ("marca.md", "frame.md"):
            for linha in safe_read_text(perfil / arq, "").splitlines():
                for h in _HEX_RE.findall(linha):
                    rotulo = re.sub(r"[#*`|:\-]+", " ", _HEX_RE.sub("", linha)).strip()[:32]
                    cores.setdefault(h.upper(), []).append(rotulo)
    return [{"hex": h, "nomes": list(dict.fromkeys(n for n in ns if n))} for h, ns in cores.items()], degrades


def _papeis(marca: dict[str, Any] | None) -> list[dict[str, Any]]:
    papeis = ((marca or {}).get("tipografia") or {}).get("papeis") or {}
    return [{"papel": k, **{c: v.get(c) for c in ("familia", "peso", "estilo", "caixa", "tracking", "entrelinha")}}
            for k, v in papeis.items() if isinstance(v, dict) and v.get("familia")]


def _faltando(v: dict[str, Any]) -> list[str]:
    """O que falta, por regra — vai para a tela e para o prompt do resumo."""
    if not v["disponivel"]:
        return ["A pasta do cliente não está neste computador."]
    out = []
    cats = v["categorias"]
    if not cats["logos"]["arquivos"]:
        out.append("Nenhum logo.")
    if not cats["icones"]["pasta"]:
        out.append("Nenhuma pasta de ícones escolhida.")
    elif not cats["icones"]["arquivos"]:
        out.append("A pasta de ícones não tem imagens (ou não está neste computador).")
    if not v["fontes"]:
        out.append("Nenhum arquivo de fonte.")
    nao_instaladas = [f["familia"] for f in v["fontes"] if not f["instalada"]]
    if nao_instaladas:
        out.append("Fontes não instaladas neste computador: " + ", ".join(nao_instaladas) + ".")
    if not v["cores"]:
        out.append("Nenhuma cor definida.")
    if not v["marca_json"]:
        out.append("Sem marca.json: legendas e GC ainda não saem sozinhos.")
    if not v["pessoas"]:
        out.append("Nenhuma pessoa cadastrada (nomes e cargos para o GC).")
    if not cats["referencias"]["arquivos"]:
        out.append("Nenhuma referência de trabalho aprovado.")
    if v["status"] != identidades.STATUS_PRONTO:
        out.append("O perfil ainda não foi marcado como pronto.")
    return out


def _assinatura(v: dict[str, Any]) -> str:
    fatos = {k: v[k] for k in ("cores", "degrades", "papeis", "fontes", "pessoas", "faltando")}
    fatos["arquivos"] = {c: [a["nome"] for a in x["arquivos"]] for c, x in v["categorias"].items()}
    return hashlib.sha1(json.dumps(fatos, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def coletar(client_id: str) -> dict[str, Any] | None:
    client = clients_module.get_client(client_id)
    if not client:
        return None
    perfil = identidades.profile_dir(client_id)
    meta = identidades._load_meta(client_id)
    marca = safe_read_json(perfil / "marca.json", None) if perfil else None
    marca = marca if isinstance(marca, dict) else None
    escolhidas = meta.get("vitrine_pastas") or {}
    categorias, fontes = {}, []
    for cat, (rotulo, exts, _) in CATEGORIAS.items():
        raizes = _raizes(client_id, cat, meta)
        arquivos, cortado = [], False
        for origem, raiz in raizes.items():
            xs, c = _listar(raiz, exts)
            arquivos += [{**x, "origem": origem} for x in xs]
            cortado = cortado or c
        pasta = escolhidas.get(cat)
        categorias[cat] = {
            "rotulo": rotulo, "arquivos": arquivos, "cortado": cortado, "pasta": pasta,
            "pasta_ok": bool(pasta) and "pasta" in raizes and raizes["pasta"].is_dir(),
        }
        if cat == "fontes":
            fontes = _fontes(arquivos, raizes)
    cores, degrades = _cores(perfil, marca)
    # os arquivos que o marca.json liga a peso/estilo: a tela desenha cada papel tipográfico com o arquivo certo
    nomes_perfil = {a["nome"] for a in categorias["fontes"]["arquivos"] if a["origem"] == "perfil"}
    fontes_marca = [{"familia": f.get("familia"), "peso": f.get("peso") or 400, "estilo": f.get("estilo") or "normal",
                     "origem": "perfil", "nome": f["arquivo"]}
                    for f in ((marca or {}).get("tipografia") or {}).get("fontes") or []
                    if isinstance(f, dict) and f.get("familia") and f.get("arquivo") in nomes_perfil]
    pessoas = identidades.parse_pessoas(safe_read_text(perfil / "pessoas.csv", "")) if perfil else []
    v = {
        "client_id": client_id,
        "disponivel": perfil is not None,
        "status": identidades.get_status(client_id),
        "marca_json": marca is not None,
        "cores": cores, "degrades": degrades, "papeis": _papeis(marca), "fontes": fontes, "fontes_marca": fontes_marca,
        "pessoas": pessoas, "categorias": categorias,
        "amostras": identidades.templates_info(perfil)["amostras"],
    }
    v["faltando"] = _faltando(v)
    v["assinatura"] = _assinatura(v)
    resumo = meta.get("vitrine_resumo")
    v["resumo"] = resumo if isinstance(resumo, dict) else None
    v["resumo_atual"] = bool(v["resumo"]) and v["resumo"].get("assinatura") == v["assinatura"]
    v["ia_disponivel"] = bool(_chave())
    return v


def escolher_pasta(client_id: str, cat: str, pasta: str | None) -> None:
    """Grava (ou apaga, com None/"") a pasta escolhida de uma categoria."""
    if cat not in CATEGORIAS:
        raise ValueError("categoria inválida")
    client = clients_module.get_client(client_id)
    d = identidades.ensure_profile(client_id, textos=False)
    if not client or not d:
        raise LookupError("pasta do cliente não encontrada no disco")
    meta = identidades._load_meta(client_id)
    pastas = dict(meta.get("vitrine_pastas") or {})
    if pasta:
        p = Path(pasta)
        if not p.is_absolute() or not p.is_dir():
            raise ValueError("escolha uma pasta que exista")
        base = clients_module.client_dir(client)
        try:
            pasta = p.resolve().relative_to(base.resolve()).as_posix() if base is not None else str(p)
        except ValueError:
            pasta = str(p)
        pastas[cat] = pasta
    else:
        pastas.pop(cat, None)
    meta["vitrine_pastas"] = pastas
    identidades._save_meta(client_id, meta)


def arquivo(client_id: str, cat: str, origem: str, nome: str) -> Path | None:
    """Caminho de um arquivo da vitrine, validado para ficar dentro da origem."""
    if cat not in CATEGORIAS or not nome:
        return None
    raiz = _raizes(client_id, cat).get(origem)
    if raiz is None:
        return None
    raiz = raiz.resolve()
    alvo = (raiz / nome).resolve()
    if raiz not in alvo.parents or not alvo.is_file() or alvo.suffix.lower() not in CATEGORIAS[cat][1]:
        return None
    return alvo


# ------------------------------------------------------------------ resumo (IA)
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="ia")


def com_prazo(fn: Any, segundos: float, modelo: str) -> Any:
    """Prazo de verdade para a chamada inteira: o timeout do urllib é por leitura, e a OpenRouter manda
    "processando" enquanto espera um modelo lento, então ele nunca estourava (o roteador gratuito chegou
    a 2 min numa revisão). shortcut: a thread abandonada termina sozinha quando a resposta chegar."""
    fut = _pool.submit(fn)
    try:
        return fut.result(timeout=segundos)
    except concurrent.futures.TimeoutError:
        raise autocorte.Falha(f"{modelo} não respondeu em {int(segundos)} s. Tente de novo, ou escolha um modelo "
                              "mais rápido em Configurações → Modelos de IA (o Testar mostra quanto cada um demora).") from None


def cfg_openrouter() -> dict[str, Any]:
    """Modelo/temperatura/timeout das funções do app (Configurações → Modelos de IA) + a chave."""
    ia = editor.get_config()["ia"]
    return {"openrouter_modelo": ia["openrouter_modelo"] or MODELO, "temperatura": ia["temperatura"],
            "timeout": ia["timeout"], "openrouter_api_key": _chave()}


def _chave() -> str:
    return os.environ.get("OPENROUTER_API_KEY") or editor.get_config()["corte"].get("openrouter_api_key") or ""


def _prompt(v: dict[str, Any], client: dict[str, Any], perfil: Path | None) -> str:
    marca_md = safe_read_text(perfil / "marca.md", "")[:4000] if perfil else ""
    regras = safe_read_text(perfil / "regras.md", "")[:1500] if perfil else ""
    contagem = {c: len(x["arquivos"]) for c, x in v["categorias"].items()}
    fatos = {
        "cliente": client.get("name"), "cores": v["cores"][:24], "degrades": [d["nome"] for d in v["degrades"]],
        "fontes": [{"familia": f["familia"], "instalada": f["instalada"]} for f in v["fontes"]],
        "papeis_tipograficos": v["papeis"], "pessoas": len(v["pessoas"]), "arquivos_por_categoria": contagem,
        "logos": [a["nome"] for a in v["categorias"]["logos"]["arquivos"][:20]],
        "faltando_pelas_regras": v["faltando"],
    }
    return (
        "Você é diretor de arte de um estúdio de edição de vídeo e motion design. Abaixo estão os fatos "
        "coletados por script sobre a identidade visual de um cliente e o manual dele. Responda SÓ com JSON "
        '{"descricao": "...", "faltando": ["..."]}, em português do Brasil:\n'
        "- descricao: 2 a 4 frases sobre a personalidade visual (cores, tipografia, elementos, tom). "
        "Nada de inventar o que não está nos fatos.\n"
        "- faltando: até 6 itens curtos e práticos do que falta para editar e animar vídeos desse cliente "
        "com segurança, começando pelo mais importante. Pode reescrever os itens das regras e somar o que "
        "o manual deixa vago (ex.: sem regra de uso do logo).\n\n"
        f"FATOS:\n{json.dumps(fatos, ensure_ascii=False, indent=1)}\n\n"
        f"MANUAL (marca.md):\n{marca_md or '(vazio)'}\n\nREGRAS (regras.md):\n{regras or '(vazio)'}\n"
    )


def gerar_resumo(client_id: str) -> dict[str, Any]:
    """Chama o modelo gratuito da OpenRouter e guarda o resultado em perfil.json."""
    client = clients_module.get_client(client_id)
    v = coletar(client_id)
    if not client or not v:
        raise LookupError("cliente não encontrado")
    if not v["disponivel"]:
        raise LookupError("pasta do cliente não encontrada no disco")
    cfg = cfg_openrouter()
    if not cfg["openrouter_api_key"]:
        raise PermissionError("sem chave da OpenRouter (Configurações → Modelos de IA, ou OPENROUTER_API_KEY)")
    perfil = identidades.profile_dir(client_id)
    resp, _ = com_prazo(lambda: autocorte.llm_openrouter(_prompt(v, client, perfil), cfg, timeout=cfg["timeout"]),
                        cfg["timeout"], cfg["openrouter_modelo"])
    resp = resp if isinstance(resp, dict) else {}
    descricao = str(resp.get("descricao") or "").strip()
    if not descricao:
        raise autocorte.Falha("o modelo não devolveu uma descrição; tente de novo")
    faltando = [str(x).strip() for x in resp.get("faltando") or [] if str(x).strip()][:8]
    resumo = {"descricao": descricao[:1200], "faltando": faltando, "modelo": cfg["openrouter_modelo"],
              "assinatura": v["assinatura"], "gerado_em": datetime.now(timezone.utc).isoformat()}
    identidades.ensure_profile(client_id, textos=False)
    meta = identidades._load_meta(client_id)
    meta["vitrine_resumo"] = resumo
    identidades._save_meta(client_id, meta)
    return resumo
