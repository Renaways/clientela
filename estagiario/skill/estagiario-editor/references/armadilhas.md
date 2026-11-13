# Armadilhas técnicas

Cada uma custou uma iteração no 049 ou nos testes do kit.

| Sintoma | Causa | Correção |
|---|---|---|
| Fim da palavra cortado | `background-clip:text` + itálico pendendo para fora da caixa | Caixa com `padding .3em .16em .06em .06em` e margem negativa igual (a `.tdeg` já faz) |
| Acento ou til some | `.mask` sem folga, ou `clip-path` do realce colado à caixa | `.mask` com padding de .32em no topo (já no `common.py`); `clip-path` com `-60%` no topo |
| Palavra quebra no meio | Split por letra sem agrupar por palavra | `.split` já envolve cada palavra num `.w` com `nowrap` |
| Texto some sobre o vídeo (parte clara ou escura do quadro) | Sem fundo, a leitura depende do vídeo atrás | Sombra do perfil (`.sombra`) e cor por peça no gerador (`color: var(--texto)`); nunca um campo de cor para "resolver" |
| Número "01" vira "1" | `tl.set(el, {textContent: "01"})`: o GSAP interpola como número | Um elemento por número, alternando a opacidade; contagem real via `onUpdate` de um proxy |
| Lint: `gsap_repeated_fromto_without_baseline` | O mesmo seletor em vários `fromTo` (até num helper chamado duas vezes) | Ids únicos por elemento (`#k1-sub`, não `.realce-sub`); ou `immediateRender: false` nos fromTo depois do primeiro |
| Path SVG some | Atributo `d` sem aspas na concatenação | Monte o SVG com f-string e aspas |
| Fantasma no motion blur | 2 subquadros + movimento muito rápido + borda dura | 120 fps com 4 subquadros (`render.py`) + borda em degradê + texto que não corre junto |
| Frase-chave ilegível numa parede de texto | Ela fazia parte das faixas que correm | Faixa central própria, estática e centralizada |
| `<br>` proibido | Regra do HyperFrames | Spans com `display:block` (`.linha`) |
| Transform no CSS + GSAP no mesmo elemento | Conflito de valores | Estado inicial só via `fromTo` (`immediateRender`) ou `opacity:0` no CSS |
| Máscara de imagem falha ou vira mosaico | `tl.set` alternando a imagem; `mask-size:0px` é inválido | Função pura do tempo; mínimo de 1 px |
| Chaves `{{ }}` aparecem no CSS | f-string dentro de f-string, ou CSS passado já escapado | No `css=` dos geradores, f-string com `{{ }}` só quando a string é f-string; o `common.py` não usa `.format` |
| `rm` com variável é bloqueado | Checagem de segurança | `rm -f "${D:?}"/*.mov` |
| `lint`/`snapshot` não acham a composição | Eles só leem `index.html` | Rode em `hf/_check/<comp>/` (o `write()` já cria, com `assets` por symlink) |
| `npx hyperframes init` reclama da pasta | Ele exige pasta nova | O `preparar.py` cria `hf/` pelo init antes de copiar os assets |
| Transcrição com nome errado ("Band" → "Bundy") | ASR | Corrija `palavras.json` pelo `pessoas.csv`, pelas observações e pelo contexto, e registre |
| ASR em inglês | Modelo `small.en` | O `preparar.py` usa `-m small -l pt` |
| `entregar.py` recusa "tem quadro com fundo" | Sobrou um campo de cor, textura ou retângulo de tela inteira | Tire o fundo: só os elementos. Se as observações pediram fundo, o nome leva `(tela cheia)` |
