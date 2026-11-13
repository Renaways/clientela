"""Sequência multipista do estúdio: o arquivo ``.clientela`` e o render dele.

Uma sequência é um projeto de edição como o do Premiere: formato (resolução e
fps), as mídias que entraram (takes, música) e os clipes de cada trilha — vídeo
empilhado de V1 (embaixo) para cima, áudio somado. Tudo o que a edição sabe
mora no próprio arquivo ``<nome>.clientela`` (JSON) na pasta do projeto; as
mídias de dentro do projeto ficam com caminho relativo a ele, para o mesmo
arquivo abrir no Mac e no Windows pelo Drive.

Como o ``autocorte.py``, este módulo só usa a biblioteca padrão: o app importa
as funções puras (normalizar, grafo) e roda o arquivo como script no job de
export, falando o stream-json do Claude Code — o painel acompanha e cancela
sem saber a diferença.

uso: python3 clientela/sequencia.py --args <arquivo.json>
  args: {"doc": <sequência>, "midias": {id: caminho absoluto}, "entrega": pasta, "nome": arquivo.mp4}
"""

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:   # importado pelo app (pacote) ou rodado como script do job (a pasta clientela/ no sys.path)
    from . import autocorte
except ImportError:  # pragma: no cover - caminho do script
    import autocorte

FORMATO = "clientela.sequencia"
VERSAO = 1
EXTENSAO = ".clientela"

# preset → (largura, altura, rótulo). "vertical" é o dos cortes para Reels/TikTok/Shorts.
PRESETS = {
    "completo": (1920, 1080, "Projeto completo · 16:9"),
    "vertical": (1080, 1920, "Reels / TikTok / Shorts · 9:16"),
    "quadrado": (1080, 1080, "Quadrado · 1:1"),
    "feed": (1080, 1350, "Feed · 4:5"),
    "4k": (3840, 2160, "4K · 16:9"),
}
FPS_VALIDOS = (23.976, 24, 25, 29.97, 30, 50, 59.94, 60)
TIPOS = ("video", "audio", "grafico")   # gráfico = template de texto/forma desenhado pelo estúdio
AJUSTES = ("conter", "preencher")   # conter = cabe inteiro (sobra faixa), preencher = cobre o quadro (corta a sobra)
MAX_TRILHAS = 8
MIN_CLIPE = 1 / 60
MAX_GRAFICO = 60_000   # bytes do template guardado em cada clipe de gráfico
MAX_PALAVRAS = 30_000  # por mídia (uma live de ~3 h)
LEGENDA_PADRAO = {"ativa": False, "modo": "animada", "queimar": False, "fonte": "Arial Black", "tamanho": 0,
                  "cor_texto": "#FFFFFF", "cor_destaque": "#FFD60A", "margem": -1, "maiusculas": True,
                  "palavras": 4, "estilo": ""}   # tamanho 0 / margem -1 = proporcional ao quadro (legenda_auto)
LEGENDA_MODOS = ("animada", "fixa")
MAX_MARCADORES = 500
TEMP_K = 0.15   # temperatura ±1 = ±15 % no vermelho/azul (= SEQ_TEMP_K do shader)
MARCADOR_CORES = ("verde", "vermelho", "amarelo", "azul", "roxo", "laranja")
ENQ_MODOS = ("inteiro", "dividido", "desfocado")   # = autocorte.LAYOUT_MODOS, no tamanho da sequência
# keyframes: propriedade → [{t (no tempo do clipe), v, ease}]; com keyframes, o valor fixo da propriedade não vale
KF_VISUAL = ("escala", "pos_x", "pos_y", "opacidade", "rotacao")
KF_AUDIO = ("ganho",)
KF_EASES = ("linear", "suave", "segurar")
MAX_KF = 200
TRANSICOES_V = ("dissolver", "preto", "branco", "deslizar")   # na entrada do clipe, centrada no corte
TRANSICOES_A = ("cruzado",)
TRANSICAO_MAX = 5.0


class Falha(Exception):
    pass


# ------------------------------------------------------------------ documento
def nova(nome, preset="completo", largura=None, altura=None, fps=30):
    """Sequência vazia. `largura`/`altura` sobrepõem o preset (formato personalizado)."""
    pw, ph, _ = PRESETS.get(preset, PRESETS["completo"])
    doc = {
        "formato": FORMATO, "versao": VERSAO, "nome": str(nome or "").strip() or "Sequência",
        "config": {"largura": int(largura or pw), "altura": int(altura or ph), "fps": fps,
                   "preset": preset if preset in PRESETS and not (largura or altura) else "personalizado"},
        "midias": [], "trilhas": {"video": 2, "audio": 2}, "clipes": [],
    }
    return normalizar(doc)


def _num(v, nome):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{nome} inválido: {v!r}")
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError(f"{nome} inválido: {v!r}")
    return x


def _par(n):
    # ,5 arredonda para cima, como o Math.round do inspetor (round() do Python iria para o par)
    return max(16, min(7680, int(n / 2 + 0.5) * 2))


def _visual(c, out):
    """Escala, posição, opacidade e cor de um clipe de vídeo, imagem ou gráfico (o monitor WebGL e o
    ffmpeg fazem a mesma conta: escala 1 = cabe/cobre o quadro; posição = centro, 0–1 do quadro)."""
    out["escala"] = round(max(0.05, min(4.0, _num(c.get("escala", 1), "escala"))), 4)
    out["pos_x"] = round(max(-1.0, min(2.0, _num(c.get("pos_x", 0.5), "posição"))), 4)
    out["pos_y"] = round(max(-1.0, min(2.0, _num(c.get("pos_y", 0.5), "posição"))), 4)
    out["opacidade"] = round(max(0.0, min(1.0, _num(c.get("opacidade", 1), "opacidade"))), 4)
    rot = round(max(-3600.0, min(3600.0, _num(c.get("rotacao", 0) or 0, "rotação"))), 3)
    if rot:
        out["rotacao"] = rot
    cor = c.get("cor") or {}
    out["cor"] = {k: round(max(0.0, min(3.0, _num(cor.get(k, 1), k))), 4) for k in ("brilho", "contraste", "saturacao")}
    temp = round(max(-1.0, min(1.0, _num(cor.get("temperatura", 0) or 0, "temperatura"))), 4)
    if temp:   # -1 frio … +1 quente
        out["cor"]["temperatura"] = temp


def _grafico(g):
    """Template de gráfico de um clipe: o id, os parâmetros e a definição inteira (camadas e expressões),
    para o arquivo não depender do pacote de templates de onde ele saiu."""
    if not isinstance(g, dict) or not isinstance(g.get("def"), dict):
        raise ValueError("gráfico sem template")
    bruto = json.dumps(g, ensure_ascii=False)
    if len(bruto) > MAX_GRAFICO:
        raise ValueError("template de gráfico grande demais")
    params = {str(k)[:40]: (v[:500] if isinstance(v, str) else v) for k, v in (g.get("params") or {}).items()
              if isinstance(v, (str, int, float, bool))}
    return {"template": str(g.get("template") or g["def"].get("id") or "")[:80], "params": params, "def": g["def"]}


def _palavras(lista):
    out = []
    for w in (lista or [])[:MAX_PALAVRAS]:
        try:
            t, fim = float(w["t"]), float(w["fim"])
        except (KeyError, TypeError, ValueError):
            continue
        texto = str(w.get("texto") or "").strip()[:60]
        if not texto:
            continue
        p = {"texto": texto, "t": round(t, 3), "fim": round(max(t, fim), 3)}
        for marca in ("quebra", "junto", "duvidosa"):
            if w.get(marca):
                p[marca] = True
        out.append(p)
    return out


def _hex(v, padrao):
    return v.upper() if isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", v) else padrao


