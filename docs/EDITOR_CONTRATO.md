# Contrato: Clientela ↔ skill `estagiario-editor`

O Clientela não anima nada. A função **Editor** monta um prompt padronizado e roda o Claude Code CLI local em segundo plano; quem sabe animar é a skill `estagiario-editor`. Este arquivo define o que um lado garante ao outro. Ao mudar um lado, mude este arquivo e o outro lado juntos.

**Onde mora cada coisa.** Tudo é desenvolvido dentro do repositório do Clientela:

| O quê | Onde |
|---|---|
| App (perfis, jobs, tela) | `clientela/identidades.py`, `clientela/editor.py`, `templates/index.html` |
| Este contrato | `docs/EDITOR_CONTRATO.md` |
| Código da skill | `estagiario/skill/estagiario-editor/` (versionado no git) |
| Onde o Claude Code procura a skill | `~/.claude/skills/estagiario-editor` → **symlink** para a pasta acima |
| Perfil de cada cliente | `<pasta do cliente>/_Estagiário/` (fora do repositório, junto do cliente) |

Implementação no app: `clientela/editor.py` (jobs, prompt, entregas) e `clientela/identidades.py` (perfis). Testes: `tests/test_editor.py` e `tests/test_identidades.py`.

---

## 0. IA só no motion (2026-10-20)

O que é template **não chama o Claude**. Cada tarefa tem `modo` na config (`editor.DEFAULT_CONFIG["tarefas"]`):

| Tarefa | Modo padrão | Quem faz |
|---|---|---|
| Legendas dinâmicas | `auto` | `kit/auto.py legendas`: preparar (whisper `large-v3` + texto da transcrição) → `legendas.py blocos`/`html` → render → entrega |
| GC | `auto` | `kit/auto.py gc`: o editor escolhe pessoa e entrada no painel → `templates/gc/` do perfil (ou `gc.py` com o `marca.json`) → render → entrega |
| Inserts, Motion curto | `ia` (único) | `kit/auto.py motion` prepara a bancada e faz `exec` do `claude` |
| Corte vertical 9:16 | `corte` (único) | `clientela/autocorte.py`, do app, **fora da skill**: whisper → LLM (uma chamada, só os trechos) → ffmpeg. Fala o mesmo stream-json; nada aqui depende da skill |

- O `kit/auto.py` fala o **mesmo stream-json** do Claude Code (`system/auto` no começo, `assistant` com `text`/`tool_use.description`, `result` com `total_cost_usd: 0` no fim). O app não distingue: o mesmo monitor, cancelamento e retomada.
- O kit roda da bancada: o app copia `estagiario/skill/estagiario-editor/templates/kit/` para `{trabalho}/kit/` a cada job e grava os parâmetros em `{trabalho}/auto-args.json`.
- **O modo automático exige o template do cliente** (`<perfil>/marca.json`, §5). Sem ele o app recusa com 409 e aponta a aba Identidade. O usuário pode trocar legendas/GC para `ia` no modal de configuração.
- Nas tarefas com IA, o claude começa com a bancada pronta (`job.json`, `palavras.json`, `transcricao.txt`, `kit/`, `hf/`, `marca.json` do perfil) e o prompt diz para não refazer o preparo.

## 1. Como o app chama o CLI

```
claude -p --output-format stream-json --verbose \
       --permission-mode auto --permission-prompts none \
       -n "Clientela · <tarefa> · <projeto>" \
       [--model …] [--effort …] [--max-budget-usd …] [flags extras] \
       [--add-dir <dir> …]
       < prompt (stdin)
```

- **cwd**:
  - tarefa de projeto: a pasta do projeto (`<cliente>/<NNN> - <nome>/`);
  - análise de identidade: a pasta do cliente.
- **`--add-dir`**:
  - tarefa de projeto: o perfil do cliente (`<cliente>/_Estagiário/`), só se ele existir. O app não cria o perfil só porque um job rodou;
  - análise: as pastas escolhidas para estudo que ficam **fora** da pasta do cliente (o perfil já está dentro do cwd).
