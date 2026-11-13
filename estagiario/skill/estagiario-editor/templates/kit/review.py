#!/usr/bin/env python3
"""Revisão em movimento: monta as camadas como na edição (vídeo → ATRÁS → recorte → FRENTE) e gera uma folha.

uso:
  python3 kit/review.py camadas <base> <inicio> <dur> [fps] [cols] [largura]   (usa hf/_check/<base>-atras e -frente)
  python3 kit/review.py unica   <nome> <inicio> <dur> [fps] [cols] [largura]
  ou com --at t1,t2,... no lugar de fps (quadros específicos, maiores)

<inicio> = início do insert no clipe (s); os tempos da folha são locais (0 = início do insert).
Saída: {trabalho}/review/<seq|at>_<nome>.jpg (abra a imagem e olhe de verdade).
"""
import glob
import json
import re
import subprocess
import sys
from pathlib import Path

TRAB = Path(__file__).resolve().parent.parent
HF = TRAB / "hf"
JOB = json.loads((TRAB / "job.json").read_text(encoding="utf-8"))
VID = Path(JOB["video"]["caminho"])
LIMITE = JOB["video"]["duracao"] - 0.05
CUT = Path(JOB["recorte"]) if JOB.get("recorte") else TRAB / "recorte.mov"

mode, name, off, dur = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
rest = sys.argv[5:]
if rest and rest[0] == "--at":
    times = [float(x) for x in rest[1].split(",")]
    cols, w = int(rest[2]) if len(rest) > 2 else len(times), int(rest[3]) if len(rest) > 3 else 405
    tag = "at"
else:
    fps = float(rest[0]) if rest else 5
    cols = int(rest[1]) if len(rest) > 1 else 8
    w = int(rest[2]) if len(rest) > 2 else 240
    times = [round(i / fps, 3) for i in range(int(dur * fps) + 1) if i / fps <= dur - 0.02]
    tag = "seq"


def snap(comp):
    chk = HF / "_check" / comp
    if not (chk / "index.html").exists():
        sys.exit(f"falta hf/_check/{comp}/index.html (rode o gerador)")
    sd = chk / ".hyperframes/artifacts/snapshots"
    subprocess.run(["rm", "-rf", str(sd)])
    subprocess.run(["npx", "hyperframes", "snapshot", "--at", ",".join(map(str, times)), "--no-end"], cwd=chk,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    out = {}
    for f in glob.glob(str(sd / "*/frame-*-at-*.png")):
        out[round(float(re.search(r"at-([\d.]+)s", f).group(1)), 3)] = f
    return out


com_recorte = mode == "camadas" and CUT.exists()
if mode == "camadas" and not com_recorte:
    print("aviso: sem recorte (recorte.mov): a folha mostra as camadas sem a pessoa entre elas")
layers = [snap(f"{name}-atras"), snap(f"{name}-frente")] if mode == "camadas" else [snap(name)]
od = TRAB / "review" / tag / name
subprocess.run(["rm", "-rf", str(od)])
od.mkdir(parents=True, exist_ok=True)
for t in times:
    a = f"{min(t + off, LIMITE):.3f}"
    if mode == "camadas" and com_recorte:
        ins = ["-ss", a, "-i", str(VID), "-i", layers[0][t], "-ss", a, "-i", str(CUT), "-i", layers[1][t]]
        fc = f"[0][1]overlay[a];[a][2]overlay[b];[b][3]overlay,scale={w}:-2"
    elif mode == "camadas":
        ins = ["-ss", a, "-i", str(VID), "-i", layers[0][t], "-i", layers[1][t]]
        fc = f"[0][1]overlay[a];[a][2]overlay,scale={w}:-2"
    else:
        ins = ["-ss", a, "-i", str(VID), "-i", layers[0][t]]
        fc = f"[0][1]overlay,scale={w}:-2"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *ins, "-filter_complex", fc, "-frames:v", "1",
                    str(od / f"{t:06.2f}.png")], check=True)
rows = -(-len(times) // cols)
sheet = TRAB / "review" / f"{tag}_{name}.jpg"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "1", "-pattern_type", "glob", "-i", str(od / "*.png"),
                "-vf", f"tile={cols}x{rows}:padding=4:color=white", "-frames:v", "1", "-q:v", "3", str(sheet)], check=True)
print(sheet, len(times), "quadros:", ", ".join(f"{t:.2f}" for t in times))
