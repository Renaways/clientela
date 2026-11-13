#!/usr/bin/env python3
"""Confere e copia os MOVs de {trabalho}/renders/ para {entrega}/ (Motion - <Tarefa> vN), no padrão do contrato.

Recusa tudo (não copia nada) se algum arquivo estiver fora do padrão:
nome MM-SS-FF - …, ProRes 4444, com alpha, 30 fps, e sem fundo: um quadro 100% opaco só passa com "(tela cheia)"
no nome (o usuário quer só os elementos; fundo próprio, só quando as observações pedem).
Nunca reaproveita uma pasta de entrega que não foi criada por este trabalho.

uso:
  python3 kit/entregar.py [--recorte <recorte.mov|.webm> --pessoa <Nome>] [arquivos.mov …]
  (sem arquivos: todos os .mov de renders/)

  ajuste de um elemento (references/ajuste.md): os arquivos atuais dele vão para {entrega}/_anteriores/<data>/
  e só os arquivos passados entram no lugar. <elemento> é o número ("3", "5a") ou o nome sem qualificadores:
  python3 kit/entregar.py --substituir 3 "renders/00-58-00 - 3 Sintomas.mov"
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TRAB = Path(__file__).resolve().parent.parent
JOB_PATH = TRAB / "job.json"
JOB = json.loads(JOB_PATH.read_text(encoding="utf-8"))
ENTREGA = Path(JOB["entrega"])
MARCA = ".estagiario-origem"  # arquivo oculto: o painel ignora, e ele prova que a pasta é deste trabalho
NOME = re.compile(r"^(?P<tc>\d{2}-\d{2}-\d{2})\s*-\s*(?P<nome>.+?)(?:\s*\((?P<qual>[^()]*)\))?$")
QUAIS = {"camada atras", "camada frente", "tela cheia", "com transicao", "video inteiro", "alpha"}


def probe(f):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,profile,pix_fmt,r_frame_rate,width,height", "-of", "json", str(f)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


def conferir(f):
    erros = []
    m = NOME.match(f.stem)
    if f.suffix.lower() != ".mov" or not m:
        return [f"{f.name}: nome fora do padrão 'MM-SS-FF - <n> <título> (<qualificadores>).mov'"]
    for q in [q.strip().lower() for q in (m.group("qual") or "").split(",") if q.strip()]:
        if q not in QUAIS:
            erros.append(f"{f.name}: qualificador '{q}' desconhecido (use {', '.join(sorted(QUAIS))})")
    if re.search(r"[áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]", m.group("qual") or ""):
        erros.append(f"{f.name}: qualificadores sem acento")
    s = probe(f)
    if s.get("codec_name") != "prores" or "4444" not in str(s.get("profile", "")):
        erros.append(f"{f.name}: {s.get('codec_name')} {s.get('profile')} (precisa ProRes 4444)")
    if not str(s.get("pix_fmt", "")).startswith("yuva"):
        erros.append(f"{f.name}: sem alpha ({s.get('pix_fmt')})")
    if s.get("r_frame_rate") != "30/1":
        erros.append(f"{f.name}: {s.get('r_frame_rate')} fps (precisa 30)")
    v = JOB["video"]
    if (s.get("width"), s.get("height")) != (v["largura"], v["altura"]):
        erros.append(f"{f.name}: {s.get('width')}×{s.get('height')} (o vídeo é {v['largura']}×{v['altura']})")
    qual = (m.group("qual") or "").lower()
    if not erros and "tela cheia" not in qual and "video inteiro" not in qual and opaco(f):
        erros.append(f"{f.name}: tem quadro com fundo (100% opaco). Entregue só os elementos sobre transparência; "
                     "fundo próprio só se as observações pedirem, e aí o nome leva \"(tela cheia)\"")
    return erros


def opaco(f):
    """Algum quadro (amostra de 2 por segundo) sem nenhum pixel transparente, ou seja, com fundo."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-vf",
                        "fps=2,alphaextract,format=gray,scale=64:-1:flags=area,signalstats,"
                        "metadata=print:key=lavfi.signalstats.YMIN:file=-", "-f", "null", "-"],
                       capture_output=True, text=True)
    return any(float(x) >= 250 for x in re.findall(r"YMIN=([\d.]+)", r.stdout + r.stderr))


