"""Perfis de identidade visual por cliente — o que o estagiário de edição
(editor.py) lê antes de animar qualquer coisa.

Cada perfil mora DENTRO da pasta do cliente, em `<cliente>/_Estagiário/`
(decisão do usuário: identidade, estilos e referências ficam junto do cliente,
não no data/ do app). O "_" põe a pasta no topo do Finder e, sem número na
frente, o scanner de projetos (que lê "NN - nome") não a confunde com um projeto
— "00-Estagiário" colidiria com o "00 - Projeto" que um cliente já tinha. A
pasta só é criada quando algo é escrito (salvar, subir asset, analisar), nunca
ao abrir a aba. Cliente cuja pasta não está no disco fica sem perfil.

O formato é o contrato com a skill `estagiario-editor` e está documentado em
docs/EDITOR_CONTRATO.md — mude os dois juntos.

Status: "sem_perfil" (nada escrito ainda) → "em_analise" (job de onboarding
rodando) → "em_revisao" (rascunho, da análise ou de edição manual) → "pronto"
(só o usuário marca, depois de ler). Editar um perfil pronto não o tira de
pronto: quem edita é o próprio usuário.
"""

import csv
import io
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import clients as clients_module
from . import paths
from .persistence import safe_read_json, safe_read_text, safe_write_json, safe_write_text

STATUS_SEM_PERFIL = "sem_perfil"
STATUS_EM_ANALISE = "em_analise"
STATUS_EM_REVISAO = "em_revisao"
STATUS_PRONTO = "pronto"
STATUSES = (STATUS_SEM_PERFIL, STATUS_EM_ANALISE, STATUS_EM_REVISAO, STATUS_PRONTO)

# chave da API → arquivo no perfil. `duvidas` é escrito pelo estagiário quando
# a análise roda sem ninguém pra responder; o usuário lê e apaga.
TEXT_FILES = {
    "marca": "marca.md",
    "frame": "frame.md",
    "regras": "regras.md",
    "pessoas": "pessoas.csv",
    "duvidas": "duvidas.md",
}

ASSET_CATEGORIES = {
    "fonts": "assets/fonts",
    "logos": "assets/logos",
    "texturas": "assets/texturas",
    "referencias": "referencias",
}

PERFIL_JSON = "perfil.json"
# NFC explícito: o mesmo "á" pode vir composto ou decomposto, e o APFS aceita
# os dois — melhor sempre escrever na mesma forma.
PROFILE_DIR_NAME = unicodedata.normalize("NFC", "_Estagiário")
PESSOAS_HEADER = ["nome", "cargo"]

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

