# Tarefa: Inserts (vídeo com apresentador + transcrição)

O padrão foi aprovado pelo usuário em 2026-10-19 ("Agora sim, ficou muito bom"): 7 inserts num clipe de 57,7 s, entregues em 14 MOVs. Chegou-se a ele depois de uma v1 reprovada (pequena e genérica) e de um lote reprovado por exagero. **O padrão vale para qualquer cliente**; o que muda de um cliente para outro é só a marca, que vem do perfil. O que foi reprovado está em `acabamento.md` §2.

## 0. O padrão é o piso, não o molde (2026-11-10)

O usuário reprovou entregas que saíram "simples, como se tivesse que seguir tudo igual na IDV, sem criatividade". O padrão abaixo diz o mínimo de qualidade (leitura, fidelidade, acabamento, não cobrir o rosto); ele **não** diz como cada insert deve ser.

- A IDV é o vocabulário (cores, fontes, realce, tom), não a frase. Use-a para inventar, não para repetir.
- Cada momento pede a sua ideia visual: escala, ritmo, direção do movimento, quebra de linha, objeto, contraste, o espaço em volta da pessoa. Antes de montar, escreva no `DECISOES.md` a ideia de cada tela em uma linha.
- Não use o mesmo tipo da tabela §2 duas vezes seguidas, e não repita a composição do vídeo anterior do mesmo cliente.
- Pelo menos um momento por vídeo deve ter uma ideia que surpreenda (um movimento de câmera tipográfico, uma interação com o corpo, um número que vira objeto).
- Para ter ideias e se policiar contra o genérico, use `repertorio-motion.md` (lista de proibições, um objeto que vira o próximo, verificação de quadros).
- O pedido pode trazer **LIÇÕES** (avaliações do editor sobre entregas anteriores, feitas no app). Elas valem mais que esta página: siga o que funcionou e não repita o que ficou ruim.

## 1. O padrão

1. **Um momento tipográfico forte por tela.**
   - A frase-chave da fala, gigante, no papel `destaque`.
   - A palavra principal vai no **realce** do cliente (`componentes.realce`: pílula, sublinhado ou só cor).
2. **No máximo um elemento de apoio** (chips de nome, sublinhado, número-índice, ícone). Antes de adicionar algo, pergunte: "a frase já se sustenta sozinha?". Se sim, não adicione.
3. **Profundidade com a pessoa.** O texto passa **atrás da cabeça**, em três arquivos: ATRÁS + recorte + FRENTE (`entrega.md` §3).
4. **Só os elementos, sem fundo.** Nada de campo de cor, textura ou fundo trocado atrás da pessoa: o vídeo é o fundo de todo insert. Fundo próprio só se as observações pedirem (aí o nome leva `(tela cheia)`).
5. **Nada opaco cobrindo o corpo** sem motivo forte. Cards, relógios e fotos cobrindo a pessoa ficaram feios.
6. **Movimento com acabamento.**
   - Letras cinéticas: sobem da máscara com leve rotação.
   - Motion blur real.
   - Nada parado por mais de cerca de 1 s: durante o hold, deriva lenta de escala.
7. **Leitura no celular** (px em 1080×1920; use `k.px()`):

   | Elemento | Tamanho |
   |---|---|
   | destaque | 150 a 236 |
   | apoio | 72 a 92 |
   | qualquer texto | nunca abaixo de 56 |
8. **Fiel à fala.**
   - Só palavras ditas, sincronizadas palavra a palavra.
   - Pessoas citadas recebem tratamento visual idêntico, na ordem em que aparecem na fala.
9. **Contraste de ritmo a serviço da narrativa.** Um "antes" calmo (ease suave, sem blur, peso fino) contra um "agora" rápido (chegada forte, blur, black). O contraste vem do movimento e da tipografia, não de trocar o fundo.

## 2. Vocabulário de tipos (genérico)

Os sete inserts aprovados reduzidos a tipos. Cada cliente pode ter o próprio vocabulário no `marca.md`/`regras.md`; se tiver, ele vence esta tabela.

