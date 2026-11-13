# Templates do cliente: o que deixa legendas e GC automáticos

O usuário quer **IA só no motion**. O que é template (legenda, GC) o Clientela faz sozinho, com `kit/auto.py`, sem chamar o Claude e sem gastar token. Para isso o perfil precisa de dois templates, escritos **uma vez**, na análise de IDV (`onboarding.md`) ou no modo templates (abaixo):

```
<perfil>/
  marca.json              ← o estilo, no esquema de perfil.md §3, validado pelo check_marca.py
  templates/gc/           ← opcional: o GC aprovado do cliente, em HyperFrames
    index.html            ← com {{NOME}} e {{CARGO}} no lugar dos textos
    template.json         ← {"fps": 30} (o fps em que ele foi validado)
    assets/               ← só o que o index.html usa (fontes, logo), nos caminhos relativos dele
  amostras/               ← do app: Legenda.mp4 e GC.mp4 renderizados do template, para o usuário conferir
```

## `marca.json`

- **É o `frame.md` traduzido para o esquema fixo** (`perfil.md` §3), com as lacunas preenchidas pelo `marca.md`. Toda tarefa copia este arquivo para `{trabalho}/marca.json`: ninguém traduz o `frame.md` de novo.
- **A legenda sai inteira dele:** `tipografia.papeis.legenda_apoio`, `legenda_destaque`, `legenda_complemento` (família, peso, estilo, caixa, px, cor) e `componentes.legenda` (`faixa_y`, `max_palavras`). Meça na legenda aprovada do cliente: o corpo do destaque costuma ser 1,6–2× o do apoio.
- **O GC genérico também** (`componentes.gc` + `papeis.gc_nome`/`gc_cargo`), usado quando não existe `templates/gc/`.
- Valide: `python3 <skill>/templates/kit/check_marca.py --marca "<perfil>/marca.json" --assets "<perfil>/assets"`, até 0 erros.

## `templates/gc/`

Só quando o cliente tem um **GC aprovado** (um `.mov`/`.mogrt` de lower third nas entregas, ou um HTML validado em `referencias/`). Reproduza esse GC em HyperFrames, uma composição de `data-duration` fixa, e:

- troque o nome por `{{NOME}}` e o cargo por `{{CARGO}}` (o app escapa o texto; a caixa alta é do CSS);
- caminhos relativos ao `index.html` (`assets/fonts/…`, `assets/logo/…`) e os arquivos copiados para `templates/gc/assets/`;
- a saída faz parte do GC (ele é a exceção à regra "sem transição de fim");
- confira quadro a quadro contra o GC aprovado (`hf/_check` + `snapshot`) antes de gravar.

Sem GC aprovado, **não crie** `templates/gc/`: o genérico do `marca.json` resolve, e a dúvida vai para o `duvidas.md`.

## Modo templates

O prompt diz "modo templates": o perfil já foi analisado, faltam só os templates.

1. Leia `regras.md`, `marca.md` (as seções de tipografia, legenda e GC) e `frame.md`. Não releia as pastas do cliente inteiras: o que o perfil cita em "Referências" basta.
2. Escreva `marca.json` e valide.
3. Se houver GC aprovado, escreva `templates/gc/`.
4. Não mexa nos outros arquivos do perfil. Dúvidas vão para o `duvidas.md`.
5. Termine com uma linha: o que foi gerado. O app renderiza as amostras sozinho (sem IA) quando o job termina.
