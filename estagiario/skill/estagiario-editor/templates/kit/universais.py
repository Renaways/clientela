#!/usr/bin/env python3
"""Templates universais de insert: a mesma peça serve a qualquer cliente porque tudo de marca vem do marca.json.

Lê {trabalho}/inserts.json e escreve hf/u-<id>.html (via common.write) e as entradas "u-…" do jobs.json.
As outras entradas do jobs.json (inserts feitos à mão no inserts.py) ficam como estão.

inserts.json: {"inserts": [
  {"id": "3", "template": "numero", "estilo": "padrao" | "clean", "posicao": "topo" | "centro" | "base",
   "inicio": 52.7, "dur": 4.0, "titulo": "145 Milhoes",
   "textos": {"numero": "145", "rotulo": "milhões", "apoio": "de brasileiros"},
   "tempos": {"rotulo": 0.8}}          ← opcional: t local (s) de cada campo; sem ele, o tempo da fala
]}
Catálogo (campos, rótulos, duração padrão): universais.json. O app monta a tela de "Adicionar insert" a partir dele.

Ajuste por cliente, opcional, no marca.json: "inserts": {"padrao": {"escala": 1, "posicao": "centro"},
                                                       "clean": {"escala": 0.72, "posicao": "base"}}

uso: python3 kit/universais.py
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

KIT = Path(__file__).resolve().parent
TRAB = KIT.parent
CATALOGO = json.loads((KIT / "universais.json").read_text(encoding="utf-8"))
PREFIXO = "u-"
ESTILO_PADRAO = {"padrao": {"escala": 1.0, "posicao": "centro"}, "clean": {"escala": 0.72, "posicao": "base"}}


def norm(s):
    s = "".join(c for c in unicodedata.normalize("NFD", str(s).lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", s)


def quando(texto, palavras, inicio, dur, desde=0.0):
    """t local em que a primeira palavra de `texto` é dita dentro do insert (a partir de `desde`), ou None."""
    alvo = next((norm(w) for w in str(texto).split() if norm(w)), "")
    if not alvo:
        return None
    for p in palavras:
        t = p["t"] - inicio
        if desde - 0.05 <= t < dur and norm(p["texto"]) == alvo:
            return round(max(0.0, t - 0.05), 2)
    return None


def tempos(spec, palavras):
    """Um t local por campo (e por item da lista), na ordem: o pedido no spec, senão o da fala, senão escalonado."""
    campos = [c["chave"] for c in CATALOGO["templates"][spec["template"]]["campos"]]
    textos = spec.get("textos") or {}
    pedidos = spec.get("tempos") or {}
    out, ultimo = {}, 0.0
    for c in campos:
        partes = [x for x in str(textos.get(c) or "").split("\n") if x.strip()] if c == "itens" else [textos.get(c)]
        for i, txt in enumerate(partes):
            if not txt or not str(txt).strip():
                continue
            chave = f"{c}{i}" if c == "itens" else c
            t = pedidos.get(chave)
            if t is None:
                t = quando(txt, palavras, spec["inicio"], spec["dur"], ultimo)
            if t is None:
                t = 0.15 if not out else ultimo + 0.45
            t = max(ultimo if out else 0.0, min(float(t), spec["dur"] - 0.3))
            out[chave] = round(t, 2)
            ultimo = out[chave]
    return out


def nome_arquivo(spec):
    """'<id> <título>' sem acento e sem caracteres que o Finder/Windows recusam (o entregar confere o padrão)."""
    valores = [str(v).split("\n")[0] for v in (spec.get("textos") or {}).values() if str(v or "").strip()]
    titulo = spec.get("titulo") or " ".join(valores[:2]) or "Insert"
    titulo = "".join(c for c in unicodedata.normalize("NFD", titulo) if unicodedata.category(c) != "Mn")
    titulo = re.sub(r'[\\/:*?"<>|()\n]+', " ", titulo)
    return re.sub(r"\s+", " ", f"{spec['id']} {titulo}").strip()[:60]


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def validar(spec):
    if spec.get("template") not in CATALOGO["templates"]:
        raise ValueError(f"template desconhecido: {spec.get('template')}")
    if not re.fullmatch(r"[0-9]{1,3}[a-z]?", str(spec.get("id", ""))):
        raise ValueError(f"id inválido: {spec.get('id')!r} (número do insert, ex.: 3 ou 3b)")
    spec["estilo"] = spec.get("estilo") if spec.get("estilo") in CATALOGO["estilos"] else "padrao"
    spec["inicio"] = max(0.0, float(spec.get("inicio") or 0))
    spec["dur"] = min(30.0, max(1.0, float(spec.get("dur") or CATALOGO["templates"][spec["template"]]["dur"])))
    return spec


# ------------------------------------------------------------------ montagem (precisa do common: job.json + marca.json)
def montar(spec, k):
    est = spec["estilo"]
    ajuste = {**ESTILO_PADRAO[est], **(((k.MARCA.get("inserts") or {}).get(est)) or {})}
    pos = spec.get("posicao") if spec.get("posicao") in CATALOGO["posicoes"] else ajuste["posicao"]
    esc_k = float(ajuste.get("escala") or 1)
    clean = est == "clean"
    T = tempos(spec, k.palavras(spec["inicio"], spec["inicio"] + spec["dur"]))
    tx = {c: str(v).strip() for c, v in (spec.get("textos") or {}).items() if str(v or "").strip()}
    acento = k.cor("acento") if "acento" in k.CORES else "var(--texto)"

    def tam(n, texto="", larg=0.0):
        """Corpo em px; com `larg` (largura média de um caractere em em), encolhe até a linha caber entre as margens.
        Nenhum texto abaixo de 56 px (leitura no celular)."""
        n *= esc_k
        maior = max((len(x) for x in str(texto).split("\n")), default=0)
        if larg and maior:
            n = min(n, (1080 - 2 * 72 - 40) / (larg * maior))
        return k.px(max(56, n))

    def linha(cid, texto, papel, size, cor=None):
        estilo = f"font-size: {tam(size, texto, 0.66 if papel in ('destaque', 'numero') else 0)};" + (f" color: {cor};" if cor else "")
        return f'<div class="mask p-{papel} u-l" id="{cid}" style="{estilo}"><span class="split">{esc(texto)}</span></div>'

    def chave(cid, texto, size):
        """A palavra/frase principal: no realce do perfil (padrão) ou só na cor de acento (clean)."""
        if clean:
            return linha(cid, texto, "destaque", size, acento)
        return k.realce(esc(texto), cid).replace('class="mask p-destaque', f'style="font-size: {tam(size, texto, 0.72)};" class="mask p-destaque', 1)

    corpo, js = [], []

    def entra(cid, t, principal=False):
        if clean:
            js.append(f'suave("#{cid}", {t});')
        elif principal:
            js.append(f'realceIn("{cid}", {t});')
        else:
            js.append(f'letras("#{cid} .c", {t}, {{ rot: false, st: 0.02 }});')

    tpl = spec["template"]
    if tpl == "frase":
        if "apoio" in tx:
            corpo.append(linha("a", tx["apoio"], "apoio", 88)); entra("a", T["apoio"])
        corpo.append(chave("k", tx.get("destaque", ""), 164)); entra("k", T.get("destaque", 0.15), True)
    elif tpl == "numero":
        num = tx.get("numero", "")
        digitos = re.fullmatch(r"\d{1,3}(\.\d{3})*|\d+", num)
        inicial = "0" if digitos and not clean else esc(num)
        corpo.append(f'<div class="mask p-numero u-l" id="n" style="font-size: {tam(300, num, 0.66)}; color: {acento};"><span id="nv">{inicial}</span></div>')
        tn = T.get("numero", 0.15)
        if clean:
            js.append(f'suave("#n", {tn});')
        else:
            js.append(f'tl.fromTo("#nv", {{ yPercent: 130, rotation: -7 }}, {{ yPercent: 0, rotation: 0, duration: 0.6, ease: E.chegada }}, {tn});')
            js.append('blur("y", %s, 0.5, 10);' % tn)
            if digitos:
                valor = int(num.replace(".", ""))
                sep = "." if "." in num else ""
                js.append(f'conta("nv", {tn}, {valor}, {min(1.6, spec["dur"] / 2):.2f}, "{sep}");')
        if "rotulo" in tx:
            corpo.append(chave("k", tx["rotulo"], 150)); entra("k", T["rotulo"], True)
        if "apoio" in tx:
            corpo.append(linha("a", tx["apoio"], "apoio", 80)); entra("a", T["apoio"])
    elif tpl == "palavra":
        corpo.append(linha("k", tx.get("palavra", ""), "destaque", 236, acento))
        t = T.get("palavra", 0.15)
        js.append(f'suave("#k", {t});' if clean else f'letras("#k .c", {t}); blur("y", {t}, 0.55, 12);')
    elif tpl == "lista":
        if "titulo" in tx:
            corpo.append(chave("k", tx["titulo"], 120)); entra("k", T["titulo"], True)
        itens = [x.strip() for x in tx.get("itens", "").split("\n") if x.strip()][:5]
        for i, item in enumerate(itens):
            corpo.append(linha(f"i{i}", item, "apoio", 92))
            entra(f"i{i}", T.get(f"itens{i}", 0.15 + i * 0.5))
            if i:  # o anterior esmaece quando o próximo entra
                js.append(f'tl.to("#i{i - 1}", {{ opacity: 0.35, duration: 0.3, ease: "power2.out" }}, {T.get(f"itens{i}", 0.15 + i * 0.5)});')
    elif tpl == "troca":
        corpo.append(f'<div class="u-pilha">{linha("a", tx.get("antes", ""), "destaque", 150)}{chave("k", tx.get("depois", ""), 164)}</div>')
        ta, tb = T.get("antes", 0.15), T.get("depois", spec["dur"] / 2)
        if clean:
            js.append(f'suave("#a", {ta}); tl.to("#a", {{ opacity: 0, y: -20, duration: 0.3, ease: "power2.in" }}, {tb - 0.2}); suave("#k", {tb});')
        else:
            js.append(f'letras("#a .c", {ta}); sai("#a .c", {tb - 0.15}); realceIn("k", {tb});')
    elif tpl == "citacao":
        corpo.append(linha("a", "“" + tx.get("frase", "").replace("\n", " ") + "”", "narrativa", 92, acento if clean else None))
        entra("a", T.get("frase", 0.15))
        if "autor" in tx:
            corpo.append(linha("r", "— " + tx["autor"], "rotulo", 56)); entra("r", T["autor"] if "autor" in T else 1.0)

    if not clean:
        js.append(f'deriva("#u", 0.15, {max(0.5, spec["dur"] - 0.15):.2f}, 1.04);')
    alinha = "flex-start" if clean else "center"
    ancora = {"topo": f"top: {k.px(k.SAFE['topo'])};",
              "centro": "top: 50%; transform: translateY(-50%);",
              "base": f"bottom: {k.px(k.SAFE['base'])};"}[pos]
    css = f"""
      #u-pos {{ position: absolute; left: {k.px(72)}; right: {k.px(72)}; {ancora} }}
      #u {{ display: flex; flex-direction: column; align-items: {alinha}; text-align: {'left' if clean else 'center'}; gap: {k.px(10)}; transform-origin: 50% 50%; }}
      .u-l {{ max-width: 100%; }} .u-l > span {{ white-space: normal !important; }}
      .u-pilha {{ display: grid; justify-items: {alinha}; }} .u-pilha > * {{ grid-area: 1 / 1; }}
      {'' if clean else '#n, #k.u-l { filter: url(#mby); }'}
    """
    js_base = """
      // clean: entrada suave, sem rotação nem blur
      const suave = (sel, t) => tl.fromTo(sel, { opacity: 0, y: 28 }, { opacity: 1, y: 0, duration: 0.5, ease: "power2.out" }, t);
      // contagem real e determinística (seek-safe): o texto vem de um proxy, nunca de setInterval
      const conta = (id, t, val, dur, sep) => {
        const el = document.getElementById(id), p = { v: 0 };
        const fmt = (v) => sep ? String(Math.round(v)).replace(/\\B(?=(\\d{3})+(?!\\d))/g, sep) : String(Math.round(v));
        tl.fromTo(p, { v: 0 }, { v: val, duration: dur, ease: "power2.out", immediateRender: false, onUpdate: () => { el.textContent = fmt(p.v); } }, t);
      };
    """
    body = f'<div id="u-pos"><div id="u" class="sombra">{"".join(corpo)}</div></div>'
    comp = PREFIXO + k.slug(spec["id"])
    k.write(comp, nome_arquivo(spec), spec["dur"], css=css, body=body, js=js_base + "\n      " + "\n      ".join(js))
    return {"comp": comp, "inicio": round(spec["inicio"], 3), "nome": nome_arquivo(spec), "universal": True}


def main():
    sys.path.insert(0, str(KIT))
    import common as k  # noqa: E402  (lê job.json e marca.json da bancada)

    arq = TRAB / "inserts.json"
    specs = [validar(dict(s)) for s in (json.loads(arq.read_text(encoding="utf-8")).get("inserts") or [])] if arq.exists() else []
    ids = [s["id"] for s in specs]
    if len(ids) != len(set(ids)):
        sys.exit(f"ids repetidos no inserts.json: {sorted({i for i in ids if ids.count(i) > 1})}")
    novos = [montar(s, k) for s in specs]
    feitos = {j["comp"] for j in novos}
    for velho in (TRAB / "hf").glob(f"{PREFIXO}*.html"):  # insert universal removido: a composição sai junto
        if velho.stem not in feitos:
            velho.unlink()
    jobs_path = TRAB / "jobs.json"
    antigos = json.loads(jobs_path.read_text(encoding="utf-8")) if jobs_path.exists() else []
    k.write_jobs(sorted([j for j in antigos if not j["comp"].startswith(PREFIXO)] + novos, key=lambda j: j["inicio"]))


if __name__ == "__main__":
    main()