- **O prompt vai pelo stdin.** O `--add-dir` é variádico e engoliria um prompt posicional.
- **Permissões.** `auto` + `--permission-prompts none`: um classificador aprova cada ação. O que precisaria de uma pessoa é **negado** (não trava). A skill não deve depender de `AskUserQuestion`, que fica indisponível nesse modo.
- **Ninguém responde perguntas.** A skill decide pelo perfil e pelas regras do cliente e registra decisões e dúvidas em arquivo (§4).
- **Um job por vez** no app inteiro. A análise de identidade conta como job.
- **Cancelar.** O app manda SIGINT, depois SIGTERM e por fim SIGKILL para o **grupo de processos** (a cada 10 s). Renders filhos (`npx hyperframes`, `ffmpeg`) morrem junto. O que já foi escrito fica.
- **Fim do job.** O job termina na linha `result` do stream:
  - `is_error: false` → concluído;
  - qualquer outra coisa → erro.
- **Progresso.** O app mostra o texto do assistente e um resumo de cada `tool_use`; para `Bash`, é o campo `description`. Por isso, escrever `description` legível nos comandos Bash melhora o painel.
- **Ambiente.** O ambiente do filho chega **sem** as variáveis `CLAUDE_CODE_*`, e o `PATH` ganha `~/.local/bin`, `/opt/homebrew/bin` e `/usr/local/bin`.

Os modelos de prompt ficam editáveis em **Configurações → Configurar Editor (IA)**. Os padrões estão no código (`editor.DEFAULT_PROMPT_*`), e o que o usuário muda fica em `data/editor/config.json`.

## 2. Prompt da tarefa no projeto

O padrão, com os placeholders que o app troca:

```
Use a skill estagiario-editor.
Cliente: {cliente} (perfil de identidade visual em {perfil}).
Projeto: {projeto}.
Tarefa: {tarefa} — {instrucoes}
Vídeo bruto: {video}.
Transcrição: {transcricao}.
Trabalhe (builds, rascunhos, revisões) em {trabalho}/ e entregue os arquivos finais em {entrega}/ — versão {versao}.
Você está rodando em segundo plano, sem ninguém para responder perguntas: …
Observações do editor: {observacoes}
```

| Placeholder | Valor |
|---|---|
| `{cliente}` | nome do cliente |
| `{perfil}` | caminho absoluto de `<pasta do cliente>/_Estagiário` (pode não existir: aí a skill trabalha sem manual e avisa no `DECISOES.md`) |
| `{projeto}` | caminho absoluto da pasta do projeto |
| `{tarefa}` | rótulo: `Inserts`, `GC`, `Legendas dinâmicas`, `Motion curto` |
| `{instrucoes}` | instruções da tarefa (editáveis) |
| `{video}` | caminho absoluto do vídeo bruto (escolhido na pasta do projeto, ou em qualquer pasta pelo "Edição IA" do topo, §4) |
| `{transcricao}` | caminho absoluto do `.txt`/`.json`/`.srt`/`.vtt`, ou `nenhuma (transcreva o vídeo)` |
| `{trabalho}` | `<projeto>/.estagiario/<tarefa> vN` (o app cria a pasta) |
| `{entrega}` | `<projeto>/<pasta da tarefa> vN` (o app **não** cria: a skill cria ao entregar) |
| `{versao}` | `vN` |
| `{observacoes}` | texto livre do usuário, ou `nenhuma` |

A transcrição do Premiere é o JSON com `segments[].words[]` (com `start`). É a fonte mais precisa e deve ser preferida ao ASR.

**O `.txt` do Premiere** (blocos `HH:MM:SS:FF - HH:MM:SS:FF` / falante / texto) tem tempo só por bloco, e esse tempo é da **sequência** em que foi transcrito, que muitas vezes já é a edição: no 049 a deriva em relação ao bruto chegava a 36 s. O `kit/preparar.py` usa dele só o texto: roda o ASR para os tempos por palavra, corrige o que o ASR ouviu pelo texto (lista em `{trabalho}/correcoes.md`) e registra no `job.json` se os tempos do `.txt` batem com o bruto. Um `.txt` sem timecode é tratado como roteiro, com correção mais conservadora (só palavra por palavra, onde as duas versões são parecidas).

**Calibração (decisão do usuário, 2026-10-20).** Sem observação em contrário, a tarefa Inserts faz **só os 3 momentos mais fortes** do vídeo; os demais candidatos ficam listados no `DECISOES.md` para a próxima versão. As observações mandam ("todos", "um a cada 10 s", "só até 0:30").

## 2b. Prompt do ajuste de um elemento (2026-10-20)