| Tipo | Arquivos | Quando usar | Como é |
|---|---|---|---|
| **Gancho atrás da cabeça** | ATRÁS + FRENTE | abertura, tema do vídeo | Título de 2 linhas atrás da cabeça, sobre o vídeo (sem trocar o fundo). Uma frase nova empurra a anterior. Na frente, chips com os nomes citados ou um sticker |
| **Frase-chave atrás da cabeça** | ATRÁS + FRENTE | afirmação central | Apoio (76–92 px) + linha de destaque + realce (160–168 px). Se o realce ficar escondido pela cabeça, só ele vai para a FRENTE (§4) |
| **Troca de frase** | ATRÁS (+ FRENTE) | duas ideias em sequência | As letras da frase antiga saem para cima e a nova entra 0,1–0,2 s depois, no mesmo lugar |
| **Sobre o tronco** | camada única | lista de sintomas, itens, "três coisas" | Um item por vez, grande (Black de 120 px), sobre a camisa, com sombra. Cada um empurra o anterior |
| **Parede tipográfica** | camada única (ou ATRÁS + FRENTE) | conceito repetido ou contraste "isto ou aquilo" | Faixas de texto em contorno correndo em sentidos opostos, sobre o vídeo, e **uma faixa central legível e estática**. Na virada, as faixas trocam de texto. Sem campo de cor |
| **Antes × agora** | 2 arquivos (`a` + `b`, com transição) | comparação no tempo | Tela calma, depois tela rápida, as duas sobre o vídeo. Lista que cresce, itens anteriores esmaecidos a 30% e um número-índice gigante em contorno |
| **Dado / número** | ATRÁS + FRENTE, ou única | número dito na fala | Número gigante no papel `numero`. Se a fala detalha, um objeto simples e honesto (anel completando, contagem real) que **não cubra o rosto** |

## 3. Processo (nesta ordem)

1. **Preparar.** `kit/preparar.py`; veja o `kit/README.md`.
   - Transcrição: o JSON do Premiere (`segments[].words[]`) é o mais preciso e vem primeiro. Sem ele, o script roda o ASR em português (`-m small -l pt`, nunca `small.en`).
   - **Revise o ASR** contra o sentido da fala. Nomes próprios saem errados ("Band" vira "Bundy"): corrija em `palavras.json` pelo `pessoas.csv`, pelas observações e pelo contexto, e registre as correções no `DECISOES.md`.
2. **Recortar a pessoa, só se o pedido disser `RECORTE: recorte a pessoa`.** Desde 2026-10-29 o padrão é **não** recortar: o editor recorta à mão. Sem recorte, um insert com texto atrás da cabeça ainda sai em ATRÁS + FRENTE (ele monta com o recorte dele). Quando pedido:

   ```bash
   npx hyperframes remove-background "<video>" -o "{trabalho}/recorte.mov"
   ```

   Leva cerca de 2 min para cada 30 s, com timeout alto. Sem nenhum insert ATRÁS+FRENTE no mapa, pule este passo.
   Sem recorte, a revisão `review.py camadas` compõe só vídeo → ATRÁS → FRENTE: confira o FRENTE pensando que a pessoa ficará entre as camadas.
3. **Mapa de inserts** em `{trabalho}/mapa.md`, uma tabela com tempo · fala · tipo · arquivos · por quê.
   - **Calibração (decisão do usuário, 2026-10-20):** sem observação em contrário, faça **só os 3 momentos mais fortes** do vídeo.
   - Os demais candidatos vão para "Próximos" no `DECISOES.md`, com tempo e frase.
   - Se as observações pedirem mais ("todos", "um a cada 10 s", "só até 0:30"), elas mandam.
   - Escolha os momentos pela força da frase (tese, número, contraste, pergunta), não pela distribuição no tempo.
4. **Construir.** Primeiro os **templates universais** (§8): todo momento que couber num deles (frase-chave, número, palavra, lista, troca, citação) vai para o `inserts.json`, e `python3 kit/universais.py` gera o HTML na marca do cliente. É mais barato, é igual entre clientes e o editor consegue mexer neles na prévia sem IA. Só o que os templates não fazem (profundidade atrás da cabeça, fotos, objetos, telas múltiplas) vai para o gerador à mão: copie `kit/exemplo_inserts.py` para `kit/inserts.py` e reescreva os blocos:
   - um HTML por insert, ou dois (`two_layers`);
   - t local = tempo no clipe − início do insert;
   - comente em cada bloco os tempos das palavras, tirados do `transcricao.txt`.
5. **Duas varreduras por insert:** construir → revisar **em movimento** → corrigir → revisar de novo.

   ```bash
   python3 kit/review.py camadas <base> <inicio> <dur>
   ```

   Use `unica` para peças de camada única. A revisão compõe vídeo → ATRÁS → recorte → FRENTE, como na edição. Abra a folha e procure:
   - abertura vazia ou fraca;
   - texto cortado (bordas, itálico, acentos);
   - palavra-chave escondida atrás da cabeça;
   - excesso de elementos;
   - algo cobrindo o rosto ou a boca;
   - fora das safe zones.
