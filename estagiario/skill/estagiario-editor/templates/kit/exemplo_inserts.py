#!/usr/bin/env python3
"""EXEMPLO de gerador de inserts: um de cada tipo, só com tokens do perfil (nada de marca aqui).

Copie para kit/inserts.py e reescreva os blocos com os textos e tempos da fala:
  t local = tempo no clipe − início do insert;  marcas(t0, t1) imprime as palavras já no tempo local.
Tipos (references/tarefa-inserts.md §2):
  1 · frase-chave atrás da cabeça + troca de frase + chips na frente  → two_layers() → ATRÁS + FRENTE
  2 · antes × agora, duas telas sobre o vídeo (sem fundo)             → write() + "partes" no jobs.json
  3 · itens sobre o tronco, um empurrando o outro                     → write() camada única
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import CORES, W, chip, cor, marcas, px, realce, two_layers, write, write_jobs  # noqa: E402

# Só os elementos, sem fundo: o vídeo é o fundo de todo insert (fundo próprio só se as observações pedirem, e aí o
# nome leva "(tela cheia)"; o entregar.py recusa quadro opaco sem isso). As cores de cada peça são escolhidas aqui,
# pelo vídeo atrás; não confie na cor padrão do papel, e use a sombra do perfil para a leitura.
ACENTO = cor("acento") if "acento" in CORES else "var(--texto)"

# ═════════════════ 1 · FRASE-CHAVE ATRÁS DA CABEÇA (início no clipe: 0,0 s) · ATRÁS + FRENTE ═════════════════
# palavras (tempo local): troque pelo marcas(0.0, 4.4) da fala real
two_layers(
    "ins-1-exemplo", "1 Exemplo", 4.4,
    css=f"""
      .tit {{ left: 0; width: {W}px; top: {px(214)}; display: flex; flex-direction: column; align-items: center; text-align: center; }}
      #l1 {{ font-size: {px(92)}; }}
      #k1 {{ margin-top: {px(10)}; }} #k1 .p-destaque {{ font-size: {px(160)}; }}
      #l2 {{ font-size: {px(84)}; }}
      #k2 {{ margin-top: {px(10)}; }} #k2 .p-destaque {{ font-size: {px(164)}; }}
      #chL {{ top: {px(1080)}; left: {px(50)}; transform-origin: 0% 50%; }}
      #chR {{ top: {px(1220)}; right: {px(50)}; transform-origin: 100% 50%; }}
    """,
    body=f"""
      <div id="atras" class="full">
        <div id="fA" class="abs tit">
          <div class="mask p-destaque" id="l1"><span class="split" id="l1s">uma frase de</span></div>
          {realce("exemplo", "k1")}
        </div>
        <div id="fB" class="abs tit">
          <div class="mask p-narrativa" id="l2"><span class="split" id="l2s">que troca por</span></div>
          {realce("outra", "k2")}
        </div>
      </div>
      <div id="frente" class="full">
        {chip("Pessoa A", "chL")}
        {chip("Pessoa B", "chR")}
      </div>
    """,
    js="""
      // ATRÁS · letras cinéticas atrás da cabeça + realce na palavra-chave
      letras("#l1s .c", 0.2);
      realceIn("k1", 0.9);
      deriva("#fA", 0.2, 2.4);
      // FRENTE · os nomes entram quando são ditos (mesmo tratamento, ordem da fala)
      chipIn("#chL", 1.3, 1); chipIn("#chR", 1.6, -1);
      tl.to("#chL, #chR", { scale: 0, opacity: 0, duration: 0.24, ease: E.saida, stagger: 0.04 }, 2.45);
      // TROCA · a frase nova empurra a antiga (sem saída no fim: o insert segura o último quadro)
      sai("#l1s .c", 2.5); realceOut("k1", 2.5);
      letras("#l2s .c", 2.7, { rot: false });
      realceIn("k2", 3.1);
      deriva("#fB", 2.7, 1.7, 1.03);
    """,
)

# ═════════════════ 2 · ANTES × AGORA (início no clipe: 4,5 s) · duas telas sobre o vídeo ═════════════════
ITENS = ["primeiro item", "segundo item", "terceiro item"]
lista = "\n".join(f'<div class="item p-apoio sombra" id="it{i}"><span class="mask"><span class="split">{t}</span></span></div>'
                  for i, t in enumerate(ITENS))
# índice gigante: um elemento por número (trocar textContent na timeline não é seek-safe, e "01" vira 1 no GSAP)
idx = "".join(f'<div id="idx{i}" class="abs idx p-numero">{i + 1:02d}</div>' for i in range(len(ITENS)))
write(
    "ins-2-exemplo", "2 Exemplo", 8.0,
    css=f"""
      #tA {{ left: {px(72)}; top: {px(520)}; }}
      #tA .p-destaque {{ font-size: {px(236)}; color: var(--texto); }}
      #tA .p-narrativa {{ font-size: {px(72)}; color: var(--texto); margin-top: {px(24)}; max-width: {W - 144 * W / 1080:.0f}px; }}
      #tB {{ left: {px(72)}; top: {px(360)}; right: {px(72)}; }}
      #tB .p-destaque {{ font-size: {px(236)}; color: {ACENTO}; }}
      .item {{ font-size: {px(90)}; margin-top: {px(28)}; color: var(--texto); }}
      .idx {{ right: {px(64)}; top: {px(1180)}; font-size: {px(420)}; color: transparent; -webkit-text-stroke: {px(4)} var(--texto); opacity: 0; }}
    """,
    body=f"""
      <div id="tA" class="abs sombra">
        <div class="mask p-destaque"><span class="split" id="tAs">antes</span></div>
        <div class="p-narrativa" id="tAn">um ritmo calmo, sem blur</div>
      </div>
      <div id="tB" class="abs">
        <div class="mask p-destaque sombra"><span class="split" id="tBs">agora</span></div>
        {lista}
      </div>
      {idx}
    """,
    js="""
      // tela A · calma: ease suave, sem blur
      tl.fromTo("#tAs .c", { yPercent: 120 }, { yPercent: 0, duration: 0.9, ease: "power2.out", stagger: 0.05 }, 0.2);
      tl.fromTo("#tAn", { opacity: 0, y: 20 }, { opacity: 1, y: 0, duration: 0.8, ease: "power2.out" }, 0.9);
      deriva("#tA", 0.2, 3.4, 1.03);
      // virada · a frase calma sai para cima (é a transição que o arquivo 2b mantém); nada de campo de cor
      sai("#tAs .c", 3.6); tl.to("#tAn", { opacity: 0, duration: 0.3, ease: E.saida }, 3.6);
      // tela B · rápida: chegada forte + blur; lista que cresce, anteriores a 30%, índice em contorno
      letras("#tBs .c", 4.0); blur("y", 4.0, 0.55, 12);
      const T = [5.0, 5.9, 6.8];
      T.forEach((t, i) => {
        letras(`#it${i} .c`, t, { rot: false, st: 0.02, d: 0.5 });
        if (i) tl.to(`#it${i - 1}`, { opacity: 0.3, duration: 0.3, ease: "power2.out" }, t);
        punch(`#idx${i}`, t, { s: 1.2 });
        if (i) tl.to(`#idx${i - 1}`, { opacity: 0, duration: 0.12, ease: E.saida }, t);
      });
      deriva("#tB", 4.0, 4.0, 1.03);
    """,
)

# ═════════════════ 3 · ITENS SOBRE O TRONCO (início no clipe: 13,0 s) · camada única ═════════════════
PARES = [("primeiro", "sintoma"), ("segundo", "sintoma")]
pares = "\n".join(
    f'<div id="p{i}" class="abs par"><div class="sombra"><div class="mask p-destaque a"><span class="split" id="p{i}a">{a}</span></div></div>'
    f'<div class="sombra"><div class="mask p-destaque b"><span class="split" id="p{i}b">{b}</span></div></div>'
    f'<div class="sub" id="s{i}"></div></div>' for i, (a, b) in enumerate(PARES))
write(
    "ins-3-exemplo", "3 Exemplo", 5.0,
    css=f"""
      .par {{ left: 0; width: {W}px; top: {px(1160)}; display: flex; flex-direction: column; align-items: center; text-align: center; }}
      .par .p-destaque {{ font-size: {px(120)}; line-height: 0.98; }}
      .par .a {{ color: var(--texto); }}
      .par .b {{ color: {ACENTO}; }}
      .sub {{ height: {px(12)}; width: 60%; margin-top: {px(18)}; background: var(--g-acento, var(--acento, var(--texto))); transform-origin: 0% 50%; }}
    """,
    body=pares,
    js="""
      // um item por vez: o novo empurra o anterior para cima
      const P = [["#p0", 0.4, 0.9], ["#p1", 2.2, 2.7]];
      P.forEach(([id, ta, tb], k) => {
        letras(`${id}a .c`, ta, { rot: false });
        letras(`${id}b .c`, tb);
        tl.fromTo(`#s${k}`, { scaleX: 0 }, { scaleX: 1, duration: 0.5, ease: "power2.inOut" }, tb + 0.2);
        if (k < P.length - 1) {
          const nx = P[k + 1][1] - 0.06;
          sai(`${id}a .c, ${id}b .c`, nx, { st: 0.008 });
          tl.to(`#s${k}`, { scaleX: 0, transformOrigin: "100% 50%", duration: 0.22, ease: E.saida }, nx);
        }
      });
      deriva("#p1", 2.2, 2.8);
    """,
)

# ═════════════════ jobs.json · o que o render.py vai gerar (nome = timecode da entrada + nome) ═════════════════
write_jobs([
    {"comp": "ins-1-exemplo-atras", "inicio": 0.0, "nome": "1 Exemplo (camada ATRAS)"},
    {"comp": "ins-1-exemplo-frente", "inicio": 0.0, "nome": "1 Exemplo (camada FRENTE)"},
    {"comp": "ins-2-exemplo", "inicio": 4.5, "nome": "2",
     "partes": [[0.0, 3.7, "2a ANTES"], [3.5, 8.0, "2b AGORA (com transicao)"]]},
    {"comp": "ins-3-exemplo", "inicio": 13.0, "nome": "3 Exemplo"},
])
print("# palavras do insert 1 (tempo local):", marcas(0.0, 4.4))