No painel de entregas, cada elemento (insert, GC, legendas) tem **Prévia** e **Ajustar**. O ajuste roda o CLI com este modelo (`editor.DEFAULT_PROMPT_AJUSTE`, editável):

| Placeholder | Valor |
|---|---|
| `{cliente}`, `{perfil}`, `{projeto}` | como na tarefa |
| `{tarefa}` / `{versao}` | rótulo da tarefa que fez a entrega (pela pasta base) e `vN` |
| `{trabalho}` | a bancada **que criou a entrega**, lida do `.estagiario-origem` da pasta. Sem ela (apagada, ou entrega feita à mão) o app recusa o ajuste com 409 |
| `{entrega}` | a pasta `Motion - … vN` existente |
| `{elemento}` | `<n> <título> (entra em MM:SS:FF, <tipo>)` |
| `{arquivos}` | os MOVs do elemento, em linhas `- <nome>` |
| `{pedido}` | o que o editor escreveu |

A skill (`references/ajuste.md`) muda só o bloco do elemento no gerador da bancada, renderiza só ele e entrega com `kit/entregar.py --substituir <n>`: os arquivos antigos do elemento vão para `{entrega}/_anteriores/<data>/` e os novos entram no lugar. Nenhuma pasta de entrega nova é criada, e o job conta como "um job por vez".

**Prévia** é do app, não da skill: `POST /api/editor/previa` monta com ffmpeg um MP4 leve (lado maior 960 px, H.264 + áudio) do trecho do vídeo bruto com as camadas do elemento por cima, na ordem vídeo → ATRÁS → recorte → FRENTE. Fica em cache em `<projeto>/.estagiario/_previas/` (chave = arquivos + mtimes, últimos 40). O vídeo bruto vem do `job.json` da bancada ou do job do app que fez a entrega. A skill **não** monta mais a prévia do conjunto (`_Previa (video + motion).mp4`): custava minutos em todo job.

## 2c. Prévia dos inserts antes do export (2026-10-29)

Em projeto, a tarefa Inserts **não entrega mais sozinha**. As opções do job (`editor.inserts_opcoes`: `estilo` padrao|clean, `recorte`, `manual`, `previa`) viram linhas acrescentadas a `{instrucoes}` (`editor.inserts_instrucoes`), então valem também para um `prompt_projeto` personalizado:

- `ESTILO: …` (clean = `tarefa-inserts.md` §6);
- `RECORTE: …` (o padrão é não recortar; o editor recorta à mão);
- `TEMPLATES UNIVERSAIS: …` (`kit/universais.py`, `tarefa-inserts.md` §8);
- `PRÉVIA: … PARE` (só em projeto; no avulso da ~/Downloads a skill entrega como antes).

A skill monta, revisa, passa o lint e escreve o `jobs.json`. O app (`clientela/previa_inserts.py`) mostra `hf/<comp>.html` sobre o vídeo e guarda em `{trabalho}/previa.json` a aprovação e a fonte de cada insert. A fonte vai para o HTML num `<style id="clientela-fonte">`. "Exportar" roda `kit/auto.py exportar` (tipo de job `exportar`, sem IA) com as composições dos aprovados que ainda não estão na entrega (ou cujo HTML mudou depois do export): `render.py <comps>` e `entregar.py <arquivos>` na mesma `Motion - Inserts vN`. "Montar à mão (sem IA)" roda `auto.py bancada` (preparar + `inserts.json` vazio). O ajuste na prévia usa `editor.DEFAULT_PROMPT_AJUSTE_PREVIA` (`prompt_ajuste_previa`), com `{elemento}` = nome e tempo do insert e `{arquivos}` = as composições: muda o HTML e para, sem render.

### Telas em cards e tela fixa primeiro (2026-11-09)

Duas opções a mais em `inserts_opcoes`, também linhas em `{instrucoes}`:

