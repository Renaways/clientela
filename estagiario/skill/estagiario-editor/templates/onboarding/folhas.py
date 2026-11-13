#!/usr/bin/env python3
"""Folhas de contato de vídeos (N quadros espalhados) para OLHAR o estilo: tipografia, cores, GC, legenda, ritmo.

uso: python3 folhas.py --saida <perfil>/.analise/folhas [--n 12] [--cols 6] <video> [<video> …]
Vídeos com alpha são compostos sobre cinza médio, para o grafismo aparecer.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path


def dur_alpha(v):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=pix_fmt:format=duration",
                          "-of", "json", str(v)], capture_output=True, text=True).stdout
    d = json.loads(out)
    return float(d["format"]["duration"]), str((d.get("streams") or [{}])[0].get("pix_fmt", "")).startswith("yuva")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--saida", required=True)
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--cols", type=int, default=6)
    a = ap.parse_args()
    out = Path(a.saida)
    out.mkdir(parents=True, exist_ok=True)
    rows = -(-a.n // a.cols)
    for v in a.videos:
        try:
            dur, alpha = dur_alpha(v)
        except (ValueError, KeyError):
            print("pulei (ilegível):", v)
            continue
        nome = re.sub(r"[^\w.-]+", "_", Path(v).stem)[:80] + ".jpg"
        vf = f"fps={a.n / max(dur, 0.1):.5f},scale=320:-2"
        if alpha:  # grafismo com alpha: sobre cinza, senão some no preto
            vf = f"{vf},format=rgba,split[a][b];[a]drawbox=c=0x808080:t=fill[bg];[bg][b]overlay"
        vf += f",tile={a.cols}x{rows}:padding=4:color=white"
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(v), "-vf", vf, "-frames:v", "1", "-q:v", "3",
                            str(out / nome)], capture_output=True, text=True)
        print(("ok " + str(out / nome)) if r.returncode == 0 else f"falhou {v}: {r.stderr[-200:]}")


if __name__ == "__main__":
    main()
