#!/usr/bin/env python3
"""Legendas dinâmicas palavra a palavra, no estilo de 3 camadas (apoio / DESTAQUE / complemento) do perfil.

Duas etapas (o app roda as duas sozinho, sem IA, no modo automático):
  python3 kit/legendas.py blocos   → {trabalho}/legendas.json a partir de palavras.json (quebra automática + destaque)
  (opcional, com IA ou à mão: junte/separe blocos, troque o "destaque" (índice da palavra ou null), corrija o texto)
  python3 kit/legendas.py html [--animacao perfil|karaoke|pop|maquina|suave|caixa] [--fonte "Família"]
                                   → hf/legendas.html (vídeo inteiro) + entrada "legendas" no jobs.json (30 fps, sem subquadros)

Estilo: papéis legenda_apoio / legenda_destaque / legenda_complemento; faixa vertical e limites em componentes.legenda.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import COMP, CORES, DURACAO, FALLBACK, FONTES, FORMATO, H, HF, PALAVRAS, REALCE, SAFE, TRAB, W, cor, papel, px, write  # noqa: E402

L = {"faixa_y": [1050, 1500], "max_palavras": 4, "destaque_por_bloco": 1, **(COMP.get("legenda") or {})}
STOP = set("""a o e é as os um uma uns umas de do da dos das no na nos nas em por para pra com sem que se mas ou
me te lhe nos vos eu tu ele ela eles elas isso isto aquilo esse essa este esta ao aos à às já não sim mais muito
foi ser são está estão tem têm ter vai vão como quando onde também então aqui ali lá""".split())
JSON_PATH = TRAB / "legendas.json"


# palavra que não pode fechar bloco: artigo, preposição, conjunção e pronome átono puxam a próxima palavra
GRUDA = set("""a o as os um uma uns umas de do da dos das no na nos nas em por pelo pela pelos pelas para pra com sem
que se e ou mas nem ao aos à às num numa como seu sua seus suas meu minha este esta esse essa não""".split())


# verbos auxiliares e palavras de ligação longas: nunca são o destaque
FRACAS = set("""estamos estou estava estavam estão está esteve tínhamos tinha tinham temos tenho teve tiveram havia houve
haver hoje sendo sido seria seriam foram fosse eram pode podem poderia deve devem precisa precisam fazer feito fez fazem
acho sobre entre depois antes ainda apenas também porque porém quanto quando enquanto todos todas tudo nada algum
alguma outra outro outras outros mesmo mesma cada qual quais aqui isso""".split())


def _limpo(t):
    return re.sub(r"[^\w-]", "", t.lower())


def _quebras(ws):
    """Blocos de até max_palavras: quebra em pontuação, pausa > 0,4 s ou bloco > 2,5 s; nunca termina em
    artigo/preposição ("de | dados" vira "de dados"); bloco de uma palavra só gruda no anterior se couber."""
    maxp = int(L["max_palavras"])
    out, cur = [], []
    for i, w in enumerate(ws):
        if cur:
            pausa = w["t"] - ws[i - 1]["fim"] > 0.4
            cheio = len(cur) >= maxp or w["t"] - cur[0]["t"] > 2.5
            if pausa or (cheio and _limpo(cur[-1]["texto"]) not in GRUDA) or len(cur) >= maxp + 2:
                out.append(cur)
                cur = []
        cur.append(w)
        if re.search(r"[.?!…:;]$", w["texto"]) or (re.search(r",$", w["texto"]) and len(cur) >= 2):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    juntos = []
    for b in out:
        if juntos and len(b) == 1 and len(juntos[-1]) < maxp and b[0]["t"] - juntos[-1][-1]["fim"] < 0.3 \
                and not re.search(r"[.?!…]$", juntos[-1][-1]["texto"]):
            juntos[-1] = juntos[-1] + b
        else:
            juntos.append(b)
    return juntos


def _nomes_proprios(ws):
    """Palavras que aparecem com maiúscula no meio da frase em algum lugar da fala (Lula, Band, Serra)."""
    nomes = set()
    for i, w in enumerate(ws):
        if i and w["texto"][:1].isupper() and not re.search(r"[.?!…]$", ws[i - 1]["texto"]):
            nomes.add(_limpo(w["texto"]))
    return nomes


NOMES = _nomes_proprios(PALAVRAS)


def _nota(w):
    t = _limpo(w["texto"])
    if not t or t in STOP or t in GRUDA or t in FRACAS:
        return 0, False
    if re.search(r"\d", t):
        return 30, True
    if t in NOMES:
        return 20 + len(t) / 10, True
    n = min(len(t), 12)
    if t.endswith("mente"):  # advérbio longo ("necessariamente") não é o sentido da frase
        n -= 7
    elif re.search(r"(ando|endo|indo)$", t):  # gerúndio ("assistindo") é ação em curso, não o sentido da frase
        n -= 6
    return n, False


def _destaque(b, anterior):
    """A palavra que carrega o sentido: número > nome próprio > substantivo/adjetivo longo. Nunca stopword.
    Depois de um bloco com destaque, o seguinte só acende se tiver número ou nome (evita destaque em todo bloco)."""
    if not b:
        return None
    notas = [_nota(w) for w in b]
    j = max(range(len(b)), key=lambda k: notas[k][0])
    if notas[j][0] < 7:
        return None
    if anterior is not None and not notas[j][1]:
        return None
    return j


def blocos():
    res, ant = [], None
    for b in _quebras(PALAVRAS):
        d = _destaque(b, ant)
        res.append({"palavras": [{"texto": w["texto"], "t": w["t"]} for w in b], "fim": b[-1]["fim"], "destaque": d})
        ant = d
    JSON_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"ok legendas.json: {len(res)} blocos de {len(PALAVRAS)} palavras, {sum(r['destaque'] is not None for r in res)} com destaque.")


def limpa(t):
    return re.sub(r"[.,;:]+$", "", t)


def _tam(p, chars, avail):
    """Corpo do papel (px de 1080×1920), reduzido se a linha estimada não couber na largura útil."""
    base = float(p.get("px", 48))
    k = 0.62 if int(p.get("peso", 700)) >= 800 else 0.55
    return min(base, avail / max(1, k * chars))


# Animações universais: valem para qualquer cliente (cores, papéis e realce vêm do marca.json).
# O app espelha as chaves em editor.ANIMACOES_LEGENDA — mude os dois juntos.
ANIMACOES = {
    "perfil": "Do perfil: três camadas (apoio / DESTAQUE / complemento), palavra a palavra",
    "karaoke": "Karaokê: o bloco inteiro aparece e a palavra falada acende na cor de destaque",
    "pop": "Pop: uma palavra por vez, grande, no centro",
    "maquina": "Máquina de escrever: as letras aparecem no ritmo da fala",
    "suave": "Suave: o bloco entra com fade e leve subida; destaque só na cor",
    "caixa": "Caixa: o bloco numa tarja de fundo sólido nas cores do realce",
}


def _args(argv):
    o = {"animacao": "perfil", "fonte": ""}
    for k, v in zip(argv[::2], argv[1::2]):
        if k in ("--animacao", "--fonte"):
            o[k[2:]] = v.strip()
    if o["animacao"] not in ANIMACOES:
        raise SystemExit(f"animação desconhecida: {o['animacao']} (use {', '.join(ANIMACOES)})")
    return o


def _literal(ref, padrao):
    """Cor do perfil como valor literal (o GSAP não anima var(--x)); degradê não é cor de texto → padrão."""
    v = CORES.get(ref, ref) if ref else None
    return v if v and not str(ref).startswith("g:") and "gradient" not in str(v) else padrao


def _acento(pd):
    """A cor do destaque: a do papel legenda_destaque, senão a do realce, senão o acento do perfil."""
    for ref in (pd.get("cor"), REALCE.get("fundo"), "acento" if "acento" in CORES else None):
        if ref and ref != "texto":
            return _literal(ref, "#FFD60A")
    return "#FFD60A"


def _css_fonte(fam):
    """Troca a família de todo texto. Arquivo em hf/assets/fonts com esse nome (sem extensão) e fora do marca.json
    ganha @font-face cobrindo todos os pesos (não sintetiza negrito); senão vale a declarada ou a instalada."""
    if not fam:
        return ""
    if re.search(r'[\\"<>{};]', fam):
        raise SystemExit(f"nome de fonte inválido: {fam}")
    out = []
    if fam not in {f.get("familia") for f in FONTES}:
        fmt = {".otf": "opentype", ".ttf": "truetype", ".woff2": "woff2", ".woff": "woff"}
        for arq in sorted((HF / "assets" / "fonts").glob("*")):
            if arq.stem == fam and arq.suffix.lower() in fmt:
                out.append(f'      @font-face {{ font-family: {json.dumps(fam)}; font-weight: 100 900; '
                           f'src: url("assets/fonts/{arq.name}") format("{fmt[arq.suffix.lower()]}"); }}')
    out.append(f"      #root, #root * {{ font-family: {json.dumps(fam)}, {FALLBACK} !important; }}")
    return "\n".join(out)


def _sai(bl, i):
    b = bl[i]
    prox = bl[i + 1]["palavras"][0]["t"] if i + 1 < len(bl) else DURACAO
    return max(min(prox - 0.04, b["fim"] + 0.6), b["palavras"][-1]["t"] + 0.2)


def _perfil(bl, i, pa, pd, pc, avail):
    """O estilo original: apoio / DESTAQUE / complemento em linhas, destaque com blur e escala."""
    ws, d = bl[i]["palavras"], bl[i].get("destaque")
    partes = [(ws[:d], "legenda-apoio", pa), ([ws[d]], "legenda-destaque", pd), (ws[d + 1:], "legenda-complemento", pc)] \
        if d is not None else [(ws, "legenda-apoio", pa)]
    linhas = []
    for grupo, cls, p in partes:
        if not grupo:
            continue
        chars = sum(len(limpa(w["texto"])) + 1 for w in grupo)
        spans = " ".join(f'<span class="lw" id="b{i}w{ws.index(w)}">{limpa(w["texto"])}</span>' for w in grupo)
        linhas.append(f'<span class="ln p-{cls}" style="font-size:{px(_tam(p, chars, avail))}">{spans}</span>')
    js = []
    for j, w in enumerate(ws):
        if j == d:
            js.append(f'tl.fromTo("#b{i}w{j}", {{ opacity: 0, scale: 1.15, filter: "blur(8px)" }}, '
                      f'{{ opacity: 1, scale: 1, filter: "blur(0px)", duration: 0.3, ease: E.chegada }}, {w["t"]:.3f});')
        else:
            js.append(f'tl.fromTo("#b{i}w{j}", {{ opacity: 0, y: {px(20)[:-2]} }}, {{ opacity: 1, y: 0, duration: 0.15, ease: E.chegada }}, {w["t"]:.3f});')
    js.append(f'tl.to("#b{i}", {{ opacity: 0, duration: 0.1, ease: E.saida }}, {_sai(bl, i):.3f});')
    return f'<div id="b{i}" class="abs bloco sombra-leg">{"".join(linhas)}</div>', js


def _corrido(bl, i, pa, avail, anim, letras=False):
    """Bloco numa frase só (quebra em até duas linhas) no papel de apoio; o destaque ganha a classe .dest."""
    ws, d = bl[i]["palavras"], bl[i].get("destaque")
    chars = sum(len(limpa(w["texto"])) + 1 for w in ws)
    esc = 0.85 if anim == "suave" else 1
    # o papel de apoio foi pensado para a linha pequena sobre o DESTAQUE; sozinho, o bloco precisa de mais corpo
    corpo = dict(pa, px=float(pa.get("px", 48)) * 1.4)
    tam = _tam(corpo, chars / (2 if chars > 22 else 1), avail * (0.86 if anim == "caixa" else 1)) * esc

    def conteudo(t):
        return "".join(f'<span class="c">{ch}</span>' for ch in t) if letras else t
    # no karaokê o que acende é a palavra falada; um destaque fixo no meio parecia uma palavra aleatória acesa
    d = None if anim == "karaoke" else d
    spans = " ".join(f'<span class="lw{" dest" if j == d else ""}" id="b{i}w{j}">{conteudo(limpa(w["texto"]))}</span>'
                     for j, w in enumerate(ws))
    caixa = " caixa" if anim == "caixa" else ""
    return (f'<div id="b{i}" class="abs bloco sombra-leg"><span class="ln corrido p-legenda-apoio{caixa}" '
            f'style="font-size:{px(tam)}">{spans}</span></div>')


def html():
    o = _args(sys.argv[2:])
    anim = o["animacao"]
    bl = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    y0, y1 = L["faixa_y"]
    avail = 1080 - 2 * max(SAFE.get("esquerda", 64), 64) - (SAFE.get("direita", 140) - 64 if FORMATO == "retrato" else 0)
    pa, pd, pc = papel("legenda_apoio"), papel("legenda_destaque"), papel("legenda_complemento")
    acc = _acento(pd)
    base = _literal(pa.get("cor"), "#ffffff")
    css = f"""
      .bloco {{ left: 0; width: {W}px; top: {px(y0)}; height: {px(y1 - y0)}; display: flex; flex-direction: column;
               align-items: center; justify-content: center; text-align: center; gap: {px(6)}; }}
      .ln {{ display: block; white-space: nowrap; }}
      .ln.corrido {{ white-space: normal; max-width: {px(avail)}; line-height: 1.12; }}
      .lw {{ display: inline-block; opacity: 0; }}
      .dest {{ color: {acc}; }}
      .caixa {{ background: {cor(REALCE.get('fundo'), '#000')}; color: {cor(REALCE.get('texto'), '#fff')};
               padding: 0.14em 0.42em 0.1em; border-radius: {px(float(REALCE.get('raio') or 0) or 14)}; }}
      .caixa .dest {{ color: inherit; font-weight: 900; }}
      .sombra-leg {{ filter: drop-shadow({COMP.get('sombra_texto') or '0 4px 18px rgba(0,0,0,.45)'}); }}
{_css_fonte(o["fonte"])}
    """
    if anim in ("karaoke", "suave", "caixa"):
        css += "      .lw { opacity: 1; }\n"
    if anim == "maquina":
        css += "      .lw { opacity: 1; } .lw .c { opacity: 0; }\n"
    if anim == "karaoke":
        css += "      .lw { opacity: 0.55; }\n"
    body, js = [], []
    for i, b in enumerate(bl):
        ws, d, t0, sai = b["palavras"], b.get("destaque"), b["palavras"][0]["t"], _sai(bl, i)
        if anim == "perfil":
            h, j = _perfil(bl, i, pa, pd, pc, avail)
            body.append(h)
            js += j
            continue
        if anim == "pop":
            # cada palavra é um bloco: entra com escala e sai quando a próxima chega
            for j, w in enumerate(ws):
                prox = ws[j + 1]["t"] if j + 1 < len(ws) else sai
                p = pd if j == d else pa
                t = limpa(w["texto"])
                tam = _tam(p if j == d else dict(pa, px=float(pa.get("px", 48)) * 1.6), len(t) + 1, avail)
                body.append(f'<div id="b{i}w{j}" class="abs bloco sombra-leg" style="opacity:0"><span class="ln '
                            f'p-legenda-{"destaque" if j == d else "apoio"}" style="font-size:{px(tam)}">{t}</span></div>')
                js.append(f'tl.fromTo("#b{i}w{j}", {{ opacity: 0, scale: 1.3 }}, {{ opacity: 1, scale: 1, duration: 0.16, ease: "back.out(2)" }}, {w["t"]:.3f});')
                js.append(f'tl.set("#b{i}w{j}", {{ opacity: 0 }}, {max(prox - 0.02, w["t"] + 0.12):.3f});')
            continue
        body.append(_corrido(bl, i, pa, avail, anim, letras=anim == "maquina"))
        if anim in ("suave", "caixa", "karaoke"):
            de = {"suave": "opacity: 0, y: 16", "caixa": "opacity: 0, scale: 0.92", "karaoke": "opacity: 0"}[anim]
            para = {"suave": "opacity: 1, y: 0, duration: 0.35, ease: \"power2.out\"",
                    "caixa": "opacity: 1, scale: 1, duration: 0.22, ease: \"back.out(1.6)\"",
                    "karaoke": "opacity: 1, duration: 0.12"}[anim]
            js.append(f'tl.fromTo("#b{i}", {{ {de} }}, {{ {para} }}, {max(t0 - 0.12, 0):.3f});')
        if anim == "karaoke":
            for j, w in enumerate(ws):
                prox = ws[j + 1]["t"] if j + 1 < len(ws) else sai
                js.append(f'tl.to("#b{i}w{j}", {{ opacity: 1, color: "{acc}", scale: 1.08, duration: 0.08 }}, {w["t"]:.3f});')
                js.append(f'tl.to("#b{i}w{j}", {{ color: "{base}", scale: 1, duration: 0.12 }}, {max(prox - 0.02, w["t"] + 0.1):.3f});')
        if anim == "maquina":
            for j, w in enumerate(ws):
                prox = ws[j + 1]["t"] if j + 1 < len(ws) else w["t"] + 0.4
                n = max(1, len(limpa(w["texto"])))
                st = min(0.05, max(0.01, (prox - w["t"]) / n))
                js.append(f'tl.set("#b{i}w{j} .c", {{ opacity: 1, stagger: {st:.3f} }}, {w["t"]:.3f});')
        js.append(f'tl.to("#b{i}", {{ opacity: 0, duration: {0.2 if anim == "suave" else 0.1}, ease: E.saida }}, {sai:.3f});')
    write("legendas", "Legendas", round(DURACAO, 3), css=css, body="\n".join(body), js="\n".join("      " + x for x in js))
    jp = TRAB / "jobs.json"
    jobs = [j for j in (json.loads(jp.read_text(encoding="utf-8")) if jp.exists() else []) if j["comp"] != "legendas"]
    jobs.append({"comp": "legendas", "inicio": 0.0, "nome": "Legendas (video inteiro)", "fps": 30})
    jp.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"ok jobs.json + legendas (animação {anim}{', fonte ' + o['fonte'] if o['fonte'] else ''})")


if __name__ == "__main__":
    {"blocos": blocos, "html": html}[sys.argv[1] if len(sys.argv) > 1 else "blocos"]()
