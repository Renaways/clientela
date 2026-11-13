# Tarefa: GC (lower third)

Um GC por **aparição** de pessoa com nome e cargo, no componente `gc` do perfil.

> **Normalmente isto roda sem você.** No painel, o editor escolhe a pessoa e a entrada, e o app faz o GC no modo automático (`kit/auto.py gc`), com o `templates/gc/` do perfil (o GC aprovado, só troca nome e cargo) ou o componente genérico do `marca.json`. Você só chega aqui se o usuário ligou o modo "com IA" para GC.

## Processo

1. **Preparar** (`kit/preparar.py`) e traduzir o perfil para `marca.json` (`perfil.md`).
2. **Quem aparece e quando.**
   - Procure na transcrição apresentações ("eu sou…", "aqui é a…", "comigo está…") e nomes citados como falantes.
   - Cruze com o `pessoas.csv` do perfil, que já está em `job.json` → `perfil.pessoas`.
   - Sem apresentação na fala:
     - pessoa única no `pessoas.csv`: ela, a partir de ~1,5 s do primeiro plano em que fala;
     - ou o que disserem as observações;
     - senão, **não invente**: registre a dúvida e não faça GC.
3. **Onde entra.**
   - O GC entra no começo da fala da pessoa, ~0,5–1,5 s depois de ela começar, nunca em cima de um corte.
   - Um por aparição. Se a mesma pessoa volta muito depois (> 60 s), repita só se as observações pedirem.
4. **`{trabalho}/gcs.json`:**

   ```json
   [{"n": 1, "nome": "Fulana de Tal", "cargo": null, "inicio": 2.4}]
   ```

   - `nome` exatamente como no `pessoas.csv`.
   - `cargo: null` puxa o cargo do CSV.
5. `python3 kit/gc.py` gera `hf/gc-<n>.html` e as entradas no `jobs.json`.
6. **Revise** com `python3 kit/review.py unica gc-<n> <inicio> <duracao> --at 0.5,1.2,2.5,<dur-0.3>`. Confira:
   - **não cobre a boca nem o rosto** neste vídeo. Se cobrir, passe `"y"` no `gcs.json` (px de 1080×1920, dentro da safe zone de baixo) e registre;
   - nome e cargo cabem na barra;
   - o logo do GC (se o perfil tiver) aparece inteiro.
7. Se o `marca.md` descreve um movimento de GC diferente do padrão do `gc.py` (por exemplo, um mascote que caminha sobre a barra), ajuste o `gc-<n>.html` gerado, ou copie o `gc.py` e altere a cópia em `kit/`. A descrição do perfil vence o padrão.
8. **Render e entrega:**

   ```bash
   python3 kit/render.py
   python3 kit/entregar.py
   ```

   O GC não tem recorte.

## Regras

- **Nome e cargo só do `pessoas.csv` ou das observações**, nunca da sua leitura da fala ("ela parece ser diretora"). Sem cargo no CSV, o GC sai só com o nome, e isso vai para as dúvidas.
- **Nome do arquivo:** `MM-SS-FF - <n> GC <Nome sem acento>.mov`. O `gc.py` gera o nome.
- **Saída animada:** a saída faz parte do GC (as barras recolhem). É a exceção à regra "sem transição de fim", porque o GC é um componente fechado.
- **Contenção:** GC é para identificar, não para enfeitar. Sem elementos extras além do que o perfil define.
