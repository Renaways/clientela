# Modo ajuste: refazer um elemento de uma entrega

O editor viu um elemento no Clientela (botão **Prévia**, sobre o vídeo) e pediu uma mudança só nele. O prompt diz "modo ajuste" e traz:

| Linha do prompt | O que é |
|---|---|
| `Tarefa original:` | a tarefa e a versão da entrega (Inserts v1, Legendas v2…) |
| `Bancada de trabalho:` | o `{trabalho}` **de quando a entrega foi feita**: `job.json`, `marca.json`, `palavras.json`, `jobs.json`, o gerador em `kit/`, `hf/`, `recorte.mov`, `DECISOES.md` |
| `Entrega:` | a pasta `Motion - … vN` que já existe |
| `Elemento a ajustar:` | número, título e timecode de entrada |
| `Arquivos dele na entrega:` | os MOVs desse elemento hoje |
| `O que o editor pediu:` | o pedido. É a autoridade máxima deste job |

## Regras

1. **Só esse elemento.** Não mexa em outro insert, não refaça o recorte, não rode `preparar.py` (ele reescreveria `job.json` e `palavras.json`). Não crie pasta de entrega nova: a entrega continua a mesma.
2. **O pedido manda**, depois `regras.md`, depois o resto (`SKILL.md` §"Regras que valem sempre"). Se o pedido for impossível ou contradizer a fala (texto que não foi dito, nome que não está no `pessoas.csv`), faça o mais próximo possível e registre.
3. **Só os elementos, sem fundo**, como em toda entrega. Se o elemento antigo tinha fundo (`tela cheia`) e o pedido não fala nisso, mantenha como estava.
4. **Rápido.** O editor está esperando um elemento só: uma rodada de revisão em movimento costuma bastar.

## Processo

1. **Situe-se** na bancada: leia `DECISOES.md`, `jobs.json` (qual `comp` gera os arquivos do elemento) e o gerador em `kit/` (normalmente `kit/inserts.py`, `kit/legendas.py` + `legendas.json`, ou `gcs.json` + `kit/gc.py`). Ache o bloco do elemento.
2. **Atualize só o `entregar.py` da bancada**, que é de quando a entrega foi feita e pode não ter o `--substituir`:

   ```bash
   cp ~/.claude/skills/estagiario-editor/templates/kit/entregar.py "{trabalho}/kit/entregar.py"
   ```

   Não troque os outros scripts do kit: o gerador antigo depende deles como estavam.
3. **Mude o bloco** do elemento no gerador e rode o gerador de novo. Ele reescreve todos os HTMLs, mas só o do elemento muda. Se o tempo de entrada mudar, mude o `inicio` no `jobs.json` (pelo gerador) e o nome do arquivo sai com o timecode novo.
4. **Revise em movimento** só esse elemento (`kit/review.py camadas|unica …`) e rode `npx hyperframes lint` em `hf/_check/<comp>/`.
5. **Renderize só ele:** `python3 kit/render.py <comp> [<comp-frente>]`.
6. **Entregue no lugar do antigo:**

   ```bash
   python3 kit/entregar.py --substituir <número> "renders/<arquivo novo 1>.mov" ["renders/<arquivo novo 2>.mov"]
   ```

   `<número>` é o do elemento (`3`, `5a`); sem número (legendas), use o nome sem qualificadores (`Legendas`). Os arquivos antigos vão para `{entrega}/_anteriores/<data>/`, e os novos entram no lugar. Um elemento de duas telas (`5a` + `5b`) que mudou nas duas leva `--substituir 5a --substituir 5b`.
7. **Registre** no fim do `DECISOES.md`:

   ```markdown
   ## Ajuste <AAAA-MM-DD> · <número> <título>
   - Pedido: "<o que o editor pediu>"
   - Feito: <o que mudou, em uma ou duas linhas>
   - Arquivos: `<novo>.mov` (o anterior está em `_anteriores/<data>/`)
   ```

   A última mensagem da sessão é um resumo curto disso.

## Se a bancada não servir

Se faltar `job.json` ou o gerador do elemento (a bancada foi apagada ou a entrega foi feita à mão), não tente reconstruir a entrega: registre o motivo em `{trabalho}/DECISOES.md`, se a pasta existir, e termine dizendo que o ajuste precisa de uma versão nova (botão Gerar, com o pedido nas observações).

## Ajuste na prévia (insert ainda não exportado)

O prompt diz "modo ajuste, na prévia" e traz `Insert a ajustar:` e `Composições dele:` (`hf/<comp>.html`). Não há entrega desse insert ainda:

1. Mude só essas composições: no gerador da bancada (`kit/inserts.py`) e rode-o, ou, se for `u-…`, no `inserts.json` + `python3 kit/universais.py`. Mantenha o nome das composições e a entrada delas no `jobs.json`.
2. Revise em movimento e passe o lint.
3. **Não renderize nem entregue.** O editor confere na prévia e exporta de lá.
4. Registre o ajuste no `DECISOES.md`.

O app pode ter escrito um `<style id="clientela-fonte">` no HTML (a fonte escolhida na prévia). Se você regenerar o HTML, ele some, e o app o põe de volta na próxima vez que a prévia salvar. Não o copie para o gerador.
