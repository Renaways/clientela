"""Prévia dos inserts em HTML, antes do export.

O job de Inserts (com IA, ou a bancada vazia "à mão") para depois de montar as composições
HyperFrames em `<projeto>/.estagiario/inserts vN/hf/` e escrever o `jobs.json`. Aqui o app:

- agrupa as composições em inserts (ATRÁS + FRENTE são um insert só) e serve o HTML para a prévia,
  que toca cada composição em cima do vídeo bruto no navegador, sem render;
- guarda o que foi aprovado e a fonte escolhida em `previa.json`, na bancada;
- troca a fonte escrevendo um `<style id="clientela-fonte">` no HTML de cada composição (o render lê
  o mesmo arquivo, então o export sai com a fonte da prévia);
- acrescenta, edita e tira inserts dos templates universais (`inserts.json` + `kit/universais.py`, sem IA).

O export (render + entrega só dos aprovados) e o ajuste com IA são jobs: `editor.start_inserts_export_job`
e `editor.start_preview_adjust_job`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import ambiente, editor
from . import projects as projects_module
from .persistence import safe_read_json, safe_write_json, safe_write_text

PASTA_RE = re.compile(r"^inserts v(\d+)$")
PREVIA_JSON = "previa.json"
UNIVERSAIS_JSON = "inserts.json"
FONTE_TAG_RE = re.compile(r'\n?\s*<style id="clientela-fonte">.*?</style>', re.S)
FONTE_NOME_RE = re.compile(r"^[\w À-ÿ.'&+-]{1,60}$")
FALLBACK = '"Helvetica Neue", "Inter", Arial, sans-serif'
TELA_RE = re.compile(r"^tela-(\d+)$")
TELA_COMP_RE = re.compile(r"^tela-(\d+)(?:-(?:atras|frente))?$")
CAMADA_RE = re.compile(r"-(atras|frente)$")
QUAL_RE = re.compile(r"\s*\([^()]*\)\s*$")
FONT_EXTS = {".otf", ".ttf", ".woff", ".woff2"}
FONT_FMT = {".otf": "opentype", ".ttf": "truetype", ".woff2": "woff2", ".woff": "woff"}
# Peso e estilo pelo nome do arquivo ("HandoTrial-BlackOblique.otf"): a ordem importa (ExtraBold antes de Bold).
# Abreviações de arquivo ("built titling bd it.otf") só valem como palavra inteira.
ABREV = {"th": 100, "el": 200, "xl": 200, "lt": 300, "rg": 400, "md": 500, "sb": 600, "bd": 700, "xb": 800, "eb": 800, "bk": 900, "hv": 900}
PESOS = (("thin", 100), ("hairline", 100), ("extralight", 200), ("ultralight", 200), ("light", 300),
         ("medium", 500), ("semibold", 600), ("demibold", 600), ("extrabold", 800), ("ultrabold", 800),
         ("black", 900), ("heavy", 900), ("bold", 700))


def _bancada(project_id: str, pasta: str) -> tuple[dict[str, Any], Path]:
    project = projects_module.get_project(project_id)
    if not project:
        raise editor.EditorError("projeto não encontrado", 404)
    if not project.get("folder_available"):
        raise editor.EditorError("pasta do projeto não encontrada no disco", 409)
    if not PASTA_RE.match(pasta or ""):
        raise editor.EditorError("prévia inválida", 404)
    trabalho = Path(project["path"]) / editor.WORK_DIR / pasta
    if not (trabalho / "job.json").is_file():
        raise editor.EditorError("essa bancada não existe mais", 404)
    return project, trabalho


def _ler(path: Path, padrao: Any) -> Any:
    return safe_read_json(path, padrao) if path.exists() else padrao


def _tc(sec: float) -> str:
    """Mesmo nome que o kit/render.py dá (timecode MM-SS-FF a 30 fps): mantenha os dois iguais."""
    f = round(sec * 30)
    return f"{f // 1800:02d}-{(f // 30) % 60:02d}-{f % 30:02d}"


def _saidas(job: dict[str, Any]) -> list[str]:
    """Espelho de kit/render.py saidas(): os nomes dos MOVs que uma entrada do jobs.json vira."""
    if not job.get("partes"):
        return [f"{_tc(job['inicio'])} - {job['nome']}.mov"]
    return [f"{_tc(job['inicio'] + t0)} - {label}.mov" for t0, _, label in job["partes"]]


def _duracao(html_path: Path) -> float:
    try:
        m = re.search(r'data-duration="([\d.]+)"', html_path.read_text(encoding="utf-8"))
    except OSError:
        return 0.0
    return float(m.group(1)) if m else 0.0


def _itens(trabalho: Path) -> list[dict[str, Any]]:
    """As entradas do jobs.json agrupadas por insert: '<base>-atras' e '<base>-frente' são o mesmo."""
    jobs = _ler(trabalho / "jobs.json", [])
    hf = trabalho / "hf"
    universais = {f"u-{s.get('id')}": s for s in (_ler(trabalho / UNIVERSAIS_JSON, {}).get("inserts") or [])}
    grupos: dict[str, dict[str, Any]] = {}
    for j in jobs if isinstance(jobs, list) else []:
        comp = str(j.get("comp") or "")
        if not comp or not (hf / f"{comp}.html").is_file():
            continue
        m = CAMADA_RE.search(comp)
        chave = comp[: m.start()] if m else comp
        nome = " / ".join(QUAL_RE.sub("", p[2]).strip() for p in j["partes"]) if j.get("partes") else str(j.get("nome") or chave)
        g = grupos.setdefault(chave, {
            "chave": chave, "nome": QUAL_RE.sub("", nome).strip(),
            "inicio": float(j.get("inicio") or 0), "dur": 0.0, "camadas": [], "arquivos": [],
            "universal": universais.get(comp),
        })
        g["camadas"].append({"comp": comp, "camada": m.group(1) if m else None})
        g["dur"] = max(g["dur"], _duracao(hf / f"{comp}.html"))
        g["arquivos"] += _saidas(j)
    for g in grupos.values():  # ATRÁS embaixo, FRENTE em cima: a ordem da edição
        g["camadas"].sort(key=lambda c: {"atras": 0, None: 1, "frente": 2}[c["camada"]])
    return sorted(grupos.values(), key=lambda g: (g["inicio"], g["chave"]))


def _exportado(trabalho: Path, entrega: Path, item: dict[str, Any]) -> bool:
    """Todos os MOVs do insert estão na entrega e são mais novos que o HTML (fonte trocada = exportar de novo)."""
    try:
        html = max((trabalho / "hf" / f"{c['comp']}.html").stat().st_mtime for c in item["camadas"])
        return all((entrega / n).stat().st_mtime >= html for n in item["arquivos"])
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------ fontes
def _peso_estilo(nome: str) -> tuple[int, str]:
    palavras = re.split(r"[\s_\-.]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", nome).lower())
    n = "".join(palavras)
    estilo = "italic" if ("italic" in n or "oblique" in n or "it" in palavras) else "normal"
    abrev = next((ABREV[w] for w in palavras if w in ABREV), None)
    return abrev or next((p for k, p in PESOS if k in n), 400), estilo


def fontes_disponiveis(trabalho: Path) -> list[dict[str, Any]]:
    """As famílias do cliente: as do marca.json (já declaradas em todo HTML) e as dos arquivos em hf/assets/fonts."""
    marca = _ler(trabalho / "marca.json", {})
    declaradas = (marca.get("tipografia") or {}).get("fontes") or []
    out: dict[str, dict[str, Any]] = {}
    usados = set()
    for f in declaradas:
        if f.get("familia"):
            out.setdefault(f["familia"], {"familia": f["familia"], "origem": "marca", "arquivos": []})
            usados.add(f.get("arquivo"))
    pasta = trabalho / "hf" / "assets" / "fonts"
    for arq in sorted(pasta.iterdir()) if pasta.is_dir() else []:
        if arq.suffix.lower() not in FONT_EXTS or arq.name in usados:
            continue
        for fam in ambiente.familias_da_fonte(arq) or {arq.stem.split("-")[0]}:
            if fam not in out:
                out[fam] = {"familia": fam, "origem": "arquivo", "arquivos": []}
            if out[fam]["origem"] == "arquivo":
                out[fam]["arquivos"].append(arq.name)
    return sorted(out.values(), key=lambda f: (f["origem"] != "marca", f["familia"].lower()))


def _css_fonte(familia: str, disponiveis: list[dict[str, Any]]) -> str:
    """Troca a família de todo texto da composição. Família de arquivo ganha @font-face (peso pelo nome do
    arquivo); a do marca.json já está declarada; qualquer outra é a instalada no sistema (local)."""
    faces = []
    f = next((d for d in disponiveis if d["familia"] == familia), None)
    for arq in (f or {}).get("arquivos") or []:
        peso, estilo = _peso_estilo(Path(arq).stem)
        fmt = FONT_FMT.get(Path(arq).suffix.lower(), "opentype")
        faces.append(f'@font-face {{ font-family: {json.dumps(familia)}; font-weight: {peso}; font-style: {estilo}; '
                     f'src: url("assets/fonts/{arq}") format("{fmt}"); }}')
    regra = f"#root, #root * {{ font-family: {json.dumps(familia)}, {FALLBACK} !important; }}"
    return "\n".join(["    <style id=\"clientela-fonte\">", *("      " + x for x in faces + [regra]), "    </style>"])


def _aplicar_fontes(trabalho: Path, previa: dict[str, Any], itens: list[dict[str, Any]]) -> None:
    disponiveis = fontes_disponiveis(trabalho)
    geral = previa.get("fonte")
    for item in itens:
        fonte = ((previa.get("itens") or {}).get(item["chave"]) or {}).get("fonte") or geral
        for c in item["camadas"]:
            path = trabalho / "hf" / f"{c['comp']}.html"
            html = path.read_text(encoding="utf-8")
            novo = FONTE_TAG_RE.sub("", html)
            if fonte:
                novo = novo.replace("</head>", _css_fonte(fonte, disponiveis) + "\n  </head>", 1)
            if novo != html:  # sem escrever à toa: a data do HTML diz se o export ficou velho
                safe_write_text(path, novo)


def _validar_fonte(nome: Any) -> str | None:
    nome = str(nome or "").strip()
    if not nome:
        return None
    if not FONTE_NOME_RE.match(nome):
        raise editor.EditorError("nome de fonte inválido")
    return nome


# ------------------------------------------------------------------ leitura
def listar(project_id: str) -> list[dict[str, Any]]:
    """As bancadas de inserts do projeto que têm o que mostrar na prévia (mais nova primeiro)."""
    project = projects_module.get_project(project_id)
    if not project or not project.get("folder_available"):
        return []
    base = Path(project["path"]) / editor.WORK_DIR
    out = []
    for d in sorted(base.glob("inserts v*"), key=lambda p: int(PASTA_RE.match(p.name).group(1)) if PASTA_RE.match(p.name) else 0,
                    reverse=True):
        if not PASTA_RE.match(d.name) or not (d / "job.json").is_file():
            continue
        if not (d / "jobs.json").is_file() and not (d / UNIVERSAIS_JSON).is_file():
            continue
        try:
            e = estado(project_id, d.name, leve=True)
        except editor.EditorError:
            continue
        out.append({k: e[k] for k in ("pasta", "entrega", "total", "aprovados", "exportados", "pendentes")})
    return out


def estado(project_id: str, pasta: str, leve: bool = False) -> dict[str, Any]:
    project, trabalho = _bancada(project_id, pasta)
    job = _ler(trabalho / "job.json", {})
    previa = _ler(trabalho / PREVIA_JSON, {})
    entrega = Path(job.get("entrega") or "")
    itens = _itens(trabalho)
    escolhas = previa.get("itens") or {}
    telas = _ler(trabalho / editor.TELAS_JSON, {})
    pedidas = telas.get("telas") or []
    for it in itens:
        e = escolhas.get(it["chave"]) or {}
        it["aprovado"] = bool(e.get("aprovado"))
        it["fonte"] = e.get("fonte")
        it["exportado"] = _exportado(trabalho, entrega, it)
        m = TELA_RE.match(it["chave"])  # o card "Tela n" vira a composição tela-<n>
        n = int(m.group(1)) if m else 0
        it["tela"] = {"numero": n, **pedidas[n - 1]} if 0 < n <= len(pedidas) else None
        it["animada"] = it["chave"] in (telas.get("animar") or [])
    out = {
        "pasta": pasta,
        "estatico": bool(telas.get("estatico")),
        "telas": pedidas,
        "entrega": entrega.name,
        "total": len(itens),
        "aprovados": sum(1 for i in itens if i["aprovado"]),
        "exportados": sum(1 for i in itens if i["exportado"]),
        "pendentes": sum(1 for i in itens if i["aprovado"] and not i["exportado"]),
    }
    # telas já escritas em hf/, mesmo antes do jobs.json: o card do pedido mostra cada uma assim que fica pronta
    prontas: dict[str, list[str]] = {}
    for f in sorted((trabalho / "hf").glob("tela-*.html"), key=lambda f: (f.stem.endswith("-frente"), f.stem)):
        m = TELA_COMP_RE.match(f.stem)
        if m:
            prontas.setdefault(m.group(1), []).append(f.stem)
    out["telas_prontas"] = prontas
    if leve:
        return out
    video = job.get("video") or {}
    catalogo = _ler(editor.KIT_DIR / "universais.json", {})
    return {
        **out,
        "project_id": project_id,
        "video": video.get("caminho"),
        "largura": video.get("largura"), "altura": video.get("altura"), "duracao": video.get("duracao"),
        "fonte": previa.get("fonte"),
        "fontes": fontes_disponiveis(trabalho),
        "recorte": (trabalho / "recorte.mov").is_file(),
        "tem_template": (trabalho / "marca.json").is_file(),
        "catalogo": catalogo,
        "itens": itens,
    }


def arquivo(project_id: str, pasta: str, rel: str) -> Path:
    """Um arquivo de hf/ (composição ou asset) para o iframe da prévia; nada fora dele."""
    _, trabalho = _bancada(project_id, pasta)
    hf = (trabalho / "hf").resolve()
    alvo = (hf / rel).resolve()
    if hf not in alvo.parents or not alvo.is_file() or "_check" in alvo.relative_to(hf).parts:
        raise editor.EditorError("arquivo não encontrado", 404)
    return alvo


def html_previa(path: Path) -> str:
    """A composição como o navegador da prévia precisa: o HyperFrames cria window.__timelines antes de carregar
    a página e o HTML só acrescenta a timeline nele; sem o runtime, a prévia cria o objeto."""
    html = path.read_text(encoding="utf-8")
    return html.replace("<head>", "<head>\n    <script>window.__timelines = window.__timelines || {};</script>", 1)


# ------------------------------------------------------------------ escrita
def salvar(project_id: str, pasta: str, dados: dict[str, Any]) -> dict[str, Any]:
    """Aprovação e fonte: {"fonte": "…"|null, "itens": {chave: {"aprovado": bool, "fonte": "…"|null}}}.
    Só as chaves enviadas mudam."""
    _, trabalho = _bancada(project_id, pasta)
    with editor._lock:
        previa = _ler(trabalho / PREVIA_JSON, {})
        itens = _itens(trabalho)
        chaves = {i["chave"] for i in itens}
        if "fonte" in dados:
            previa["fonte"] = _validar_fonte(dados.get("fonte"))
        escolhas = previa.setdefault("itens", {})
        for chave, e in (dados.get("itens") or {}).items():
            if chave not in chaves or not isinstance(e, dict):
                continue
            atual = escolhas.setdefault(chave, {})
            if "aprovado" in e:
                atual["aprovado"] = bool(e["aprovado"])
            if "fonte" in e:
                atual["fonte"] = _validar_fonte(e["fonte"])
        safe_write_json(trabalho / PREVIA_JSON, previa)
        _aplicar_fontes(trabalho, previa, itens)
    return estado(project_id, pasta)


def _rodar_universais(trabalho: Path) -> None:
    """kit/universais.py da bancada. Os dois arquivos vêm sempre do app: bancadas antigas não os têm."""
    kit = trabalho / "kit"
    kit.mkdir(exist_ok=True)
    for nome in ("universais.py", "universais.json", "common.py"):
        if nome != "common.py" or not (kit / nome).exists():  # o common da bancada também serve os inserts da IA
            shutil.copy2(editor.KIT_DIR / nome, kit / nome)
    r = subprocess.run([sys.executable or "python3", str(kit / "universais.py")], cwd=trabalho, capture_output=True,
                       text=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        erro = (r.stderr or r.stdout).strip().splitlines()
        raise editor.EditorError(f"não consegui montar o insert: {erro[-1] if erro else 'erro no kit'}", 422)


def _proximo_id(trabalho: Path, specs: list[dict[str, Any]]) -> str:
    usados = {int(m.group(1)) for i in _itens(trabalho) if (m := re.match(r"(\d+)", i["nome"]))}
    usados |= {int(m.group(1)) for s in specs if (m := re.match(r"(\d+)", str(s.get("id"))))}
    return str(max(usados, default=0) + 1)


def salvar_universal(project_id: str, pasta: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Acrescenta (sem `id`) ou substitui (com o `id` de um que existe) um insert de template universal."""
    _, trabalho = _bancada(project_id, pasta)
    if not (trabalho / "marca.json").is_file():
        raise editor.EditorError("este cliente não tem o template (marca.json): gere na aba Identidade", 409)
    catalogo = _ler(editor.KIT_DIR / "universais.json", {}).get("templates") or {}
    tpl = catalogo.get(spec.get("template"))
    if not tpl:
        raise editor.EditorError("escolha um template")
    textos = {c["chave"]: str((spec.get("textos") or {}).get(c["chave"]) or "").strip()[:300] for c in tpl["campos"]}
    if not any(textos.values()):
        raise editor.EditorError("escreva o texto do insert")
    try:
        inicio = max(0.0, float(spec.get("inicio") or 0))
        dur = float(spec.get("dur") or tpl.get("dur") or 4)
    except (TypeError, ValueError):
        raise editor.EditorError("tempo inválido")
    with editor._lock:
        caminho = trabalho / UNIVERSAIS_JSON
        antes = _ler(caminho, {"inserts": []})
        specs = [dict(s) for s in antes.get("inserts") or []]
        sid = str(spec.get("id") or "").strip()
        novo = {"id": sid or _proximo_id(trabalho, specs), "template": spec["template"],
                "estilo": spec.get("estilo") if spec.get("estilo") in ("padrao", "clean") else "padrao",
                "posicao": spec.get("posicao") if spec.get("posicao") in ("topo", "centro", "base") else None,
                "inicio": round(inicio, 2), "dur": round(min(30.0, max(1.0, dur)), 2), "textos": textos}
        specs = [s for s in specs if str(s.get("id")) != sid] + [novo] if sid else specs + [novo]
        safe_write_json(caminho, {"inserts": sorted(specs, key=lambda s: s["inicio"])})
        try:
            _rodar_universais(trabalho)
        except (editor.EditorError, subprocess.TimeoutExpired):
            safe_write_json(caminho, antes)  # o que estava volta: um insert com erro não derruba os outros
            raise
        previa = _ler(trabalho / PREVIA_JSON, {})
        previa.setdefault("itens", {}).setdefault(f"u-{novo['id']}", {})
        safe_write_json(trabalho / PREVIA_JSON, previa)
        _aplicar_fontes(trabalho, previa, _itens(trabalho))
    return estado(project_id, pasta)


def remover_universal(project_id: str, pasta: str, sid: str) -> dict[str, Any]:
    _, trabalho = _bancada(project_id, pasta)
    with editor._lock:
        caminho = trabalho / UNIVERSAIS_JSON
        specs = [s for s in (_ler(caminho, {}).get("inserts") or []) if str(s.get("id")) != str(sid)]
        safe_write_json(caminho, {"inserts": specs})
        _rodar_universais(trabalho)
        previa = _ler(trabalho / PREVIA_JSON, {})
        (previa.get("itens") or {}).pop(f"u-{sid}", None)
        safe_write_json(trabalho / PREVIA_JSON, previa)
    return estado(project_id, pasta)


def para_exportar(project_id: str, pasta: str) -> tuple[dict[str, Any], Path, list[dict[str, Any]]]:
    """Os inserts aprovados que ainda não estão (ou não estão atualizados) na entrega."""
    project, trabalho = _bancada(project_id, pasta)
    e = estado(project_id, pasta)
    return project, trabalho, [i for i in e["itens"] if i["aprovado"] and not i["exportado"]]
