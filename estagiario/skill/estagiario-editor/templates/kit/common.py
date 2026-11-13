#!/usr/bin/env python3
"""Base dos geradores do estagiário: lê job.json + marca.json e monta o HTML de cada composição HyperFrames.

Nada de marca aqui: cores, fontes, papéis tipográficos, realce, chips e eases vêm do marca.json
(traduzido do frame.md do perfil, esquema em references/perfil.md). Derivado do kit aprovado do 049 v3.

Uso num gerador (kit/inserts.py):
    from common import *            # W, H, px(), write(), two_layers(), realce(), chip(), marcas() …
    two_layers("ins-1-gancho", "1 Gancho", 4.4, css=..., body=..., js=...)
"""
import json
import re
from pathlib import Path

KIT = Path(__file__).resolve().parent
TRAB = KIT.parent
HF = TRAB / "hf"

JOB = json.loads((TRAB / "job.json").read_text(encoding="utf-8"))
MARCA = json.loads((TRAB / "marca.json").read_text(encoding="utf-8"))

W = int(JOB["video"]["largura"])
H = int(JOB["video"]["altura"])
FPS = int(JOB["video"].get("fps", 30))
DURACAO = float(JOB["video"]["duracao"])


def _formato():
    if H > W:
        return "retrato"
    return "quadrado" if W == H else "paisagem"


FORMATO = _formato()
_ESC_FORMATO = {"paisagem": 0.75, "quadrado": 0.85, **(MARCA.get("formatos") or {})}
# 1080×1920 é a referência de todas as medidas (perfil, skill, receitas)
S = W / 1080 if FORMATO == "retrato" else (min(W, H) / 1080) * float(_ESC_FORMATO[FORMATO])


def px(n):
    """Medida de 1080×1920 → px no formato do vídeo (string CSS)."""
    return f"{n * S:.1f}px"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


CORES = MARCA.get("cores") or {}
DEGRADES = MARCA.get("degrades") or {}
COMP = MARCA.get("componentes") or {}
PAPEIS = (MARCA.get("tipografia") or {}).get("papeis") or {}
FONTES = (MARCA.get("tipografia") or {}).get("fontes") or []
EASES = {"chegada": "expo.out", "saida": "power3.in", "travessia": "power4.inOut",
         "elastico": "back.out(1.5)", "respiro": "sine.inOut", **(MARCA.get("eases") or {})}
SAFE = {"topo": 250, "base": 420, "esquerda": 64, "direita": 140, **(MARCA.get("safe") or {})}
FALLBACK = '"Helvetica Neue", "Inter", Arial, sans-serif'


def cor(ref, padrao="transparent"):
    """Referência de cor do perfil → CSS. 'acento' → var(--acento); 'g:acento' → var(--g-acento); literal passa."""
    if ref is None or ref == "":
        return padrao
    ref = str(ref)
    if ref.startswith("g:"):
        return f"var(--g-{slug(ref[2:])})"
    if ref in CORES:
        return f"var(--{slug(ref)})"
    return ref


def _papel_css(nome, p):
    fam = p.get("familia")
    decl = [f"font-family: {json.dumps(fam) + ', ' if fam else ''}{FALLBACK}",
            f"font-weight: {p.get('peso', 700)}",
            f"font-style: {p.get('estilo', 'normal')}"]
    if p.get("caixa") == "alta":
        decl.append("text-transform: uppercase")
    if p.get("entrelinha") is not None:
        decl.append(f"line-height: {p['entrelinha']}")
    if p.get("tracking"):
        decl.append(f"letter-spacing: {p['tracking']}")
    if p.get("cor"):
        decl.append(f"color: {cor(p['cor'])}")
    if p.get("px"):
        decl.append(f"font-size: {px(float(p['px']))}")
    return f"      .p-{slug(nome)} {{ {'; '.join(decl)}; }}"