SKELETONS = {
    "marca": (
        "# Manual de identidade visual\n\n"
        "## A marca\n\n## Logo e usos\n\n## Cores\n\n## Tipografia\n\n"
        "## Elementos gráficos\n\n## Linguagem de movimento\n\n## Faça / não faça\n"
    ),
    "frame": (
        "# Tokens de design (YAML)\n\n```yaml\ncores: {}\ndegrades: {}\n"
        "tipografia: {}\neases: {}\ncomponentes: {}\n```\n"
    ),
    "regras": "# Regras deste cliente\n\n",
    "pessoas": "nome,cargo\n",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def profile_dir(client_id: str) -> Path | None:
    """`<pasta do cliente>/_Estagiário`, ou None se o cliente não existe ou a
    pasta dele não está no disco."""
    if not client_id or not _CLIENT_ID_RE.match(client_id):
        return None
    client = clients_module.get_client(client_id)
    if not client or not clients_module.folder_available(client):
        return None
    return Path(client["path"]) / PROFILE_DIR_NAME


# ------------------------------------------------------------------ pessoas
def parse_pessoas(text: str) -> list[dict[str, str]]:
    """pessoas.csv → [{nome, cargo}]. Usa o módulo csv (cargo com vírgula vem
    entre aspas) e pula o cabeçalho e linhas vazias."""
    out = []
    for row in csv.reader(io.StringIO(text or "")):
        if not row or not any(c.strip() for c in row):
            continue
        nome = row[0].strip()
        cargo = ",".join(row[1:]).strip() if len(row) > 2 else (row[1].strip() if len(row) > 1 else "")
        if [nome.lower(), cargo.lower()] == PESSOAS_HEADER:
            continue
        out.append({"nome": nome, "cargo": cargo})
    return out


def format_pessoas(pessoas: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(PESSOAS_HEADER)
    for p in pessoas or []:
        nome = str((p or {}).get("nome") or "").strip()
        cargo = str((p or {}).get("cargo") or "").strip()
        if nome or cargo:
            w.writerow([nome, cargo])
    return buf.getvalue()


def _meta_path(client_id: str) -> Path | None:
    d = profile_dir(client_id)
    return d / PERFIL_JSON if d else None


def _load_meta(client_id: str) -> dict[str, Any]:
    p = _meta_path(client_id)
    return safe_read_json(p, {}) if p else {}


def _save_meta(client_id: str, meta: dict[str, Any]) -> None:
    p = _meta_path(client_id)
    if p:
        safe_write_json(p, meta)


def ensure_profile(client_id: str, textos: bool = True) -> Path | None:
    """Cria a estrutura de pastas do perfil (idempotente). Os textos nascem
    com um esqueleto de seções só se ainda não existirem — e só quando
    `textos`: abrir no Finder cria as pastas, não um perfil."""
    d = profile_dir(client_id)
    if not d:
        return None
    for rel in ASSET_CATEGORIES.values():
        (d / rel).mkdir(parents=True, exist_ok=True)
    if not textos:
        return d
    for key, skeleton in SKELETONS.items():
        target = d / TEXT_FILES[key]
        if not target.exists():
            safe_write_text(target, skeleton)
    if not (d / PERFIL_JSON).exists():
        _save_meta(client_id, {"status": STATUS_SEM_PERFIL, "atualizado_em": _now_iso()})
    return d


def _has_content(client_id: str) -> bool:
    """Algo foi escrito além do esqueleto, ou algum asset foi adicionado."""
    d = profile_dir(client_id)
    if not d or not d.is_dir():
        return False
    for key, name in TEXT_FILES.items():
        text = safe_read_text(d / name, "")
        if text.strip() and text.strip() != SKELETONS.get(key, "").strip():
            return True
    return any(_list_assets(d, rel) for rel in ASSET_CATEGORIES.values())


def get_status(client_id: str) -> str:
    status = _load_meta(client_id).get("status")
    if status in STATUSES:
        return status
    return STATUS_EM_REVISAO if _has_content(client_id) else STATUS_SEM_PERFIL


def _list_assets(d: Path, rel: str) -> list[dict[str, Any]]:
    folder = d / rel
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or any(part.startswith(".") for part in p.relative_to(folder).parts):
            continue
        st = p.stat()
        out.append({
            "name": p.relative_to(folder).as_posix(),
            "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        })
    return out


def templates_info(d: Path | None) -> dict[str, Any]:
    """Os templates do cliente, que deixam legendas e GC automáticos (sem IA):
    marca.json (tokens validados), templates/gc/ (o GC aprovado, com {{NOME}}
    e {{CARGO}}) e as amostras renderizadas em amostras/."""
    if not d or not d.is_dir():
        return {"marca": False, "gc": False, "amostras": []}
    amostras = []
    pasta = d / "amostras"
    if pasta.is_dir():
        for f in sorted(pasta.glob("*.mp4")):
            amostras.append({"name": f.name, "mtime": f.stat().st_mtime})
    return {
        "marca": (d / "marca.json").is_file(),
        "gc": (d / "templates" / "gc" / "index.html").is_file(),
        "amostras": amostras,
    }


def sample_path(client_id: str, name: str) -> Path | None:
    d = profile_dir(client_id)
    if not d or not re.fullmatch(r"[\w -]+\.mp4", name or ""):
        return None
    p = d / "amostras" / name
    return p if p.is_file() else None


def get_profile(client_id: str) -> dict[str, Any] | None:
    """None se o cliente não existe. Se a pasta do cliente sumiu do disco, o
    perfil vem vazio com `disponivel: false` — não há onde ler nem gravar."""
    if not client_id or not _CLIENT_ID_RE.match(client_id):
        return None
    client = clients_module.get_client(client_id)
    if not client:
        return None
    d = profile_dir(client_id)
    if not d:
        return {
            "client_id": client_id,
            "disponivel": False,
            "path": (
                str(base / PROFILE_DIR_NAME)
                if (base := clients_module.client_dir(client)) is not None
                else ""
            ),
            "existe": False,
            "status": STATUS_SEM_PERFIL,
            "atualizado_em": None,
            "analise_job_id": None,
            "pastas_analise": [],
            "textos": {key: "" for key in TEXT_FILES},
            "pessoas": [],
            "assets": {cat: [] for cat in ASSET_CATEGORIES},
            "templates": templates_info(None),
        }
    meta = _load_meta(client_id)
    textos = {key: safe_read_text(d / name, "") for key, name in TEXT_FILES.items()}
    return {
        "client_id": client_id,
        "disponivel": True,
        "path": str(d),
        "existe": d.is_dir(),
        "status": get_status(client_id),
        "atualizado_em": meta.get("atualizado_em"),
        "analise_job_id": meta.get("analise_job_id"),
        "pastas_analise": meta.get("pastas_analise") or [],
        "textos": textos,
        "pessoas": parse_pessoas(textos["pessoas"]),
        "assets": {cat: _list_assets(d, rel) for cat, rel in ASSET_CATEGORIES.items()},
        "templates": templates_info(d),
    }


def save_texts(client_id: str, textos: dict[str, Any]) -> dict[str, Any] | None:
    d = ensure_profile(client_id)
    if not d:
        return None
    for key, value in (textos or {}).items():
        if key in TEXT_FILES and isinstance(value, str):
            safe_write_text(d / TEXT_FILES[key], value)
    # A tela edita pessoas como lista (nome — cargo); o CSV é montado aqui,
    # com aspas onde o cargo tiver vírgula.
    if isinstance((textos or {}).get("pessoas_lista"), list):
        safe_write_text(d / TEXT_FILES["pessoas"], format_pessoas(textos["pessoas_lista"]))
    meta = _load_meta(client_id)
    meta["atualizado_em"] = _now_iso()
    if meta.get("status") in (None, STATUS_SEM_PERFIL) and _has_content(client_id):
        meta["status"] = STATUS_EM_REVISAO
    _save_meta(client_id, meta)
    return get_profile(client_id)


def set_status(client_id: str, status: str) -> dict[str, Any] | None:
    """Troca manual de status — "pronto" depois de revisar, ou voltar pra
    "em_revisao". "em_analise" é só do job de onboarding."""
    if status not in (STATUS_EM_REVISAO, STATUS_PRONTO, STATUS_SEM_PERFIL):
        return None
    if not ensure_profile(client_id):
        return None
    meta = _load_meta(client_id)
    meta["status"] = status
    meta["atualizado_em"] = _now_iso()
    _save_meta(client_id, meta)
    return get_profile(client_id)


def mark_analysis_started(client_id: str, job_id: str, pastas: list[str] | None = None) -> None:
    ensure_profile(client_id)
    meta = _load_meta(client_id)
    # Lembra as pastas escolhidas: a próxima análise já abre com elas.
    meta["pastas_analise"] = list(pastas or [])
    if meta.get("status") != STATUS_EM_ANALISE:
        meta["status_antes_da_analise"] = meta.get("status") or STATUS_SEM_PERFIL
    meta["status"] = STATUS_EM_ANALISE
    meta["analise_job_id"] = job_id
    meta["atualizado_em"] = _now_iso()
    _save_meta(client_id, meta)


def mark_analysis_finished(client_id: str, ok: bool) -> None:
    """Análise que terminou bem vira rascunho pra revisar (nunca "pronto"
    sozinha). Se falhou ou foi cancelada, volta ao status de antes — mas se o
    estagiário chegou a escrever algo, isso é um rascunho."""
    meta = _load_meta(client_id)
    if meta.get("status") != STATUS_EM_ANALISE:
        return
    before = meta.pop("status_antes_da_analise", STATUS_SEM_PERFIL)
    if ok or (before == STATUS_SEM_PERFIL and _has_content(client_id)):
        meta["status"] = STATUS_EM_REVISAO
    else:
        meta["status"] = before
    meta["atualizado_em"] = _now_iso()
    _save_meta(client_id, meta)


def _clean_filename(name: str) -> str:
    # Mantém acentos e espaços (são nomes de fonte e de vídeo que o usuário
    # reconhece), só tira separadores, `..` e o ponto inicial de arquivo oculto.
    cleaned = paths.sanitize_folder_component(Path(name or "").name)
    return cleaned.lstrip(".").strip()


def asset_path(client_id: str, categoria: str, name: str) -> Path | None:
    """Caminho de um asset existente, validado pra ficar dentro da categoria."""
    d = profile_dir(client_id)
    rel = ASSET_CATEGORIES.get(categoria)
    if not d or not rel or not name:
        return None
    folder = (d / rel).resolve()
    target = (folder / name).resolve()
    if folder not in target.parents or not target.is_file():
        return None
    return target


def add_asset(client_id: str, categoria: str, filename: str, file_storage: Any) -> dict[str, Any] | None:
    """Grava um upload na categoria. Nome repetido ganha sufixo " (2)" em vez
    de sobrescrever — uma fonte trocada sem querer quebra todo render."""
    rel = ASSET_CATEGORIES.get(categoria)
    d = ensure_profile(client_id)
    name = _clean_filename(filename)
    if not d or not rel or not name:
        return None
    folder = d / rel
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    n = 2
    while dest.exists():
        dest = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    file_storage.save(str(dest))
    meta = _load_meta(client_id)
    meta["atualizado_em"] = _now_iso()
    if meta.get("status") in (None, STATUS_SEM_PERFIL):
        meta["status"] = STATUS_EM_REVISAO
    _save_meta(client_id, meta)
    return {"categoria": categoria, "name": dest.name}


def delete_asset(client_id: str, categoria: str, name: str) -> bool:
    target = asset_path(client_id, categoria, name)
    if not target:
        return False
    target.unlink()
    return True



# ------------------------------------------------------------------ projetos aprovados como referência visual
def referencias_projetos(client_id: str) -> list[dict[str, Any]]:
    """Projetos do cliente que o editor marcou como referência visual aprovada (perfil.json →
    `referencias_projetos`). Alimentam a análise de identidade e os pedidos de motion."""
    from . import projects as projects_module

    out = []
    for pid in _load_meta(client_id).get("referencias_projetos") or []:
        p = projects_module.get_project(pid)
        if p:
            out.append({"id": pid, "nome": p.get("folder_name") or p.get("name"),
                        "pasta": p.get("path") if p.get("folder_available") else None})
    return out


def marcar_referencia(project_id: str, ativo: bool) -> list[dict[str, Any]]:
    from . import projects as projects_module

    p = projects_module.get_project(project_id)
    if not p:
        raise LookupError("projeto não encontrado")
    client_id = p["client_id"]
    if not profile_dir(client_id):
        raise LookupError("pasta do cliente não encontrada no disco")
    ensure_profile(client_id, textos=False)
    meta = _load_meta(client_id)
    ids = [i for i in meta.get("referencias_projetos") or [] if i != project_id]
    if ativo:
        ids.append(project_id)
    meta["referencias_projetos"] = ids
    _save_meta(client_id, meta)
    return referencias_projetos(client_id)


def referencias_texto(client_id: str | None, exceto: str | None = None) -> tuple[str, list[str]]:
    """(linha para o pedido, pastas) das referências aprovadas; ("", []) sem nenhuma."""
    if not client_id:
        return "", []
    refs = [r for r in referencias_projetos(client_id) if r["pasta"] and r["id"] != exceto]
    if not refs:
        return "", []
    return ("REFERÊNCIAS APROVADAS: o editor marcou estes projetos do cliente como referência visual do que foi "
            "aprovado. Olhe as entregas finais deles (vídeos e Motion - … vN) para calibrar o nível e o tom, sem "
            "copiar a composição: " + "; ".join(r["pasta"] for r in refs) + "."), [r["pasta"] for r in refs]