- `telas` (até `MAX_TELAS`, cada uma `{titulo, descricao, nivel 0–3}`) → `TELAS PEDIDAS …` (`editor.telas_instrucoes`): uma composição por tela, chamada **`tela-<n>`** (ou `tela-<n>-atras` / `-frente`), com o nome no `jobs.json` começando por `Tela <n>`. É por esse nome que o app casa o card com a composição e mostra a prévia dentro do card assim que `hf/tela-<n>*.html` existe, antes do `jobs.json` (`previa_inserts.estado` → `telas_prontas`). O nível vai em texto (`editor.NIVEIS_ANIMACAO`).
- `estatico` (só com prévia) → `TELA FIXA PRIMEIRO: …`: cada tela sai parada, no estado final. "Animar aprovadas" (`editor.start_animate_job`, `POST /api/editor/inserts/<pasta>/animar`, token gate) usa o mesmo `prompt_ajuste_previa` com as composições aprovadas e o nível de cada uma; a tela aprovada é o estado de repouso.

O app grava `{trabalho}/telas.json` (`{estatico, telas, animar: [chaves já pedidas]}`). As sugestões de telas (`POST /api/editor/inserts-sugestoes`, `clientela/projeto_ia.py`) vêm da IA gratuita da OpenRouter lendo a transcrição da pasta; não passam pela skill.

### Criatividade, lições e referências (2026-11-10)

Todo pedido de tarefa de IA (inserts, motion curto, e o "Animar aprovadas") ganha até três blocos ao fim de `{instrucoes}`, nesta ordem:

- `CRIATIVIDADE: …` (`editor.CRIATIVIDADE`): a IDV é vocabulário, não molde — variar composição, escala e ritmo entre telas, e um momento que surpreenda. A skill diz o mesmo em `tarefa-inserts.md` §0.
- `LIÇÕES das avaliações …` (`aprendizado.licoes`): até 8 avaliações recentes da mesma tarefa, as do cliente primeiro ("(deste cliente)"), com "funcionou / ficou ruim / fazer diferente". Ausente sem avaliações. A fonte é `data/editor/avaliacoes.json`; a versão legível é `data/editor/APRENDIZADOS.md`.
- `REFERÊNCIAS APROVADAS: …` (`identidades.referencias_texto`): projetos que o editor marcou como referência visual do cliente (`perfil.json` → `referencias_projetos`), com as pastas — que também entram como `--add-dir`. A análise de identidade recebe essas pastas em `{pastas_analise}`.

## 3. Prompt da análise de identidade (onboarding)

Placeholders:

| Placeholder | Valor |
|---|---|
| `{cliente}` | nome do cliente |
| `{pasta_cliente}` | pasta do cliente: só leitura, **exceto** `_Estagiário/` |
| `{perfil}` | `<pasta do cliente>/_Estagiário` (o app cria a estrutura antes de rodar) |
| `{pastas_analise}` | lista em linhas `- <caminho>` das pastas que o usuário escolheu (qualquer lugar do disco); sem escolha, a pasta inteira do cliente |
| `{observacoes}` | texto livre do usuário, ou `nenhuma` |

A skill estuda os projetos aprovados, os vídeos, as fontes e os logos dessas pastas e preenche o perfil (§5). O app lembra as pastas escolhidas em `perfil.json` (`pastas_analise`) para a próxima análise.

## 4. Pastas: trabalho e entrega

- **Trabalho**: `<projeto>/.estagiario/<tarefa> vN/`. O ponto inicial é proposital: o organizador de arquivos e o estimador de horas do Clientela ignoram caminhos com parte oculta. Ali ficam o projeto HyperFrames, o recorte de revisão, as folhas de revisão e o `DECISOES.md` (decisões tomadas sem perguntar, dúvidas, o que calibrar).
- **Entrega**: `<projeto>/<pasta> vN/`, com estas pastas por tarefa:
  - `Motion - Inserts vN`
  - `Motion - GC vN`
  - `Motion - Legendas vN`
  - `Motion - Curto vN`

  O N é o próximo livre, calculado pelo app. Uma pasta `Motion - * vN` que já existe **nunca** é reaproveitada por uma tarefa nova; só o ajuste de um elemento (§2b) mexe nela, pela mesma bancada.