_PAPEIS_PADRAO = {
    "destaque": {"peso": 800, "estilo": "normal", "caixa": "alta", "entrelinha": 0.92, "tracking": "-0.012em"},
    "apoio": {"peso": 600, "estilo": "normal", "entrelinha": 1.05},
    "narrativa": {"peso": 400, "estilo": "italic", "entrelinha": 1.1},
    "numero": {"peso": 800, "estilo": "normal", "entrelinha": 0.85, "tracking": "-0.02em"},
    "rotulo": {"peso": 700, "estilo": "normal", "caixa": "alta", "tracking": "0.12em"},
    "gc_nome": {"peso": 700, "estilo": "normal", "caixa": "alta", "px": 40},
    "gc_cargo": {"peso": 500, "estilo": "normal", "px": 29},
    "legenda_apoio": {"peso": 700, "estilo": "normal", "px": 48},
    "legenda_destaque": {"peso": 800, "estilo": "normal", "caixa": "alta", "px": 84},
    "legenda_complemento": {"peso": 600, "estilo": "italic", "px": 48},
}


def papel(nome):
    """Papel tipográfico do perfil com os padrões neutros por baixo (só o que faltar)."""
    base = dict(_PAPEIS_PADRAO.get(nome, {}))
    base.update(PAPEIS.get(nome) or {})
    if "familia" not in base and PAPEIS.get("destaque", {}).get("familia"):
        base["familia"] = PAPEIS["destaque"]["familia"]
    if "cor" not in base and "texto" in CORES:  # sem cor, o texto herdaria preto e sumiria no fundo escuro
        base["cor"] = "texto"
    return base


def _fontes_css():
    fmt = {".otf": "opentype", ".ttf": "truetype", ".woff2": "woff2", ".woff": "woff"}
    out = []
    for f in FONTES:
        arq = f["arquivo"]
        ext = Path(arq).suffix.lower()
        out.append(f'      @font-face {{ font-family: {json.dumps(f["familia"])}; font-weight: {f.get("peso", 400)}; '
                   f'font-style: {f.get("estilo", "normal")}; src: url("assets/fonts/{arq}") format("{fmt.get(ext, "opentype")}"); }}')
    return "\n".join(out)


def _vars_css():
    v = [f"--{slug(k)}: {val};" for k, val in CORES.items()]
    v += [f"--g-{slug(k)}: {val};" for k, val in DEGRADES.items()]
    return "      :root { " + " ".join(v) + " }"


REALCE = {"tipo": "pilula", "fundo": "texto", "texto": "fundo", "raio": 0, **(COMP.get("realce") or {})}
CHIP = {"fundo": REALCE.get("fundo"), "texto": REALCE.get("texto"), "raio": REALCE.get("raio", 0), **(COMP.get("chip") or {})}
SOMBRA_TEXTO = COMP.get("sombra_texto") or "0 6px 22px rgba(0,0,0,.55)"


