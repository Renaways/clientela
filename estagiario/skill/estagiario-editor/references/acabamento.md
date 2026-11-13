# Acabamento: o padrão exigido e o que já foi reprovado

O usuário estudou quadro a quadro 21 referências do prompt-motion.com (2026-10-19) e aprovou os inserts do 049 depois de três rodadas. Isto vale para **qualquer cliente**; a marca de cada um entra pelo perfil.

> **Sem fundo (2026-10-20).** Depois do 049 o usuário pediu **só os elementos**: nada de campo de cor, textura, papel ou fundo trocado atrás da pessoa. O vídeo é sempre o fundo. Onde uma referência abaixo fala em "campo de cor" ou "tela cheia", ela vale só quando as observações pedirem fundo.

## 1. Os princípios que se repetem nas referências

1. **A composição é o texto gigante com a pessoa.** O vídeo é o fundo, e o texto ocupa 70–90% da largura. Nada de elemento pequeno boiando: o que preenche o quadro é a tipografia, não um fundo.
2. **A tipografia é a imagem.** Black ou condensada gigante, letras cinéticas, paredes de texto repetido, contraste entre itálico fino e black.
3. **Continuidade de objeto.** Uma coisa vira a outra (a frase vira a lista, a palavra-chave cresce e vira o título da próxima tela). Prefira isso a cortar.
4. **Ritmo.** Algo acontece a cada batida da fala (0,8 a 1,5 s), e nada fica parado: durante o hold, deriva de escala.
5. **Lista progressiva.** O item novo entra forte e os anteriores esmaecem (30%).
6. **Material real decomposto.** Prints viram componentes animados; nunca o print cru jogado na tela.
7. **Pessoa no vídeo.** Profundidade: o texto atrás da cabeça, os chips e stickers na frente.
8. **Contraste de ritmo** a serviço da narrativa: calmo × rápido, fino × black.

O que o usuário **não** quer das referências: chrome de **HUD** (colchetes nos cantos, labels, timecode, trilho de progresso). Foi testado e recusado.

## 2. O que foi reprovado (não repita)

| Rodada | Problema |
|---|---|
| v1 | Texto pequeno (22 a 50 px) |
| v1 | Formas de PowerPoint (púlpitos, TV-caixa, ícones genéricos) |
| v1 | A mesma entrada em todos os inserts |
| v1 | Rótulos "eyebrow" pequenos em cima dos títulos |
| v1 | Texto em degradê em todo lugar |
| v1 | Ignorar as referências da marca |
| Lote B (exagero) | Elementos demais por tela: fotos + chips + títulos; luz + 3D + grade de pontos + mira + brilho; cards + visor + REC |
| Lote B (exagero) | Objetos opacos cobrindo o corpo da pessoa |
| Lote B (exagero) | Ideias repetidas da versão anterior |
| Qualquer | **HUD** |
| 049 v1 (2026-10-20) | **Fundo**: campo escuro em tela cheia, fundo creme trocado atrás da pessoa. O usuário quer só os elementos |
| Qualquer | **Transição de início ou fim de take** (máscara entrando ou saindo, saída animada): o corte do take é do editor |

**Mais animação não é melhor animação.** Antes de adicionar um elemento, pergunte: "a frase já se sustenta sozinha?". Se sim, não adicione.

## 3. Regras de leitura (1080×1920)

| Elemento | Medida |
|---|---|
| Destaque | 150 a 236 px |
| Apoio | 72 a 92 px |
| Qualquer texto | nunca abaixo de 56 px (GC e legenda seguem os tamanhos do perfil) |
| Itens de lista | 90 px |

- Uma palavra-chave por bloco, no realce.
- Pessoas citadas recebem tratamento idêntico, na ordem em que aparecem na fala.
- Safe zones do 9:16: as do perfil (o padrão é topo 250, base 420, direita 140). A exceção aprovada é o bloco atrás da cabeça, que começa em y=214.

## 4. Auto-auditoria antes de renderizar

- [ ] Só os elementos, sem fundo (nada cobre o quadro inteiro, a não ser que as observações peçam)
- [ ] Só cores e fontes do `marca.json` nos grafismos (o vídeo e os prints de terceiros não contam)
- [ ] Uma palavra-chave por bloco, no realce do perfil
- [ ] Nada abaixo de 56 px; o destaque é gigante
- [ ] Nada cobrindo o rosto ou a boca; a palavra-chave não ficou escondida atrás da cabeça
- [ ] Nenhum texto cortado (bordas, itálico, acentos)
- [ ] As entradas usam `E.chegada`; nada de bounce em texto; o hold tem deriva
- [ ] Sem HUD, sem transição de take; o insert segura o quadro final
- [ ] Nada inventado (nomes, cargos, números)
- [ ] O que o `regras.md` diz foi seguido, e o que foi seguido está no `DECISOES.md`
