#!/usr/bin/env python3
"""GC (lower third) no estilo do perfil: uma composição por aparição, a partir de {trabalho}/gcs.json.

gcs.json:
  [{"n": 1, "nome": "Fulana de Tal", "cargo": null, "inicio": 2.4}]
  "y" opcional por GC (px de 1080×1920): use quando o GC do perfil cair sobre o rosto/boca neste vídeo.
  cargo null → vem do pessoas.csv do perfil (job.json → perfil.pessoas); sem cargo lá, o GC sai só com o nome.
Layout e tempos: componentes.gc do marca.json (x, y, largura, nome_fundo, cargo_fundo, raio, logo, duracao).
Sem componentes.gc: GC neutro (barra do nome no realce do perfil, barra do cargo no fundo).

uso: python3 kit/gc.py            → hf/gc-<n>.html + entradas "gc-*" no jobs.json (as outras ficam)
Depois de gerar, revise (review.py unica gc-<n> <inicio> <dur>) e ajuste o movimento se o marca.md descrever outro.
"""
import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import COMP, FORMATO, H, JOB, PAPEIS, REALCE, SAFE, TRAB, W, cor, px, write  # noqa: E402

G = {"largura": 452, "raio": 0, "duracao": 4.5, "nome_fundo": REALCE.get("fundo"), "cargo_fundo": "fundo", **(COMP.get("gc") or {})}
if FORMATO == "retrato":
    X, Y = px(G.get("x", SAFE.get("esquerda", 64) + 41)), px(G.get("y", 932))
else:  # paisagem/quadrado: canto inferior esquerdo
    X, Y = f"{W * 0.0625:.0f}px", f"{H * 0.74:.0f}px"


# cor do texto: a do papel, se o perfil definir; senão contraste com a barra (barra no realce → texto do realce)
NOME_TEXTO = cor((PAPEIS.get("gc_nome") or {}).get("cor") or (REALCE.get("texto") if G["nome_fundo"] == REALCE.get("fundo") else "texto"))
CARGO_TEXTO = cor((PAPEIS.get("gc_cargo") or {}).get("cor") or "texto")


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def cargo_de(nome):
    for p in (JOB.get("perfil") or {}).get("pessoas") or []:
        if p["nome"].strip().lower() == nome.strip().lower():
            return p.get("cargo") or ""
    return ""


def gc_html(nome, cargo, dur, y=None):
    logo = G.get("logo")
    logo_html = f'<img id="logo" class="abs" src="assets/logos/{logo}" />' if logo else ""
    cargo_html = f'<div id="cb" class="bar"><span id="ct" class="p-gc-cargo txt">{cargo}</span></div>' if cargo else ""
    css = f"""
      #gc {{ left: {X}; top: {px(y) if y is not None else Y}; display: flex; flex-direction: column; align-items: flex-start; gap: {px(6)}; }}
      .bar {{ min-width: {px(G['largura'])}; padding: 0.3em 1.1em 0.24em; border-radius: {px(float(G.get('raio') or 0))}; white-space: nowrap; }}
      #nb {{ background: {cor(G['nome_fundo'])}; }}
      #cb {{ background: {cor(G['cargo_fundo'])}; }}
      #nt {{ color: {NOME_TEXTO}; }} #ct {{ color: {CARGO_TEXTO}; }}
      .txt {{ display: inline-block; }}
      #linha {{ position: absolute; left: 0; top: -{px(10)}; height: {px(4)}; width: {px(G['largura'])}; background: {cor(G['nome_fundo'])}; transform-origin: 0% 50%; }}
      #logo {{ left: {px(24)}; bottom: 100%; height: {px(61)}; margin-bottom: {px(-4)}; transform-origin: 50% 100%; }}
    """
    body = f"""
      <div id="gc" class="abs">
        <div id="linha"></div>
        {logo_html}
        <div id="nb" class="bar"><span id="nt" class="p-gc-nome txt">{nome}</span></div>
        {cargo_html}
      </div>
    """
    sai = max(dur - 0.72, 1.6)
    js = f"""
      tl.fromTo("#linha", {{ scaleX: 0 }}, {{ scaleX: 1, duration: 0.2, ease: "power2.out" }}, 0);
      tl.to("#linha", {{ opacity: 0, duration: 0.15 }}, 0.35);
      tl.fromTo("#nb", {{ clipPath: "inset(0% 100% 0% 0%)" }}, {{ clipPath: "inset(0% 0% 0% 0%)", duration: 0.4, ease: "power1.out" }}, 0.2);
      tl.fromTo("#nt", {{ opacity: 0, y: 14 }}, {{ opacity: 1, y: 0, duration: 0.35, ease: E.chegada }}, 0.32);
      {'tl.fromTo("#cb", { clipPath: "inset(0% 100% 0% 0%)" }, { clipPath: "inset(0% 0% 0% 0%)", duration: 0.4, ease: "power1.out" }, 0.6);' if cargo else ''}
      {'tl.fromTo("#ct", { opacity: 0, y: 12 }, { opacity: 1, y: 0, duration: 0.35, ease: E.chegada }, 0.72);' if cargo else ''}
      {'tl.fromTo("#logo", { scale: 0, y: -20 }, { scale: 1, y: 0, duration: 0.5, ease: E.elastico }, 0.67);' if logo else ''}
      // saída faz parte do componente GC: textos somem, barras recolhem da direita para a esquerda
      tl.to(".txt{', #logo' if logo else ''}", {{ opacity: 0, duration: 0.12, ease: E.saida }}, {sai:.2f});
      tl.to("{'#cb, ' if cargo else ''}#nb", {{ clipPath: "inset(0% 100% 0% 0%)", duration: 0.42, ease: E.saida, stagger: 0.1 }}, {sai + 0.1:.2f});
    """
    return css, body, js


def main():
    gcs = json.loads((TRAB / "gcs.json").read_text(encoding="utf-8"))
    dur = float(G["duracao"])
    novos = []
    for g in gcs:
        cargo = g.get("cargo") if g.get("cargo") is not None else cargo_de(g["nome"])
        if not cargo:
            print(f"aviso: sem cargo para {g['nome']} (pessoas.csv): GC só com o nome; registre no DECISOES.md")
        name = f"gc-{g['n']}"
        css, body, js = gc_html(g["nome"], cargo, dur, g.get("y"))
        write(name, f"GC {g['nome']}", dur, css=css, body=body, js=js)
        novos.append({"comp": name, "inicio": float(g["inicio"]), "nome": f"{g['n']} GC {sem_acento(g['nome'])}"})
    jp = TRAB / "jobs.json"
    antigos = [j for j in json.loads(jp.read_text(encoding="utf-8"))] if jp.exists() else []
    jobs = [j for j in antigos if not j["comp"].startswith("gc-")] + novos
    jp.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
    print("ok jobs.json", len(novos), "GC(s)")


if __name__ == "__main__":
    main()
