#!/usr/bin/env python3
"""Render: 120 fps → mescla 4 subquadros em 30 fps (motion blur contínuo) → ProRes 4444 + alpha, com corte das telas múltiplas.

Lê {trabalho}/jobs.json, uma lista de:
  {"comp": "ins-3-debate-atras", "inicio": 11.3, "nome": "3 Debate (camada ATRAS)"}
  {"comp": "ins-5-antes-agora", "inicio": 23.1, "nome": "5",
   "partes": [[0.0, 6.1, "5a ANTES"], [6.1, 16.9, "5b AGORA (com transicao)"]]}
  {"comp": "legendas", "inicio": 0, "nome": "Legendas (video inteiro)", "fps": 30}     ← sem subquadros
"inicio" = entrada no clipe (s). Nome do arquivo = timecode MM-SS-FF (30 fps) da entrada do trecho + nome.

uso: python3 kit/render.py [comp …]     (sem argumento: todos)
     python3 kit/render.py --lista [comp …]  (só mostra os nomes que vão sair)
Saída: {trabalho}/renders/*.mov
"""
import json
import subprocess
import sys
from pathlib import Path

TRAB = Path(__file__).resolve().parent.parent
HF = TRAB / "hf"
OUT = TRAB / "renders"
TMP = OUT / "brutos"
JOBS = json.loads((TRAB / "jobs.json").read_text(encoding="utf-8"))


def tc(sec):
    f = round(sec * 30)
    return f"{f // 1800:02d}-{(f // 30) % 60:02d}-{f % 30:02d}"


def saidas(job):
    if not job.get("partes"):
        return [(None, None, f"{tc(job['inicio'])} - {job['nome']}.mov")]
    return [(t0, t1, f"{tc(job['inicio'] + t0)} - {label}.mov") for t0, t1, label in job["partes"]]


def blend(src, dst, sub, t0=None, t1=None):
    vf = f"tmix=frames={sub}:weights='{' '.join(['1'] * sub)}',framestep={sub}" if sub > 1 else "null"
    if t0 is not None:
        vf += f",trim=start={t0}:end={t1},setpts=PTS-STARTPTS"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vf", vf, "-r", "30",
                    "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-alpha_bits", "16",
                    "-vendor", "apl0", str(dst)], check=True)


def main():
    args = sys.argv[1:]
    if "--lista" in args:
        comps = [x for x in args if x != "--lista"]
        for job in JOBS:
            if comps and job["comp"] not in comps:
                continue
            for _, _, nome in saidas(job):
                print(nome)
        return
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    for job in JOBS:
        comp = job["comp"]
        if args and comp not in args:
            continue
        fps = int(job.get("fps", 120))
        sub = max(1, fps // 30)
        raw = TMP / f"{comp}.mov"
        r = subprocess.run(["npx", "hyperframes", "render", ".", "-c", f"{comp}.html", "--format", "mov", "-f", str(fps),
                            "-o", str(raw)], cwd=HF, capture_output=True, text=True)
        if r.returncode != 0:
            print((r.stdout + r.stderr)[-3000:])
            sys.exit(f"falhou o render de {comp}")
        for t0, t1, nome in saidas(job):
            blend(raw, OUT / nome, sub, t0, t1)
            print("ok", nome)
        raw.unlink()  # o bruto a 120 fps é grande; o MOV final já está em renders/


if __name__ == "__main__":
    main()
