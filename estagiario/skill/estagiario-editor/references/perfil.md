# O perfil do cliente e o `marca.json`

## 1. Onde fica e o que tem

```
<pasta do cliente>/_Estagiário/
  perfil.json        ← do app. Pode ler (status, pastas da última análise); NUNCA escreva.
  marca.md           ← manual de IDV em texto: a marca, logo e usos, cores, tipografia, elementos, movimento, faça/não faça
  frame.md           ← tokens em YAML, no esquema da §3 (é o que vira marca.json)
  regras.md          ← preferências só deste cliente: o que foi aprovado, o que foi recusado, como entregar
  pessoas.csv        ← CSV de verdade, cabeçalho `nome,cargo` (fonte dos GCs e do nome no recorte)
  duvidas.md         ← o que a análise não conseguiu decidir (o usuário lê e apaga)
  assets/fonts/      ← .otf/.ttf
  assets/logos/      ← PNG/SVG transparentes
  assets/texturas/   ← grão, papel etc.
  referencias/       ← vídeos e entregas aprovadas
```

- **Status** (em `perfil.json`): `sem_perfil` → `em_analise` → `em_revisao` → `pronto`. Um perfil que não está `pronto` também serve, mas trate o que ele infere com cautela e diga no `DECISOES.md` que o perfil ainda está em revisão.
- **Autoridade:** `regras.md` > `marca.md`/`frame.md`. Se o `frame.md` disser uma coisa e o `regras.md` outra, vale o `regras.md`, e a divergência vai para o `DECISOES.md`.
- `pessoas.csv`: leia com o módulo `csv`, porque o cargo pode ter vírgula entre aspas. Nunca faça `split(",")`.

## 2. Ler o perfil numa tarefa

1. **`regras.md` inteiro.** São as preferências que já custaram uma versão. Anote no `DECISOES.md` as que se aplicam a esta tarefa.
2. **`marca.md` inteiro**, uma vez. É o porquê das escolhas: a ideia da marca, o tom e o que nunca fazer.
3. **`frame.md`** → escreva `{trabalho}/marca.json` no esquema da §3. A tradução é mecânica, porque as chaves são as mesmas. Resolva só três coisas:
   - **Arquivos.** Os caminhos de fonte, logo e textura são **nomes de arquivo** dentro de `assets/fonts|logos|texturas` do perfil. Confira que existem: o `preparar.py` já os copiou para `hf/assets/`.
   - **Lacunas.** Se o `frame.md` não define um papel, um componente ou um ease, preencha pelo `marca.md`. Se nem o `marca.md` define, use o neutro (§5) e registre cada lacuna no `DECISOES.md`.
   - **YAML solto.** Se o `frame.md` estiver fora do esquema (YAML de outro formato, ou só texto), traduza pelo sentido e diga isso no `DECISOES.md`. Não reescreva o `frame.md`: na tarefa, o perfil é só leitura.
4. `python3 kit/check_marca.py`: valida hex, referências de cor e arquivos de fonte. Corrija até passar.

## 3. Esquema (`frame.md` em YAML = `marca.json` em JSON)

As chaves são as mesmas nos dois arquivos. O `frame.md` é um markdown com **um bloco** ` ```yaml `, como no esqueleto que o app cria (front matter `---` também é aceito). Fora do bloco, o texto é livre. Todas as medidas estão em px de um quadro **1080×1920**; o kit converte para outros formatos (§4).

```yaml
cores:                       # nome → hex. Viram var(--<nome>) no CSS
  fundo: "#101010"
  texto: "#FFFFFF"
  acento: "#2F6BFF"
degrades:                    # nome → gradiente CSS. Viram var(--g-<nome>)
  acento: "linear-gradient(100deg, #5B8CFF 0%, #2F6BFF 100%)"

tipografia:
  fontes:                    # arquivos em assets/fonts/ do perfil → @font-face
    - { familia: "Marca Sans", peso: 900, estilo: italic, arquivo: "MarcaSans-BlackItalic.otf" }
    - { familia: "Marca Sans", peso: 700, estilo: normal, arquivo: "MarcaSans-Bold.otf" }
  papeis:                    # cada papel vira a classe .p-<papel>
    destaque:            { familia: "Marca Sans", peso: 900, estilo: italic, caixa: alta, entrelinha: 0.92, tracking: "-0.012em", cor: texto }
    apoio:               { familia: "Marca Sans", peso: 700, estilo: normal, entrelinha: 1.05, cor: texto }
    narrativa:           { familia: "Marca Sans", peso: 400, estilo: italic, entrelinha: 1.1, cor: texto }
    numero:              { familia: "Marca Sans", peso: 900, estilo: normal, entrelinha: 0.85, tracking: "-0.02em", cor: acento }
    rotulo:              { familia: "Marca Sans", peso: 700, estilo: normal, caixa: alta, tracking: "0.12em", cor: texto }
    gc_nome:             { familia: "Marca Sans", peso: 700, estilo: italic, caixa: alta, px: 40, cor: texto }
    gc_cargo:            { familia: "Marca Sans", peso: 700, estilo: italic, px: 29, cor: texto }
    legenda_apoio:       { familia: "Marca Sans", peso: 700, estilo: normal, px: 48, cor: texto }
    legenda_destaque:    { familia: "Marca Sans", peso: 900, estilo: italic, caixa: alta, px: 84, cor: acento }
    legenda_complemento: { familia: "Marca Sans", peso: 700, estilo: italic, px: 48, cor: acento }

