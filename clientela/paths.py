import json
import os
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Onde mora data/. O app roda no Mac e nos desktops Windows com os mesmos
# clientes, projetos e horas, então data/ pode ficar numa pasta do Google
# Drive — que tem um caminho diferente em cada sistema. Ordem:
#   1. variável de ambiente CLIENTELA_DATA_DIR;
#   2. data_dir.json ao lado do app, com uma chave por sistema
#      ({"mac": "~/Library/CloudStorage/…/Clientela", "windows": "G:\\…"}) —
#      uma chave por sistema, e não um caminho só, porque a pasta do app
#      também pode estar sincronizada e o mesmo arquivo é lido nas duas;
#   3. data/ ao lado do app (o padrão de sempre).
# Lido aqui, uma vez: todos os caminhos abaixo derivam dele na importação.
DATA_DIR_CONFIG = BASE_DIR / "data_dir.json"


def _os_key() -> str:
    # Duplicado de machine.current_os de propósito: paths não importa nada
    # do pacote (todo módulo importa paths).
    if sys.platform == "darwin":
        return "mac"
    return "windows" if sys.platform.startswith("win") else "linux"


def resolve_data_dir(env: dict | None = None, config_file: Path | None = None) -> Path:
    env = os.environ if env is None else env
    raw = (env.get("CLIENTELA_DATA_DIR") or "").strip()
    if not raw:
        cfg_path = config_file if config_file is not None else DATA_DIR_CONFIG
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        if isinstance(cfg, dict):
            raw = str(cfg.get(_os_key()) or "").strip()
    if raw:
        return Path(os.path.expandvars(raw)).expanduser()
    return BASE_DIR / "data"


DATA_DIR = resolve_data_dir()

CLIENTS_JSON = DATA_DIR / "clients.json"
TAGS_JSON = DATA_DIR / "tags.json"
COST_SETTINGS_JSON = DATA_DIR / "cost_settings.json"
MESSAGE_SETTINGS_JSON = DATA_DIR / "message_settings.json"
MONTHLY_LEDGER_JSON = DATA_DIR / "monthly_ledger.json"
CONFIG_JSON = DATA_DIR / "config.json"
TIMER_ACTIVE_JSON = DATA_DIR / "timer_active.json"
# Marcadores das migrações de dados que só podem rodar uma vez (ver stages.py).
MIGRATIONS_JSON = DATA_DIR / "migrations.json"
# Dependências extras (ou ajustes nas padrão) da verificação de ambiente: startup.py
DEPENDENCIES_JSON = DATA_DIR / "dependencies.json"

COVERS_DIR = DATA_DIR / "covers"
UPLOADS_DIR = DATA_DIR / "uploads"
BACKUPS_DIR = DATA_DIR / "backups"
LOGS_DIR = DATA_DIR / "logs"

# Registro central de projetos: um JSON por projeto em data/projects/<client_id>/
# <NNN>.json, com etapa, cronômetro, custo, histórico, feed, links. É a fonte
# da verdade de tudo que não é arquivo de trabalho — a pasta do projeto pode
# sumir do disco que o histórico continua aqui (ver store.py).
PROJECTS_DIR = DATA_DIR / "projects"

# Função "Editor" (estagiário de edição movido a IA, ver editor.py e
# docs/EDITOR_CONTRATO.md). Os jobs do CLI `claude` guardam status (.json) e
# o stream bruto (.ndjson) lado a lado. Os perfis de identidade NÃO moram em
# data/: ficam em <pasta do cliente>/_Estagiário/ (identidades.py).
EDITOR_DIR = DATA_DIR / "editor"
EDITOR_JOBS_DIR = EDITOR_DIR / "jobs"
EDITOR_CONFIG_JSON = EDITOR_DIR / "config.json"

# Destino alternativo de "Baixar mídia" e "Edição IA" no topo da tela, quando o
# arquivo não pertence a projeto nenhum. Fora de data/: é a pasta do usuário.
USER_DOWNLOADS_DIR = Path.home() / "Downloads"

# LEGADO — só a importação (store.register_from_folder) lê estes caminhos.
#
# Até setembro de 2026 os metadados de cada projeto viviam dentro da própria
# pasta dele, numa subpasta LOG/ (e antes disso soltos na raiz, ver
# LEGACY_PROJECT_FILES). O app não escreve mais nada neles; na primeira vez que
# uma pasta é vista sem registro, o conteúdo é copiado pro store e os arquivos
# ficam onde estão, intocados. O prefixo com ponto continua importando porque
# `organizer.list_project_files` e `timer.estimate_sessions` pulam qualquer
# caminho com parte iniciada em "." — sem isso o organizador ofereceria mover
# esses arquivos e o estimador contaria as escritas antigas como horas.
PROJECT_LOG_DIR = "LOG"
PROJECT_META = f"{PROJECT_LOG_DIR}/.meta.json"
PROJECT_STAGE = f"{PROJECT_LOG_DIR}/.stage.json"
PROJECT_TIME = f"{PROJECT_LOG_DIR}/.time.json"
PROJECT_COST = f"{PROJECT_LOG_DIR}/.cost.json"
PROJECT_HISTORY = f"{PROJECT_LOG_DIR}/.history.json"
PROJECT_COMMENTS = f"{PROJECT_LOG_DIR}/.comments.json"
PROJECT_CHECKLIST = f"{PROJECT_LOG_DIR}/.checklist.json"

# Onde esses arquivos ficavam antes de LOG/ — a importação olha aqui em
# segundo lugar, pra pastas que nunca passaram pela migração antiga.
LEGACY_PROJECT_FILES = (
    ".meta.json",
    ".stage.json",
    ".time.json",
    ".cost.json",
    ".history.json",
)

_INVALID_SEQUENCE = re.compile(r"\.\.")
# Separadores, mais o que o Windows recusa num nome de arquivo (<>:"|?* e
# controles). A pasta criada no Mac é aberta no Windows pelo Drive: um ":"
# que o Finder aceita deixaria o projeto inacessível do outro lado.
_INVALID_CHARS = re.compile(r'[\\/<>:"|?*\x00-\x1f]')
# Nomes que o Windows reserva, com ou sem extensão ("CON", "nul.txt").
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)


def ensure_data_dirs() -> None:
    for d in (DATA_DIR, PROJECTS_DIR, EDITOR_JOBS_DIR, COVERS_DIR, UPLOADS_DIR, BACKUPS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def sanitize_folder_component(name: str) -> str:
    """Strip path separators, '..' sequences and characters Windows refuses
    from user-supplied text before it is used as a folder/file name
    component — the same name has to work on macOS and on Windows."""
    if not name:
        return ""
    cleaned = _INVALID_CHARS.sub("", name)
    while _INVALID_SEQUENCE.search(cleaned):
        cleaned = _INVALID_SEQUENCE.sub("", cleaned)
    # O Windows ignora ponto e espaço no fim do nome: "Projeto." viraria
    # "Projeto" lá e o caminho guardado não bateria mais.
    cleaned = cleaned.strip().rstrip(". ")
    if _WINDOWS_RESERVED.match(cleaned):
        cleaned = f"_{cleaned}"
    return cleaned
