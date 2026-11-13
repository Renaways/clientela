# Onboarding: análise de identidade visual de um cliente

O app chama com `{cliente}`, `{pasta_cliente}`, `{perfil}` (= `<pasta do cliente>/_Estagiário`, estrutura já criada pelo app), `{pastas_analise}` (uma lista `- <caminho>`, ou a pasta inteira do cliente) e `{observacoes}`.

**O objetivo** é um perfil que deixe uma tarefa futura fazer motion **com cara do cliente sem perguntar nada**. O usuário revisa o perfil na aba Identidade do app, e só ele marca "pronto".

## Limites

- **Escreva só em `{perfil}/`**, com uma exceção: o `perfil.json`, que é do app e nunca é escrito.
- `{pasta_cliente}` e as pastas estudadas são **só leitura**. Assets são **copiados** (`cp -n`), nunca movidos.
- **Rascunho da análise** (inventário, folhas, paletas): `{perfil}/.analise/`. É oculto, o app ignora, e fica como evidência.
- **Ninguém responde perguntas.** O que não der para decidir vai para `{perfil}/duvidas.md`, escrito como perguntas objetivas.

## Se já existe perfil

Leia `perfil.json` (status, pastas da última análise) e os textos atuais **antes** de tudo.

- Arquivo só com o esqueleto do app (títulos vazios): reescreva inteiro.
- **Arquivo com conteúdo**, sobretudo se o status for `pronto` ou se houver texto claramente escrito pelo usuário:
  - **não apague nem contradiga**;
  - acrescente o que a análise descobriu numa seção `## Da análise de <data> (inferido)`;
  - conflitos vão para o `duvidas.md` ("o manual diz X, o `regras.md` diz Y: qual vale?").
- `pessoas.csv`: sempre pelo `pessoas.py escrever`, que mescla sem apagar e mantém o cargo que já existe.

## Processo

As ferramentas estão em `<skill>/templates/onboarding/`. Rode-as de lá, com `--saida "{perfil}/.analise"`.

1. **Inventário:**

   ```bash
   python3 inventario.py --saida "{perfil}/.analise" <pastas…>
   ```

   Leia o `inventario.md` inteiro. Ele separa manuais, entregas aprovadas, fontes (família, peso e itálico lidos do arquivo), logos candidatos (com e sem alpha), texturas, vídeos, projetos e planilhas.
2. **Manual de marca primeiro.** Se houver PDF de brandbook/IDV, leia com o Read (até 20 páginas por chamada). Cores, fontes e usos do logo que estão no manual **valem mais** que o que você medir.
3. **Olhe as entregas.** Faça folhas de contato (`folhas.py`) e **abra cada imagem**. Selecione até ~15 vídeos:
   - a versão mais alta de cada `Motion - … vN` (o padrão aprovado);
   - renders com alpha (GC, legendas, inserts): o grafismo puro;
   - finais ou exports curtos;
   - vídeos que mostram tipos diferentes de peça.

   Anote o que se repete: família e peso de cada papel tipográfico, cores de texto e de fundo, como a palavra-chave é destacada (pílula? sublinhado? cor?), GC (layout, barras, ícone), legenda (camadas, posição), entradas e saídas, ritmo, texturas e o que nunca aparece.
4. **Cores.**
   - `paleta.py` nos logos e nos grafismos com alpha (o vídeo cru mistura pele e cenário).
   - Arredonde para os hex do manual quando houver.
   - Separe: cores da **marca**, **neutros** (temperatura: quente ou frio?) e **degradês** (direção e paradas).
5. **Fontes.**
   - Copie para `{perfil}/assets/fonts/` só as famílias que **aparecem nas peças**, todos os pesos e estilos delas.
   - Fonte "Trial" ou sem licença clara vai para o `duvidas.md`.
   - Fonte usada nas peças mas ausente das pastas: registre o nome e uma alternativa próxima no `duvidas.md`, e não copie nada de fora.
