# Entrega

Regras do usuário, aprovadas em 2026-10-19, e o formato que o painel do Clientela lê (`editor.parse_delivery_name`). **O nome do arquivo é a interface**: fora do padrão, o arquivo cai em "outros" no painel.

## 1. Regras gerais

- **Só a animação** nos MOVs, nunca a pessoa nem o vídeo bruto dentro. O editor monta no Premiere.
- **Só os elementos, sem fundo** (decisão do usuário, 2026-10-20). Texto, realce, chips, ícones e fotos sobre transparência; nada de campo de cor, textura, papel ou fundo trocado atrás da pessoa. Se o elemento precisar de leitura sobre o vídeo, use a sombra do perfil (`.sombra`), não um fundo. Fundo próprio só quando as observações pedirem, com `(tela cheia)` no nome. O `entregar.py` recusa um MOV com quadro 100% opaco sem esse qualificador.
- **ProRes 4444 com alpha, 30 fps**, no tamanho do vídeo bruto (normalmente 1080×1920).
- **Um arquivo por peça** (insert, GC).
- **Sem transição de início ou fim de take.** A peça termina **segurando o quadro final**: nada de máscara entrando ou saindo, nada de animação de saída. As animações internas, como uma frase trocando por outra, continuam valendo. O GC é a exceção: a saída faz parte do componente.
- **Sem HUD**: cantos, labels e timecode na tela foram recusados.
- **Pasta**: `{entrega}/` = `{projeto}/Motion - <Tarefa> vN/`. Ela é criada pelo `kit/entregar.py`, nunca à mão.

## 2. Nome dos arquivos

```
MM-SS-FF - <número><letra?> <título>[ (<qualificadores>)].mov
```

- **`MM-SS-FF`** é o timecode de **entrada** da peça no clipe, a 30 fps: minutos-segundos-quadros. O `kit/render.py` calcula (`tc()`), não faça à mão. Exemplo: 29,2 s → `00-29-06`.
- **Número** é a ordem na fala (1, 2, 3…). A letra separa as telas de um mesmo insert (`5a`, `5b`).
- **Título** é curto, sem acento (`Atencao`, `Camera`), com as palavras da fala.
- **Qualificadores**, entre parênteses e separados por vírgula, sem acento:

  | Qualificador | Quando |
  |---|---|
  | `camada ATRAS` / `camada FRENTE` | texto atrás da cabeça (§3) |
  | `tela cheia` | tem fundo próprio e cobre o quadro inteiro (só quando as observações pedirem) |
  | `com transicao` | segunda tela de um insert, que começa na virada (§4) |
  | `video inteiro` | dura o vídeo todo (recorte, legendas) |

### Por tarefa

| Tarefa | Exemplos |
|---|---|
| Inserts | `00-11-09 - 3 Debate (camada ATRAS).mov` · `00-11-09 - 3 Debate (camada FRENTE).mov` · `00-49-27 - 7 Camera.mov` · `00-23-03 - 5a ANTES.mov` + `00-29-06 - 5b AGORA (com transicao).mov` |
| Recorte | `00-00-00 - Recorte <Pessoa> (alpha, video inteiro).mov` |
| GC | `MM-SS-FF - <n> GC <Nome da Pessoa>.mov`, um por aparição: `00-02-12 - 1 GC Ana Souza.mov` |
| Legendas | `00-00-00 - Legendas (video inteiro).mov` |
| Motion curto | `00-00-00 - 1 <Título>.mov` (com mais de uma peça: `2 …`, `3 …`) |

## 3. Texto atrás da cabeça: três arquivos

Quando o texto passa atrás da pessoa, saem três arquivos com o mesmo timecode, número e título:

1. `… (camada ATRAS).mov`: o que fica atrás da pessoa;
2. `… (camada FRENTE).mov`: o que fica na frente;
3. `00-00-00 - Recorte <Pessoa> (alpha, video inteiro).mov`: a pessoa recortada, o vídeo inteiro, **um só** por entrega.

A ordem de empilhar no Premiere é **vídeo → ATRÁS → recorte → FRENTE**.

- **Nome da pessoa**: vem do `pessoas.csv` (o primeiro nome basta), da apresentação na fala ou das observações, nessa ordem de confiança. Sem nenhuma dessas fontes, use `Apresentador` e registre no `DECISOES.md`.
- O recorte vem de `npx hyperframes remove-background` e é convertido pelo `entregar.py --recorte` para ProRes 4444 a 30 fps, no nome certo.

## 4. Telas múltiplas = arquivos separados

- Um insert que troca de tela (antes → agora, uma frase inteira que dá lugar a outra composição) vira **um arquivo por tela**: `5a …` e `5b … (com transicao)`.
- O segundo arquivo começa **um pouco antes da virada** e **mantém a transição**. Na edição, ele vai na trilha de cima.
- No `jobs.json` do `render.py`, isso é uma composição só, com `partes`.

## 5. Conferência (`kit/entregar.py`)

O script recusa a entrega se algo estiver fora do padrão:

- nome fora do regex do contrato;
- codec diferente de ProRes 4444;
- sem canal alpha;
- fps diferente do `job.json`;
- quadro 100% opaco (com fundo) sem `(tela cheia)` no nome;
- pasta de entrega que já existia sem ter sido criada por este trabalho.

Corrija a causa e rode de novo. Não renomeie o arquivo à mão para "passar".

## 6. Ajuste de um elemento

O Clientela pede o ajuste de **um** elemento de uma entrega já feita (`references/ajuste.md`). A entrega continua sendo a mesma pasta: os arquivos antigos do elemento vão para `{entrega}/_anteriores/<data>/` (o painel não lista subpastas) e os novos entram no lugar, pelo `entregar.py --substituir`. O resto da entrega não é tocado.
