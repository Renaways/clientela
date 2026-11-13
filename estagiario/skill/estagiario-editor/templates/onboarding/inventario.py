#!/usr/bin/env python3
"""Inventário das pastas de um cliente para o onboarding: o que existe e onde (só lê; não altera nada).

Lista vídeos (ffprobe), fontes (família/peso/itálico lidos do próprio arquivo), imagens com alpha (candidatas a logo),
texturas, projetos (.aep/.prproj/.mogrt…), manuais (PDF), planilhas/CSVs e entregas aprovadas (Motion - … vN, "final", "aprovad").

uso: python3 inventario.py --saida <perfil>/.analise <pasta> [<pasta> …]
Saída: inventario.md (para ler) + inventario.json (para scripts).
"""
import argparse
import json
import os
import re
import struct
import subprocess
from pathlib import Path

VIDEO = {".mp4", ".mov", ".m4v", ".mxf", ".webm", ".avi", ".mkv"}
FONTE = {".otf", ".ttf", ".ttc", ".woff", ".woff2"}
IMAGEM = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".gif", ".svg"}
VETOR = {".ai", ".eps", ".pdf", ".svg"}
PROJETO = {".aep", ".aet", ".prproj", ".mogrt", ".psd", ".fig", ".sketch", ".xd", ".indd", ".jsx"}
DADOS = {".csv", ".xlsx", ".xls", ".numbers", ".txt", ".md", ".docx", ".doc", ".pages", ".srt", ".vtt", ".json"}
PULAR = {"Adobe Premiere Pro Auto-Save", "Adobe After Effects Auto-Save", "node_modules", "_Estagiário", "_Estagiário",
         "Media Cache Files", "Media Cache", "Peak Files"}
APROVADO = re.compile(r"(^Motion - .+ v\d+$)|aprovad|final|entrega|render|export", re.I)
LOGO = re.compile(r"logo|marca|simbolo|símbolo|icone|ícone|assinatura|brand|selo", re.I)
TEXTURA = re.compile(r"textur|grao|grão|grain|noise|papel|paper|ruido|ruído|overlay", re.I)
MANUAL = re.compile(r"manual|brand|idv|identidade|guide|guia|book", re.I)
MAX_PROBE = 400


def font_info(path):
    """Família, subfamília, peso (OS/2) e itálico lidos da tabela 'name' sem dependências."""
    try:
        data = Path(path).read_bytes()
        off = struct.unpack(">I", data[12:16])[0] if data[:4] == b"ttcf" else 0
        n = struct.unpack(">H", data[off + 4:off + 6])[0]
        tabs = {}
        for i in range(n):
            r = off + 12 + 16 * i
            tabs[data[r:r + 4]] = struct.unpack(">II", data[r + 8:r + 16])
        names = {}
        if b"name" in tabs:
            t, _ = tabs[b"name"]
            _, count, soff = struct.unpack(">HHH", data[t:t + 6])
            for i in range(count):
                pid, eid, lid, nid, ln, so = struct.unpack(">HHHHHH", data[t + 6 + 12 * i:t + 18 + 12 * i])
                raw = data[t + soff + so:t + soff + so + ln]
                if pid == 3 or pid == 0:
                    s = raw.decode("utf-16-be", "replace")
                elif pid == 1:
                    s = raw.decode("mac_roman", "replace")
                else:
                    continue
                if nid not in names or pid == 3:
                    names[nid] = s
        peso, italico = None, None
        if b"OS/2" in tabs:
            t, _ = tabs[b"OS/2"]
            peso = struct.unpack(">H", data[t + 4:t + 6])[0]
            italico = bool(struct.unpack(">H", data[t + 62:t + 64])[0] & 1)
        sub = names.get(17) or names.get(2) or ""
        if italico is None:
            italico = bool(re.search(r"italic|oblique", sub, re.I))
        return {"familia": names.get(16) or names.get(1), "subfamilia": sub, "peso": peso, "italico": italico,
                "nome_completo": names.get(4)}
    except Exception as e:  # fonte corrompida ou formato sem tabela (woff comprimido)
        return {"erro": str(e)[:80]}


def probe(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=codec_name,profile,width,height,r_frame_rate,pix_fmt:format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=20).stdout
        d = json.loads(out)
        s = (d.get("streams") or [{}])[0]
        num, den = (int(x) for x in s.get("r_frame_rate", "0/1").split("/"))
        return {"codec": s.get("codec_name"), "perfil": s.get("profile"), "w": s.get("width"), "h": s.get("height"),
                "fps": round(num / den, 2) if den else None, "alpha": str(s.get("pix_fmt", "")).startswith(("yuva", "rgba", "argb", "bgra")),
                "dur": round(float(d.get("format", {}).get("duration") or 0), 2)}
    except Exception as e:
        return {"erro": str(e)[:80]}