def _legenda(leg, altura):
    leg = leg or {}
    p = dict(LEGENDA_PADRAO)
    p.update({
        "ativa": bool(leg.get("ativa")), "modo": leg.get("modo") if leg.get("modo") in LEGENDA_MODOS else "animada",
        "queimar": bool(leg.get("queimar")), "fonte": str(leg.get("fonte") or p["fonte"])[:80],
        "tamanho": int(max(0, min(400, _num(leg.get("tamanho", 0), "tamanho")))),
        "cor_texto": _hex(leg.get("cor_texto"), p["cor_texto"]), "cor_destaque": _hex(leg.get("cor_destaque"), p["cor_destaque"]),
        "margem": int(max(-1, min(altura, _num(leg.get("margem", -1), "margem")))),
        "maiusculas": bool(leg.get("maiusculas", True)),
        "palavras": int(max(1, min(10, _num(leg.get("palavras", 4), "palavras")))),
        "estilo": str(leg.get("estilo") or "")[:80],
    })
    if re.fullmatch(r"[0-9a-f]{16,64}", str(leg.get("render") or "")):
        p["render"] = leg["render"]
    return p


def legenda_auto(leg, largura, altura):
    """Tamanho e margem de verdade: 0 / -1 = proporcional ao quadro (o lado menor manda — 9:16 e 16:9
    ficam com letra parecida em relação à tela)."""
    base = min(largura, altura)
    return (leg["tamanho"] or round(base * 0.075), round(altura * 0.29) if leg["margem"] < 0 else leg["margem"])


def palavras_da_sequencia(doc):
    """As palavras no tempo da sequência: de cada clipe de áudio (não mudo, não efeito), as palavras do take
    cujo meio cai no trecho usado, deslocadas para onde o clipe está. `midia`/`i` apontam a palavra de origem
    (é ela que o estúdio corrige)."""
    por_id = {m["id"]: m for m in doc["midias"]}
    out = []
    for c in doc["clipes"]:
        if c["tipo"] != "audio" or c.get("mudo") or c.get("sfx") or c.get("ativo") is False:
            continue
        m = por_id.get(c["midia"]) or {}
        for i, w in enumerate(m.get("palavras") or []):
            meio = (w["t"] + w["fim"]) / 2
            if not c["entrada"] <= meio <= c["saida"]:
                continue
            t = c["inicio"] + max(w["t"], c["entrada"]) - c["entrada"]
            fim = c["inicio"] + min(w["fim"], c["saida"]) - c["entrada"]
            out.append({**w, "t": round(t, 3), "fim": round(fim, 3), "locutor": "Locutor 1", "midia": m["id"], "i": i})
    out.sort(key=lambda w: w["t"])
    return out


def cues_da_sequencia(doc):
    """Telas de legenda da sequência, pela mesma regra do corte vertical (autocorte.blocos: termos que não
    quebram, preposição que não fica no fim, e as marcas quebra/junto da passada semântica)."""
    palavras = [w for w in palavras_da_sequencia(doc) if not w.get("duvidosa")]
    return autocorte.blocos(palavras, max_palavras=doc["legenda"]["palavras"])


def ass_da_sequencia(doc):
    W, H = doc["config"]["largura"], doc["config"]["altura"]
    leg = doc["legenda"]
    tam, margem = legenda_auto(leg, W, H)
    cfg = {"legenda_fonte": leg["fonte"], "legenda_tamanho": tam, "legenda_maiusculas": leg["maiusculas"],
           "legenda_margem": margem, "legenda_cores": [leg["cor_destaque"]], "legenda_estatica": leg["modo"] == "fixa"}
    return autocorte.gerar_ass(cues_da_sequencia(doc), ["Locutor 1"], cfg, largura=W, altura=H)


def trilha_de(tipo):
    """Trilhas de vídeo levam vídeo, imagem e gráfico; as de áudio, áudio."""
    return "audio" if tipo == "audio" else "video"


def normalizar(doc):
    """Valida e limpa uma sequência vinda do estúdio (ou de um arquivo editado à mão). Levanta ValueError
    com o motivo; o que é só arrumação (dimensão ímpar, clipe passando do fim da mídia) é corrigido."""
    if not isinstance(doc, dict):
        raise ValueError("sequência inválida")
    cfg = doc.get("config") or {}
    fps = _num(cfg.get("fps", 30), "fps")
    fps = min(FPS_VALIDOS, key=lambda f: abs(f - fps))
    out = {
        "formato": FORMATO, "versao": VERSAO,
        "nome": str(doc.get("nome") or "Sequência").strip()[:120] or "Sequência",
        "config": {"largura": _par(_num(cfg.get("largura", 1920), "largura")),
                   "altura": _par(_num(cfg.get("altura", 1080), "altura")), "fps": fps,
                   "preset": str(cfg.get("preset") or "personalizado")[:40]},
    }
    trilhas = doc.get("trilhas") or {}
    out["trilhas"] = {t: max(1, min(MAX_TRILHAS, int(_num(trilhas.get(t, 2), "trilhas")))) for t in ("video", "audio")}

    midias, ids = [], set()
    for m in doc.get("midias") or []:
        mid = str(m.get("id") or "").strip()
        caminho = str(m.get("caminho") or "").strip()
        if not mid or not caminho or mid in ids:
            raise ValueError(f"mídia inválida ou repetida: {mid or caminho!r}")
        ids.add(mid)
        imagem = bool(m.get("imagem"))
        midias.append({
            "id": mid, "caminho": caminho,
            "nome": str(m.get("nome") or Path(caminho.replace("\\", "/")).name)[:200],
            # imagem não tem duração: o clipe dura o que a pessoa quiser
            "duracao": 0.0 if imagem else max(0.0, _num(m.get("duracao", 0), "duração")),
            "largura": int(_num(m.get("largura", 0), "largura")), "altura": int(_num(m.get("altura", 0), "altura")),
            "video": True if imagem else bool(m.get("video", True)),
            "audios": 0 if imagem else max(0, int(_num(m.get("audios", 1), "faixas"))),
            "imagem": imagem, "sfx": bool(m.get("sfx")),
            "pasta": str(m.get("pasta") or "").strip()[:60],   # pasta do bin (organização; não afeta o render)
            "nota": str(m.get("nota") or "")[:500], "estrela": bool(m.get("estrela")),
            # data do arquivo: o storyboard ordena os takes por ela ("ordem de gravação")
            "modificado": str(m.get("modificado") or "")[:40],
            # transcrição do take (whisper local), revisada no estúdio; fica no arquivo para viajar com ele
            "palavras": _palavras(m.get("palavras")),
        })
    out["midias"] = midias
    pastas = []
    for nome in [*(doc.get("pastas") or []), *(m["pasta"] for m in midias)]:
        nome = str(nome or "").strip()[:60]
        if nome and nome not in pastas and len(pastas) < 40:
            pastas.append(nome)
    out["pastas"] = pastas
    por_id = {m["id"]: m for m in midias}

    clipes, cids = [], set()
    for c in doc.get("clipes") or []:
        cid = str(c.get("id") or "").strip()
        tipo = c.get("tipo")
        m = por_id.get(str(c.get("midia") or ""))
        if not cid or cid in cids or tipo not in TIPOS or (tipo != "grafico" and not m):
            raise ValueError(f"clipe inválido: {cid or c!r}")
        if tipo == "video" and not m["video"]:
            raise ValueError(f"{m['nome']} não tem vídeo")
        if tipo == "audio" and not m["audios"]:
            raise ValueError(f"{m['nome']} não tem áudio")
        cids.add(cid)
        entrada = 0.0 if tipo == "grafico" else max(0.0, _num(c.get("entrada", 0), "entrada"))
        saida = _num(c.get("saida", entrada), "saída")
        if m and m["duracao"]:
            saida = min(saida, m["duracao"])
        if saida - entrada < MIN_CLIPE:
            raise ValueError(f"clipe {cid} sem duração")
        trilha = int(_num(c.get("trilha", 1), "trilha"))
        tt = trilha_de(tipo)
        if not 1 <= trilha <= out["trilhas"][tt]:
            raise ValueError(f"clipe {cid} numa trilha que não existe ({tt[0].upper()}{trilha})")
        clipe = {"id": cid, "tipo": tipo, "trilha": trilha,
                 "inicio": round(max(0.0, _num(c.get("inicio", 0), "início")), 4),
                 "entrada": round(entrada, 4), "saida": round(saida, 4)}
        if m:
            clipe["midia"] = m["id"]
        if c.get("vinculo"):
            clipe["vinculo"] = str(c["vinculo"])[:40]
        if tipo == "audio":
            clipe["faixa"] = max(1, min(m["audios"], int(_num(c.get("faixa", 1), "faixa"))))
            clipe["ganho"] = round(max(-60.0, min(12.0, _num(c.get("ganho", 0), "ganho"))), 2)
            clipe["mudo"] = bool(c.get("mudo"))
            if c.get("sfx"):
                clipe["sfx"] = True
        else:
            clipe["ajuste"] = c.get("ajuste") if c.get("ajuste") in AJUSTES else "conter"
            _visual(c, clipe)
            if tipo == "video" and c.get("enquadramento") and not m.get("imagem"):
                clipe["enquadramento"] = _enquadramento(c["enquadramento"])
            if tipo == "grafico":
                clipe["grafico"] = _grafico(c.get("grafico"))
                if re.fullmatch(r"[0-9a-f]{16,64}", str(c.get("render") or "")):
                    clipe["render"] = c["render"]
        if c.get("ativo") is False:   # desativado (Shift+E): fica na timeline, some do monitor e do export
            clipe["ativo"] = False
        dur = clipe["saida"] - clipe["entrada"]
        kfs = _keyframes(c.get("keyframes"), KF_AUDIO if tipo == "audio" else KF_VISUAL, dur)
        if kfs:
            clipe["keyframes"] = kfs
        for k in ("fade_in", "fade_out"):   # alças de fade: áudio = volume, vídeo/gráfico = opacidade
            f = round(max(0.0, min(dur, _num(c.get(k, 0) or 0, k))), 4)
            if f >= 1 / 120:
                clipe[k] = f
        tr = c.get("transicao")
        if isinstance(tr, dict) and tr.get("tipo") in (TRANSICOES_A if tipo == "audio" else TRANSICOES_V):
            clipe["transicao"] = {"tipo": tr["tipo"], "dur": round(max(1 / 30, min(TRANSICAO_MAX, _num(tr.get("dur", 0.5), "duração da transição"))), 4)}
        clipes.append(clipe)
    clipes.sort(key=lambda c: (trilha_de(c["tipo"]), c["trilha"], c["inicio"]))
    out["clipes"] = clipes
    out["legenda"] = _legenda(doc.get("legenda"), out["config"]["altura"])
    out["trilhas_estado"] = _estado_trilhas(doc.get("trilhas_estado"), out["trilhas"])
    out["marcadores"] = _marcadores(doc.get("marcadores"))
    return out


