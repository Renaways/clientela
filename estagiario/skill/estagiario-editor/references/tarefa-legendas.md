# Tarefa: Legendas dinâmicas

Legenda **palavra a palavra**, sincronizada com a fala, no estilo de 3 camadas: **apoio / DESTAQUE / complemento**. A entrega é **um MOV do vídeo inteiro**, `00-00-00 - Legendas (video inteiro).mov`, com alpha.

> **Normalmente isto roda sem você.** O app faz as legendas no modo automático (`kit/auto.py legendas`: preparar com whisper large-v3 + texto da transcrição, `legendas.py blocos` e `html`, render, entrega), com o estilo do `marca.json` do perfil. Você só chega aqui se o usuário ligou o modo "com IA" para legendas. Nesse caso, faça a mesma sequência e use os turnos **só** na revisão do `legendas.json` (passo 4): a transcrição e os tempos já vêm prontos e ajustados.

## Processo

1. **Preparar** e traduzir o perfil (`perfil.md`). Os papéis usados são `legenda_apoio`, `legenda_destaque` e `legenda_complemento`, mais `componentes.legenda` (faixa vertical e máximo de palavras por bloco).
2. **A transcrição precisa estar certa palavra por palavra.** É o produto inteiro.
   - Com o JSON do Premiere, confie nos tempos.
   - Com o **`.txt` do Premiere** (blocos `00:00:11:16 - 00:00:23:06` / falante / texto), o `preparar.py` já fez o trabalho: tempos por palavra do ASR, texto corrigido pelo `.txt`, e a lista do que mudou em `correcoes.md`. Os tempos do `.txt` costumam ser da **sequência editada**, não do bruto (no 049 a deriva chegava a 36 s), então não use os tempos dele. Revise o `correcoes.md` (o texto do Premiere também é transcrição automática e pode estar errado) e siga. **Não rode outro ASR** com modelo maior só por garantia: custa minutos. Só faça isso se a revisão achar palavras fora do lugar, e registre.
   - Com ASR, revise o texto todo contra o sentido: nomes próprios, números, concordância. Corrija em `palavras.json` **antes** dos blocos e registre o que foi corrigido.
   - Com srt/vtt, os tempos por palavra são aproximados (`precisao: frase`). Avise no `DECISOES.md`.
3. `python3 kit/legendas.py blocos` cria `legendas.json`, com a quebra automática e um destaque sugerido por bloco.
4. **Revise o `legendas.json` como um legendador.** É aqui que está a qualidade:
   - **Quebras.** Um bloco é uma unidade de sentido, não um número de palavras. Não separe artigo do substantivo, nem "não" do verbo. Junte e separe blocos à vontade.
   - **Destaque.** Uma palavra por bloco: a que carrega o sentido (substantivo forte, número, verbo da ação). Nunca artigo, preposição ou conectivo. Use `null` quando nada merece destaque, e evite mais de um destaque seguido.
   - **Texto.** Números por extenso viram algarismos quando ajudam a leitura ("dois mil e vinte e seis" → "2026"); a fala se mantém. Tire os vícios ("né", "tipo", "éé") só se `regras.md` mandar.
5. `python3 kit/legendas.py html` gera `hf/legendas.html` e a entrada no `jobs.json`. Ela renderiza a 30 fps sem subquadros, porque legenda não precisa de motion blur e o vídeo é longo.
6. **Revise** com `python3 kit/review.py unica legendas 0 <duração> 2 10 240` (2 quadros por segundo). Procure:
   - linha estourando a largura;
   - bloco por cima da boca ou fora da faixa;
   - destaque fraco ou repetido;
   - sobreposição de blocos.
7. **Render e entrega:**

   ```bash
   python3 kit/render.py legendas
   python3 kit/entregar.py
   ```

## Regras

- **Sem fundo**: só as palavras (e o realce do perfil), sem tarja nem caixa atrás, a menos que o perfil defina a tarja como parte da legenda.
- **Faixa vertical**: `componentes.legenda.faixa_y`. O padrão é y 1050–1500 em 9:16, longe da UI das redes (safe zones do perfil).
- **Fiel à fala**: nada de resumir nem reescrever. A legenda é o que foi dito.
- **Movimento contido**: as palavras entram (pop curto de 0,15 s) e o destaque ganha impacto (escala + blur, 0,3 s). Nada de bounce em texto. Os blocos saem rápido (0,1 s) para dar lugar ao próximo.
- **Se o perfil tiver um estilo de legenda próprio** descrito no `marca.md` (caixa, cores alternadas, outra hierarquia), adapte o `legendas.py` numa cópia em `kit/` e registre.