def img_info(path):
    if path.suffix.lower() == ".svg":
        return {"vetor": True}
    try:
        from PIL import Image
        with Image.open(path) as im:
            alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            return {"w": im.width, "h": im.height, "alpha": alpha}
    except Exception as e:
        return {"erro": str(e)[:80]}


def walk(raiz):
    for d, dirs, files in os.walk(raiz):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in PULAR)
        for f in sorted(files):
            if not f.startswith("."):
                yield Path(d) / f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pastas", nargs="+")
    ap.add_argument("--saida", required=True)
    a = ap.parse_args()
    out = Path(a.saida)
    out.mkdir(parents=True, exist_ok=True)
    inv = {"pastas": a.pastas, "videos": [], "fontes": [], "logos": [], "texturas": [], "imagens": 0, "projetos": [],
           "manuais": [], "dados": [], "entregas_aprovadas": set()}
    probed = 0
    for raiz in a.pastas:
        for p in walk(raiz):
            ext = p.suffix.lower()
            rel = str(p)
            if any(APROVADO.search(part) for part in p.parent.parts[-3:]):
                inv["entregas_aprovadas"].add(str(p.parent))
            if ext in VIDEO:
                item = {"arquivo": rel, "mb": round(p.stat().st_size / 1e6, 1)}
                if probed < MAX_PROBE:
                    item.update(probe(p))
                    probed += 1
                inv["videos"].append(item)
            elif ext in FONTE:
                inv["fontes"].append({"arquivo": rel, **font_info(p)})
            elif ext in IMAGEM:
                inv["imagens"] += 1
                if LOGO.search(p.name) or LOGO.search(p.parent.name):
                    inv["logos"].append({"arquivo": rel, **img_info(p)})
                elif TEXTURA.search(p.name) or TEXTURA.search(p.parent.name):
                    inv["texturas"].append({"arquivo": rel, **img_info(p)})
            elif ext in PROJETO:
                inv["projetos"].append(rel)
            if ext in VETOR and MANUAL.search(p.name):
                inv["manuais"].append(rel)
            elif ext in VETOR and (LOGO.search(p.name) or LOGO.search(p.parent.name)) and ext != ".svg":
                inv["logos"].append({"arquivo": rel, "vetor": True})
            elif ext in DADOS:
                inv["dados"].append(rel)
    inv["entregas_aprovadas"] = sorted(inv["entregas_aprovadas"])
    (out / "inventario.json").write_text(json.dumps(inv, ensure_ascii=False, indent=1), encoding="utf-8")

    L = [f"# Inventário · {', '.join(a.pastas)}", "",
         f"{len(inv['videos'])} vídeos · {len(inv['fontes'])} fontes · {len(inv['logos'])} logos candidatos · "
         f"{len(inv['texturas'])} texturas candidatas · {inv['imagens']} imagens · {len(inv['projetos'])} projetos · "
         f"{len(inv['manuais'])} manuais · {len(inv['dados'])} planilhas/textos", ""]
    L += ["## Manuais / brandbooks (leia primeiro)", *([f"- {m}" for m in inv["manuais"]] or ["- nenhum"]), ""]
    L += ["## Entregas aprovadas / finais (referência de padrão)", *([f"- {d}" for d in inv["entregas_aprovadas"]] or ["- nenhuma"]), ""]
    L += ["## Fontes", "| família | subfamília | peso | itálico | arquivo |", "|---|---|---|---|---|"]
    L += [f"| {f.get('familia')} | {f.get('subfamilia')} | {f.get('peso')} | {'sim' if f.get('italico') else ''} | {f['arquivo']} |"
          for f in inv["fontes"]]
    L += ["", "## Logos candidatos (alpha = transparente)", *[f"- {x['arquivo']} — "
          + ("vetor" if x.get("vetor") else f"{x.get('w')}×{x.get('h')}{' alpha' if x.get('alpha') else ' SEM alpha'}") for x in inv["logos"]]]
    L += ["", "## Texturas candidatas", *[f"- {x['arquivo']} — {x.get('w')}×{x.get('h')}" for x in inv["texturas"]]]
    L += ["", "## Vídeos", "| arquivo | dur | formato | fps | codec | alpha |", "|---|---|---|---|---|---|"]
    L += [f"| {v['arquivo']} | {v.get('dur', '')} | {v.get('w', '')}×{v.get('h', '')} | {v.get('fps', '')} | "
          f"{v.get('codec', '')} {v.get('perfil') or ''} | {'sim' if v.get('alpha') else ''} |" for v in inv["videos"]]
    L += ["", "## Projetos", *[f"- {p}" for p in inv["projetos"]], "", "## Planilhas e textos", *[f"- {d}" for d in inv["dados"]]]
    (out / "inventario.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"ok {out / 'inventario.md'}: {len(inv['videos'])} vídeos, {len(inv['fontes'])} fontes, {len(inv['logos'])} logos, "
          f"{len(inv['manuais'])} manuais, {len(inv['entregas_aprovadas'])} pastas aprovadas/finais")


if __name__ == "__main__":
    main()