6. **Logos** em `assets/logos/`: só PNG transparente ou SVG, nas versões que existirem (completo e reduzido, claro e escuro, ícone, mascote). Dê nomes claros (`logo-completo-branco.png`). Se só houver `.ai`/`.eps`/`.pdf`, não converta à força: registre no `duvidas.md` quais arquivos exportar.
7. **Texturas** (grão, papel, fundos usados nas peças) em `assets/texturas/`.
8. **Referências** em `referencias/`: a entrega aprovada mais recente (os MOVs), até ~2 GB no total. O resto fica listado por caminho no `marca.md` ("Referências").
9. **Pessoas.**
   - Fontes: os CSVs/planilhas do inventário (`pessoas.py ler <csv>` detecta colunas e separador), mais os GCs **legíveis** nas folhas.
   - Grave com `pessoas.py escrever "{perfil}" <lista.json>`.
   - Nunca deduza cargo: sem cargo escrito em algum lugar, deixe vazio e pergunte no `duvidas.md`.
10. **Escreva os textos** (abaixo) e confira que todo arquivo citado existe.
11. **Gere os templates** (`templates.md`): `{perfil}/marca.json` validado pelo `check_marca.py` e, se houver GC aprovado, `{perfil}/templates/gc/`. São eles que deixam legendas e GC automáticos, sem IA, para sempre: capriche na legenda (meça a aprovada) e no GC.
12. **Mensagem final curta:** o que foi preenchido, quantos assets, os templates gerados e as 3 dúvidas mais importantes. O app renderiza as amostras dos templates sozinho.

## O que vai em cada arquivo

### `marca.md`: o manual, em texto

Siga as seções do esqueleto do app e acrescente as que fizerem sentido:

- **A marca**: o que o cliente faz, o tom e as 2–3 ideias que guiam o visual.
- **Logo e usos**: versões, quando usar cada uma, área de respiro, o que nunca fazer (redigitar o wordmark, distorcer, aplicar sobre fundo X).
- **Cores**: papel de cada cor, proporção de uso, contraste (o que vai sobre o quê).
- **Tipografia**: família e peso de cada papel (destaque, apoio, narrativa, número, GC, legenda), caixa, tracking, a regra da palavra-destaque.
- **Elementos gráficos**: formas, texturas e motivos próprios (com o arquivo de referência).
- **Linguagem de movimento**: como as coisas entram e saem, as curvas (em nomes GSAP), as durações e o ritmo.
- **Componentes existentes**: GC, legenda, encerramento, vinheta. Para cada um, layout medido + movimento + o arquivo de referência.
- **Faça / não faça.**
- **Referências**: os caminhos das entregas aprovadas e o que cada uma ensina.

**Cada afirmação com evidência**: "(manual p. 12)", "(visto em `…/Motion - Inserts v3/00-11-09 …`)". Afirmação sem evidência leva "(inferido)".

### `frame.md`: os tokens

- Markdown com **um bloco** ` ```yaml ` no **esquema de `perfil.md` §3**: `cores`, `degrades`, `tipografia.fontes`/`papeis`, `eases`, `componentes` (`realce`, `chip`, `gc`, `legenda`, `sombra_texto`, `texto_degrade`), `safe`, `formatos`, `logos`, `texturas`.
- Os arquivos são **nomes** dentro de `assets/…` do perfil.
- Medidas em px de 1080×1920.
- Fora do bloco, 3–5 linhas dizendo de onde vieram os valores.
- O que é texto (vocabulário de inserts, motivos, regras de uso) vai no `marca.md`, não no YAML.

### `regras.md`: preferências deste cliente

O que **este** cliente aprovou e recusou, e como entregar:

- duração, formato, legendas sim ou não;
- encerramento;
- o que mudou entre versões aprovadas (v1 → v3 mostra o que foi corrigido).

Use frases curtas e imperativas. Marque "(inferido)" o que não estiver escrito em lugar nenhum. Este arquivo vence o `marca.md`: seja conservador.

### `pessoas.csv`

CSV de verdade: `nome,cargo`, com o cargo entre aspas quando tem vírgula. Só pelo `pessoas.py`.

### `duvidas.md`

Perguntas que o usuário responde em uma linha, agrupadas: **Licenças e arquivos** · **Cores e fontes** · **Componentes** · **Pessoas**. Sem dúvida retórica. Se não houver dúvida, não crie o arquivo.

## Erros a evitar

- Inventar hex, cargo, fonte ou regra para "completar" o perfil. Lacuna declarada é melhor que valor falso.
- Copiar a pasta do cliente inteira para `referencias/`.
- Tratar sub-marca, parceiro ou cliente do cliente (logos de terceiros dentro da pasta) como a marca. Na dúvida, pergunte no `duvidas.md`.
- Escrever fora de `{perfil}/`, ou no `perfil.json`.