- **Trabalho avulso (2026-10-20).** O "Edição IA" do topo da tela aceita um vídeo de qualquer pasta do disco (absoluto, escolhido no Finder; só extensões de vídeo/transcrição) e um destino: um projeto ativo, como acima, ou a `~/Downloads`. Na Downloads, `<projeto>` nos caminhos acima é a própria `~/Downloads` (entrega em `~/Downloads/Motion - * vN/`, bancada em `~/Downloads/.estagiario/`), `{projeto}` é esse caminho, e o cliente é opcional: sem ele `{cliente}` = `nenhum (trabalho avulso)` e `{perfil}` = `nenhum (sem identidade visual: siga um estilo neutro e limpo)`, e as tarefas automáticas recusam (o estilo delas é o `marca.json` do cliente). Vídeo/transcrição fora da cwd entram como `--add-dir`. O job leva `destino: "projeto" | "downloads"` e, no avulso, `project_id: null` (sem evento no histórico, sem Prévia/Ajustar).
- O painel lista toda pasta que case com `^Motion - .+ v\d+$` no nível do projeto. O seletor de vídeo bruto ignora essas pastas, além de `Links/`, das pastas ocultas e do auto-save do Premiere.

### Regras de entrega (inserts, padrão aprovado em 2026-10-19)

- **Só a animação** nos MOVs, nunca a apresentadora nem o vídeo. O usuário monta no Premiere.
- **Só os elementos, sem fundo** (2026-10-20): nada de campo de cor, textura ou fundo trocado atrás da pessoa. Fundo próprio só quando as observações pedirem, com `(tela cheia)` no nome; o `entregar.py` recusa um MOV com quadro 100% opaco sem esse qualificador.
- **ProRes 4444 com alpha, 30 fps**, 1080×1920 (ou o formato do clipe).
- **Um arquivo por insert.**
- **Nome**: `MM-SS-FF - <número> <título>[ (<qualificadores>)].mov`, com o timecode de **entrada** no clipe a 30 fps (minutos-segundos-quadros). Exemplo: `00-29-06 - 5b AGORA (tela cheia, com transicao).mov`.
- **Texto atrás da cabeça**, três arquivos com o mesmo timecode, número e título:
  - `… (camada ATRAS).mov`
  - `… (camada FRENTE).mov`
  - o recorte `<TC> - Recorte <pessoa> (alpha, video inteiro).mov`, **só quando pedido** (desde 2026-10-29 o padrão é o editor recortar à mão)

  A ordem de empilhar é vídeo → ATRÁS → recorte → FRENTE.
- **Telas múltiplas viram arquivos separados**, cada uma com seu número e letra (`5a`, `5b`). O segundo arquivo começa um pouco antes da virada e mantém a transição (`com transicao`).
- **Sem transição de início ou fim de take.** O insert termina segurando o quadro final.
- **Sem HUD.**

Como o painel lê os nomes (`editor.parse_delivery_name`):

| Parte do nome | Como é lida |
|---|---|
| `NN-NN-NN - ` | timecode |
| começa com `Recorte` | recorte do vídeo inteiro |
| `<número><letra opcional> <título>` | número e título do insert |
| qualificadores entre parênteses | separados por vírgula: `camada ATRAS`, `camada FRENTE`, `tela cheia` (= tem fundo próprio, só se pedido), `com transicao` (sem acento; com acento também é aceito) |
| fora desse padrão | aparece em "outros" |

### Nomes das outras tarefas (2026-10-20)

| Tarefa | Nome |
|---|---|
| GC | `MM-SS-FF - <n> GC <Nome sem acento>.mov`, um por aparição (o painel lê como insert nº n) |
| Legendas | `00-00-00 - Legendas (video inteiro).mov` |
| Motion curto | `00-00-00 - <n> <Título>.mov` (ou o timecode de entrada, se for sobre o vídeo) |

- **Marcador de origem.** A skill cria a pasta de entrega com um arquivo oculto `.estagiario-origem`, que guarda o caminho do `{trabalho}`. É assim que ela sabe que pode reentregar na mesma pasta, e que recusa uma `Motion - … vN` que já existia sem ela. O app lê o mesmo marcador para achar a bancada do ajuste e o vídeo bruto da prévia (§2b). O painel ignora arquivos ocultos e subpastas (`_anteriores/`).
- **Conferência.** Antes de copiar, a skill recusa a entrega inteira se algum MOV estiver fora do padrão: nome, ProRes 4444, alpha, 30 fps, tamanho do vídeo, ou fundo opaco sem `(tela cheia)`.

## 5. Formato do perfil de identidade