def _keyframes(raw, props, dur):
    out = {}
    if not isinstance(raw, dict):
        return out
    for prop in props:
        lista = raw.get(prop)
        if not isinstance(lista, list):
            continue
        vistos, kfs = set(), []
        for k in lista[:MAX_KF]:
            if not isinstance(k, dict):
                continue
            try:
                t, v = float(k.get("t")), float(k.get("v"))
            except (TypeError, ValueError):
                continue
            if t != t or v != v or abs(v) > 1e6:
                continue
            t = round(max(0.0, min(dur, t)), 4)
            if t in vistos:
                continue
            vistos.add(t)
            kfs.append({"t": t, "v": round(v, 5), "ease": k.get("ease") if k.get("ease") in KF_EASES else "linear"})
        if kfs:
            out[prop] = sorted(kfs, key=lambda k: k["t"])
    return out


def valor(c, prop, T, padrao=None):
    """Valor de uma propriedade no tempo T do clipe (segundos desde o início dele). Antes do 1º keyframe vale
    o 1º, depois do último o último; entre dois, linear, suave (smoothstep) ou segurar, pela ease do
    keyframe da esquerda. Sem keyframes, o valor fixo. = seqValor do estúdio."""
    kfs = (c.get("keyframes") or {}).get(prop)
    if not kfs:
        return c.get(prop, padrao)
    if T <= kfs[0]["t"]:
        return kfs[0]["v"]
    for a, b in zip(kfs, kfs[1:]):
        if T < b["t"]:
            if a["ease"] == "segurar" or b["t"] <= a["t"]:
                return a["v"]
            p = (T - a["t"]) / (b["t"] - a["t"])
            if a["ease"] == "suave":
                p = p * p * (3 - 2 * p)
            return a["v"] + (b["v"] - a["v"]) * p
    return kfs[-1]["v"]


def expr_kf(kfs, T):
    """Os keyframes como expressão do ffmpeg em `T` (texto já com o tempo do clipe): ifs encadeados, a mesma
    conta de `valor`."""
    def n(x):
        return _f(x) if abs(x) >= 1e-4 or x == 0 else f"{x:.6f}"
    partes, fecha = [], 0
    partes.append(f"if(lt({T},{n(kfs[0]['t'])}),{n(kfs[0]['v'])},")
    fecha += 1
    for a, b in zip(kfs, kfs[1:]):
        dt = b["t"] - a["t"]
        if a["ease"] == "segurar" or dt <= 0:
            seg = n(a["v"])
        else:
            P = f"(({T}-{n(a['t'])})/{n(dt)})"
            k = f"{P}*{P}*(3-2*{P})" if a["ease"] == "suave" else P
            seg = f"{n(a['v'])}+({n(b['v'] - a['v'])})*{k}"
        partes.append(f"if(lt({T},{n(b['t'])}),{seg},")
        fecha += 1
    partes.append(n(kfs[-1]["v"]) + ")" * fecha)
    return "".join(partes)


def faixa_valores(c, prop, padrao):
    """Menor e maior valor que a propriedade assume no clipe (para tamanhos fixos no grafo)."""
    kfs = (c.get("keyframes") or {}).get(prop)
    if not kfs:
        v = c.get(prop, padrao)
        return v, v
    vs = [k["v"] for k in kfs]
    return min(vs), max(vs)


def _caixa_norm(b):
    if not isinstance(b, dict):
        return None
    try:
        x, y, w, h = (float(b.get(k)) for k in ("x", "y", "w", "h"))
    except (TypeError, ValueError):
        return None
    if not (0 < w <= 1.0001 and 0 < h <= 1.0001):
        return None
    return {"x": round(min(1.0, max(0.0, x)), 4), "y": round(min(1.0, max(0.0, y)), 4),
            "w": round(min(1.0, w), 4), "h": round(min(1.0, h), 4)}


def _enquadramento(e):
    """O enquadramento de um clipe de vídeo (o do corte vertical, valendo para qualquer formato): caixas
    normalizadas 0–1 do quadro da fonte; a conta em pixels é feita no render (`cadeia_enquadramento`)."""
    if not isinstance(e, dict):
        raise ValueError("enquadramento inválido")
    modo = e.get("modo") if e.get("modo") in ENQ_MODOS else "inteiro"
    out = {"modo": modo,
           "divisao": round(min(autocorte.DIVISAO_MAX, max(autocorte.DIVISAO_MIN, _num(e.get("divisao", 0.35) or 0.35, "divisão"))), 4),
           "escala": round(max(1.0, min(8.0, _num(e.get("escala", 1) or 1, "escala"))), 4),
           "posicao": round(min(1.0, max(0.0, _num(e.get("posicao", 0.5) if e.get("posicao") is not None else 0.5, "posição"))), 4)}
    for k in ("principal", "camera"):
        b = _caixa_norm(e.get(k))
        if b:
            out[k] = b
    return out


