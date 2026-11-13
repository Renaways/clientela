#!/usr/bin/env python3
"""Cores dominantes de imagens e vídeos (quadros espalhados) → hex com participação, + uma amostra em PNG.

É uma pista, não a verdade: o vídeo traz pele, cenário e luz. Cruze com o manual, os logos e os grafismos das folhas.
Para isolar o grafismo, prefira renders com alpha, logos e telas cheias (o alpha é ignorado na contagem).

uso: python3 paleta.py --saida <perfil>/.analise [--n 8] [--nome paleta] <arquivo> [<arquivo> …]
"""
import argparse
import json
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from PIL import Image

VIDEO = {".mp4", ".mov", ".m4v", ".mxf", ".webm"}


def quadros(v, n, tmp):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(v)],
                         capture_output=True, text=True).stdout
    dur = float(json.loads(out)["format"]["duration"])
    pat = Path(tmp) / (Path(v).stem + "-%03d.png")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(v), "-vf", f"fps={n / max(dur, 0.1):.5f},scale=240:-2",
                    "-frames:v", str(n), str(pat)], check=False)
    return sorted(Path(tmp).glob(Path(v).stem + "-*.png"))


def conta(img, cnt):
    im = img.convert("RGBA")
    im.thumbnail((240, 240))
    rgb = Image.new("RGB", im.size)
    rgb.paste(im, mask=im.split()[3])
    q = rgb.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    alpha = im.split()[3].load()
    px = q.load()
    for y in range(im.height):
        for x in range(im.width):
            if alpha[x, y] < 128:
                continue
            i = px[x, y]
            r, g, b = pal[3 * i:3 * i + 3]
            cnt[(r // 8 * 8, g // 8 * 8, b // 8 * 8)] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arquivos", nargs="+")
    ap.add_argument("--saida", required=True)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--nome", default="paleta")
    a = ap.parse_args()
    out = Path(a.saida)
    out.mkdir(parents=True, exist_ok=True)
    cnt = Counter()
    with tempfile.TemporaryDirectory() as tmp:
        for f in a.arquivos:
            imgs = quadros(f, a.n, tmp) if Path(f).suffix.lower() in VIDEO else [Path(f)]
            for i in imgs:
                try:
                    with Image.open(i) as im:
                        conta(im, cnt)
                except OSError:
                    print("pulei (ilegível):", i)
    total = sum(cnt.values()) or 1
    top = [(f"#{r:02X}{g:02X}{b:02X}", round(100 * c / total, 1)) for (r, g, b), c in cnt.most_common(16)]
    (out / f"{a.nome}.json").write_text(json.dumps(top, indent=1), encoding="utf-8")
    sw = Image.new("RGB", (80 * len(top), 80), "white")
    for i, (h, _) in enumerate(top):
        sw.paste(Image.new("RGB", (76, 80), h), (80 * i, 0))
    sw.save(out / f"{a.nome}.png")
    print("\n".join(f"{h}  {p}%" for h, p in top))
    print("ok", out / f"{a.nome}.png")


if __name__ == "__main__":
    main()