6. **Lint.** `npx hyperframes lint` em cada `hf/_check/<comp>/`, até 0 erros e 0 avisos.
7. **Render.** Escreva `{trabalho}/jobs.json` e rode `python3 kit/render.py`: 120 fps → 4 subquadros → 30 fps, ProRes 4444 + alpha, com corte das telas múltiplas. **Com `PRÉVIA:` no pedido, pare antes deste passo** (§7).
8. **Entrega.**

   ```bash
   python3 kit/entregar.py --recorte "{trabalho}/recorte.mov" --pessoa <Nome>
   ```

   O editor confere cada insert sobre o vídeo no próprio Clientela (botão Prévia): não monte prévia do conjunto.
9. **Fechar o `DECISOES.md`.**

## 4. Medidas que funcionaram (1080×1920)

- **Bloco atrás da cabeça.**
  - Topo em y=214; apoio de 76 a 92 px; linha de destaque de 104 a 128 px; realce de 160 a 164 px.
  - O bloco precisa terminar acima de y≈520, porque o topo da cabeça varia de 500 a 560. **Meça no quadro**: o `review.py --at` mostra onde a cabeça está.
  - Se a palavra-chave ficar escondida, passe só o realce para a FRENTE, com um "fantasma" invisível (`visibility:hidden`) das linhas de cima, para manter o alinhamento.
- **Chips de nome:** 112 px de altura e texto de 64 px, em diagonal (o da esquerda em y=1080, o da direita em y=1220), com rotação de ±3°.
- **Texto sobre o tronco** (y≈1150–1500): destaque de 120 px com `filter: drop-shadow(<componentes.sombra_texto>)`.
- **Telas de lista / antes × agora:** margem lateral de 64 a 80 px; títulos de 236 px; itens de lista de 90 px; sub-itens de 56 px, para não quebrar linha. Sem fundo, todo texto leva a sombra do perfil.
- **Largura estimada** de uma linha: cerca de 0,62 × tamanho × número de caracteres para Black e cerca de 0,5 × para Regular. Passou de 1080 − 2 × margem? Diminua o corpo ou quebre a linha.
- **Safe zones** (9:16): `safe` do perfil; o padrão é topo 250, base 420, direita 140. O bloco atrás da cabeça em y=214 invade o topo **de propósito**: foi aprovado assim, porque fica atrás da cabeça e acima dela.

## 5. Movimento (receitas)

Os helpers estão no `common.py`. `E.*` são os eases do perfil.

- **Letras cinéticas:**

  ```js
  tl.fromTo(".c", {yPercent:150, rotation:(i)=>[-7,5,-4,8,…][i%n]},
                  {yPercent:0, rotation:0, duration:.6, ease:E.chegada, stagger:.03})
  ```

  Sempre dentro de uma `.mask` (overflow hidden, com folga).
- **Realce do tipo pílula:** revele por `clip-path: inset(-60% 100% -10% -10%) → inset(-60% -10% -10% -10%)` em 0,42–0,45 s, com folga para os acentos. As letras sobem junto.
- **Realce do tipo sublinhado:** `scaleX 0→1` em 0,5 s, `power2.inOut`, depois da palavra.
- **Troca de frase:** as letras da frase antiga saem com `yPercent:-160` (0,26 s, `E.saida`, stagger 0,01) e a nova entra 0,1 a 0,2 s depois.
- **Chips:** `scale 0→1`, `rotation ±14→±3`, `E.elastico`. Os chips entram na palavra exata em que o nome é dito.
- **Hold vivo:**
  - escala de 1 a 1,04 linear durante todo o hold;
  - respiração das letras: `y −7`, `yoyo`, `E.respiro`.
- **Campo que cobre a tela** (cor subindo), **só quando as observações pedirem fundo**:
  - um elemento mais alto que a tela, com **degradê transparente de 260 px na borda de ataque**;
  - `yPercent 100→0` com `power3.inOut` em 0,6 a 0,65 s;
  - o texto que está saindo só esmaece, sem correr junto.
- **Motion blur:** o render a 120 fps + `tmix` de 4 subquadros já dá o blur. Em entradas muito rápidas, use também `filter:url(#mby)` no contêiner + o helper `blur("y", t, dur, pico)`.
- **Contagem / anel:** SVG com `pathLength=1` e `stroke-dashoffset 1→0`. Os dígitos vêm do `onUpdate` de um proxy: contagem real e determinística, nunca `setInterval`.
- **Sem saída.** O insert termina segurando o quadro final; o corte do take é do editor.