def _componentes_css():
    r = REALCE
    out = []
    if r["tipo"] == "pilula":
        out.append(f"      .realce {{ display: inline-block; background: {cor(r['fundo'])}; color: {cor(r['texto'])}; "
                   f"padding: 0.06em 0.22em 0 0.16em; border-radius: {px(float(r.get('raio') or 0))}; "
                   f"box-shadow: {r.get('sombra') or 'none'}; }}")
        out.append("      .realce .p-destaque, .realce .c { color: inherit; }")
    elif r["tipo"] == "sublinhado":
        out.append("      .realce { display: inline-block; position: relative; }")
        out.append(f"      .realce-sub {{ height: 0.09em; margin-top: 0.06em; background: {cor(r['fundo'])}; "
                   f"border-radius: {px(float(r.get('raio') or 0))}; transform-origin: 0% 50%; }}")
    else:  # cor: só a palavra muda de cor
        out.append(f"      .realce {{ display: inline-block; }} .realce .p-destaque, .realce .c {{ color: {cor(r['fundo'])}; }}")
    c = CHIP
    dest = papel("destaque")
    out.append(f"      .chip {{ height: {px(112)}; padding: 0 {px(36)}; display: flex; align-items: center; white-space: nowrap; "
               f"background: {cor(c['fundo'])}; color: {cor(c['texto'])}; border-radius: {px(float(c.get('raio') or 0))}; "
               f"font-family: {json.dumps(dest.get('familia')) + ', ' if dest.get('familia') else ''}{FALLBACK}; "
               f"font-weight: {dest.get('peso', 800)}; font-style: {dest.get('estilo', 'normal')}; font-size: {px(64)}; line-height: 1; "
               f"{'text-transform: uppercase; ' if dest.get('caixa') == 'alta' else ''}box-shadow: 0 18px 40px rgba(0,0,0,.35); }}")
    out.append(f"      .sombra {{ filter: drop-shadow({SOMBRA_TEXTO}); }}")
    if COMP.get("texto_degrade"):
        out.append(f"      .tdeg, .tdeg .c {{ display: inline-block; padding: 0.3em 0.16em 0.06em 0.06em; margin: -0.3em -0.16em -0.06em -0.06em; "
                   f"-webkit-background-clip: text; background-clip: text; color: transparent; background-image: {cor(COMP['texto_degrade'])}; }}")
    for k in CORES:
        out.append(f"      .cor-{slug(k)} {{ color: var(--{slug(k)}); }} .bg-{slug(k)} {{ background: var(--{slug(k)}); }}")
    return "\n".join(out)


def _brand_css():
    nomes = sorted(set(_PAPEIS_PADRAO) | set(PAPEIS))
    return "\n".join([_fontes_css(), _vars_css(), *(_papel_css(n, papel(n)) for n in nomes), _componentes_css()])


