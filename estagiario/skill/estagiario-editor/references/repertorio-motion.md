# Repertório de direção: o que os bons prompts de motion pedem

Destilado em 2026-11-10 de 42 prompts do prompt-motion.com que o usuário separou (fontes no fim). `acabamento.md` cobre o **visual** das referências; aqui fica a **direção**: como os melhores prompts descrevem um vídeo para o resultado não sair genérico. **Os 42 vídeos estão em `repertorio/`**: uma pasta por referência, com `folha.jpg` (os quadros em ordem), `quadros/`, o prompt completo e a análise visual em `ficha.md`. O índice por técnica fica em `repertorio/README.md`. Consulte-o ao escolher a ideia de cada tela. Tudo foi adaptado ao nosso caso (elementos sobre o vídeo da pessoa, 1080×1920, sem fundo, sem transição de take). Se algo daqui bater com `acabamento.md`, `regras.md` ou as LIÇÕES do pedido, eles vencem.

## 1. O que separa um prompt bom de um ruim

Os prompts curtos ("faça um showreel incrível, capriche") geram vídeos bonitos mas aleatórios. Os que dão resultado de estúdio têm sempre as mesmas cinco partes. Use-as como checklist ao escrever a ideia de cada tela no `DECISOES.md`:

1. **Direção numa frase**, com o tom ("high-end minimalista", "keynote", "uma tomada contínua").
2. **Uma lista do que é proibido.** É ela que mata o genérico. Exemplos que aparecem várias vezes: crossfade em tudo, blur-in, partículas, brilho neon, anel de choque, RGB split, tremida de câmera, lens flare, grade no chão, easing com bounce, cards de vidro, degradês, "parado mais de 1 s", "qualquer coisa com cara de template". Para nós vale ainda: HUD, fundo, transição de take (`acabamento.md` §2).
3. **Estrutura com tempos**: cada batida com o que acontece. Nós não temos música, mas temos a fala. A batida é a palavra dita (`palavras.json`): cada entrada cai no início de uma palavra, nunca entre duas.
4. **Regras de construção** (técnicas abaixo, §3).
5. **Gotchas e verificação**: checar quadros antes de entregar (§4).

## 2. Ideias de direção que valem para inserts

- **Um objeto vira o próximo, em vez de cortar.** Botão vira página, palavra vira lista, ponto vira pílula, pílula vira o número. É o "continuidade de objeto" do `acabamento.md` levado a sério: a passagem é um *elemento compartilhado* (o que sai leva o próprio texto até a forma nova), não um fade de um para o outro.
- **Uma ideia por tela, muito espaço vazio, uma cor de acento.** O acento é o realce do cliente e aparece só onde a fala aponta.
- **Texto sobe de uma linha de máscara**, palavra por palavra, nos tempos da fala. Sem ponto final no texto na tela.
- **Uma linguagem de câmera só.** Se houver movimento de "câmera" tipográfico (zoom, push, pan no bloco de texto), é um movimento por tela, suave, e nunca dois ao mesmo tempo. Nunca zoom in seguido de zoom out.
- **Inundação de forma** (um círculo ou pílula que cresce): só quando as observações pedirem tela cheia. Nesse caso, ela tem que passar do canto mais distante e levar ~0,3–0,35 s, senão parece um flash de um quadro.
- **Conteúdo real, nunca placeholder.** Nome, cargo, número e frase vêm da fala ou do perfil, ditos como foram ditos. "Invented numbers" e "paraphrased taglines" aparecem como proibidos em vários prompts, e para nós já são regra (`tarefa-inserts.md` §1.8).
- **Fuja das marcas de vídeo feito por IA**: molduras e textos nos cantos, labels "eyebrow", bege "artsy", tudo entrando com o mesmo fade. Um dos prompts pede isso explicitamente.
- **Inspiração sem cópia.** Estude o tempo, a duração dos planos e os elementos de uma referência, pegue técnicas, mas mude o desenho para parecer coisa sua (vale para `referencias/` e para os projetos de referência do cliente).

## 3. Técnicas de construção (HyperFrames)

- **Tudo é função do tempo.** Nada de CSS transition, `setTimeout` ou estado guardado entre quadros: o render pede quadros fora de ordem. No HyperFrames isso já é a timeline GSAP pausada; não fuja dela.
- **Molas de forma fechada** para "pop" e assentamento: uma mola é uma função de t. Se um valor muda de alvo várias vezes, some uma mola por mudança, e ele continua sendo função pura do tempo.
- **Zoom interpolado em escala logarítmica**: zoom linear parece acelerar no fim.
- **Meça posições em tempo de execução** para passagens de elemento (onde a palavra realmente está depois da quebra de linha), em vez de chutar coordenadas.
- **Opacidade ou filter num elemento `preserve-3d` achata o 3D** e mostra as duas faces: anime o wrapper.
- **`visibility: visible` num filho aparece mesmo com o pai escondido**: use `inherit`.
- **Texto que troca dentro de uma forma que se deforma** precisa da própria máscara, senão vaza pela borda durante a deformação.
- **z-index explícito** em toda camada que se cruza, para um card não "boiar" por cima da forma que deveria cobri-lo.
- **Motion blur com poucos subquadros deixa cópias fantasma** em movimentos rápidos. Se aparecer fantasma, deixe o movimento mais lento antes de aumentar o blur.

## 4. Verificação antes de entregar

- **Quadros em cada batida antes do render completo.** Os prompts bons sempre pedem 4 a 20 stills (abertura, ponto alto, fim) antes de renderizar. Use o lint/snapshot do kit e olhe os quadros de entrada, do pico e de hold de cada tela.
- **Procure "pops" de um quadro**: um elemento que aparece ou some num quadro só, ou um salto. Em movimento rápido, olhe quadro a quadro.
- **Procure tempo congelado**: um trecho sem nada se mexendo (contraria "nada parado mais de ~1 s").
- **Último quadro de cada movimento de câmera**: textos tendem a escorregar para as bordas e safe zones.
- **Um olhar de fora**: depois de montar, revise como se não tivesse feito, procurando o que ficou amontoado, sobreposto, cortado ou corrido demais, e corrija antes de entregar.

## Fontes

Fichas completas, com quadros e prompt, em `repertorio/<NN> - <título>/ficha.md`.

Os prompts mais completos (as partes 1–4 acima vieram principalmente deles): Food craving launch film (`ultimaxbt-2ddf1e`), Photo print app launch film (`twoclipping-221cab`), UGC ad generator promo (`twoclipping-6dd14e`), Landing page 3D scroll (`twoclipping-d89be5`), Prompt Motion site promo (`antonio-kodheli-490109`), SaaS launch video (`aschapmann-131210`), Supademo launch video (`jhylee95-e1152f`), Engraving-style Claude ad (`lexnlin-038035`), Claude self-intro motion graphic (`1littlecoder-9fef89`). Todos em `https://prompt-motion.com/<id>`. Os outros 30 são variações curtas de "faça um showreel de 15 s, capriche" e não acrescentam direção.