eases:                       # nomes GSAP. Viram E.<nome> no JS
  chegada: "expo.out"
  saida: "power3.in"
  travessia: "power4.inOut"
  elastico: "back.out(1.5)"  # só para ícones, chips e números; nunca em texto
  respiro: "sine.inOut"

componentes:
  realce:                    # o destaque da palavra-chave (a "pílula" de cada marca)
    tipo: pilula             # pilula | sublinhado | cor
    fundo: "g:acento"        # cor ou degradê da pílula / do sublinhado
    texto: fundo             # cor do texto sobre a pílula (tipo pilula)
    raio: 0                  # px
    sombra: "0 18px 40px rgba(0,0,0,.32)"
  texto_degrade: "g:acento"  # opcional: degradê para texto (.tdeg); omita se a marca não usa
  sombra_texto: "0 6px 22px rgba(0,0,0,.55)"
  chip: { fundo: "g:acento", texto: fundo, raio: 0 }   # chips de nome / tag
  gc:                        # lower third; omita para o GC neutro do kit
    x: 105
    y: 932
    largura: 452
    nome_fundo: "g:acento"
    cargo_fundo: fundo
    raio: 0
    logo: null               # arquivo em assets/logos/ que acompanha o GC (opcional)
    duracao: 4.5
    descricao: "barra do nome revela L→R, cargo 0,4 s depois, segura, recolhe R→L"
  legenda:
    faixa_y: [1050, 1500]    # faixa vertical do bloco de legenda
    max_palavras: 4
    destaque_por_bloco: 1

safe:                        # áreas proibidas em 9:16 (UI das redes)
  topo: 250
  base: 420
  esquerda: 64
  direita: 140

formatos:                    # escala tipográfica fora do 9:16
  paisagem: 0.75
  quadrado: 0.85

logos:                       # papel → arquivo em assets/logos/
  claro: "logo-branco.png"
  escuro: "logo-preto.png"
texturas:                    # papel → arquivo em assets/texturas/
  grao: "grao.png"
```

**Referência de cor** (em `cor`, `fundo`, `texto`, `nome_fundo`…):

| Forma | Exemplo | Vira |
|---|---|---|
| nome em `cores` | `acento` | `var(--acento)` |
| `g:` + nome em `degrades` | `g:acento` | `var(--g-acento)` |
| literal | `#FFFFFF`, `rgba(0,0,0,.5)`, `transparent` | o próprio valor |

**Obrigatórios:** `cores` com pelo menos `fundo` e `texto`, `tipografia.papeis.destaque` e `componentes.realce`. Todo o resto tem padrão (§5).

## 4. Formatos

- O kit lê largura, altura e fps do vídeo bruto (`job.json`). 1080×1920 é a referência.
- O helper `k.px(n)` converte uma medida de 1080×1920 para o formato do vídeo:
  - 9:16: escala = largura/1080;
  - paisagem: escala = altura/1080 × `formatos.paisagem`;
  - quadrado: escala = largura/1080 × `formatos.quadrado`.
- As safe zones só valem para o 9:16. Em paisagem, use a margem de 5% da largura.

## 5. Perfil neutro (sem perfil ou com lacunas)

Use quando `{perfil}` não existe, está vazio (só o esqueleto do app) ou falta uma chave. **Avise no topo do `DECISOES.md`:** "Trabalhei sem manual de marca (perfil vazio): visual neutro; preencha o perfil e gere uma v nova."

- Fonte: `"Helvetica Neue", "Inter", Arial, sans-serif` (do sistema, sem `@font-face`). Destaque em peso 800 caixa alta, apoio em 600, narrativa em 400 itálico.
- Cores: `fundo #111111`, `texto #FFFFFF`, `acento #FFFFFF`.
- Realce: pílula branca com texto `#111111`, raio 0.
- Eases: os da §3.
- Sem logo e sem textura. Nunca invente um logo nem redigite o nome da marca como wordmark.
