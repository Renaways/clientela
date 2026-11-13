# Rodando em segundo plano (headless)

O app roda `claude -p … --permission-mode auto --permission-prompts none`, com o prompt pelo stdin.

- **Permissões.** Um classificador aprova cada ação, e o que precisaria de uma pessoa é **negado**, sem travar.
- **Sem perguntas.** `AskUserQuestion` não está disponível. Nenhuma pergunta será respondida: se você perguntar, o job só perde tempo.

## 1. Pastas

| Pasta | O que é | Pode escrever? |
|---|---|---|
| `{projeto}/` | a pasta do projeto do cliente (cwd da tarefa) | **não**: só leitura |
| `{trabalho}/` = `{projeto}/.estagiario/<Tarefa> vN/` | sua bancada. O app já a criou | sim, à vontade |
| `{entrega}/` = `{projeto}/Motion - <Tarefa> vN/` | a entrega | só via `kit/entregar.py`, no fim |
| `{perfil}/` = `<cliente>/_Estagiário/` | o perfil | **não** na tarefa; **sim** no onboarding (menos `perfil.json`) |

- O ponto em `.estagiario` é proposital: o organizador e o estimador de horas do Clientela ignoram pastas ocultas. Nada de trabalho fora dela.
- **Nunca reaproveite** uma pasta `Motion - * vN` que já existe. O `entregar.py` recusa a pasta se ela já existir sem ter sido criada por este trabalho; nesse caso, registre e pare. O N certo é o do prompt: não calcule outro. A exceção é o **modo ajuste** (`ajuste.md`): ele refaz um elemento dentro da entrega que a própria bancada criou, pelo `entregar.py --substituir`.
- Arquivos grandes (recorte, renders a 120 fps) ficam em `{trabalho}/`. O usuário apaga quando quiser.

## 2. O `DECISOES.md`

Crie `{trabalho}/DECISOES.md` **no começo** e atualize a cada etapa. O job pode ser cancelado no meio, e o arquivo precisa fazer sentido mesmo assim. Modelo:

```markdown
# <Tarefa> vN · <projeto> · <cliente>

> Perfil: <status do perfil.json, ou "sem perfil">. Transcrição: <Premiere | ASR (revisada) | srt>.
> <avisos que o editor precisa ver primeiro, ex.: "trabalhei sem manual de marca">

## Entregue
- `00-11-09 - 3 Debate (camada ATRAS).mov` + FRENTE: "1º DEBATE DO / 2º TURNO" (11,3–16,8 s)
- …

## Decisões (tomadas sem perguntar)
- Calibração: fiz 3 inserts (os momentos mais fortes), como pede o padrão sem observação em contrário. Os candidatos para a v2 estão na seção "Próximos".
- Nome no recorte: "Ana", pelo pessoas.csv (ela se apresenta em 0:02).
- …

## Dúvidas (para o editor responder nas observações da próxima versão)
- …

## Próximos / o que calibrar
- Candidatos a insert que ficaram de fora: 0:23 "antes × agora", 0:40 "o próprio formato", …
```

- **Decisão** é o que você escolheu sozinho e o porquê, numa linha.
- **Dúvida** é o que só o editor sabe. Escreva como uma pergunta que ele responde em uma frase nas "Observações do editor" da próxima versão.
- Sem histórico, sem desculpas e sem repetir o prompt.

## 3. O que mostrar no painel

O painel do app mostra o seu texto e, para cada `Bash`, o campo **`description`**.

- **Descrições** em PT-BR, que digam a etapa:
  - "Preparando o trabalho e normalizando a transcrição";
  - "Recortando a apresentadora do fundo (≈2 min por 30 s)";
  - "Revisando o insert 2 em movimento";
  - "Renderizando o insert 3 a 120 fps";
  - "Conferindo e copiando os MOVs para a entrega".
- **Mensagens curtas** de transição entre etapas ("Mapa de inserts pronto: 3 momentos. Construindo o 1."). O painel é o que o editor acompanha.
- **Comandos longos** (render, remove-background, transcrição) rodam em primeiro plano, com timeout alto (até 600000 ms). Para mais que isso, rode em segundo plano e acompanhe o arquivo de saída. Nunca use `sleep` em loop.

## 4. Cuidados com o shell

- `rm` com variável é bloqueado pela checagem de segurança. Use `rm -f "${D:?}"/*.mov` ou apague por caminho literal dentro de `{trabalho}/`.
- **Nunca** apague nem mova nada fora de `{trabalho}/` (na tarefa) ou de `{perfil}/` (no onboarding).
- Caminhos de cliente têm espaço e acento (`Estagiário`, `049 - Análise …`): sempre entre aspas.
- O `PATH` já tem `~/.local/bin`, `/opt/homebrew/bin` e `/usr/local/bin` (`npx`, `ffmpeg`, `ffprobe`, `python3`).
- `npx hyperframes` baixa o pacote na primeira vez. Se falhar por rede, registre no `DECISOES.md` e pare.
