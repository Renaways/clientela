#!/usr/bin/env python3
"""Valida {trabalho}/marca.json contra o esquema de references/perfil.md §3. Sai com 1 se houver erro.

uso: python3 kit/check_marca.py                                   (numa tarefa: {trabalho}/marca.json + hf/assets)
     python3 check_marca.py --marca <arquivo> --assets <perfil>/assets (no onboarding)
"""
import json
import re
import sys
from pathlib import Path

TRAB = Path(__file__).resolve().parent.parent
_a = sys.argv[1:]
MARCA_JSON = Path(_a[_a.index("--marca") + 1]) if "--marca" in _a else TRAB / "marca.json"
ASSETS = Path(_a[_a.index("--assets") + 1]) if "--assets" in _a else TRAB / "hf" / "assets"
HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
LITERAL = re.compile(r"^(#|rgba?\(|hsla?\(|transparent$|white$|black$|linear-gradient|radial-gradient)")

erros, avisos = [], []
try:
    m = json.loads(MARCA_JSON.read_text(encoding="utf-8"))
except (OSError, ValueError) as e:
    print(f"ERRO marca.json ilegível: {e}")
    sys.exit(1)

cores = m.get("cores") or {}
degr = m.get("degrades") or {}
for k in ("fundo", "texto"):
    if k not in cores:
        erros.append(f"cores.{k} é obrigatório")
for k, v in cores.items():
    if not HEX.match(str(v)) and not LITERAL.match(str(v)):
        erros.append(f"cores.{k} = {v!r} não é cor CSS")
for k, v in degr.items():
    if "gradient(" not in str(v):
        erros.append(f"degrades.{k} = {v!r} não é gradiente CSS")


def ref(campo, v):
    if v is None or v == "":
        return
    v = str(v)
    if v.startswith("g:"):
        if v[2:] not in degr:
            erros.append(f"{campo} = {v!r}: degradê '{v[2:]}' não existe em degrades")
    elif v not in cores and not LITERAL.match(v):
        erros.append(f"{campo} = {v!r}: não é nome em cores, g:<degradê> nem cor literal")


tip = m.get("tipografia") or {}
fontes = tip.get("fontes") or []
familias = {f.get("familia") for f in fontes}
for i, f in enumerate(fontes):
    for k in ("familia", "arquivo"):
        if not f.get(k):
            erros.append(f"tipografia.fontes[{i}].{k} é obrigatório")
    if f.get("estilo", "normal") not in ("normal", "italic"):
        erros.append(f"tipografia.fontes[{i}].estilo deve ser normal ou italic")
    if f.get("arquivo") and not (ASSETS / "fonts" / f["arquivo"]).exists():
        erros.append(f"fonte {f['arquivo']} não está em assets/fonts/")
papeis = tip.get("papeis") or {}
if "destaque" not in papeis:
    erros.append("tipografia.papeis.destaque é obrigatório")
for n, p in papeis.items():
    ref(f"papeis.{n}.cor", p.get("cor"))
    if p.get("familia") and fontes and p["familia"] not in familias:
        avisos.append(f"papeis.{n}.familia {p['familia']!r} não tem @font-face em fontes (vai cair no fallback)")
    if p.get("caixa") not in (None, "alta", "normal"):
        erros.append(f"papeis.{n}.caixa deve ser alta ou normal")
    want = (p.get("peso", 400), p.get("estilo", "normal"))
    if fontes and p.get("familia") in familias and want not in {(f.get("peso", 400), f.get("estilo", "normal"))
                                                                for f in fontes if f.get("familia") == p.get("familia")}:
        avisos.append(f"papeis.{n}: {p['familia']} peso {want[0]} {want[1]} não tem arquivo (o navegador vai sintetizar)")

comp = m.get("componentes") or {}
r = comp.get("realce")
if not r:
    erros.append("componentes.realce é obrigatório")
else:
    if r.get("tipo") not in ("pilula", "sublinhado", "cor"):
        erros.append("componentes.realce.tipo deve ser pilula, sublinhado ou cor")
    ref("realce.fundo", r.get("fundo"))
    ref("realce.texto", r.get("texto"))
for k in ("fundo", "texto"):
    ref(f"chip.{k}", (comp.get("chip") or {}).get(k))
ref("texto_degrade", comp.get("texto_degrade"))
gc = comp.get("gc") or {}
for k in ("nome_fundo", "cargo_fundo"):
    ref(f"gc.{k}", gc.get(k))
if gc.get("logo") and not (ASSETS / "logos" / gc["logo"]).exists():
    erros.append(f"gc.logo {gc['logo']} não está em assets/logos/")
for grupo, pasta in (("logos", "logos"), ("texturas", "texturas")):
    for k, arq in (m.get(grupo) or {}).items():
        if arq and not (ASSETS / pasta / arq).exists():
            erros.append(f"{grupo}.{k} = {arq} não está em assets/{pasta}/")

for a in avisos:
    print("aviso", a)
for e in erros:
    print("ERRO", e)
print(f"{len(erros)} erro(s), {len(avisos)} aviso(s)")
sys.exit(1 if erros else 0)