def _elemento(nome):
    """'3 Sintomas (camada ATRAS)' → ('3', '3 sintomas'): número e nome sem qualificadores."""
    m = NOME.match(Path(nome).stem)
    if not m:
        return None, None
    base = m.group("nome").strip()
    return base.split()[0].lower(), base.lower()


def substituir(alvos):
    """Move para _anteriores/<data>/ os arquivos atuais dos elementos `alvos` (número ou nome)."""
    alvos = {a.strip().lower() for a in alvos}
    velhos = [f for f in sorted(ENTREGA.glob("*.mov"))
              if not f.stem.lower().split(" - ", 1)[-1].startswith("recorte") and alvos & set(_elemento(f.name))]
    if not velhos:
        sys.exit(f"--substituir: nenhum arquivo de {', '.join(sorted(alvos))} em {ENTREGA}")
    dst = ENTREGA / "_anteriores" / datetime.now().strftime("%Y-%m-%d %H%M%S")
    dst.mkdir(parents=True)
    for f in velhos:
        shutil.move(str(f), dst / f.name)
        print("guardado", f"_anteriores/{dst.name}/{f.name}")


def preparar_pasta():
    if ENTREGA.exists():
        marca = ENTREGA / MARCA
        if not marca.exists() or marca.read_text(encoding="utf-8").strip() != str(TRAB):
            sys.exit(f"RECUSADO: {ENTREGA} já existe e não foi criada por este trabalho. Uma pasta Motion - … vN "
                     "nunca é reaproveitada: registre no DECISOES.md e pare.")
    else:
        ENTREGA.mkdir(parents=False)
        (ENTREGA / MARCA).write_text(str(TRAB), encoding="utf-8")


def recorte(src, pessoa):
    dst = ENTREGA / f"00-00-00 - Recorte {pessoa} (alpha, video inteiro).mov"
    s = probe(src) if Path(src).suffix.lower() == ".mov" else {}
    if s.get("codec_name") == "prores" and "4444" in str(s.get("profile")) and str(s.get("pix_fmt")).startswith("yuva") \
            and s.get("r_frame_rate") == "30/1":
        shutil.copy2(src, dst)  # o remove-background já sai em ProRes 4444: copiar é idêntico e não leva minutos
    else:
        dec = ["-c:v", "libvpx-vp9"] if Path(src).suffix.lower() == ".webm" else []  # decodifica o alpha do webm
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *dec, "-i", str(src), "-r", "30", "-an",
                        "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-alpha_bits", "16",
                        "-vendor", "apl0", str(dst)], check=True)
    JOB["recorte"] = str(src)
    JOB_PATH.write_text(json.dumps(JOB, ensure_ascii=False, indent=1), encoding="utf-8")
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arquivos", nargs="*")
    ap.add_argument("--recorte")
    ap.add_argument("--pessoa")
    ap.add_argument("--substituir", action="append", default=[], metavar="ELEMENTO")
    a = ap.parse_args()
    if a.recorte and not a.pessoa:
        sys.exit("--recorte precisa de --pessoa <Nome> (pessoas.csv, fala ou 'Apresentador')")
    if a.substituir and (not a.arquivos or not ENTREGA.is_dir()):
        sys.exit("--substituir precisa da entrega já feita e dos arquivos novos do elemento (renders/…)")

    files = [Path(f) for f in a.arquivos] or sorted((TRAB / "renders").glob("*.mov"))
    if not files and not a.recorte:
        sys.exit("nada para entregar em renders/")
    erros = [e for f in files for e in conferir(f)]
    if erros:
        print("\n".join(erros))
        sys.exit("RECUSADO: nada foi copiado. Corrija a causa (render/nome) e rode de novo.")

    preparar_pasta()
    if a.substituir:
        substituir(a.substituir)
    for f in files:
        shutil.copy2(f, ENTREGA / f.name)
        print("ok", f.name)
    if a.recorte:
        dst = recorte(a.recorte, a.pessoa)
        for e in conferir(dst):
            print("ERRO", e)
        print("ok", dst.name)
    print(f"Entrega: {ENTREGA}")
    for f in sorted(ENTREGA.iterdir()):
        if not f.name.startswith("."):
            print("  ", f.name)


if __name__ == "__main__":
    main()