def _estado_trilhas(raw, trilhas):
    """Cabeçalho de cada trilha: V = oculto/travado, A = mudo/solo/travado e `ganho` (dB, o fader do mixer).
    Só o que está ligado (ou fora do neutro) é guardado."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for tt, letra, chaves in (("video", "V", ("oculto", "travado")), ("audio", "A", ("mudo", "solo", "travado"))):
        for n in range(1, trilhas[tt] + 1):
            st = raw.get(f"{letra}{n}") or {}
            if not isinstance(st, dict):
                continue
            liga = {k: True for k in chaves if st.get(k)}
            if tt == "audio" and st.get("ganho"):
                g = round(max(-60.0, min(12.0, _num(st["ganho"], "volume da trilha"))), 2)
                if g:
                    liga["ganho"] = g
            if liga:
                out[f"{letra}{n}"] = liga
    return out


def _marcadores(raw):
    out, ids = [], set()
    for mk in (raw or [])[:MAX_MARCADORES]:
        if not isinstance(mk, dict):
            continue
        try:
            t = float(mk.get("t"))
        except (TypeError, ValueError):
            continue
        mid = str(mk.get("id") or "").strip()[:40] or f"m{len(out)}"
        if mid in ids or t != t or t < 0:
            continue
        ids.add(mid)
        out.append({"id": mid, "t": round(t, 4), "nome": str(mk.get("nome") or "").strip()[:120],
                    "cor": mk.get("cor") if mk.get("cor") in MARCADOR_CORES else "verde"})
    out.sort(key=lambda mk: mk["t"])
    return out


def clipes_no_render(doc):
    """Os clipes que aparecem/soam: sem os desativados, sem as trilhas de vídeo ocultas e as de áudio mudas;
    com alguma trilha de áudio em solo, só as em solo."""
    est = doc.get("trilhas_estado") or {}
    solos = {k for k, v in est.items() if k.startswith("A") and v.get("solo")}
    out = []
    for c in doc["clipes"]:
        if c.get("ativo") is False:
            continue
        chave = f"{'A' if c['tipo'] == 'audio' else 'V'}{c['trilha']}"
        st = est.get(chave) or {}
        if c["tipo"] == "audio":
            if st.get("mudo") or (solos and chave not in solos):
                continue
        elif st.get("oculto"):
            continue
        out.append(c)
    return out


def dur_clipe(c):
    return c["saida"] - c["entrada"]


def duracao(doc):
    return round(max((c["inicio"] + dur_clipe(c) for c in doc.get("clipes") or []), default=0.0), 4)


def sobreposicoes(doc):
    """Clipes que se atropelam na mesma trilha (o estúdio não deixa, mas um arquivo editado à mão pode):
    no render o de cima na lista ganha, então só avisamos."""
    out = []
    for tt in ("video", "audio"):
        for n in range(1, doc["trilhas"][tt] + 1):
            cs = sorted((c for c in doc["clipes"] if trilha_de(c["tipo"]) == tt and c["trilha"] == n),
                        key=lambda c: c["inicio"])
            for a, b in zip(cs, cs[1:]):
                if b["inicio"] < a["inicio"] + dur_clipe(a) - 1e-3:
                    out.append((a["id"], b["id"]))
    return out


# ------------------------------------------------------------------ ffmpeg
def _f(x):
    return f"{x:.4f}".rstrip("0").rstrip(".") or "0"


def cadeia_cor(cor):
    """Igual a autocorte.cadeia_cor e ao shader do monitor: brilho multiplica o RGB, contraste e
    saturação pelo eq. Neutro = nada."""
    cor = cor or {}
    b, c, s = (float(cor.get(k, 1)) for k in ("brilho", "contraste", "saturacao"))
    k = float(cor.get("temperatura", 0) or 0)   # temperatura: ganho no vermelho e no azul em sentidos opostos
    partes = []
    if abs(b - 1) > 1e-3 or abs(k) > 1e-3:
        rr, bb = b * (1 + TEMP_K * k), b * (1 - TEMP_K * k)
        partes.append(f"colorchannelmixer=rr={rr:.3f}:gg={b:.3f}:bb={bb:.3f}")
    if abs(c - 1) > 1e-3 or abs(s - 1) > 1e-3:
        partes.append(f"eq=contrast={c:.3f}:saturation={s:.3f}")
    return partes


def cadeia_enquadramento(e, sw, sh, W, H, k):
    """Enquadramento do clipe → filtros que levam a fonte (sw×sh) ao quadro inteiro da sequência (W×H), com
    as regras do corte vertical (autocorte._caixa/normalizar_layout/_cadeia_desfocada) no tamanho da
    sequência em vez do 1080x1920 fixo. `k` numera os rótulos internos (vários clipes no mesmo grafo).
    O monitor do estúdio faz a mesma conta (seqEnqCamadas) — mudar os dois juntos."""
    modo = e.get("modo", "inteiro")
    if modo == "dividido":
        topo = autocorte._par(H * e.get("divisao", 0.35))
        cam = autocorte._caixa(e.get("camera") or {"x": 0.72, "y": 0.05, "w": 0.26, "h": 0.3}, sw, sh, W / topo)
        pri = autocorte._caixa(e.get("principal"), sw, sh, W / (H - topo))
        return (f"split[cam{k}][jogo{k}];[cam{k}]{autocorte._crop(cam)},scale={W}:{topo}:flags=lanczos,setsar=1[camv{k}];"
                f"[jogo{k}]{autocorte._crop(pri)},scale={W}:{H - topo}:flags=lanczos,setsar=1[jogov{k}];"
                f"[camv{k}][jogov{k}]vstack=inputs=2")
    if modo == "desfocado":
        esc = min(max(1.0, H * sw / (W * sh)), max(1.0, e.get("escala", 1)))
        fw = autocorte._par(W * esc)
        fh = autocorte._par(math.ceil(fw * sh / sw / 2) * 2)
        cw, ch = min(fw, W), min(fh, H)
        x, y = int((fw - cw) * e.get("posicao", 0.5)), (fh - ch) // 2
        raio = max(2, round(40 * min(W, H) / 1080))
        return (f"split[fundo{k}][frente{k}];[fundo{k}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"boxblur={raio}:2,eq=brightness=-0.08[fundob{k}];"
                f"[frente{k}]scale={fw}:{fh}:flags=lanczos,crop={cw}:{ch}:{x}:{y}[frentes{k}];"
                f"[fundob{k}][frentes{k}]overlay=(W-w)/2:(H-h)/2")
    pri = autocorte._caixa(e.get("principal"), sw, sh, W / H)
    return f"{autocorte._crop(pri)},scale={W}:{H}:flags=lanczos"


def entrada_de(c):
    """Chave da entrada do ffmpeg de um clipe: uma entrada por clipe, já cortada na leitura (`janela_de`), em
    vez de uma por mídia alimentando vários `trim` — que decodificava o arquivo desde o começo para cada
    recorte."""
    return c["id"]


def janela_de(c, m, plano):
    """(início na sequência, entrada no take, duração) de um clipe, contando as alças da transição."""
    pl = plano.get(c["id"], {"antes": 0.0, "depois": 0.0})
    ini = c["inicio"] - pl["antes"]
    dur = dur_clipe(c) + pl["antes"] + pl["depois"]
    ent = 0.0 if (m.get("imagem") or c["tipo"] == "grafico") else c["entrada"] - pl["antes"]
    return ini, ent, dur


def _folgas(c, m):
    """Quanto do take existe antes da entrada e depois da saída do clipe (as "alças" que uma transição usa).
    Gráfico não tem (a animação começa e acaba no clipe); imagem tem à vontade."""
    if c["tipo"] == "grafico":
        return 0.0, 0.0
    if m.get("imagem"):
        return float("inf"), float("inf")
    return max(0.0, c["entrada"]), max(0.0, (m.get("duracao") or 0) - c["saida"])


def plano_transicoes(doc, clipes=None):
    """Por clipe (id): `antes`/`depois` = quanto ele se estende além do início/fim para uma transição, e
    `trans` = as transições que tocam nele [{tipo, lado: entra|sai, a, b}] (janela em tempo da sequência).
    A transição mora no clipe que entra e vale só se o anterior na mesma trilha acaba exatamente no início
    dele. Dissolver/deslizar/cruzado ficam centrados no corte, cada lado limitado pela alça do seu take (sem
    alça, a janela encolhe daquele lado); mergulho no preto/branco não precisa de alça: o de antes escurece
    até o corte e o de depois clareia a partir dele. = seqPlanoTransicoes do estúdio."""
    clipes = clipes if clipes is not None else clipes_no_render(doc)
    por_id = {m["id"]: m for m in doc["midias"]}
    plano = {c["id"]: {"antes": 0.0, "depois": 0.0, "trans": []} for c in clipes}
    for c in clipes:
        tr = c.get("transicao")
        if not tr:
            continue
        tt = trilha_de(c["tipo"])
        prev = [o for o in clipes if o is not c and trilha_de(o["tipo"]) == tt and o["trilha"] == c["trilha"]
                and abs(o["inicio"] + dur_clipe(o) - c["inicio"]) < 1e-3]
        if not prev:
            continue
        a, corte, d = prev[0], c["inicio"], tr["dur"]
        if tr["tipo"] in ("preto", "branco"):
            meio = min(d / 2, dur_clipe(a), dur_clipe(c))
            plano[a["id"]]["trans"].append({"tipo": tr["tipo"], "lado": "sai", "a": round(corte - meio, 4), "b": corte})
            plano[c["id"]]["trans"].append({"tipo": tr["tipo"], "lado": "entra", "a": corte, "b": round(corte + meio, 4)})
            continue
        depois_a = min(d / 2, _folgas(a, por_id.get(a.get("midia")) or {})[1])
        antes_c = min(d / 2, _folgas(c, por_id.get(c.get("midia")) or {})[0])
        if depois_a + antes_c < 1 / 120:
            continue
        plano[a["id"]]["depois"] = round(depois_a, 4)
        plano[c["id"]]["antes"] = round(antes_c, 4)
        jan = {"tipo": tr["tipo"], "a": round(corte - antes_c, 4), "b": round(corte + depois_a, 4)}
        plano[a["id"]]["trans"].append({**jan, "lado": "sai"})
        plano[c["id"]]["trans"].append({**jan, "lado": "entra"})
    return plano


def _cadeia_visual(c, m, k, W, H, fps, plano, enq_px):
    """Filtros de um clipe visual depois do trim/setpts: tamanho (escala com ou sem keyframes), cor, rotação,
    opacidade (fixa, keyframes ou fades) e as transições de mergulho/dissolver. Devolve (filtros, expr x, expr y)."""
    ini, fim = c["inicio"], c["inicio"] + dur_clipe(c)
    T = f"(t-{_f(ini)})"
    kfs = c.get("keyframes") or {}
    sw = W if c["tipo"] == "grafico" else (m.get("largura") or 0)
    sh = H if c["tipo"] == "grafico" else (m.get("altura") or 0)
    f = []
    if enq_px:
        f.append(enq_px)
        bw_max, bh_max = W, H
    elif "escala" in kfs and sw and sh:
        e = expr_kf(kfs["escala"], T)
        s = f"max(W/{sw},H/{sh})" if c.get("ajuste") == "preencher" else f"min(W/{sw},H/{sh})"
        s = s.replace("W", str(W)).replace("H", str(H))
        f.append(f"scale=w='max(2,trunc({sw}*({e})*{s}/2)*2)':h='max(2,trunc({sh}*({e})*{s}/2)*2)':eval=frame")
        emax = faixa_valores(c, "escala", 1)[1]
        k_s = (max if c.get("ajuste") == "preencher" else min)(W / sw, H / sh) * emax
        bw_max, bh_max = sw * k_s, sh * k_s
    else:
        e = c.get("escala", 1)
        bw, bh = _par(W * e), _par(H * e)
        if c.get("ajuste") == "preencher":
            f.append(f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh}")
        else:
            f.append(f"scale={bw}:{bh}:force_original_aspect_ratio=decrease")
        bw_max, bh_max = bw, bh
    f += ["setsar=1", f"fps={_f(fps)}", "format=yuva420p"]
    cor = cadeia_cor(c.get("cor"))
    if cor:
        f += ["format=rgba", *cor, "format=yuva420p"]
    if not enq_px and ("rotacao" in kfs or c.get("rotacao")):
        rot = expr_kf(kfs["rotacao"], T) if "rotacao" in kfs else _f(c["rotacao"])
        lado = autocorte._par(math.ceil(math.hypot(bw_max, bh_max)) + 2)
        f.append(f"rotate=a='({rot})*PI/180':ow={lado}:oh={lado}:c=black@0")
    if "opacidade" in kfs:
        op = expr_kf(kfs["opacidade"], T.replace("t", "T"))
        f.append(f"split[oc{k}][om{k}];[om{k}]alphaextract,geq=lum='p(X,Y)*clip({op},0,1)'[oa{k}];[oc{k}][oa{k}]alphamerge")
    elif c.get("opacidade", 1) < 0.999:
        f += ["format=rgba", f"colorchannelmixer=aa={c['opacidade']:.3f}", "format=yuva420p"]
    if c.get("fade_in"):
        f.append(f"fade=t=in:st={_f(ini)}:d={_f(c['fade_in'])}:alpha=1")
    if c.get("fade_out"):
        f.append(f"fade=t=out:st={_f(fim - c['fade_out'])}:d={_f(c['fade_out'])}:alpha=1")
    deslizar = ""
    for tr in plano.get(c["id"], {}).get("trans", []):
        d = max(tr["b"] - tr["a"], 1 / 120)
        if tr["tipo"] in ("preto", "branco"):
            cor_t = "black" if tr["tipo"] == "preto" else "white"
            f.append(f"fade=t={'out' if tr['lado'] == 'sai' else 'in'}:st={_f(tr['a'])}:d={_f(d)}:color={cor_t}")
        elif tr["lado"] == "entra" and tr["tipo"] == "dissolver":
            f.append(f"fade=t=in:st={_f(tr['a'])}:d={_f(d)}:alpha=1")
        elif tr["lado"] == "entra" and tr["tipo"] == "deslizar":   # entra da direita por cima do anterior
            deslizar = f"+W*(1-clip((t-{_f(tr['a'])})/{_f(d)},0,1))"
    if enq_px:
        px, py = "0.5", "0.5"
    else:
        px = expr_kf(kfs["pos_x"], T) if "pos_x" in kfs else _f(c.get("pos_x", 0.5))
        py = expr_kf(kfs["pos_y"], T) if "pos_y" in kfs else _f(c.get("pos_y", 0.5))
    return f, f"({px})*W-w/2{deslizar}", f"({py})*H-h/2"


def _cadeia_audio(c, k, plano, entrada_label, ganho_trilha=0.0):
    """Um clipe de áudio: recorte (com as alças da transição), ganho fixo ou com keyframes, fades de entrada
    e saída, a transição cruzada e o atraso até o início."""
    p = plano.get(c["id"], {"antes": 0.0, "depois": 0.0, "trans": []})
    antes, depois = p["antes"], p["depois"]
    dur = dur_clipe(c)
    f = [f"atrim=duration={_f(dur + antes + depois)}", "asetpts=PTS-STARTPTS",   # entrada já cortada (-ss/-t)
         "aformat=sample_rates=48000:channel_layouts=stereo"]
    kfs = (c.get("keyframes") or {}).get("ganho")
    if kfs:
        f.append(f"volume='pow(10,({expr_kf(kfs, f'(t-{_f(antes)})')})/20)':eval=frame")
    else:
        f.append(f"volume={c.get('ganho', 0):.2f}dB")
    if ganho_trilha:   # o fader da trilha no mixer
        f.append(f"volume={ganho_trilha:.2f}dB")
    if c.get("fade_in"):
        f.append(f"afade=t=in:st={_f(antes)}:d={_f(c['fade_in'])}")
    if c.get("fade_out"):
        f.append(f"afade=t=out:st={_f(antes + dur - c['fade_out'])}:d={_f(c['fade_out'])}")
    for tr in p["trans"]:   # cruzado: o de antes some e o de depois aparece na mesma janela (potência constante)
        d = max(tr["b"] - tr["a"], 1 / 120)
        st = tr["a"] - (c["inicio"] - antes)
        f.append(f"afade=t={'out' if tr['lado'] == 'sai' else 'in'}:st={_f(st)}:d={_f(d)}:curve=qsin")
    ms = int(round((c["inicio"] - antes) * 1000))
    f.append(f"adelay={ms}:all=1")
    return f"{entrada_label}" + ",".join(f) + f"[a{k}]"


def grafo_video(doc, ordem_entradas, legenda=False):
    """filter_complex do vídeo. `ordem_entradas`: chave (`entrada_de`) → índice do -i. Um fundo preto do
    tamanho da sequência e cada clipe por cima, de V1 para cima e em ordem de tempo, deslocado para o seu
    início (setpts) e ligado só no seu intervalo (mais as alças de uma transição). A caixa do clipe é o
    quadro vezes a escala; conter = cabe na caixa, preencher = cobre e corta; o centro vai para (pos_x,
    pos_y). Keyframes viram expressões no tempo (`expr_kf`): escala no scale com eval=frame (com keyframes de
    escala, preencher não corta na caixa), posição no overlay, rotação no rotate, opacidade num geq só do
    canal alfa. Termina em [vout]."""
    W, H, fps = doc["config"]["largura"], doc["config"]["altura"], doc["config"]["fps"]
    D = max(duracao(doc), MIN_CLIPE)
    partes = [f"color=c=black:s={W}x{H}:r={_f(fps)}:d={_f(D)},format=yuv420p[base]"]
    atual = "base"
    por_id = {m["id"]: m for m in doc["midias"]}
    no_render = clipes_no_render(doc)
    plano = plano_transicoes(doc, no_render)
    videos = sorted((c for c in no_render if c["tipo"] != "audio"), key=lambda c: (c["trilha"], c["inicio"]))
    for k, c in enumerate(videos):
        i = ordem_entradas[entrada_de(c)]
        m = por_id.get(c.get("midia")) or {}
        ini, _, dur = janela_de(c, m, plano)
        fim = ini + dur
        enq = c.get("enquadramento") if m.get("largura") and m.get("altura") else None
        enq_px = cadeia_enquadramento(enq, m["largura"], m["altura"], W, H, k) if enq else None
        filtros, ox, oy = _cadeia_visual(c, m, k, W, H, fps, plano, enq_px)
        # a entrada já vem cortada (-ss/-t); o trim só garante a duração exata
        cadeia = [f"trim=duration={_f(dur)}", f"setpts=PTS-STARTPTS+{_f(ini)}/TB", *filtros]
        partes.append(f"[{i}:v:0]" + ",".join(cadeia) + f"[v{k}]")
        prox = f"o{k}"
        partes.append(f"[{atual}][v{k}]overlay=x='{ox}':y='{oy}':eof_action=pass:enable='between(t,{_f(ini)},{_f(fim)})'[{prox}]")
        atual = prox
    if legenda:   # legenda queimada: o .mov com alfa feito dos PNG do estúdio (video_da_legenda), por cima de tudo
        partes.append(f"[{ordem_entradas['legenda']}:v:0]fps={_f(fps)},format=yuva420p[leg]")
        partes.append(f"[{atual}][leg]overlay=0:0:eof_action=pass[oleg]")
        atual = "oleg"
    partes.append(f"[{atual}]format=yuv420p[vout]")
    return ";".join(partes)


def grafo_audio(doc, ordem_entradas):
    """filter_complex do áudio: cada clipe recortado, com ganho (fixo ou keyframes), fades e transição
    cruzada, atrasado até o início e somado sem normalizar. Termina em [aout]."""
    D = max(duracao(doc), MIN_CLIPE)
    no_render = clipes_no_render(doc)
    plano = plano_transicoes(doc, no_render)
    partes, rotulos = [], []
    for k, c in enumerate(c for c in no_render if c["tipo"] == "audio" and not c.get("mudo")):
        ganho_trilha = ((doc.get("trilhas_estado") or {}).get(f"A{c['trilha']}") or {}).get("ganho", 0.0)
        partes.append(_cadeia_audio(c, k, plano, f"[{ordem_entradas[entrada_de(c)]}:a:{c.get('faixa', 1) - 1}]", ganho_trilha))
        rotulos.append(f"[a{k}]")
    if rotulos:
        mix = (f"{''.join(rotulos)}amix=inputs={len(rotulos)}:normalize=0:duration=longest" if len(rotulos) > 1
               else f"{rotulos[0]}anull")
        # apad com fim (whole_dur): sem ele o apad gera silêncio para sempre e depende do atrim avisar o fim, e
        # o ffmpeg 8.1 às vezes não avisa — o apad girava a 100% de CPU sem terminar (livelock intermitente,
        # ~3% das passadas de áudio; 0 em 60 com o limite)
        partes.append(f"{mix},apad=whole_dur={_f(D)},atrim=0:{_f(D)}[aout]")
    else:
        partes.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{_f(D)}[aout]")
    return ";".join(partes)


def grafo(doc, ordem_entradas, legenda=False):
    """Vídeo e áudio num grafo só (para inspecionar; o render usa duas passadas, ver `comando`)."""
    return grafo_video(doc, ordem_entradas, legenda) + ";" + grafo_audio(doc, ordem_entradas)


def _entradas(doc, caminhos, graficos, audio):
    """As entradas de um lado do render (`audio` True = só os clipes de áudio; False = os visuais): uma por
    clipe, cortada na leitura (`-ss`/`-t` com as alças da transição; imagem em loop; gráfico = os PNG que o
    estúdio renderizou, em `graficos[id do clipe]`). Devolve (args, chaves na ordem)."""
    graficos = graficos or {}
    por_id = {m["id"]: m for m in doc["midias"]}
    fps = doc["config"]["fps"]
    usados, cmd_in = [], []
    vivos = clipes_no_render(doc)
    plano = plano_transicoes(doc, vivos)
    for c in vivos:
        if (c["tipo"] == "audio") != audio or (c["tipo"] == "audio" and c.get("mudo")):
            continue
        usados.append(entrada_de(c))
        m = por_id.get(c.get("midia")) or {}
        _, ent, dur = janela_de(c, m, plano)
        if c["tipo"] == "grafico":
            cmd_in += ["-framerate", _f(fps), "-start_number", "0", "-i", str(Path(graficos[c["id"]]) / "%05d.png")]
        elif m.get("imagem"):
            cmd_in += ["-loop", "1", "-framerate", _f(fps), "-t", _f(dur), "-i", caminhos[c["midia"]]]
        else:
            cmd_in += ["-ss", _f(ent), "-t", _f(dur), "-i", caminhos[c["midia"]]]
    return cmd_in, usados


def comando_audio(doc, caminhos, saida):
    """1ª passada: a mixagem inteira num WAV, que a 2ª (`comando` com `audio=`) só copia para o MP4. A mixagem
    fica fora do grafo de vídeo: o travamento intermitente que apareceu com as transições era o `apad` sem
    fim (ver `grafo_audio`), e com as passadas separadas um problema de áudio nunca segura o vídeo."""
    cmd_in, usados = _entradas(doc, caminhos, None, True)
    ordem = {k: i for i, k in enumerate(usados)}
    return ["ffmpeg", "-nostdin", "-v", "error", "-y", *cmd_in, "-filter_complex", grafo_audio(doc, ordem),
            "-map", "[aout]", "-c:a", "pcm_s16le", "-t", _f(max(duracao(doc), MIN_CLIPE)), str(saida)]


def comando(doc, caminhos, saida, graficos=None, legenda=None, audio=None):
    """2ª passada: o vídeo (entradas visuais + a legenda queimada, se houver) com o WAV da `comando_audio`
    como trilha, em H.264/AAC. Sem `audio`, monta vídeo e áudio num comando só (só para inspecionar)."""
    cmd_in, usados = _entradas(doc, caminhos, graficos, False)
    if legenda:   # o vídeo da legenda que `video_da_legenda` montou (nunca o concat direto: ver lá)
        usados.append("legenda")
        cmd_in += ["-i", str(legenda)]
    fps = doc["config"]["fps"]
    if audio:
        cmd_in += ["-i", str(audio)]
        filtro, mapa_a = grafo_video(doc, {k: i for i, k in enumerate(usados)}, legenda=bool(legenda)), f"{len(usados)}:a"
    else:
        a_in, a_usados = _entradas(doc, caminhos, None, True)
        cmd_in += a_in
        usados += a_usados
        filtro, mapa_a = grafo(doc, {k: i for i, k in enumerate(usados)}, legenda=bool(legenda)), "[aout]"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-stats", "-y", *cmd_in]
    cmd += ["-filter_complex", filtro, "-map", "[vout]", "-map", mapa_a,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-r", _f(fps),
            "-c:a", "aac", "-b:a", "192k", "-t", _f(max(duracao(doc), MIN_CLIPE)), "-movflags", "+faststart", str(saida)]
    return cmd


# ------------------------------------------------------------------ Auto-SFX
# A pasta de efeitos (whoosh, impacto, clique…) vira uma biblioteca por categoria, pelo nome do arquivo e
# das pastas; os cortes de V1 e as entradas nas trilhas de cima viram eventos; cada evento ganha um efeito
# da categoria pedida, ancorado do jeito que soa certo (whoosh centrado no corte, impacto começando nele,
# riser terminando nele). Determinístico: a mesma sequência gera os mesmos efeitos.
SFX_CATEGORIAS = (   # ordem importa: "Reverse riser" é riser, não whoosh
    ("riser", ("riser", "risers", "rise", "build", "buildup", "uplifter", "subida", "tension")),
    ("impacto", ("impact", "impacto", "hit", "boom", "punch", "thud", "slam", "drop", "kick", "braam")),
    ("whoosh", ("whoosh", "woosh", "swoosh", "swish", "whip", "swipe", "sweep", "transition", "transicao", "pass", "fly")),
    ("glitch", ("glitch", "error", "static", "noise", "distortion")),
    ("ding", ("ding", "bell", "chime", "notification", "sino", "notificacao")),
    ("clique", ("click", "clique", "beep", "button", "shutter", "tick", "pop", "bubble", "ui", "tap", "botao", "type")),
)
SFX_ANCORA = {"whoosh": "centro", "riser": "fim"}   # o resto começa no evento
RISER_MAX = 4.0   # riser é longo de propósito: até 4 s, mesmo com max_dur menor
SFX_PADRAO = {"corte": "whoosh", "entrada": "clique", "movimento": "", "minimo": 0.8, "ganho": -10.0, "max_dur": 1.5}
SFX_REGRAS = ("corte", "entrada", "movimento")


def _sem_acento(t):
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", t) if unicodedata.category(ch) != "Mn")


def categoria_sfx(rel):
    """Categoria de um arquivo de efeito pelo nome dele e das pastas ("Risers/Riser 4.mp3" → riser)."""
    txt = _sem_acento(str(rel).replace("\\", "/")).lower()
    palavras = set(re.findall(r"[a-z]+", txt))
    for cat, chaves in SFX_CATEGORIAS:
        if palavras & set(chaves):
            return cat
    return "outros"


def sfx_params(params=None):
    p = dict(SFX_PADRAO)
    for k, v in (params or {}).items():
        if k in SFX_REGRAS:
            p[k] = str(v or "")
        elif k in SFX_PADRAO:
            p[k] = float(v)
    return p


def eventos_sfx(doc, params=None, movimentos=None):
    """Onde entra som: corte seco em V1 (um clipe começa onde o anterior acaba), a entrada de cada clipe nas
    trilhas de cima (motion, logo, gráfico) e — com `movimento` — as trocas de cena dentro dos takes de V1
    (`movimentos`: id da mídia → tempos no take, do scdet). Eventos a menos de `minimo` do anterior ficam de fora."""
    p = sfx_params(params)
    ev = []
    if p["movimento"] and movimentos:
        for c in doc["clipes"]:
            if c["tipo"] != "video" or c["trilha"] != 1:
                continue
            for tm in movimentos.get(c.get("midia")) or []:
                if c["entrada"] + 0.2 < tm < c["saida"] - 0.2:   # a borda do clipe já é um corte
                    ev.append((round(c["inicio"] + tm - c["entrada"], 4), "movimento", p["movimento"]))
    if p["corte"]:
        v1 = sorted((c for c in doc["clipes"] if c["tipo"] != "audio" and c["trilha"] == 1), key=lambda c: c["inicio"])
        for a, b in zip(v1, v1[1:]):
            if abs(b["inicio"] - (a["inicio"] + dur_clipe(a))) < 0.05:
                ev.append((round(b["inicio"], 4), "corte", p["corte"]))
    if p["entrada"]:
        for c in doc["clipes"]:
            if c["tipo"] != "audio" and c["trilha"] >= 2:
                ev.append((round(c["inicio"], 4), "entrada", p["entrada"]))
    ev.sort()
    out, ultimo = [], None
    for t, motivo, cat in ev:
        if ultimo is not None and t - ultimo < p["minimo"]:
            continue
        out.append({"t": t, "motivo": motivo, "categoria": cat})
        ultimo = t
    return out


def _semente(texto):
    return sum((i + 1) * ord(ch) for i, ch in enumerate(texto)) % 9973


def planejar_sfx(doc, biblioteca, params=None, novo_id=None, movimentos=None):
    """Plano de efeitos para a sequência: os clipes (marcados `sfx`), a trilha de áudio deles (a primeira
    que só tem efeitos, ou uma nova) e as mídias que ainda não estão na lista. Os efeitos que já existiam
    saem — gerar de novo substitui. `biblioteca`: [{caminho, nome, categoria, duracao}]."""
    p = sfx_params(params)
    novo_id = novo_id or (lambda pre, k: f"{pre}sfx{k}")
    por_cat = {}
    for a in sorted(biblioteca, key=lambda a: a["caminho"]):
        if a.get("duracao", 0) > 0:
            por_cat.setdefault(a["categoria"], []).append(a)
    # trilha: a primeira de áudio que só tem efeitos (ou nada); senão, uma nova
    sem_sfx = [c for c in doc["clipes"] if not c.get("sfx")]
    trilha = None
    for n in range(1, doc["trilhas"]["audio"] + 1):
        if not any(c["tipo"] == "audio" and c["trilha"] == n for c in sem_sfx):
            trilha = n
            break
    trilhas_audio = doc["trilhas"]["audio"]
    if trilha is None:
        if trilhas_audio >= MAX_TRILHAS:
            raise ValueError("não sobra trilha de áudio para os efeitos")
        trilhas_audio += 1
        trilha = trilhas_audio
    existentes = {m["caminho"]: m["id"] for m in doc["midias"]}
    midias, clipes, faltou = [], [], set()
    D = duracao(doc)
    ultimo_por_cat, k = {}, 0
    semente = _semente(doc.get("nome") or "")
    for n, ev in enumerate(eventos_sfx(doc, p, movimentos)):
        opcoes = por_cat.get(ev["categoria"]) or []
        # arquivo longo costuma ser coleção de vários efeitos ("swishes-and-swooshes"): os curtos primeiro
        curtos = [a for a in opcoes if a["duracao"] <= p["max_dur"] * 2.5]
        opcoes = curtos or opcoes
        if not opcoes:
            faltou.add(ev["categoria"])
            continue
        idx = (semente + n) % len(opcoes)
        if len(opcoes) > 1 and opcoes[idx]["caminho"] == ultimo_por_cat.get(ev["categoria"]):
            idx = (idx + 1) % len(opcoes)
        a = opcoes[idx]
        ultimo_por_cat[ev["categoria"]] = a["caminho"]
        ancora = SFX_ANCORA.get(ev["categoria"])
        dur = min(a["duracao"], max(p["max_dur"], RISER_MAX) if ev["categoria"] == "riser" else p["max_dur"])
        # arquivo maior que o limite: o pedaço que importa — o fim do riser (o pico), o meio do whoosh, o começo do resto
        entrada = a["duracao"] - dur if ancora == "fim" else (a["duracao"] - dur) / 2 if ancora == "centro" else 0.0
        ini = ev["t"] - dur / 2 if ancora == "centro" else ev["t"] - dur if ancora == "fim" else ev["t"]
        if ini < 0:          # não cabe antes do começo: corta a cabeça do efeito
            entrada, dur, ini = entrada - ini, dur + ini, 0.0
        if D and ini >= D:
            continue
        mid = existentes.get(a["caminho"])
        if not mid:
            mid = novo_id("m", k)
            k += 1
            existentes[a["caminho"]] = mid
            midias.append({"id": mid, "caminho": a["caminho"], "nome": a["nome"], "duracao": a["duracao"],
                           "video": False, "audios": 1, "sfx": True})
        clipes.append({"id": novo_id("c", len(clipes)), "midia": mid, "tipo": "audio", "trilha": trilha,
                       "inicio": round(ini, 4), "entrada": round(entrada, 4), "saida": round(entrada + dur, 4),
                       "faixa": 1, "ganho": p["ganho"], "mudo": False, "sfx": True,
                       "motivo": f"{ev['motivo']} · {ev['categoria']}"})
    # dois efeitos não se atropelam na trilha: o de trás é aparado
    clipes.sort(key=lambda c: c["inicio"])
    for a, b in zip(clipes, clipes[1:]):
        fim_a = a["inicio"] + a["saida"] - a["entrada"]
        if fim_a > b["inicio"]:
            a["saida"] = round(max(a["entrada"] + MIN_CLIPE, a["saida"] - (fim_a - b["inicio"])), 4)
    return {"trilha": trilha, "trilhas_audio": trilhas_audio, "midias": midias, "clipes": clipes,
            "faltou": sorted(faltou), "params": p}


# ------------------------------------------------------------------ job de export
def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def texto(msg):
    _emit({"type": "assistant", "message": {"content": [{"type": "text", "text": msg}]}})


def passo(desc):
    _emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"description": desc}}]}})


def fim(ok, msg, inicio):
    _emit({"type": "result", "subtype": "success" if ok else "error", "is_error": not ok, "result": msg,
           "total_cost_usd": 0.0, "num_turns": 0, "duration_ms": int((time.time() - inicio) * 1000)})


def video_da_legenda(legenda_dir, doc, destino):
    """Os PNGs da legenda queimada (um por mudança de tela, `lista.ffconcat` com as durações) viram um
    `.mov` qtrle com alfa, do tamanho e fps da sequência e cortado na duração dela. É uma passada à parte de
    propósito: com o demuxer concat de PNG direto no grafo grande, o ffmpeg 8.1 entra em livelock numa thread
    de filtro (100% de CPU, sem progresso, ignora SIGTERM) na maioria das vezes; a passada sozinha leva <1 s
    e o `.mov` como entrada comum nunca travou."""
    D = max(duracao(doc), MIN_CLIPE)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0",
           "-i", str(Path(legenda_dir) / "lista.ffconcat"),
           "-vf", f"fps={_f(doc['config']['fps'])},trim=duration={_f(D)},setpts=PTS-STARTPTS,format=argb",
           "-c:v", "qtrle", str(destino)]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=600)
    if r.returncode != 0 or not Path(destino).is_file():
        raise Falha("não deu para montar a legenda queimada:\n" + r.stderr.strip()[-600:])
    return Path(destino)


def renderizar(a):
    doc = normalizar(a["doc"])
    if not doc["clipes"]:
        raise Falha("a sequência está vazia")
    caminhos, graficos = {}, {}
    for c in clipes_no_render(doc):
        if c["tipo"] == "grafico":
            pasta = Path((a.get("graficos") or {}).get(c["id"]) or "")
            if not (pasta / "pronto.json").is_file():
                raise Falha(f"o gráfico {c['grafico']['def'].get('nome') or c['id']} não foi renderizado")
            graficos[c["id"]] = str(pasta)
            continue
        p = (a.get("midias") or {}).get(c["midia"])
        if not p or not Path(p).is_file():
            nome = next((m["nome"] for m in doc["midias"] if m["id"] == c["midia"]), c["midia"])
            raise Falha(f"mídia offline: {nome} (reconecte o arquivo na sequência)")
        caminhos[c["midia"]] = p
    leg = doc["legenda"]
    legenda_dir = None
    if leg["ativa"] and leg["queimar"]:
        legenda_dir = Path(a.get("legenda_dir") or "")
        if not (legenda_dir / "lista.ffconcat").is_file():
            raise Falha("a legenda queimada não foi renderizada (o estúdio faz isso ao exportar)")
    for a_id, b_id in sobreposicoes(doc):
        texto(f"aviso: os clipes {a_id} e {b_id} se sobrepõem na mesma trilha.")
    entrega = Path(a["entrega"])
    montando = entrega.with_name("." + entrega.name + ".montando")
    shutil.rmtree(montando, ignore_errors=True)
    montando.mkdir(parents=True)
    legenda = None
    temp = Path(tempfile.mkdtemp(prefix="clientela-render-"))   # o WAV da mixagem e o .mov da legenda
    try:
        D = duracao(doc)
        cfg = doc["config"]
        passo(f"Renderizando {cfg['largura']}x{cfg['altura']} a {_f(cfg['fps'])} fps · {D:.1f} s")
        saida = montando / a["nome"]
        if legenda_dir:
            legenda = temp / "legenda.mov"
            video_da_legenda(legenda_dir, doc, legenda)
        texto("Mixando o áudio")
        audio = temp / "audio.wav"
        r = subprocess.run(comando_audio(doc, caminhos, audio), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           errors="replace", timeout=3600)
        if r.returncode != 0 or not audio.is_file():
            raise Falha("ffmpeg falhou no áudio:\n" + r.stderr.strip()[-600:])
        proc = subprocess.Popen(comando(doc, caminhos, saida, graficos, legenda, audio=audio), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, errors="replace")
        resto, ultimo = [], -1
        buf = ""
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                break
            if ch in "\r\n":
                m = re.search(r"time=(\d+):(\d+):([\d.]+)", buf)
                if m and D:
                    t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    pct = int(min(99, t / D * 100) // 10 * 10)
                    if pct > ultimo:
                        ultimo = pct
                        texto(f"Render: {pct}%")
                elif buf.strip():
                    resto.append(buf.strip())
                buf = ""
            else:
                buf += ch
        if proc.wait() != 0:
            raise Falha("ffmpeg falhou:\n" + "\n".join(resto[-8:]))
        (montando / f"{doc['nome']}{EXTENSAO}").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        if leg["ativa"]:   # legenda solta para o Premiere, mesmo quando também vai queimada
            cues = cues_da_sequencia(doc)
            if cues:
                (montando / "Legenda.ass").write_text(ass_da_sequencia(doc), encoding="utf-8")
                (montando / "Legenda.srt").write_text(autocorte.gerar_srt(cues, maiusculas=leg["maiusculas"]), encoding="utf-8")
                texto(f"Legenda: {len(cues)} telas ({leg['modo']}){' · queimada no vídeo' if legenda_dir else ''}")
        if entrega.exists():
            raise Falha(f"{entrega.name} já existe")
        montando.rename(entrega)
    except BaseException:
        shutil.rmtree(montando, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return f"{entrega.name}/{a['nome']} · {D:.1f} s"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    inicio = time.time()
    if "--args" not in argv:
        print(__doc__)
        return 2
    a = json.loads(Path(argv[argv.index("--args") + 1]).read_text(encoding="utf-8"))
    _emit({"type": "system", "subtype": "auto", "modo": "sequencia"})
    try:
        msg = renderizar(a)
    except (Falha, ValueError) as exc:
        fim(False, str(exc), inicio)
        return 1
    except Exception as exc:  # o painel precisa de uma linha result, senão o job fecha sem motivo
        fim(False, f"erro inesperado: {exc.__class__.__name__}: {exc}", inicio)
        return 1
    fim(True, msg, inicio)
    return 0


if __name__ == "__main__":
    sys.exit(main())