```
<pasta do cliente>/_Estagiário/
  perfil.json          ← do app (status, pastas da última análise). A skill NÃO escreve aqui.
  marca.md             ← manual de IDV em markdown
  frame.md             ← tokens: markdown com UM bloco ```yaml no esquema da skill (ver abaixo)
  regras.md            ← preferências só deste cliente (aprovado/recusado, entrega)
  pessoas.csv          ← CSV de verdade: cabeçalho `nome,cargo`, cargo com vírgula entre aspas (fonte dos GCs). A tela mostra como lista "• nome — cargo"
  duvidas.md           ← opcional: o que a análise não conseguiu decidir
  assets/fonts/        ← .otf/.ttf
  assets/logos/        ← PNG/SVG transparentes
  assets/texturas/     ← grão, papel etc.
  referencias/         ← vídeos e projetos aprovados (ex.: entrega do 049 v3)
  marca.json           ← TEMPLATE: o frame.md no esquema fixo, validado (check_marca.py). Toda tarefa copia; o automático exige
  templates/gc/        ← TEMPLATE opcional: o GC aprovado em HyperFrames, com {{NOME}} e {{CARGO}} (+ template.json {"fps": 30}, assets/)
  amostras/            ← do app: Legenda.mp4 e GC.mp4 renderizados do template (sem IA), mostrados na aba Identidade
  .amostras/           ← oculto: bancada das amostras
  .analise/            ← oculto: rascunho do onboarding (inventário, folhas de contato, paletas). O app ignora
```

- **Esquema do `frame.md`.** As chaves de primeiro nível são `cores`, `degrades`, `tipografia` (`fontes[]`, `papeis{}`), `eases`, `componentes` (`realce`, `chip`, `gc`, `legenda`, `sombra_texto`, `texto_degrade`), `safe`, `formatos`, `logos` e `texturas`. O esquema completo, com exemplo, está em `estagiario/skill/estagiario-editor/references/perfil.md` §3.
  - **Arquivos:** fontes, logos e texturas são nomes de arquivo dentro de `assets/…` do perfil.
  - **Cores:** uma cor é um nome em `cores`, `g:<degradê>` ou um valor literal.
  - **Medidas:** em px de 1080×1920.
  - **Como a skill usa:** em cada tarefa, ela traduz o bloco para `{trabalho}/marca.json` e o valida antes de desenhar. O que é texto (vocabulário de inserts, motivos, faça/não faça) mora no `marca.md`.

- **Templates** (`estagiario/skill/estagiario-editor/references/templates.md`). A análise de IDV escreve `marca.json` e, se houver GC aprovado, `templates/gc/`; um perfil já analisado ganha os dois pelo job "Gerar templates" (`DEFAULT_PROMPT_TEMPLATES`, modo templates da skill, só lê o perfil). Quando a análise ou o job de templates termina bem e existe `marca.json`, o app dispara sozinho o job de amostras (`kit/auto.py amostras`, sem IA).
- **Lugar.** O perfil fica dentro da pasta do cliente, em `_Estagiário/`. O "_" põe a pasta no topo do Finder e, sem número, o scanner de projetos do Clientela não a lê como projeto. **Não renomeie para `00-…`**: viraria o "projeto 00" e colidiria com pastas `00 - …` que os clientes já têm. A skill lê o perfil e, na análise, escreve nele; no resto da pasta do cliente ela só lê. A pasta só é criada quando algo é escrito (salvar, subir asset, analisar, abrir no Finder), nunca ao abrir a aba.
- **Textos.** Os textos são escritos atomicamente pelo app (com `.bak`). A skill pode reescrevê-los inteiros.
- **Status.** É do app, em `perfil.json`:
  - `sem_perfil` → `em_analise` (job rodando) → `em_revisao` (rascunho) → `pronto`;
  - **só o usuário marca `pronto`**;
  - uma análise que termina bem deixa o perfil em `em_revisao`.
- **Perfil não pronto.** O projeto pode rodar com um perfil que não está `pronto`, e o painel avisa. A skill deve tratar `regras.md` como mais forte que `marca.md`, e o que o usuário escreveu como mais forte que o que ela mesma inferiu.
- **Backup.** O perfil vai junto com a pasta do cliente, no backup do próprio usuário. O backup diário do app (`data/`) não o inclui.

## 6. O que o app ainda não faz (v1)

- Não tem fila: um segundo "Gerar" é recusado enquanto houver job rodando.
- Não registra horas do job no cronômetro. O tempo da IA não é o tempo do editor.
- Ao excluir um cliente no app, a pasta `_Estagiário/` fica onde está (o app nunca apaga a pasta do cliente).
