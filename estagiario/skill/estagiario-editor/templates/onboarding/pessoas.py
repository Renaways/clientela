#!/usr/bin/env python3
"""pessoas.csv do perfil: CSV de verdade (módulo csv), cabeçalho `nome,cargo`, cargo com vírgula entre aspas.

  python3 pessoas.py ler <qualquer.csv>               → JSON [{nome, cargo}] (detecta colunas e separador ; ou ,)
  python3 pessoas.py escrever <perfil> <lista.json>   → mescla com o pessoas.csv existente e grava

A mescla nunca apaga: quem já está no CSV fica, e o cargo que já existe vence o novo (o usuário pode ter corrigido).
Escrita atômica (temporário + replace), com o anterior em pessoas.csv.bak, como o app faz.
"""
import csv
import io
import json
import os
import sys
import unicodedata
from pathlib import Path

COL_NOME = ("nome", "nomes", "name", "names", "pessoa", "pessoas", "nome completo")
COL_CARGO = ("cargo", "funcao", "função", "title", "role", "cargo/funcao", "cargo/função", "descricao", "descrição")


def norm(s):
    s = unicodedata.normalize("NFD", (s or "").strip().lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def ler(path):
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(raw), dialect))
    if not rows:
        return []
    head = [norm(h) for h in rows[0]]
    ni = next((i for i, h in enumerate(head) if h in COL_NOME), None)
    ci = next((i for i, h in enumerate(head) if norm(h) in [norm(c) for c in COL_CARGO]), None)
    corpo = rows[1:]
    if ni is None:  # sem cabeçalho reconhecível: 1ª coluna nome, 2ª cargo
        ni, ci, corpo = 0, 1, rows
    out = []
    for r in corpo:
        nome = r[ni].strip() if len(r) > ni else ""
        if nome:
            out.append({"nome": nome, "cargo": (r[ci].strip() if ci is not None and len(r) > ci else "")})
    return out


def escrever(perfil, lista_json):
    dst = Path(perfil) / "pessoas.csv"
    atuais = ler(dst) if dst.exists() else []
    novos = json.loads(Path(lista_json).read_text(encoding="utf-8"))
    idx = {norm(p["nome"]): p for p in atuais}
    for p in novos:
        k = norm(p.get("nome"))
        if not k:
            continue
        if k in idx:
            if not idx[k].get("cargo") and p.get("cargo"):
                idx[k]["cargo"] = p["cargo"].strip()
        else:
            idx[k] = {"nome": p["nome"].strip(), "cargo": (p.get("cargo") or "").strip()}
            atuais.append(idx[k])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["nome", "cargo"])
    for p in atuais:
        w.writerow([p["nome"], p.get("cargo", "")])
    tmp = dst.with_suffix(".csv.tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    if dst.exists():
        dst.replace(dst.with_suffix(".csv.bak"))
    os.replace(tmp, dst)
    print(f"ok {dst}: {len(atuais)} pessoas")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "ler":
        print(json.dumps(ler(sys.argv[2]), ensure_ascii=False, indent=1))
    elif len(sys.argv) >= 4 and sys.argv[1] == "escrever":
        escrever(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
