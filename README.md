# Clientela

Organizador gratuito para quem edita vídeo, faz motion ou design: seus clientes, os projetos de cada um, as horas trabalhadas, os custos e quanto cada trabalho rende. Roda no seu computador (Mac ou Windows), sem conta e sem nuvem obrigatória.

> **Versão pré-alpha.** O app está em desenvolvimento: nem todas as funções estão prontas, e algumas podem falhar. Mantenha backup dos seus arquivos.

O Clientela é um projeto *vibe coded*: feito conversando com uma IA (Claude Code), a partir da rotina real de um estúdio de edição e motion.

- **Seus arquivos continuam onde estão.** Cada cliente é uma pasta do seu computador, e cada subpasta `001 - Nome` é um projeto.
- **Pipeline por etapa** (Backlog → Produção → Revisão → … → Concluído), com cronômetro por projeto que liga sozinho em Produção.
- **Custos e lucro:** valor por hora calculado a partir do seu pró-labore e dos custos fixos, rateio do orçamento mensal do cliente entre os projetos, fechamento de mês.
- **Estatísticas e calendário** por mês, ano ou desde o começo.
- **Perfil de design:** etapas com nomes de estúdio (Briefing → Criação → Apresentação → Arte final → Entregue) e atalhos para Photoshop, Illustrator, InDesign, Figma e Affinity no cartão do projeto.
- **Mensagens prontas** de status para mandar ao cliente.
- **Modo completo (opcional):** editor de vídeo multipista, corte vertical 9:16 automático, legendas, transcrição (Whisper), download de mídia (yt-dlp), automações com IA e identidade visual por cliente.

## Instalar

Precisa do **Python 3.10 ou mais novo** ([python.org](https://www.python.org/downloads/)).

**Mac:** baixe o projeto (botão *Code → Download ZIP* ou o arquivo da última versão em *Releases*), descompacte e dê dois cliques em `instalar.command`. Ele prepara tudo, cria o `Clientela.app` e abre o app. Depois, abra sempre pelo `Clientela.app`.

> Na primeira vez o macOS pode bloquear o `instalar.command` por ser baixado da internet: clique com o botão direito → *Abrir*.

**Windows:** descompacte e dê dois cliques em `start_windows.bat`. Na instalação do Python, marque *Add python.exe to PATH*.

O app abre no navegador em `http://127.0.0.1:8000`. Na primeira vez, um guia de boas-vindas ajuda a configurar.

### Ferramentas opcionais

O básico funciona só com o Python. Estas ferramentas liberam recursos do modo completo, e o próprio app mostra o comando de instalação de cada uma em *Configurações → Ferramentas*:

| Ferramenta | Para quê |
|---|---|
| [FFmpeg](https://ffmpeg.org/download.html) | editor de vídeo, cortes, prévias, conversão |
| [Node.js](https://nodejs.org/) | render de motion e legendas animadas |
| [Claude Code](https://docs.claude.com/pt/docs/claude-code/setup) | automações com IA (usa a sua conta) |
| [openai-whisper](https://github.com/openai/whisper) | transcrição |
| yt-dlp, pynput, watchdog | já vêm na instalação: download de mídia, pausa por inatividade, atividade nos arquivos |

## Chaves de IA

Nenhuma chave vem com o app. Os recursos de IA usam a **sua** chave da [OpenRouter](https://openrouter.ai/keys) (tem modelos gratuitos), configurada nas boas-vindas ou em *Configurações → Modelos de IA*, ou a variável `OPENROUTER_API_KEY`. As automações de motion usam o Claude Code com a sua própria conta.

## Seus dados

Tudo fica na pasta `data/`, dentro da pasta do app (ou onde a variável `CLIENTELA_DATA_DIR` apontar). São arquivos JSON comuns, com backup diário automático em `data/backups/`. Nada é enviado para fora sem você pedir; os recursos de IA que usam serviços externos só funcionam se você configurar a chave ou a conta.

**Senha:** em *Configurações → Senha e acesso* dá para proteger o app com senha e abri-lo no celular pela rede local. Esqueceu a senha? Na pasta do app:

```bash
.venv/bin/python app.py --remover-senha
```

(no Windows: `.venv-win\Scripts\python app.py --remover-senha`)

## Atualizações

O app avisa quando sai versão nova (pergunta só ao GitHub qual é a última, sem mandar dado nenhum). Para atualizar, baixe a versão nova, substitua os arquivos do app e mantenha a pasta `data/`. Dá para desligar o aviso em *Configurações → Perfil → Sobre*.

## Feedback

Use o botão de feedback no topo do app, ou abra uma *issue* aqui no GitHub.

## Desenvolvimento

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt   # 3.10+; o python3 do macOS é 3.9
.venv/bin/python -m pytest tests/ -q
.venv/bin/python app.py
```

Flask + um único `templates/index.html` (HTML/CSS/JS sem framework e sem build) + JSON. Detalhes de arquitetura em `CLAUDE.md`.

## Créditos e licença

Criado por [renatv.com](https://renatv.com). Licença [MIT](LICENSE).
