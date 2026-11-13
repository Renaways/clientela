# templates/kit: ferramentas de uma tarefa de projeto

O kit nasceu do projeto aprovado do 049 v3 (2026-10-19), generalizado: **nenhum valor de marca está aqui**. Tudo vem de `{trabalho}/marca.json`, que você escreve a partir do `frame.md` do perfil (`references/perfil.md`).

**Copie o kit para o trabalho**, que é onde ele roda, e não edite os originais da skill:

```bash
cp -R "<skill>/templates/kit" "{trabalho}/kit"
```

## Layout de `{trabalho}/`

```
DECISOES.md        decisões, dúvidas e próximos (headless.md §2)
job.json           caminhos, vídeo (largura, altura, duração), perfil, transcrição, recorte   ← preparar.py
palavras.json      transcrição normalizada: [{texto, t, fim}]                                 ← preparar.py
transcricao.txt    uma linha por frase: "[00:11.30] primeiro@11.30 debate@11.54 …"            ← preparar.py
marca.json         tokens do perfil no esquema fixo                                            ← você
mapa.md            o mapa de inserts / GCs                                                     ← você
jobs.json          o que renderizar e com que nome                                             ← gerador (write_jobs)
kit/               estes scripts (+ o seu gerador: inserts.py, cópia de exemplo_inserts.py)
hf/                projeto HyperFrames: <comp>.html, assets/{fonts,logos,texturas}, _check/<comp>/
recorte.mov        a pessoa recortada (remove-background)
review/            folhas de revisão
renders/           MOVs finais, já com o nome de entrega
```

## Scripts

| Script | Faz | Uso |
|---|---|---|
| `auto.py` | o pipeline **sem IA** que o app roda: `legendas`, `gc`, `amostras`, `bancada` (só prepara, para inserts à mão), `exportar` (render + entrega dos aprovados na prévia), e `motion` (prepara a bancada e faz `exec` do `claude`) | `python3 kit/auto.py <modo> --args args.json` (fala stream-json) |
| `preparar.py` | `job.json`, transcrição (Premiere JSON / srt / vtt / txt do Premiere ou roteiro alinhado ao ASR → `correcoes.md` / ASR), assets do perfil, `hf/`, `DECISOES.md` | `python3 kit/preparar.py --video … --transcricao … --perfil … --entrega … --versao vN --tarefa … --cliente …` |
| `check_marca.py` | valida o `marca.json` (cores, refs, fontes, realce, logos) | `python3 kit/check_marca.py` |
| `common.py` | gera o CSS da marca e os helpers JS; `write()`, `two_layers()`, `realce()`, `chip()`, `px()`, `marcas()`, `write_jobs()` | importado pelos geradores |
| `exemplo_inserts.py` | um insert de cada tipo, só com tokens | copie para `kit/inserts.py` e reescreva |
| `universais.py` + `universais.json` | templates universais de insert (frase, número, palavra, lista, troca, citação), padrão ou clean, na marca do cliente | escreva `inserts.json` e rode `python3 kit/universais.py` |
| `gc.py` | um GC por aparição, a partir de `gcs.json` + `pessoas.csv` | `python3 kit/gc.py` |
| `legendas.py` | legenda palavra a palavra em 3 camadas | `python3 kit/legendas.py blocos`, revisar `legendas.json`, depois `python3 kit/legendas.py html` |
| `review.py` | folha de revisão em movimento, composta como na edição | `python3 kit/review.py camadas <base> <inicio> <dur> [fps]` ou `unica <comp> …`, ou `--at t1,t2` |
| `render.py` | 120 fps → 4 subquadros → 30 fps ProRes 4444 + alpha, cortes, nome por timecode | `python3 kit/render.py [comp …]` · `--lista` |
| `entregar.py` | confere (nome, ProRes 4444, alpha, 30 fps, tamanho, sem fundo) e copia para `{entrega}/`; recorte no nome certo; `--substituir` troca só um elemento | `python3 kit/entregar.py --recorte recorte.mov --pessoa <Nome>` · `--substituir 3 renders/…mov` |

## Helpers do `common.py`

### No HTML (Python)

| Helper | Faz |
|---|---|
| `px(n)` | medida de 1080×1920 convertida para o formato do vídeo |
| `realce(texto, id)` | palavra-chave no realce do perfil (pílula, sublinhado ou cor) |
| `chip(texto, id, css)` | chip de nome ou tag |
| `cor(ref)` | referência de cor do perfil em CSS |
| `papel(nome)` | o papel tipográfico já resolvido |

Classes que o CSS gerado oferece:

| Classe | O que é |
|---|---|
| `.p-<papel>` | `p-destaque`, `p-apoio`, `p-narrativa`, `p-numero`, `p-rotulo`, `p-gc-nome`, `p-gc-cargo`, `p-legenda-*` |
| `.mask` | máscara com folga para acentos |
| `.split` | divide em `.w` (palavra) e `.c` (letra) |
| `.sombra` | sombra do texto, do perfil |
| `.tdeg` | texto em degradê, se o perfil tiver |
| `.cor-<nome>` / `.bg-<nome>` | cor do texto / do fundo |
| `.abs` / `.full` | posicionamento |

### No JS da timeline

| Helper | Faz |
|---|---|
| `letras(sel, t, {rot, st, d})` | letras cinéticas |
| `sai(sel, t)` | troca de frase (as letras saem para cima) |
| `realceIn(id, t)` / `realceOut(id, t)` | entra e sai o realce |
| `chipIn(sel, t, ±1)` | entra o chip, inclinado para um lado |
| `deriva(sel, t, dur, 1.04)` | hold vivo (deriva lenta de escala) |
| `ignite`, `punch`, `rise` | entradas com blur, com impacto, subindo |
| `blur("y", t, dur, pico)` | motion blur direcional real |
| `E.chegada`, `E.saida`, `E.travessia`, `E.elastico`, `E.respiro` | eases do perfil |

## Medidas de tempo

- O `t` local de um insert é o tempo no clipe menos o início do insert.
- `marcas(t0, t1)` imprime as palavras já em tempo local; cole a saída como comentário no bloco do gerador.
- O `inicio` do `jobs.json` é o tempo no clipe. O `render.py` converte em `MM-SS-FF` (30 fps).

## Custos (medidos num clipe 1080×1920)

| Etapa | Tempo |
|---|---|
| `remove-background` | ≈ 2 min por 30 s |
| `render.py` | ≈ 5 s por segundo de composição a 120 fps |

O recorte em ProRes 4444 ocupa ≈ 50 MB por segundo de vídeo.