## 6. Estilo clean

Quando o pedido diz `ESTILO: clean`, a mesma identidade do cliente (fontes, cores, sombra) sai mais contida. O usuário definiu assim (2026-10-29):

- **Menos elementos.** Só a frase ou o número, sem chips, stickers, cartões, fotos nem pílula. O realce vira só a cor de acento.
- **Movimento discreto.** Fade com leve subida (~28 px, 0,5 s, `power2.out`). Nada de letras girando, blur forte ou deriva de escala.
- **Texto menor.** Cerca de 0,72× as medidas do §4, nunca abaixo de 56 px. Alinhado à esquerda, com mais respiro.
- A posição continua livre (o padrão é a base, acima da safe zone). O cliente pode calibrar no `marca.json` → `inserts.clean` (`escala`, `posicao`).

Os templates universais já fazem as duas versões (`"estilo": "clean"`); num insert à mão, siga as mesmas regras.

## 7. Prévia antes do export

Em projeto, o pedido traz `PRÉVIA:`. Você monta, revisa e passa o lint de cada insert, escreve o `jobs.json` e **para**: não rode `render.py` nem `entregar.py`. O app então:

- mostra cada composição de `hf/` num iframe sobre o vídeo bruto, sincronizada pelo `tl.seek` (sem motion blur nem recorte, só o HTML);
- deixa o editor aprovar, trocar a fonte (um `<style id="clientela-fonte">` que o app escreve no HTML: **não** apague nem dependa dele), acrescentar inserts dos templates universais e pedir ajuste de um insert;
- renderiza e entrega só os aprovados (`kit/auto.py exportar`: `render.py <comps>` + `entregar.py`).

Por isso o `jobs.json` precisa estar certo e completo: é ele que diz ao app o que existe, quando entra e como se chama. Composições ATRÁS/FRENTE terminam em `-atras` / `-frente` (o `two_layers` já faz assim): o app as junta num insert só. Feche o `DECISOES.md` dizendo que a entrega sai pela prévia.

### Telas pedidas e tela fixa primeiro

- `TELAS PEDIDAS`: faça exatamente essas telas, na numeração do pedido. Ache o momento de cada uma na transcrição pela descrição. Composição `tela-<n>` (ou `tela-<n>-atras` / `tela-<n>-frente`), nome no `jobs.json` começando por `Tela <n>`: o app usa esse nome para mostrar a tela no card do pedido. Escreva o HTML de cada tela assim que ela estiver pronta (o editor vê aparecendo uma a uma). Respeite o nível de animação de cada uma.
- `TELA FIXA PRIMEIRO`: cada tela é o quadro final composto, parado, segurando a duração inteira (sem entrada nem saída). Depois o app pede para animar só as aprovadas: o layout aprovado é o estado de repouso; a entrada chega nele e a saída parte dele, sem mudar posição, tamanho, fonte ou texto.

## 8. Templates universais

`kit/universais.json` é o catálogo e `kit/universais.py` o gerador. Leem o `{trabalho}/inserts.json`:

```json
{"inserts": [{"id": "3", "template": "numero", "estilo": "padrao", "posicao": "centro",
              "inicio": 52.7, "dur": 4.0, "titulo": "145 Milhoes",
              "textos": {"numero": "145", "rotulo": "milhões", "apoio": "de brasileiros"}}]}
```

- `id` é o número do insert na entrega, sem repetir os do `inserts.py`. A composição sai como `hf/u-<id>.html` e entra no `jobs.json` sem tocar nas outras entradas.
- Cada texto entra quando a primeira palavra dele é dita (`palavras.json`). `tempos` (`{"rotulo": 0.8}`, t local) força um tempo.
- Templates: `frase` (apoio + frase no realce), `numero` (conta até o valor, palavra no realce, apoio), `palavra`, `lista` (até 5 itens), `troca` (uma frase sai e a outra entra), `citacao`.
- Linhas longas encolhem até caber entre as margens. Revise em movimento como qualquer insert (`review.py unica u-<id> <inicio> <dur>`).
- O editor também monta esses inserts na prévia, sem IA. Se o pedido de ajuste for de um `u-…`, mude o `inserts.json` e rode o `universais.py` de novo; não edite o HTML gerado.
