# Tarefa: Motion curto

Uma peça curta (até ~15 s), **motion-only** ou sobre o vídeo, a partir do conteúdo da fala. O "o quê" vem das **observações do editor**, por exemplo:

- "abertura com a frase X";
- "card de número para o reel";
- "vinheta de 3 s com o logo".

Sem observação que diga o que fazer, escolha **a frase mais forte do vídeo** e faça uma tela tipográfica de 4–6 s, registrando a escolha no `DECISOES.md`.

## Processo

1. **Preparar e ler o perfil** (`perfil.md`). Para peças completas, que não são inserts, o `marca.md` pode ter regras próprias, como fechar com o encerramento ou a assinatura da marca. Siga-as.
2. **Briefing em `mapa.md`**, com 3 a 5 linhas: o que é, a duração, o formato, a frase ou o dado, e se vai sobre o vídeo ou tem fundo próprio (só se as observações pedirem fundo).
3. **Construa com o kit.**
   - Use `write()` com **fundo transparente**: só os elementos. Fundo próprio (campo de cor, textura do perfil) só quando as observações pedirem.
   - Os padrões de acabamento (`acabamento.md`) valem do mesmo jeito: tipografia como imagem, um momento forte por tela, nada parado.
   - Para peças mais elaboradas (vários takes, 3D, gráficos), as skills `hyperframes-animation`, `hyperframes-creative` e `motion-graphics` têm receitas. Use-as **como técnica**, nunca como marca: cores, fontes e componentes continuam saindo do `marca.json`.
4. **Revise** (`review.py unica`) em duas varreduras.
5. **Render.**
   - Entrada no `jobs.json`: `{"comp": "curto-1", "inicio": 0, "nome": "1 <Titulo>"}`, que vira `00-00-00 - 1 <Titulo>.mov`.
   - Se for **sobre o vídeo** num ponto específico, `inicio` é o tempo no clipe e o nome sai com esse timecode.
   - Peça com fundo próprio (pedido nas observações) também sai em ProRes 4444, com o alpha opaco e `(tela cheia)` no nome: `00-00-00 - 1 <Titulo> (tela cheia).mov`.
6. **Entrega:** `python3 kit/entregar.py`.

## Regras

- **Nada inventado.** Números e afirmações só da fala ou das observações.
- **Logo.** Só o arquivo do perfil (`assets/logos/`). Nunca redigite o nome da marca como wordmark e nunca desenhe um logo.
- **Encerramento.** Peça completa com encerramento ou assinatura só se o perfil pedir. Um corte que vai entrar no meio de um vídeo (o caso mais comum) não leva.
