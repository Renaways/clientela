"""Checklist de ambiente por cliente: antes de abrir um projeto, o que falta nesta máquina.

Três grupos, para cada cliente ativo, varridos em segundo plano quando o app sobe (``start_background_scan``,
do ``app.py``) e de novo sob pedido:

- **Fontes** — os arquivos de ``_Estagiário/assets/fonts`` agrupados pela família que eles mesmos declaram
  (tabela ``name`` do TTF/OTF, lida aqui sem dependência), comparados com as famílias instaladas no sistema.
  Comparar pelo arquivo, e não pelo ``marca.json``, é o que funciona: um cliente declarava "Hando" e o arquivo
  instala "Hando Trial". Uma família que falta pode ser instalada pelo app (cópia para a pasta de fontes do
  usuário; no Windows, mais a entrada no registro do usuário).
- **Pastas** — a do cliente neste sistema, o perfil ``_Estagiário``, os templates Premiere/AE do cliente e as
  pastas de projeto que sumiram (registro sem pasta, sem ser "sem pasta" de propósito).
- **Mídias linkadas** — nos projetos abertos, as sequências ``.clientela`` (mídia offline) e os ``.prproj``
  (gzip + XML: ``ActualMediaFilePath`` e ``RelativePath`` de cada ``<Media>``; o Premiere religa pelo
  relativo, então só falta o que não existe por nenhum dos dois). Caminho de outro sistema (``G:\\`` no Mac)
  que não resolve vira aviso, não falta.

E um relatório do app (fonte das legendas do corte vertical, pasta de SFX, pasta de clipes).
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import struct
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import clients as clients_module
from . import clips as clips_module
from . import editor, identidades, machine, organizer, paths
from . import projects as projects_module
from .persistence import safe_read_json, safe_write_json

logger = logging.getLogger(__name__)

FONT_FILE_EXTS = {".ttf", ".otf", ".ttc"}
FONTES_CACHE = "fontes-sistema.json"   # data/editor/: famílias de cada arquivo de fonte do sistema
PRPROJ_MAX = 400 * 1024 * 1024         # descompactado; projeto maior que isso é lido até aqui
PRPROJ_POR_PROJETO = 2                 # os mais recentes de cada projeto
MIDIAS_LISTADAS = 12

_lock = threading.Lock()
_relatorios: dict[str, dict[str, Any]] = {}
_estado = {"rodando": False, "fim": None}
_familias_cache: set[str] | None = None


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(nome: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(nome or "")).lower()


# ------------------------------------------------------------------ fontes
def familias_da_fonte(path: Path) -> set[str]:
    """Famílias que um TTF/OTF/TTC declara (nameID 16, o "typographic family", ou 1). WOFF fica de fora
    (comprimido; navegador, não sistema)."""
    out: set[str] = set()
    try:
        with open(path, "rb") as f:
            tag = f.read(4)
            inicios = [0]
            if tag == b"ttcf":
                f.read(4)
                (n,) = struct.unpack(">I", f.read(4))
                inicios = list(struct.unpack(f">{min(n, 16)}I", f.read(4 * min(n, 16))))
            for ini in inicios:
                f.seek(ini + 4)
                (ntab,) = struct.unpack(">H", f.read(2))
                f.seek(ini + 12)
                tabelas = f.read(16 * ntab)
                for k in range(ntab):
                    nome, _, off, tam = struct.unpack(">4sIII", tabelas[k * 16:(k + 1) * 16])
                    if nome != b"name":
                        continue
                    f.seek(off)
                    bloco = f.read(min(tam, 1 << 20))
                    _, cont, base = struct.unpack(">HHH", bloco[:6])
                    achados: dict[int, str] = {}
                    for r in range(cont):
                        plat, enc, lang, nid, comp, desl = struct.unpack(">HHHHHH", bloco[6 + r * 12:18 + r * 12])
                        if nid not in (1, 16) or nid in achados:
                            continue
                        crus = bloco[base + desl:base + desl + comp]
                        if plat == 3 or plat == 0:
                            texto = crus.decode("utf-16-be", "ignore")
                        elif plat == 1 and lang == 0:
                            texto = crus.decode("latin-1", "ignore")
                        else:
                            continue
                        if texto.strip():
                            achados[nid] = texto.strip()
                    fam = achados.get(16) or achados.get(1)
                    if fam:
                        out.add(fam)
    except (OSError, struct.error, ValueError):
        pass
    return out


def pastas_de_fontes() -> list[Path]:
    if machine.current_os() == machine.WINDOWS:
        win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Microsoft" / "Windows" / "Fonts"
        return [win, local]
    if machine.current_os() == machine.MAC:
        return [Path.home() / "Library" / "Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts"),
                Path("/System/Library/Fonts/Supplemental")]
    return [Path.home() / ".local" / "share" / "fonts", Path.home() / ".fonts", Path("/usr/share/fonts")]


def pasta_de_fontes_do_usuario() -> Path:
    """Para onde "Instalar" copia (sem pedir senha de administrador)."""
    return pastas_de_fontes()[1 if machine.current_os() == machine.WINDOWS else 0]


def familias_instaladas(refresh: bool = False) -> set[str]:
    """Famílias de todas as fontes do sistema (normalizadas), com cache por arquivo (mtime)."""
    global _familias_cache
    if _familias_cache is not None and not refresh:
        return _familias_cache
    cache_path = paths.EDITOR_DIR / FONTES_CACHE
    cache = safe_read_json(cache_path, {}) or {}
    novo: dict[str, Any] = {}
    fams: set[str] = set()
    for pasta in pastas_de_fontes():
        if not pasta.is_dir():
            continue
        for raiz, dirs, nomes in os.walk(pasta):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in nomes:
                p = Path(raiz) / n
                if p.suffix.lower() not in FONT_FILE_EXTS:
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                c = cache.get(str(p))
                lista = c["familias"] if c and c.get("mtime") == mtime else sorted(familias_da_fonte(p))
                novo[str(p)] = {"mtime": mtime, "familias": lista}
                fams.update(_norm(x) for x in lista)
                fams.add(_norm(p.stem.split("-")[0]))   # "HandoTrial-Bold.otf" também responde por "HandoTrial"
    paths.EDITOR_DIR.mkdir(parents=True, exist_ok=True)
    if novo != cache:
        safe_write_json(cache_path, novo)
    _familias_cache = fams
    return fams


def fonte_instalada(familia: str, instaladas: set[str] | None = None) -> bool:
    inst = familias_instaladas() if instaladas is None else instaladas
    alvo = _norm(familia)
    return bool(alvo) and (alvo in inst or any(f.startswith(alvo) for f in inst if len(alvo) >= 4))


def _fontes_do_cliente(perfil: Path | None, instaladas: set[str]) -> list[dict[str, Any]]:
    itens = []
    pasta = perfil / identidades.ASSET_CATEGORIES["fonts"] if perfil else None
    arquivos = sorted(p for p in pasta.iterdir() if p.suffix.lower() in FONT_FILE_EXTS) if pasta and pasta.is_dir() else []
    por_familia: dict[str, list[Path]] = {}
    for p in arquivos:
        for fam in familias_da_fonte(p) or {p.stem.split("-")[0]}:
            por_familia.setdefault(fam, []).append(p)
    for fam, ps in sorted(por_familia.items()):
        ok = fonte_instalada(fam, instaladas)
        itens.append({
            "grupo": "fontes", "estado": "ok" if ok else "falta", "titulo": fam,
            "detalhe": f"{len(ps)} arquivo{'s' if len(ps) > 1 else ''} em _Estagiário/assets/fonts"
                       + ("" if ok else " · não instalada neste computador (Premiere e After Effects não acham)"),
            "caminhos": [p.name for p in ps],
            **({} if ok else {"acao": {"tipo": "instalar_fonte", "familia": fam}}),
        })
    marca = safe_read_json(perfil / "marca.json", None) if perfil else None
    declaradas = {f.get("familia") for f in ((marca or {}).get("tipografia") or {}).get("fontes") or [] if isinstance(f, dict)}
    for fam in sorted(x for x in declaradas if x):
        if any(_norm(i["titulo"]).startswith(_norm(fam)) for i in itens) or fonte_instalada(fam, instaladas):
            continue
        itens.append({"grupo": "fontes", "estado": "falta", "titulo": fam,
                      "detalhe": "está no marca.json, mas não tem arquivo em assets/fonts nem está instalada"})
    return itens


def instalar_fonte(client_id: str, familia: str) -> dict[str, Any]:
    """Copia os arquivos da família (de _Estagiário/assets/fonts) para a pasta de fontes do usuário.
    Nunca sobrescreve um arquivo que já existe lá."""
    perfil = identidades.profile_dir(client_id)
    pasta = perfil / identidades.ASSET_CATEGORIES["fonts"] if perfil else None
    if not pasta or not pasta.is_dir():
        raise ValueError("o cliente não tem fontes no perfil")
    alvo = [p for p in sorted(pasta.iterdir()) if p.suffix.lower() in FONT_FILE_EXTS
            and familia in (familias_da_fonte(p) or {p.stem.split("-")[0]})]
    if not alvo:
        raise ValueError(f"nenhum arquivo da família {familia}")
    destino = pasta_de_fontes_do_usuario()
    destino.mkdir(parents=True, exist_ok=True)
    copiados = []
    for p in alvo:
        final = destino / p.name
        if not final.exists():
            shutil.copy2(p, final)
            copiados.append(p.name)
            if machine.current_os() == machine.WINDOWS:   # pragma: no cover - só no Windows
                _registrar_fonte_windows(final, familia)
    familias_instaladas(refresh=True)
    return {"familia": familia, "copiados": copiados, "pasta": str(destino)}


def _registrar_fonte_windows(arquivo: Path, familia: str) -> None:  # pragma: no cover - só no Windows
    import winreg

    chave = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, chave, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, f"{familia} {arquivo.stem} (TrueType)", 0, winreg.REG_SZ, str(arquivo))


# ------------------------------------------------------------------ mídias linkadas
# .aegraphic = mídia de Motion Graphics Template: o Premiere guarda o template dentro do projeto e reextrai
# numa máquina nova, então o caminho velho dela não é mídia offline
IGNORADAS = {".aegraphic"}
_HOME_ALHEIA = re.compile(r"^/Users/[^/]+/(.+)$")
_OUTRO_SO = re.compile(r"^[A-Za-z]:[\\/]") if sys.platform != "win32" else re.compile(r"^/")


def midias_do_prproj(prproj: Path) -> list[dict[str, Any]]:
    """Cada mídia de um projeto do Premiere: caminho absoluto, relativo e se existe por algum dos dois."""
    try:
        with gzip.open(prproj, "rb") as f:
            xml = f.read(PRPROJ_MAX).decode("utf-8", "replace")
    except (OSError, EOFError, gzip.BadGzipFile):
        return []
    vistos: dict[str, dict[str, Any]] = {}
    for bloco in re.finditer(r"<Media\b[^>]*>(.*?)</Media>", xml, re.S):
        corpo = bloco.group(1)
        m_abs = re.search(r"<ActualMediaFilePath>(.*?)</ActualMediaFilePath>", corpo) or re.search(r"<FilePath>(.*?)</FilePath>", corpo)
        if not m_abs:
            continue
        absoluto = _xml(m_abs.group(1))
        if not ("/" in absoluto or "\\" in absoluto) or absoluto in vistos:
            continue
        if Path(absoluto.replace("\\", "/")).suffix.lower() in IGNORADAS:
            continue
        m_rel = re.search(r"<RelativePath>(.*?)</RelativePath>", corpo)
        relativo = _xml(m_rel.group(1)) if m_rel else ""
        existe = Path(absoluto).exists() if not _OUTRO_SO.match(absoluto) else False
        if not existe and relativo:
            existe = (prproj.parent / relativo.replace("\\", "/")).exists()
        if not existe:   # mesmo caminho na home deste usuário (projeto feito em outro Mac, mídia vinda pelo Drive)
            m_home = _HOME_ALHEIA.match(absoluto)
            existe = bool(m_home) and (Path.home() / m_home.group(1)).exists()
        vistos[absoluto] = {"caminho": absoluto, "existe": existe, "outro_sistema": bool(_OUTRO_SO.match(absoluto))}
    return list(vistos.values())


def _xml(t: str) -> str:
    return (t.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
            .replace("&amp;", "&").strip())


def _midias_do_projeto(project: dict[str, Any]) -> list[dict[str, Any]]:
    itens = []
    pdir = Path(project["path"])
    for arq in editor.list_sequences(pdir):
        try:
            v = editor.load_sequence(pdir, arq["arquivo"])
        except editor.EditorError as exc:
            itens.append({"grupo": "midias", "estado": "falta", "titulo": f"{project['num']} · {arq['arquivo']}", "detalhe": str(exc)})
            continue
        off = [m for m in v["doc"]["midias"] if m["id"] in v["offline"]]
        if off:
            outro = [m for m in off if _OUTRO_SO.match(m["caminho"])]
            itens.append({"grupo": "midias", "estado": "aviso" if len(outro) == len(off) else "falta",
                          "titulo": f"{project['num']} · {arq['arquivo']}",
                          "detalhe": f"{len(off)} mídia(s) offline" + (f", {len(outro)} com caminho de outro sistema" if outro else ""),
                          "caminhos": [m["caminho"] for m in off][:MIDIAS_LISTADAS]})
    # o .prproj mora numa subpasta com o nome do projeto; list_open_files anda a árvore sem as cópias de
    # Auto-Save e já vem do mais recente para o mais antigo
    prprojs = [pdir / a["path"] for a in organizer.list_open_files(pdir)
               if str(a.get("path", "")).lower().endswith(".prproj")][:PRPROJ_POR_PROJETO]
    for pr in prprojs:
        midias = midias_do_prproj(pr)
        faltam = [m for m in midias if not m["existe"]]
        if not faltam:
            if midias:
                itens.append({"grupo": "midias", "estado": "ok", "titulo": f"{project['num']} · {pr.name}",
                              "detalhe": f"{len(midias)} mídias, todas encontradas"})
            continue
        so_outro = all(m["outro_sistema"] for m in faltam)
        itens.append({"grupo": "midias", "estado": "aviso" if so_outro else "falta",
                      "titulo": f"{project['num']} · {pr.name}",
                      "detalhe": f"{len(faltam)} de {len(midias)} mídias não encontradas"
                                 + (" (caminhos de outro sistema)" if so_outro else ""),
                      "caminhos": [m["caminho"] for m in faltam][:MIDIAS_LISTADAS],
                      "acao": {"tipo": "revelar", "caminho": str(pr)}})
    return itens


# ------------------------------------------------------------------ relatórios
def _resumo(itens: list[dict[str, Any]]) -> dict[str, int]:
    r = {"ok": 0, "aviso": 0, "falta": 0}
    for i in itens:
        r[i["estado"]] += 1
    return r


def checar_cliente(client_id: str) -> dict[str, Any]:
    client = clients_module.get_client(client_id)
    if not client:
        raise ValueError("cliente não encontrado")
    itens: list[dict[str, Any]] = []
    cdir = clients_module.client_dir(client)
    so = "Windows" if machine.current_os() == machine.WINDOWS else "Mac"
    if not cdir or not cdir.is_dir():
        itens.append({"grupo": "pastas", "estado": "falta", "titulo": "Pasta do cliente",
                      "detalhe": f"sem caminho para o {so}" if not cdir else f"não encontrada: {cdir}"})
    else:
        itens.append({"grupo": "pastas", "estado": "ok", "titulo": "Pasta do cliente", "detalhe": str(cdir),
                      "acao": {"tipo": "revelar", "caminho": str(cdir)}})
    perfil = identidades.profile_dir(client_id)
    if cdir and cdir.is_dir():
        itens.append({"grupo": "pastas", "estado": "ok" if perfil and perfil.is_dir() else "aviso", "titulo": "Perfil de IDV (_Estagiário)",
                      "detalhe": "encontrado" if perfil and perfil.is_dir() else "não criado: sem logos, fontes e marca.json no painel"})
    for chave, rotulo in (("template_path_premiere", "Modelo do Premiere"), ("template_path_ae", "Modelo do After Effects")):
        caminho = (client.get(chave) or "").strip()
        if caminho:
            existe = Path(caminho).is_file()
            itens.append({"grupo": "pastas", "estado": "ok" if existe else ("aviso" if _OUTRO_SO.match(caminho) else "falta"),
                          "titulo": rotulo, "detalhe": caminho if existe else f"não encontrado: {caminho}"})
    instaladas = familias_instaladas()
    itens += _fontes_do_cliente(perfil if perfil and perfil.is_dir() else None, instaladas)
    if cdir and cdir.is_dir():
        for p in projects_module.list_projects(client_id):
            if not p.get("folder_available"):
                if not p.get("sem_pasta"):
                    itens.append({"grupo": "pastas", "estado": "aviso", "titulo": f"{p['num']} · {p['name']}",
                                  "detalhe": "a pasta do projeto não foi encontrada (o registro continua)"})
                continue
            if p.get("stage") == "concluido" or p.get("pasta_de_assets"):
                continue
            try:
                itens += _midias_do_projeto(p)
            except Exception as exc:  # um projeto estranho não derruba a checagem dos outros
                logger.warning("checagem de mídias de %s: %s", p.get("id"), exc)
    rel = {"client_id": client_id, "cliente": client["name"], "em": _agora(), "itens": itens, "resumo": _resumo(itens)}
    with _lock:
        _relatorios[client_id] = rel
    return rel


def checar_app() -> dict[str, Any]:
    itens = []
    fonte = (editor.get_config().get("corte") or {}).get("legenda_fonte") or "Arial Black"
    ok = fonte_instalada(fonte)
    itens.append({"grupo": "fontes", "estado": "ok" if ok else "falta", "titulo": f"Legenda do corte vertical: {fonte}",
                  "detalhe": "instalada" if ok else "não instalada: a .ass cai numa fonte qualquer no Premiere/libass"})
    sfx = editor.sfx_folder()
    itens.append({"grupo": "pastas", "estado": "ok" if sfx and sfx.is_dir() else "aviso", "titulo": "Pasta de efeitos (Auto-SFX)",
                  "detalhe": str(sfx) if sfx and sfx.is_dir() else "não configurada ou não encontrada"})
    cfg = clips_module.get_config()
    if cfg.get("ativo") or cfg.get("pasta"):
        pasta = Path(cfg["pasta"]) if cfg.get("pasta") else None
        itens.append({"grupo": "pastas", "estado": "ok" if pasta and pasta.is_dir() else "falta", "titulo": "Pasta de clipes de live",
                      "detalhe": str(pasta) if pasta and pasta.is_dir() else f"não encontrada neste computador: {cfg.get('pasta') or '—'}"})
    return {"em": _agora(), "itens": itens, "resumo": _resumo(itens)}


def relatorio(client_id: str, forcar: bool = False) -> dict[str, Any]:
    with _lock:
        rel = _relatorios.get(client_id)
    return rel if rel and not forcar else checar_cliente(client_id)


def resumo_geral() -> dict[str, Any]:
    with _lock:
        clientes = {cid: {"cliente": r["cliente"], "resumo": r["resumo"], "em": r["em"]} for cid, r in _relatorios.items()}
        estado = dict(_estado)
    return {"clientes": clientes, **estado}


def _varrer() -> None:
    _estado["rodando"] = True
    try:
        familias_instaladas(refresh=True)
        for c in clients_module.list_clients():
            if not clients_module.is_active(c):
                continue
            try:
                checar_cliente(c["id"])
            except Exception as exc:
                logger.warning("checagem de ambiente de %s: %s", c.get("name"), exc)
    finally:
        _estado["rodando"] = False
        _estado["fim"] = _agora()


def start_background_scan() -> None:
    """Pré-scan silencioso ao abrir o app: nada aparece até alguém olhar (o chip do cliente)."""
    threading.Thread(target=_varrer, daemon=True, name="ambiente").start()


def reset() -> None:
    global _familias_cache
    with _lock:
        _relatorios.clear()
    _familias_cache = None
    _estado.update(rodando=False, fim=None)