HEAD = """<!doctype html>
<html lang="pt-BR">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=%%W%%, height=%%H%%" />
    <title>%%TITLE%%</title>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
%%BRAND%%
      * { margin: 0; padding: 0; box-sizing: border-box; }
      html, body { width: %%W%%px; height: %%H%%px; overflow: hidden; background: transparent; }
      #root { position: relative; width: 100%; height: 100%; overflow: hidden; font-family: %%FAMILIA%%; }
      .abs { position: absolute; }
      .full { position: absolute; left: 0; top: 0; width: %%W%%px; height: %%H%%px; }
      .c { display: inline-block; white-space: pre; }
      .w { display: inline-block; white-space: nowrap; }
      /* máscara com folga para acentos (em cima) e para o itálico que pende à direita */
      .mask { display: block; overflow: hidden; padding: 0.32em 0.3em 0.18em 0.1em; margin: -0.32em -0.3em -0.18em -0.1em; }
      .mask > span { display: inline-block; white-space: nowrap; }
      .linha { display: block; }
%%CSS%%
    </style>
  </head>
  <body>
    <svg width="0" height="0" style="position:absolute">
      <defs>
        <filter id="mby" x="-20%" y="-60%" width="140%" height="220%"><feGaussianBlur id="mby-b" stdDeviation="0 0" /></filter>
        <filter id="mbx" x="-60%" y="-20%" width="220%" height="140%"><feGaussianBlur id="mbx-b" stdDeviation="0 0" /></filter>
      </defs>
    </svg>
    <div id="root" data-composition-id="%%CID%%" data-start="0" data-duration="%%DUR%%" data-width="%%W%%" data-height="%%H%%">
%%BODY%%
    </div>
    <script>
      const E = %%EASES%%;
      const ROT = [-7, 5, -4, 8, -6, 4, -8, 5, -5, 6, -4, 7, -6];
      document.querySelectorAll(".split").forEach((el) => {
        const t = el.textContent; el.textContent = "";
        t.split(" ").forEach((word, i) => {
          if (i) el.appendChild(document.createTextNode(" "));
          const w = document.createElement("span"); w.className = "w";
          for (const ch of word) { const s = document.createElement("span"); s.className = "c"; s.textContent = ch; w.appendChild(s); }
          el.appendChild(w);
        });
      });
      const tl = gsap.timeline({ paused: true });
      // motion blur direcional real: o filtro SVG fica aplicado (filter:url(#mby)) no contêiner que se move
      const blur = (axis, t, dur, peak) => {
        const fe = document.getElementById(axis === "x" ? "mbx-b" : "mby-b");
        const p = { v: peak };
        tl.fromTo(p, { v: peak }, { v: 0, duration: dur, ease: "expo.out", immediateRender: false,
          onUpdate: () => fe.setAttribute("stdDeviation", axis === "x" ? `${p.v} 0` : `0 ${p.v}`) }, t);
      };
      const ignite = (sel, t, o = {}) => tl.fromTo(sel,
        { opacity: 0, filter: "blur(16px)", y: o.y ?? 22, scale: o.s ?? 1 },
        { opacity: 1, filter: "blur(0px)", y: 0, scale: 1, duration: o.d ?? 0.5, ease: E.chegada, stagger: o.st ?? 0.028 }, t);
      const punch = (sel, t, o = {}) => tl.fromTo(sel,
        { opacity: 0, scale: o.s ?? 1.35, filter: "blur(12px)", y: o.y ?? 0 },
        { opacity: 1, scale: 1, filter: "blur(0px)", y: 0, duration: o.d ?? 0.42, ease: E.chegada, stagger: o.st ?? 0 }, t);
      const rise = (sel, t, o = {}) => tl.fromTo(sel, { yPercent: 150 }, { yPercent: 0, duration: o.d ?? 0.6, ease: E.chegada, stagger: o.st ?? 0.06 }, t);
      // letras cinéticas: sobem da máscara com leve rotação e assentam
      const letras = (sel, t, o = {}) => tl.fromTo(sel,
        { yPercent: 150, rotation: (i) => (o.rot === false ? 0 : ROT[i % ROT.length]) },
        { yPercent: 0, rotation: 0, duration: o.d ?? 0.6, ease: E.chegada, stagger: o.st ?? 0.03 }, t);
      // troca de frase: as letras antigas saem para cima
      const sai = (sel, t, o = {}) => tl.to(sel, { yPercent: -160, duration: o.d ?? 0.26, ease: E.saida, stagger: o.st ?? 0.01 }, t);
      // hold vivo: deriva lenta de escala
      const deriva = (sel, t, dur, s = 1.04) => tl.fromTo(sel, { scale: 1 }, { scale: s, duration: dur, ease: "none" }, t);
%%REALCE_JS%%
      const chipIn = (sel, t, lado = 1) => tl.fromTo(sel, { scale: 0, rotation: -14 * lado, opacity: 0 }, { scale: 1, rotation: -3 * lado, opacity: 1, duration: 0.42, ease: E.elastico }, t);
%%JS%%
      window.__timelines["%%CID%%"] = tl;
      tl.seek(0);
    </script>
  </body>
</html>
"""


def _realce_js():
    """realceIn/realceOut só do tipo do perfil: ramos mortos fazem o lint acusar fromTo repetido."""
    tipo = REALCE["tipo"]
    ein, eout = [], []
    if tipo == "pilula":
        ein.append('tl.fromTo(el, { clipPath: "inset(-60% 100% -10% -10%)" }, { clipPath: "inset(-60% -10% -10% -10%)", '
                   'duration: o.d ?? 0.45, ease: E.chegada }, t - 0.04);')
        eout.append('tl.to(el, { clipPath: "inset(0% 0% 0% 100%)", duration: 0.24, ease: E.saida }, t + 0.05);')
    ein.append('letras(el.querySelectorAll(".c"), t, { st: o.st ?? 0.04 });')
    if tipo == "sublinhado":
        ein.append('tl.fromTo(`#${id}-sub`, { scaleX: 0 }, { scaleX: 1, duration: 0.5, ease: "power2.inOut" }, t + 0.25);')
        eout.append('tl.to(`#${id}-sub`, { scaleX: 0, transformOrigin: "100% 50%", duration: 0.22, ease: E.saida }, t);')
    eout.insert(0, 'sai(el.querySelectorAll(".c"), t);')
    j = lambda xs: "\n".join("        " + x for x in xs)
    return (f"      // realce do perfil ({tipo}), gerado por realce() no Python\n"
            f"      const realceIn = (id, t, o = {{}}) => {{\n        const el = document.getElementById(id);\n{j(ein)}\n      }};\n"
            f"      const realceOut = (id, t) => {{\n        const el = document.getElementById(id);\n{j(eout)}\n      }};")


def realce(texto, rid, classe=""):
    """HTML da palavra-chave no realce do perfil. Anime com realceIn("<rid>", t) / realceOut("<rid>", t)."""
    inner = f'<div class="mask p-destaque {classe}"><span class="split">{texto}</span></div>'
    sub = f'<div class="realce-sub" id="{rid}-sub"></div>' if REALCE["tipo"] == "sublinhado" else ""
    return f'<div class="realce" id="{rid}">{inner}{sub}</div>'


def chip(texto, cid, css=""):
    """Chip de nome/tag no estilo do perfil. Anime com chipIn("#<cid>", t, ±1)."""
    return f'<div id="{cid}" class="abs chip" style="{css}">{texto}</div>'


def _familia_root():
    fam = papel("destaque").get("familia")
    return (json.dumps(fam) + ", " if fam else "") + FALLBACK


def _check_dir(name, html):
    """lint e snapshot só leem index.html: _check/<comp>/index.html + assets por symlink."""
    d = HF / "_check" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(html, encoding="utf-8")
    link = d / "assets"
    if not link.exists() and not link.is_symlink():
        link.symlink_to(Path("../../assets"))
    for f in ("hyperframes.json", "package.json", "meta.json"):
        src = HF / f
        if src.exists() and not (d / f).exists():
            (d / f).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def write(name, title, dur, css="", body="", js=""):
    """Escreve hf/<name>.html (composição de render) e hf/_check/<name>/index.html (lint, snapshot, review)."""
    cid = slug(name)
    html = HEAD
    for k, v in {"%%BRAND%%": _brand_css(), "%%CSS%%": css.rstrip(), "%%BODY%%": body.rstrip(), "%%JS%%": js.rstrip(),
                 "%%EASES%%": json.dumps(EASES), "%%REALCE_JS%%": _realce_js(),
                 "%%FAMILIA%%": _familia_root(), "%%TITLE%%": title, "%%CID%%": cid, "%%DUR%%": str(dur),
                 "%%W%%": str(W), "%%H%%": str(H)}.items():
        html = html.replace(k, v)
    (HF / f"{name}.html").write_text(html, encoding="utf-8")
    _check_dir(name, html)
    print("ok", f"{name}.html")
    return name


def two_layers(base, title, dur, css="", body="", js=""):
    """Gera <base>-atras e <base>-frente a partir do mesmo HTML: cada um esconde a outra camada (#atras / #frente)."""
    for layer, hide in (("atras", "#frente"), ("frente", "#atras")):
        write(f"{base}-{layer}", f"{title} {layer}", dur, css=css + f"\n      {hide} {{ display: none; }}", body=body, js=js)


PALAVRAS = json.loads((TRAB / "palavras.json").read_text(encoding="utf-8"))["palavras"] if (TRAB / "palavras.json").exists() else []


def palavras(t0, t1):
    """Palavras ditas entre t0 e t1 (tempo do clipe)."""
    return [p for p in PALAVRAS if t0 <= p["t"] < t1]


def marcas(t0, t1):
    """Linha de comentário com cada palavra no tempo LOCAL do insert (t − t0): 'debate .19 da .55 …'."""
    return " ".join(f'{p["texto"]} {p["t"] - t0:.2f}' for p in palavras(t0, t1))


def write_jobs(jobs):
    """Escreve {trabalho}/jobs.json (lista lida pelo render.py). Mantenha junto do gerador, para nome e tempo não divergirem."""
    (TRAB / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
    print("ok jobs.json", len(jobs), "composições")
