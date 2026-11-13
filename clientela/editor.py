"""Função "Editor": o estagiário de edição movido a IA.

O app não sabe animar nada — ele monta um prompt padronizado e roda o Claude
Code CLI local (`claude -p`) em segundo plano, com cwd na pasta do projeto e
acesso ao perfil de identidade do cliente (identidades.py) via --add-dir. Quem
sabe animar é a skill `estagiario-editor`, que segue o contrato de
docs/EDITOR_CONTRATO.md (caminhos, formato do perfil, regras de entrega).

Decisões que importam:

- **O job sobrevive a um restart do app.** O `claude` roda numa sessão de
  processo própria (start_new_session) com a saída indo direto pra
  data/editor/jobs/<id>.ndjson; o app só lê esse arquivo. Ao subir,
  `resume_jobs()` reata os jobs "rodando": pid vivo → volta a acompanhar;
  morto → fecha o job pela última linha `result` do stream.
- **Um job por vez** (análise de identidade inclusa). Render de motion é
  pesado e dois disputando a máquina deixam os dois lentos.
- **stream-json exige --verbose** com -p (o CLI recusa sem). O fim do stream
  é uma linha `result`; falha de login volta com `subtype: "success"` e
  `is_error: true`, então o que vale é `is_error`.
- **O ambiente do filho é limpo das variáveis CLAUDE_CODE_*.** Se o app for
  aberto de dentro de uma sessão do Claude Code, ele herda dezenas delas —
  CLAUDE_CODE_SIMPLE liga o modo --bare (sem OAuth, sem skills) e o
  estagiário nasceria sem login e sem a skill.
- **O prompt vai por stdin**, não como argumento: --add-dir é variádico e
  comeria um prompt posicional que viesse depois dele.
- Os .ndjson/.prompt.txt/.stderr.log são logs de processo, não estado do
  app — por isso são escritos com open() direto, fora do persistence.py.
  O estado (o .json do job) passa por safe_write_json como todo o resto.
- **Ajuste de um elemento** reaproveita a bancada (`.estagiario/<tarefa> vN`)
  de quem fez a entrega — achada pelo `.estagiario-origem` que a skill grava
  na pasta — e troca só os arquivos daquele elemento, na mesma pasta. Sem a
  bancada não há ajuste: o estagiário não teria o gerador nem o recorte.
- **O que é template não gasta IA.** Legendas e GC rodam no modo
  automático (`modo: "auto"` da tarefa): o app roda `kit/auto.py` da skill
  no lugar do `claude`, e ele fala o mesmo stream-json, então monitor,
  cancelamento e retomada são os mesmos. O estilo vem do template do
  cliente (`<perfil>/marca.json` e `<perfil>/templates/gc/`), gerado na
  análise de IDV. Nas tarefas com IA (inserts, motion curto) o mesmo
  script prepara a bancada antes (transcrição, assets, template) e só
  então faz `exec` do `claude`: a IA começa com tudo pronto.
- **A prévia é do app, não da skill**: um MP4 leve, por elemento, com o
  trecho do vídeo bruto e as camadas por cima, gerado sob demanda pelo
  ffmpeg e guardado em `.estagiario/_previas/` (chave = arquivos + mtimes).
  A prévia do conjunto que a skill montava custava minutos em todo job.
- **Corte vertical** (`autocorte.py`) é a única tarefa que não passa pela
  skill: o app roda o script direto (whisper → locutores → LLM escolhe os
  trechos → ffmpeg 9:16) e ele fala o mesmo stream-json. O LLM é o próprio
  `claude -p` sem ferramentas ou a OpenRouter; as chaves (OpenRouter,
  Hugging Face) vão pelo ambiente do processo, nunca pelo auto-args.json,
  que fica na bancada dentro da pasta do projeto (no Drive).
"""

import copy
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import aprendizado, autocorte, sequencia
from . import clients as clients_module
from . import clips as clips_module
from . import feed as feed_module
from . import history, identidades, machine, paths, store
from . import projects as projects_module
from . import persistence
from .persistence import safe_read_json, safe_write_json

logger = logging.getLogger("clientela.editor")

SKILL_NAME = "estagiario-editor"

STATUS_PENDENTE = "pendente"
STATUS_RODANDO = "rodando"
STATUS_CONCLUIDO = "concluido"
STATUS_ERRO = "erro"
STATUS_CANCELADO = "cancelado"
ACTIVE_STATUSES = (STATUS_PENDENTE, STATUS_RODANDO)

TIPO_PROJETO = "projeto"
TIPO_ANALISE = "analise"
TIPO_AJUSTE = "ajuste"
TIPO_TEMPLATES = "templates"
TIPO_AMOSTRAS = "amostras"
TIPO_SEQUENCIA = "sequencia"
TIPO_EXPORTAR = "exportar"     # render + entrega dos inserts aprovados na prévia: kit/auto.py exportar, sem IA
ESTILOS_INSERTS = ("padrao", "clean")
# espelho de kit/legendas.py ANIMACOES — mude os dois juntos
ANIMACOES_LEGENDA = ("perfil", "karaoke", "pop", "maquina", "suave", "caixa")

MODO_AUTO = "auto"
MODO_IA = "ia"
MODO_CORTE = "corte"
MODO_SEQUENCIA = "sequencia"   # export de uma sequência .clientela: sequencia.py, só ffmpeg
TAREFA_CORTE = "corte_vertical"
AUTOCORTE_SCRIPT = Path(autocorte.__file__).resolve()
# Campos de autocorte.DEFAULTS que são segredo: o GET da config não os devolve
# e eles chegam ao script pelo ambiente.
CORTE_SEGREDOS = {"openrouter_api_key": "OPENROUTER_API_KEY", "hf_token": "HF_TOKEN"}
# Tarefas que têm pipeline automático no kit (kit/auto.py). As outras só existem com IA.
AUTO_TAREFAS = ("legendas", "gc")
KIT_DIR = Path(__file__).resolve().parent.parent / "estagiario" / "skill" / SKILL_NAME / "templates" / "kit"
SAMPLES_DIR = "amostras"        # <perfil>/amostras/: Legenda.mp4, GC.mp4
SAMPLES_WORK = ".amostras"      # <perfil>/.amostras/: bancada das amostras (oculta)

# Onde procurar o `claude` e as ferramentas que o estagiário chama (npx,
# ffmpeg). Aberto pelo Clientela.app o PATH vem só com /usr/bin:/bin.
EXTRA_PATH = (
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / ".claude" / "local"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
)

_ENV_DROP_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "CLAUDE_PREVIEW_")
_ENV_DROP_EXACT = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT"}

VIDEO_EXTS = {".mp4", ".mov", ".mxf", ".m4v", ".avi", ".mkv"}
# Música e efeitos que entram numa sequência (trilhas A). O estúdio também os toca e desenha a onda.
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".aif", ".aiff"}
TRANSCRIPT_EXTS = {".txt", ".json", ".srt", ".vtt"}

DELIVERY_DIR_RE = re.compile(r"^(Motion - .+|Corte .+|Sequência - .+) v(\d+)$")
WORK_DIR = ".estagiario"
ORIGIN_MARKER = ".estagiario-origem"  # escrito pelo kit/entregar.py: caminho da bancada que criou a entrega
PREVIEW_DIR = "_previas"
PREVIEW_MAX_SIDE = 960
PREVIEW_KEEP = 40

MAX_EVENTS = 300
CANCEL_GRACE_SECONDS = 10

DEFAULT_PROMPT_PROJETO = """Use a skill estagiario-editor.
Cliente: {cliente} (perfil de identidade visual em {perfil}).
Projeto: {projeto}.
Tarefa: {tarefa} — {instrucoes}
Vídeo bruto: {video}.
Transcrição: {transcricao}.
Trabalhe (builds, rascunhos, revisões) em {trabalho}/ e entregue os arquivos finais em {entrega}/ — versão {versao}.
A bancada já foi preparada pelo app: {trabalho}/ tem job.json, palavras.json, transcricao.txt, kit/, hf/ com os assets e, se o cliente tiver template, marca.json. Não rode o preparar.py de novo nem refaça a transcrição.
Você está rodando em segundo plano, sem ninguém para responder perguntas: não pergunte, decida pelo perfil e pelas regras do cliente e registre as decisões e dúvidas em {trabalho}/DECISOES.md.
Observações do editor: {observacoes}"""

DEFAULT_PROMPT_AJUSTE = """Use a skill estagiario-editor no modo ajuste (refazer um elemento de uma entrega já feita).
Cliente: {cliente} (perfil de identidade visual em {perfil}).
Projeto: {projeto}.
Tarefa original: {tarefa} {versao}.
Bancada de trabalho: {trabalho}/ (a mesma de quando a entrega foi feita: gerador, jobs.json, recorte, DECISOES.md).
Entrega: {entrega}/
Elemento a ajustar: {elemento}
Arquivos dele na entrega:
{arquivos}
O que o editor pediu: {pedido}
Mexa só nesse elemento e entregue no lugar dele, na mesma pasta; o resto da entrega fica como está.
Você está rodando em segundo plano, sem ninguém para responder perguntas: não pergunte, decida pelo pedido e pelas regras do cliente e registre o ajuste em {trabalho}/DECISOES.md."""

DEFAULT_PROMPT_AJUSTE_PREVIA = """Use a skill estagiario-editor no modo ajuste, na prévia (o insert ainda não foi exportado).
Cliente: {cliente} (perfil de identidade visual em {perfil}).
Projeto: {projeto}.
Bancada de trabalho: {trabalho}/ (gerador, jobs.json, hf/, inserts.json, DECISOES.md).
Insert a ajustar: {elemento}
Composições dele: {arquivos}
O que o editor pediu: {pedido}
Mexa só nesse insert, no HTML (ou no inserts.json + kit/universais.py, se for um template universal "u-…"), revise e passe o lint. Não renderize nem entregue: o editor confere na prévia do app e exporta de lá.
Você está rodando em segundo plano, sem ninguém para responder perguntas: não pergunte, decida pelo pedido e pelas regras do cliente e registre o ajuste em {trabalho}/DECISOES.md."""

DEFAULT_PROMPT_ANALISE = """Use a skill estagiario-editor no modo onboarding (análise de identidade visual).
Cliente: {cliente}.
Pasta do cliente: {pasta_cliente} — só leitura, exceto a pasta do perfil abaixo (não crie, mova nem apague nada fora dela).
Perfil a preencher: {perfil}
Pastas para estudar:
{pastas_analise}
Analise os projetos aprovados, os vídeos de referência, as fontes e os logos dessas pastas. Escreva marca.md, frame.md, regras.md e pessoas.csv e copie fontes, logos, texturas e referências para as subpastas do perfil, no formato do contrato.
Gere também os templates do cliente, que deixam legendas e GC automáticos (sem IA): {perfil}/marca.json validado e, se houver um GC aprovado nas pastas, {perfil}/templates/gc/ (guia references/templates.md da skill).
Você está rodando em segundo plano, sem ninguém para responder perguntas: registre o que ficou em dúvida em {perfil}/duvidas.md.
Observações do editor: {observacoes}"""

DEFAULT_PROMPT_TEMPLATES = """Use a skill estagiario-editor no modo templates (só os templates do cliente, sem refazer a análise).
Cliente: {cliente}.
Perfil: {perfil} (marca.md, frame.md, regras.md, pessoas.csv e assets já existem).
Escreva {perfil}/marca.json validado e, se o perfil tiver um GC aprovado (referencias/ ou marca.md), {perfil}/templates/gc/, como em references/templates.md da skill. Não mexa nos outros arquivos do perfil.
Você está rodando em segundo plano, sem ninguém para responder perguntas: registre o que ficou em dúvida em {perfil}/duvidas.md.
Observações do editor: {observacoes}"""

DEFAULT_CONFIG: dict[str, Any] = {
    # Caminho do binário; vazio = procurar no PATH (+ EXTRA_PATH).
    "claude_path": "",
    # Auto-SFX da sequência: a pasta de efeitos por sistema (config.json vai pelo Drive) e o ganho padrão.
    # Vazia = sem biblioteca de SFX (o Auto-SFX avisa).
    "sfx": {"pasta_mac": "", "pasta_windows": "", "ganho": -10},
    # Vazio = o padrão do CLI.
    "modelo": "",
    "effort": "",
    # "auto": um classificador aprova cada ação e nega o arriscado; com
    # --permission-prompts none o que precisaria de alguém é negado em vez de
    # travar o job esperando resposta.
    "permission_mode": "auto",
    "max_budget_usd": None,
    "flags_extras": [],
    "prompt_projeto": DEFAULT_PROMPT_PROJETO,
    "prompt_analise": DEFAULT_PROMPT_ANALISE,
    "prompt_ajuste": DEFAULT_PROMPT_AJUSTE,
    "prompt_ajuste_previa": DEFAULT_PROMPT_AJUSTE_PREVIA,
    "prompt_templates": DEFAULT_PROMPT_TEMPLATES,
    # Modelo do whisper nas legendas automáticas: sem IA revisando, vale o maior.
    "asr_modelo_legendas": "large-v3",
    # Corte vertical (autocorte.py): LLM, duração, enquadramento, locutores.
    "corte": copy.deepcopy(autocorte.DEFAULTS),
    # IA das funções do app (Sobre o projeto, dúvidas, revisão da descrição, telas, vitrine): OpenRouter,
    # com a chave do corte (corte.openrouter_api_key). claude_modelo = a revisão avançada.
    "ia": {"openrouter_modelo": "openrouter/free", "temperatura": 0.3, "timeout": 120, "claude_modelo": ""},
    "tarefas": {
        "inserts": {
            "rotulo": "Inserts",
            "pasta": "Motion - Inserts",
            "instrucoes": (
                "inserts de motion sobre o vídeo com apresentador(a), um momento "
                "tipográfico forte por tela. Só os elementos, sem fundo: nada de campo de "
                "cor, textura ou fundo trocado atrás da pessoa. Só a animação nos MOVs (ProRes 4444 com "
                "alpha, 30 fps), um arquivo por insert, nomeado pelo timecode de entrada "
                "MM-SS-FF; texto atrás da cabeça em \"(camada ATRAS)\" + \"(camada FRENTE)\"; "
                "telas múltiplas em arquivos separados; sem transição de início ou fim de take."
            ),
        },
        "gc": {
            "rotulo": "GC",
            "pasta": "Motion - GC",
            "modo": MODO_AUTO,
            "instrucoes": (
                "GCs (lower thirds) com nome e cargo das pessoas que aparecem, a partir de "
                "pessoas.csv do perfil. ProRes 4444 com alpha, 30 fps, um arquivo por GC "
                "nomeado pelo timecode de entrada MM-SS-FF."
            ),
        },
        "legendas": {
            "rotulo": "Legendas dinâmicas",
            "pasta": "Motion - Legendas",
            "modo": MODO_AUTO,
            "instrucoes": (
                "legendas dinâmicas sincronizadas palavra a palavra com a fala, no padrão "
                "do cliente. ProRes 4444 com alpha, 30 fps, vídeo inteiro."
            ),
        },
        TAREFA_CORTE: {
            "rotulo": "Corte vertical 9:16",
            "pasta": "Corte 9x16",
            "modo": MODO_CORTE,
            "instrucoes": (
                "o melhor corte curto e dinâmico do clipe para Reels/TikTok/Shorts, vertical "
                "1080x1920 a 30 fps, sem legenda impressa; legendas e áudios soltos."
            ),
        },
        "motion_curto": {
            "rotulo": "Motion curto",
            "pasta": "Motion - Curto",
            "instrucoes": (
                "uma peça curta de motion (até ~15 s) a partir do conteúdo do vídeo, no "
                "padrão do cliente, só os elementos sobre transparência (fundo próprio só "
                "se as observações pedirem). Detalhe nas observações."
            ),
        },
    },
}

_lock = threading.RLock()
# Estado vivo dos jobs que este processo acompanha: eventos resumidos e o
# offset já lido do .ndjson. O .json em disco é a fonte da verdade do status.
_live: dict[str, dict[str, Any]] = {}
_procs: dict[str, subprocess.Popen] = {}
_version_cache: dict[str, str | None] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ config
def get_config() -> dict[str, Any]:
    saved = safe_read_json(paths.EDITOR_CONFIG_JSON, {})
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for key, value in (saved or {}).items():
        if key == "tarefas" and isinstance(value, dict):
            for tkey, tval in value.items():
                if isinstance(tval, dict):
                    cfg["tarefas"].setdefault(tkey, {}).update(tval)
        elif key == "corte" and isinstance(value, dict):
            cfg["corte"].update({k: v for k, v in value.items() if k in autocorte.DEFAULTS})
        elif key == "ia" and isinstance(value, dict):
            cfg["ia"].update({k: v for k, v in value.items() if k in DEFAULT_CONFIG["ia"]})
        elif key in DEFAULT_CONFIG:
            cfg[key] = value
    return cfg


def _clean_ia(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in DEFAULT_CONFIG["ia"]:
            continue
        if key in ("temperatura", "timeout"):
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise EditorError(f"{key} inválido") from None
            value = min(2.0, max(0.0, value)) if key == "temperatura" else int(min(900, max(10, value)))
        else:
            value = str(value or "").strip()
        out[key] = value
    if "openrouter_modelo" in out and not out["openrouter_modelo"]:
        out["openrouter_modelo"] = DEFAULT_CONFIG["ia"]["openrouter_modelo"]
    return out


def save_config(fields: dict[str, Any]) -> dict[str, Any]:
    """Grava só o que difere do padrão — assim, um padrão melhorado numa
    versão nova do app chega a quem nunca mexeu naquele campo."""
    merged = get_config()
    for key, value in (fields or {}).items():
        if key == "tarefas" and isinstance(value, dict):
            for tkey, tval in value.items():
                if isinstance(tval, dict):
                    merged["tarefas"].setdefault(tkey, {}).update(
                        {k: v for k, v in tval.items() if isinstance(v, str)}
                    )
        elif key == "corte" and isinstance(value, dict):
            merged["corte"].update(_clean_corte(value))
        elif key == "ia" and isinstance(value, dict):
            merged["ia"].update(_clean_ia(value))
        elif key in DEFAULT_CONFIG:
            merged[key] = value
    diff: dict[str, Any] = {}
    for key, value in merged.items():
        if key == "tarefas":
            tdiff = {}
            for tkey, tval in value.items():
                base = DEFAULT_CONFIG["tarefas"].get(tkey, {})
                changed = {k: v for k, v in tval.items() if base.get(k) != v}
                if changed:
                    tdiff[tkey] = changed
            if tdiff:
                diff["tarefas"] = tdiff
        elif key == "corte":
            cdiff = {k: v for k, v in value.items() if DEFAULT_CONFIG["corte"].get(k) != v}
            if cdiff:
                diff["corte"] = cdiff
        elif key == "ia":
            idiff = {k: v for k, v in value.items() if DEFAULT_CONFIG["ia"].get(k) != v}
            if idiff:
                diff["ia"] = idiff
        elif DEFAULT_CONFIG.get(key) != value:
            diff[key] = value
    safe_write_json(paths.EDITOR_CONFIG_JSON, diff)
    return get_config()


def reset_config() -> dict[str, Any]:
    # As chaves não são "padrão": restaurar os prompts não pode apagar a chave da OpenRouter.
    corte = get_config()["corte"]
    keep = {k: corte[k] for k in CORTE_SEGREDOS if corte.get(k)}
    safe_write_json(paths.EDITOR_CONFIG_JSON, {"corte": keep} if keep else {})
    return get_config()


def _clean_corte(fields: dict[str, Any]) -> dict[str, Any]:
    """Valida o que vem do modal. Segredo vazio = manter o que está gravado
    (o GET não devolve o valor); `null` apaga."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in autocorte.DEFAULTS:
            continue
        if key in CORTE_SEGREDOS:
            if value is None:
                out[key] = ""
            elif str(value).strip():
                out[key] = str(value).strip()
            continue
        if key == "provedor_llm":
            if value not in ("claude", "openrouter"):
                raise EditorError("provedor do LLM inválido")
        elif key == "formato":
            if value not in ("corte", "desfocado"):
                raise EditorError("formato inválido")
        elif key in ("duracao_min", "duracao_max", "legenda_tamanho", "palavras_por_bloco"):
            try:
                value = max(1, int(float(value)))
            except (TypeError, ValueError):
                raise EditorError(f"valor inválido em {key}") from None
        elif key == "enquadramento":
            try:
                value = min(1.0, max(0.0, float(value)))
            except (TypeError, ValueError):
                raise EditorError("enquadramento inválido (0 = esquerda, 1 = direita)") from None
        elif key == "legenda_maiusculas":
            value = bool(value)
        elif key == "faixas_locutores":
            value = _parse_speaker_tracks(value)
        elif key == "layout":
            value = clean_layout(value)
        else:
            value = str(value or "").strip()
        out[key] = value
    if out.get("duracao_min") and out.get("duracao_max") and out["duracao_min"] > out["duracao_max"]:
        raise EditorError("a duração mínima do corte passa da máxima")
    return out


def clean_layout(value: Any) -> dict[str, Any] | None:
    """O enquadramento do editor visual: modo + caixas normalizadas (0–1 do
    quadro original). A proporção de cada caixa quem trava é o autocorte, no
    tamanho do vídeo de verdade; aqui só se garante que os números existem e
    cabem no quadro."""
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict) or value.get("modo") not in autocorte.LAYOUT_MODOS:
        raise EditorError("enquadramento inválido (modo: inteiro, dividido ou desfocado)")
    out: dict[str, Any] = {"modo": value["modo"]}
    for nome in ("principal", "camera"):
        box = value.get(nome)
        if box is None:
            continue
        try:
            x, y, w, h = (float(box[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            raise EditorError(f"caixa {nome} do enquadramento inválida") from None
        if not (0.02 <= w <= 1.0001 and 0.02 <= h <= 1.0001):
            raise EditorError(f"caixa {nome} do enquadramento pequena ou grande demais")
        x, y = min(max(0.0, x), 1 - min(w, 1.0)), min(max(0.0, y), 1 - min(h, 1.0))
        out[nome] = {"x": round(x, 4), "y": round(y, 4), "w": round(min(w, 1.0), 4), "h": round(min(h, 1.0), 4)}
    if value.get("divisao") is not None:
        try:
            d = float(value["divisao"])
        except (TypeError, ValueError):
            raise EditorError("divisão do enquadramento inválida") from None
        out["divisao"] = round(min(autocorte.DIVISAO_MAX, max(autocorte.DIVISAO_MIN, d)), 4)
    for campo, minimo, maximo in (("escala", 1.0, 4.0), ("posicao", 0.0, 1.0)):
        if value.get(campo) is None:
            continue
        try:
            out[campo] = round(min(maximo, max(minimo, float(value[campo]))), 4)
        except (TypeError, ValueError):
            raise EditorError(f"{campo} do enquadramento inválida") from None
    capa = clean_cover(value.get("capa"))
    if capa:
        out["capa"] = capa
    return out


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def clean_cover(value: Any) -> dict[str, Any] | None:
    """Capa do corte: um quadro do vídeo (`t`, segundos no bruto) ou uma
    imagem do disco (caminho completo). "nenhuma" = sem capa."""
    if not value or not isinstance(value, dict) or value.get("tipo") in (None, "", "nenhuma"):
        return None
    tipo = value.get("tipo")
    if tipo == "quadro":
        try:
            return {"tipo": "quadro", "t": round(max(0.0, float(value.get("t") or 0)), 2)}
        except (TypeError, ValueError):
            raise EditorError("tempo da capa inválido") from None
    if tipo == "imagem":
        imagem = str(value.get("imagem") or "").strip()
        if not imagem or not Path(imagem).is_absolute() or Path(imagem).suffix.lower() not in IMAGE_EXTS:
            raise EditorError("a capa precisa ser uma imagem JPG, PNG ou WebP (caminho completo)")
        return {"tipo": "imagem", "imagem": imagem}
    raise EditorError("tipo de capa inválido (nenhuma, quadro ou imagem)")


EDICAO_MAX_TRECHOS = 400
EDICAO_MAX_PALAVRAS = 60000
_HEX_COR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _num(value: Any, minimo: float, maximo: float, campo: str, padrao: float | None = None) -> float | None:
    if value is None or value == "":
        return padrao
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise EditorError(f"valor inválido em {campo}") from None
    if x != x:  # NaN
        raise EditorError(f"valor inválido em {campo}")
    return min(maximo, max(minimo, x))


def clean_edicao(value: Any) -> dict[str, Any] | None:
    """A edição feita na timeline do editor (estúdio de Edição IA): trechos do
    bruto com enquadramento e transição próprios, palavras revisadas, gancho,
    cor, estilo da legenda e mixagem. É o que o autocorte usa no lugar do
    whisper/LLM; tempos sempre em segundos do vídeo bruto."""
    if not value:
        return None
    if not isinstance(value, dict):
        raise EditorError("edição inválida")
    segs = []
    for i, item in enumerate(value.get("segmentos") or []):
        if not isinstance(item, dict):
            raise EditorError(f"trecho {i + 1} inválido")
        a = _num(item.get("inicio"), 0, 1e7, "início do trecho")
        b = _num(item.get("fim"), 0, 1e7, "fim do trecho")
        if a is None or b is None or b - a < 0.05:
            continue
        seg = {"inicio": round(a, 3), "fim": round(b, 3), "motivo": str(item.get("motivo") or "").strip()[:300]}
        if item.get("layout"):
            seg["layout"] = clean_layout(item["layout"])
        if item.get("transicao") in autocorte.TRANSICOES[1:]:
            seg["transicao"] = item["transicao"]
        segs.append(seg)
    if not segs:
        raise EditorError("a timeline está vazia: deixe pelo menos um trecho no corte")
    if len(segs) > EDICAO_MAX_TRECHOS:
        raise EditorError(f"trechos demais na timeline (máximo {EDICAO_MAX_TRECHOS})")
    out: dict[str, Any] = {"segmentos": segs}
    palavras = value.get("palavras")
    if palavras is not None:
        if not isinstance(palavras, list) or len(palavras) > EDICAO_MAX_PALAVRAS:
            raise EditorError("palavras da edição inválidas")
        limpas = []
        for w in palavras:
            if not isinstance(w, dict):
                continue
            t = _num(w.get("t"), 0, 1e7, "tempo da palavra")
            fim = _num(w.get("fim"), 0, 1e7, "tempo da palavra")
            txt = str(w.get("texto") or "").strip()[:80]
            if t is None or fim is None or not txt:
                continue
            limpas.append({"texto": txt, "t": round(t, 3), "fim": round(max(t, fim), 3),
                           "locutor": str(w.get("locutor") or "Locutor 1").strip()[:40] or "Locutor 1",
                           "duvidosa": bool(w.get("duvidosa"))})
        out["palavras"] = limpas
    for campo, limite in (("titulo", 120), ("resumo", 600), ("gancho", 120), ("origem", 80)):
        if value.get(campo) is not None:
            out[campo] = str(value[campo]).strip()[:limite]
    if value.get("origem_locutores") in ("faixas de áudio", "pyannote", "um locutor", "edição"):
        out["origem_locutores"] = value["origem_locutores"]
    out["legenda"] = value.get("legenda") is not False
    gd = _num(value.get("gancho_duracao"), 0.5, 15, "duração do gancho")
    if gd is not None:
        out["gancho_duracao"] = round(gd, 2)
    if value.get("layout"):
        out["layout"] = clean_layout(value["layout"])
    cor = value.get("cor") or {}
    if isinstance(cor, dict) and cor:
        out["cor"] = {
            "brilho": round(_num(cor.get("brilho"), 0.3, 2.0, "brilho", 1.0), 3),
            "contraste": round(_num(cor.get("contraste"), 0.3, 2.5, "contraste", 1.0), 3),
            "saturacao": round(_num(cor.get("saturacao"), 0.0, 3.0, "saturação", 1.0), 3),
        }
    est = value.get("estilo") or {}
    if isinstance(est, dict) and est:
        estilo: dict[str, Any] = {}
        if est.get("tamanho") is not None:
            estilo["tamanho"] = int(_num(est["tamanho"], 30, 220, "tamanho da legenda"))
        if est.get("margem") is not None:
            estilo["margem"] = int(_num(est["margem"], 0, 1700, "posição da legenda"))
        if est.get("palavras") is not None:
            estilo["palavras"] = int(_num(est["palavras"], 1, 10, "palavras por bloco"))
        if est.get("maiusculas") is not None:
            estilo["maiusculas"] = bool(est["maiusculas"])
        if est.get("estatica") is not None:   # legenda fixa (sem karaokê), o toggle do monitor
            estilo["estatica"] = bool(est["estatica"])
        if est.get("fonte"):
            fonte = str(est["fonte"]).strip()
            if not re.fullmatch(r"[\w .\-]{1,60}", fonte):
                raise EditorError("nome de fonte inválido")
            estilo["fonte"] = fonte
        if est.get("cores"):
            cores = [str(c) for c in est["cores"] if _HEX_COR.match(str(c))][:8]
            if cores:
                estilo["cores"] = cores
        out["estilo"] = estilo
    au = value.get("audio") or {}
    if isinstance(au, dict) and au:
        faixas = {}
        for k, v in (au.get("faixas") or {}).items():
            try:
                n = int(k)
            except (TypeError, ValueError):
                raise EditorError("faixa de áudio inválida") from None
            if not 1 <= n <= 64 or not isinstance(v, dict):
                raise EditorError("faixa de áudio inválida")
            faixas[str(n)] = {"ganho": round(_num(v.get("ganho"), -40, 18, "ganho", 0.0), 2), "mudo": bool(v.get("mudo"))}
        out["audio"] = {
            "faixas": faixas,
            "fade_entrada": round(_num(au.get("fade_entrada"), 0, 10, "fade de entrada", 0.0), 2),
            "fade_saida": round(_num(au.get("fade_saida"), 0, 10, "fade de saída", 0.0), 2),
            "normalizar": bool(au.get("normalizar")),
        }
    if isinstance(value.get("descartado"), list):
        out["descartado"] = [d for d in value["descartado"] if isinstance(d, dict)][:200]
    return out


def cover_image_path(value: str) -> Path | None:
    """Imagem escolhida para a capa, para a prévia no editor de enquadramento."""
    path = Path(value or "")
    if not path.is_absolute() or path.suffix.lower() not in IMAGE_EXTS or not path.is_file():
        return None
    return path


def _parse_speaker_tracks(value: Any) -> list[dict[str, Any]]:
    """"2=Bruno, 3=Convidado" ou a lista pronta → [{"faixa": 2, "nome": "Bruno"}, …]."""
    items = value if isinstance(value, list) else [p for p in str(value or "").split(",") if p.strip()]
    out = []
    for item in items:
        if isinstance(item, dict):
            faixa, nome = item.get("faixa"), item.get("nome")
        else:
            faixa, _, nome = str(item).partition("=")
        try:
            faixa = int(str(faixa).strip())
        except ValueError:
            raise EditorError(f"faixa de locutor inválida: {item!r} (use 2=Nome)") from None
        if faixa < 1:
            raise EditorError("as faixas de áudio contam a partir de 1")
        out.append({"faixa": faixa, "nome": str(nome or "").strip() or f"Faixa {faixa}"})
    return out


def public_config() -> dict[str, Any]:
    """A config para o frontend: sem o valor das chaves, só se estão definidas."""
    cfg = get_config()
    for key, env in CORTE_SEGREDOS.items():
        cfg["corte"][f"{key}_definida"] = bool(cfg["corte"].get(key) or os.environ.get(env))
        cfg["corte"][key] = ""
    return cfg


# ------------------------------------------------------------- disponibilidade
def _search_path() -> str:
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for extra in EXTRA_PATH:
        if extra not in parts:
            parts.append(extra)
    return os.pathsep.join(parts)


def child_env() -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if k not in _ENV_DROP_EXACT and not k.startswith(_ENV_DROP_PREFIXES)
    }
    env["PATH"] = _search_path()
    return env


def find_claude() -> str | None:
    configured = (get_config().get("claude_path") or "").strip()
    if configured:
        p = Path(configured).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return shutil.which("claude", path=_search_path())


def _claude_version(binary: str) -> str | None:
    if binary in _version_cache:
        return _version_cache[binary]
    version = None
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=15, env=child_env()
        )
        version = (out.stdout or "").strip().split("\n")[0] or None
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("claude --version failed: %s", exc)
    _version_cache[binary] = version
    return version


def skill_installed() -> bool:
    return (Path.home() / ".claude" / "skills" / SKILL_NAME / "SKILL.md").is_file()


def task_mode(task_key: str, cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or get_config()
    if task_key == TAREFA_CORTE:
        return MODO_CORTE
    modo = ((cfg.get("tarefas") or {}).get(task_key) or {}).get("modo")
    return MODO_AUTO if modo == MODO_AUTO and task_key in AUTO_TAREFAS else MODO_IA


TOKENS_STATUS = 428   # o frontend pergunta "Serão usados tokens do Claude" e repete com confirmar_tokens


def check_tokens(confirmado: bool, oque: str) -> None:
    """Toda ação que chama o Claude Code (tokens da conta) precisa da confirmação explícita da pessoa:
    sem `confirmar_tokens`, o job não começa e a rota responde 428."""
    if not confirmado:
        raise EditorError(f"{oque} usa o Claude Code e gasta tokens da sua conta.", TOKENS_STATUS)


def corte_availability(cfg: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """O corte vertical precisa do LLM escolhido: o claude logado ou a chave da OpenRouter."""
    corte = (cfg or get_config())["corte"]
    if corte.get("provedor_llm") == "openrouter":
        if not (corte.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY")):
            return False, "Corte vertical usa a OpenRouter, mas falta a chave (Configurar Editor (IA) → Corte vertical)."
        if not corte.get("openrouter_modelo"):
            return False, "Corte vertical usa a OpenRouter: escolha o modelo em Configurar Editor (IA)."
        return True, None
    if not find_claude():
        return False, "Corte vertical usa o Claude Code CLI para escolher os trechos, e ele não foi encontrado."
    return True, None


def availability() -> dict[str, Any]:
    binary = find_claude()
    if not binary:
        return {
            "disponivel": False,
            "auto_disponivel": KIT_DIR.is_dir(),
            "motivo": (
                "Claude Code CLI não encontrado. Instale com "
                "`curl -fsSL https://claude.ai/install.sh | bash`, rode `claude` uma vez "
                "no Terminal para fazer login e reabra o Clientela."
            ),
            "caminho": None,
            "versao": None,
            "skill_instalada": skill_installed(),
            "job_ativo": active_job_id(),
            **_corte_status(),
        }
    return {
        "disponivel": True,
        "auto_disponivel": KIT_DIR.is_dir(),
        "motivo": None,
        "caminho": binary,
        "versao": _claude_version(binary),
        "skill_instalada": skill_installed(),
        "skill": SKILL_NAME,
        "job_ativo": active_job_id(),
        **_corte_status(),
    }


def _corte_status() -> dict[str, Any]:
    ok, motivo = corte_availability()
    return {"corte_disponivel": ok, "corte_motivo": motivo}


# --------------------------------------------------------------- persistência
def _job_json(job_id: str) -> Path:
    return paths.EDITOR_JOBS_DIR / f"{job_id}.json"


def _job_stream(job_id: str) -> Path:
    return paths.EDITOR_JOBS_DIR / f"{job_id}.ndjson"


def _job_stderr(job_id: str) -> Path:
    return paths.EDITOR_JOBS_DIR / f"{job_id}.stderr.log"


def _job_prompt(job_id: str) -> Path:
    return paths.EDITOR_JOBS_DIR / f"{job_id}.prompt.txt"


def _valid_job_id(job_id: str) -> bool:
    return bool(job_id) and re.fullmatch(r"[0-9a-f]{32}", job_id) is not None


def load_job(job_id: str) -> dict[str, Any] | None:
    if not _valid_job_id(job_id):
        return None
    job = safe_read_json(_job_json(job_id), None)
    return job if isinstance(job, dict) else None


def _save_job(job: dict[str, Any]) -> None:
    safe_write_json(_job_json(job["id"]), job)


def _update_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    with _lock:
        job = load_job(job_id)
        if not job:
            return None
        job.update(fields)
        _save_job(job)
        return job


def list_jobs(project_id: str | None = None, client_id: str | None = None) -> list[dict[str, Any]]:
    if not paths.EDITOR_JOBS_DIR.is_dir():
        return []
    jobs = []
    for p in paths.EDITOR_JOBS_DIR.glob("*.json"):
        if p.name.endswith(".bak"):
            continue
        job = safe_read_json(p, None)
        if not isinstance(job, dict):
            continue
        if project_id and job.get("project_id") != project_id:
            continue
        if client_id and job.get("client_id") != client_id:
            continue
        jobs.append(job)
    jobs.sort(key=lambda j: j.get("criado_em") or "", reverse=True)
    return jobs


def active_job_id() -> str | None:
    for job in list_jobs():
        if job.get("status") in ACTIVE_STATUSES:
            return job["id"]
    return None


# ------------------------------------------------------------------- prompts
def fill_template(template: str, values: dict[str, Any]) -> str:
    """Troca só os {placeholders} conhecidos. Não usa str.format: o usuário
    edita o modelo na tela e uma chave solta (um JSON de exemplo) não pode
    derrubar o job com KeyError."""
    out = template or ""
    for key, value in values.items():
        out = out.replace("{" + key + "}", "" if value is None else str(value))
    return out


def next_version(project_dir: Path, pasta_base: str) -> int:
    highest = 0
    if project_dir.is_dir():
        for child in project_dir.iterdir():
            m = DELIVERY_DIR_RE.match(child.name)
            if child.is_dir() and m and m.group(1) == pasta_base:
                highest = max(highest, int(m.group(2)))
    # Um job que ainda não criou a pasta de entrega já reservou o número.
    for job in list_jobs():
        if (
            job.get("status") in ACTIVE_STATUSES
            and job.get("cwd") == str(project_dir)
            and job.get("pasta_base") == pasta_base
        ):
            highest = max(highest, int(job.get("versao") or 0))
    return highest + 1


def build_command(binary: str, add_dirs: list[str], cfg: dict[str, Any], name: str) -> list[str]:
    cmd = [
        binary, "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", cfg.get("permission_mode") or "auto",
        "--permission-prompts", "none",
        "-n", name,
    ]
    if cfg.get("modelo"):
        cmd += ["--model", str(cfg["modelo"])]
    if cfg.get("effort"):
        cmd += ["--effort", str(cfg["effort"])]
    if cfg.get("max_budget_usd"):
        cmd += ["--max-budget-usd", str(cfg["max_budget_usd"])]
    extras = cfg.get("flags_extras") or []
    if isinstance(extras, str):
        extras = extras.split()
    cmd += [str(x) for x in extras]
    # --add-dir por último: ele é variádico e engoliria o que viesse depois.
    for d in add_dirs:
        cmd += ["--add-dir", d]
    return cmd


# ------------------------------------------------------------ início de jobs
class EditorError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _safe_rel(base: Path, rel: str | None) -> Path | None:
    if not rel:
        return None
    target = (base / rel).resolve()
    if base.resolve() not in target.parents or not target.is_file():
        return None
    return target


def _resolve_media(base: Path | None, value: str | None, exts: set[str]) -> Path | None:
    """Caminho relativo à pasta do projeto (os seletores do painel do projeto)
    ou absoluto (um arquivo escolhido no Finder pelo "Edição IA" do topo, de
    qualquer lugar do disco). O absoluto só vale para arquivo de mídia de
    verdade: a extensão tem de ser de vídeo/transcrição."""
    if not value:
        return None
    if Path(value).is_absolute():
        target = Path(value).resolve()
        return target if target.is_file() and target.suffix.lower() in exts else None
    return _safe_rel(base, value) if base else None


def _rel_or_abs(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base.resolve()))
    except ValueError:
        return str(path)


def _parse_gcs(gcs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for g in gcs or []:
        nome = str((g or {}).get("nome") or "").strip()
        if not nome:
            continue
        try:
            inicio = max(0.0, float(g.get("inicio") or 0))
        except (TypeError, ValueError):
            raise EditorError(f"entrada inválida no GC de {nome}")
        cargo = g.get("cargo")
        out.append({"nome": nome, "cargo": str(cargo).strip() if cargo is not None else None, "inicio": inicio})
    return out


def _copy_kit(trabalho: Path) -> Path:
    """O kit da skill vai para a bancada a cada job: é lá que ele roda (e que o
    estagiário pode mexer sem tocar no original)."""
    if not KIT_DIR.is_dir():
        raise EditorError("o kit da skill estagiario-editor não está no repositório do app", 500)
    kit = trabalho / "kit"
    shutil.copytree(KIT_DIR, kit, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    return kit


def _auto_cmd(kit: Path, modo: str, args_path: Path) -> list[str]:
    return [sys.executable or "python3", str(kit / "auto.py"), modo, "--args", str(args_path)]


def start_project_job(
    project_id: str,
    tarefa: str,
    video: str,
    transcricao: str | None = None,
    observacoes: str = "",
    gcs: list[dict[str, Any]] | None = None,
    layout: dict[str, Any] | None = None,
    edicao: dict[str, Any] | None = None,
    confirmar_tokens: bool = False,
    opcoes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project = projects_module.get_project(project_id)
    if not project:
        raise EditorError("projeto não encontrado", 404)
    if not project.get("folder_available"):
        raise EditorError("pasta do projeto não encontrada no disco", 409)
    return _start_media_job(
        base_dir=Path(project["path"]),
        client_id=project["client_id"],
        project_id=project_id,
        projeto_nome=project.get("folder_name") or project.get("name"),
        tarefa=tarefa, video=video, transcricao=transcricao, observacoes=observacoes, gcs=gcs, layout=layout,
        edicao=edicao, confirmar_tokens=confirmar_tokens, opcoes=opcoes, previa=True,
    )


def start_downloads_job(
    tarefa: str,
    video: str,
    transcricao: str | None = None,
    observacoes: str = "",
    gcs: list[dict[str, Any]] | None = None,
    client_id: str | None = None,
    layout: dict[str, Any] | None = None,
    edicao: dict[str, Any] | None = None,
    confirmar_tokens: bool = False,
    opcoes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trabalho avulso, sem projeto: entrega e bancada na ~/Downloads. O
    cliente é opcional e só empresta a identidade visual (o perfil
    _Estagiário/); sem ele a tarefa com IA segue um estilo neutro e as
    automáticas não rodam, porque o estilo delas é o marca.json do cliente."""
    if client_id and not clients_module.get_client(client_id):
        raise EditorError("cliente não encontrado", 404)
    return _start_media_job(
        base_dir=paths.USER_DOWNLOADS_DIR,
        client_id=client_id or None,
        project_id=None,
        projeto_nome="Downloads",
        tarefa=tarefa, video=video, transcricao=transcricao, observacoes=observacoes, gcs=gcs, layout=layout,
        edicao=edicao, confirmar_tokens=confirmar_tokens, opcoes=opcoes,
    )


def inserts_opcoes(opcoes: dict[str, Any] | None, previa: bool) -> dict[str, Any]:
    """Opções da tarefa Inserts. `previa`: o job para antes do render e o editor aprova na prévia do app
    (só em projeto; no avulso da ~/Downloads não há onde abrir a prévia). `manual`: sem IA, só a bancada."""
    o = opcoes or {}
    telas = []
    for t in (o.get("telas") or [])[:MAX_TELAS] if isinstance(o.get("telas"), list) else []:
        desc = str((t or {}).get("descricao") or "").strip()[:1000] if isinstance(t, dict) else ""
        if not desc:
            continue
        try:
            nivel = min(3, max(0, int(t.get("nivel", 2))))
        except (TypeError, ValueError):
            nivel = 2
        telas.append({"titulo": str(t.get("titulo") or "").strip()[:60], "descricao": desc, "nivel": nivel})
    return {
        "estilo": o.get("estilo") if o.get("estilo") in ESTILOS_INSERTS else "padrao",
        "recorte": bool(o.get("recorte")),
        "manual": bool(o.get("manual")) and previa,
        "previa": previa,
        # tela fixa primeiro: só faz sentido com a prévia, onde o editor aprova antes de pedir a animação
        "estatico": bool(o.get("estatico")) and previa,
        "telas": telas,
    }


MAX_TELAS = 20
# O resultado estava saindo igual em todo vídeo: a IDV virava molde. Ela é o vocabulário, não a frase.
CRIATIVIDADE = (
    "CRIATIVIDADE: a identidade visual do cliente define cores, fontes, realce e tom — é o vocabulário, não um "
    "molde. Não repita a mesma composição em todo insert nem copie o último vídeo: para cada momento, ache a ideia "
    "visual que aquela fala pede (escala, ritmo, direção do movimento, quebra de linha, objeto, contraste, uso do "
    "espaço ao redor da pessoa) e varie entre os inserts. Pelo menos um momento do vídeo deve ter uma ideia que "
    "surpreenda. As regras de leitura, de fidelidade à fala e de não cobrir o rosto continuam valendo."
)
TELAS_JSON = "telas.json"
NIVEIS_ANIMACAO = {
    0: "nível 0, quase parada: só aparece e some com um fade curto",
    1: "nível 1, discreta: entrada suave, sem movimento enquanto fica na tela",
    2: "nível 2, moderada: entrada animada por elemento e um movimento leve contínuo",
    3: "nível 3, intensa: tipografia cinética, várias etapas, é o ponto alto do vídeo",
}


def telas_instrucoes(telas: list[dict[str, Any]]) -> str:
    """As telas pedidas em cards: uma composição por tela, com o nome que a prévia usa para casar card ↔ insert."""
    linhas = ["TELAS PEDIDAS pelo editor (faça exatamente estas, nesta numeração; ache o momento de cada uma na "
              "transcrição pela descrição; o nome no jobs.json começa com \"Tela <n>\" e as composições se chamam "
              "tela-<n> ou tela-<n>-atras / tela-<n>-frente):"]
    for i, t in enumerate(telas, 1):
        titulo = f" — {t['titulo']}" if t.get("titulo") else ""
        linhas.append(f"Tela {i}{titulo}: {t['descricao']} (animação: {NIVEIS_ANIMACAO[t['nivel']]}).")
    return " ".join(linhas)


def legendas_opcoes(opcoes: dict[str, Any] | None) -> dict[str, Any]:
    """Animação universal e fonte das legendas (vão para o kit/legendas.py pelo auto-args.json)."""
    o = opcoes or {}
    anim = o.get("animacao") or "perfil"
    if anim not in ANIMACOES_LEGENDA:
        raise EditorError("animação de legenda desconhecida")
    fonte = str(o.get("fonte") or "").strip()
    if len(fonte) > 80 or re.search(r'[\\"<>{};]', fonte):
        raise EditorError("nome de fonte inválido")
    return {"animacao": anim, "fonte": fonte}


def inserts_instrucoes(o: dict[str, Any]) -> str:
    """O que muda no pedido conforme as opções. Vai junto das instruções da tarefa, então vale também para
    quem personalizou o prompt do projeto."""
    linhas = [
        "ESTILO: clean (references/tarefa-inserts.md §6): só o texto, entrada suave, realce só na cor, corpo menor."
        if o["estilo"] == "clean" else "ESTILO: padrão da identidade visual do cliente.",
        "RECORTE: recorte a pessoa (remove-background) se algum insert usar ATRÁS + FRENTE." if o["recorte"] else
        "RECORTE: não recorte a pessoa (o editor recorta à mão) e não rode remove-background; texto atrás da "
        "cabeça ainda pode sair em ATRÁS + FRENTE.",
        "TEMPLATES UNIVERSAIS: quando um momento couber num template de kit/universais.json, monte-o pelo "
        "inserts.json + kit/universais.py; HTML à mão só para o que os templates não fazem.",
    ]
    if o.get("telas"):
        linhas.append(telas_instrucoes(o["telas"]))
    if o.get("estatico"):
        linhas.append("TELA FIXA PRIMEIRO: monte cada insert como uma tela parada, já no estado final composto "
                      "(sem animação de entrada nem de saída, segurando a duração inteira). Não anime agora: o "
                      "editor aprova as telas na prévia e pede a animação só das aprovadas, no nível de cada uma.")
    if o["previa"]:
        linhas.append("PRÉVIA: monte, revise e passe o lint de cada insert, escreva o jobs.json e PARE. Não rode "
                      "render.py nem entregar.py: o editor vê os inserts em HTML no app, aprova e o app exporta só os aprovados.")
    return " ".join(linhas)


def _start_media_job(
    *,
    base_dir: Path,
    client_id: str | None,
    project_id: str | None,
    projeto_nome: str,
    tarefa: str,
    video: str,
    transcricao: str | None,
    observacoes: str,
    gcs: list[dict[str, Any]] | None,
    layout: dict[str, Any] | None = None,
    edicao: dict[str, Any] | None = None,
    confirmar_tokens: bool = False,
    opcoes: dict[str, Any] | None = None,
    previa: bool = False,
) -> dict[str, Any]:
    """`base_dir` é onde saem a entrega (<base>/Motion - X vN/) e a bancada
    (<base>/.estagiario/): a pasta do projeto ou, no avulso, a ~/Downloads."""
    cfg = get_config()
    task = cfg["tarefas"].get(tarefa)
    if not task:
        raise EditorError("tarefa desconhecida")
    modo = task_mode(tarefa, cfg)
    ins = inserts_opcoes(opcoes, previa) if tarefa == "inserts" else None
    leg = legendas_opcoes(opcoes) if tarefa == "legendas" else None
    if ins and ins["manual"]:
        modo = MODO_AUTO  # inserts à mão: só a bancada, montados na prévia com os templates universais
    corte_cfg = cfg["corte"]
    # A edição da timeline (os trechos já escolhidos) só passa pelo ffmpeg: não pede LLM.
    edicao = clean_edicao(edicao) if edicao else None
    if edicao and modo != MODO_CORTE:
        raise EditorError("a edição na timeline é só do corte vertical")
    usa_claude = not edicao and (modo == MODO_IA or (modo == MODO_CORTE and corte_cfg.get("provedor_llm") != "openrouter"))
    binary = find_claude() if usa_claude else None
    if modo == MODO_IA and not binary:
        raise EditorError(availability()["motivo"], 503)
    if modo == MODO_CORTE and not edicao:
        ok, motivo = corte_availability(cfg)
        if not ok:
            raise EditorError(motivo, 503 if usa_claude else 409)
    if usa_claude:
        check_tokens(confirmar_tokens, task.get("rotulo") or tarefa)
    client = clients_module.get_client(client_id) if client_id else None
    video_path = _resolve_media(base_dir, video, VIDEO_EXTS)
    if not video_path:
        raise EditorError("escolha o vídeo bruto (um arquivo de vídeo da pasta do projeto ou do disco)")
    transcript_path = _resolve_media(base_dir, transcricao, TRANSCRIPT_EXTS) if transcricao else None
    if transcricao and not transcript_path:
        raise EditorError("transcrição não encontrada")

    # Não cria o perfil só porque um job rodou: sem _Estagiário/ no cliente o
    # prompt aponta o caminho mesmo assim e a skill trabalha sem manual.
    perfil_dir = identidades.profile_dir(client_id) if client_id else None
    gcs_ok = _parse_gcs(gcs) if tarefa == "gc" else []
    if modo == MODO_AUTO:
        if not client_id:
            raise EditorError(
                f"{task.get('rotulo', tarefa)} é automático e o estilo vem dos templates de um cliente: "
                "escolha a identidade visual de um cliente.",
                409,
            )
        if not perfil_dir or not (perfil_dir / "marca.json").is_file():
            raise EditorError(
                "este cliente ainda não tem templates (marca.json no perfil). Gere na aba Identidade; "
                "sem eles o modo automático não sabe o estilo.",
                409,
            )
        if tarefa == "gc" and not gcs_ok:
            raise EditorError("escolha a pessoa e a entrada de pelo menos um GC")
    pasta_base = task["pasta"]
    with _lock:
        _ensure_single_job()
        base_dir.mkdir(parents=True, exist_ok=True)
        versao = next_version(base_dir, pasta_base)
        entrega = base_dir / f"{pasta_base} v{versao}"
        trabalho = base_dir / WORK_DIR / f"{tarefa} v{versao}"
        trabalho.mkdir(parents=True, exist_ok=True)
        # O corte não usa a skill: roda autocorte.py direto, sem copiar o kit.
        kit = _copy_kit(trabalho) if modo != MODO_CORTE else None
        if ins and (ins["telas"] or ins["estatico"]):  # a prévia casa cada card com o insert (tela-<n>)
            safe_write_json(trabalho / TELAS_JSON, {"estatico": ins["estatico"], "telas": ins["telas"], "animar": []})
        values = {
            "cliente": client["name"] if client else (client_id or "nenhum (trabalho avulso)"),
            "perfil": str(perfil_dir) if perfil_dir else "nenhum (sem identidade visual: siga um estilo neutro e limpo)",
            "projeto": str(base_dir),
            "tarefa": task.get("rotulo", tarefa),
            "instrucoes": task.get("instrucoes", "") + (" " + inserts_instrucoes(ins) if ins else "")
            + (("\n" + CRIATIVIDADE + "\n" + aprendizado.licoes(tarefa, client_id) + "\n"
                + identidades.referencias_texto(client_id, project_id)[0]).rstrip() if modo == MODO_IA else ""),
            "video": str(video_path),
            "transcricao": str(transcript_path) if transcript_path else "nenhuma (transcreva o vídeo)",
            "trabalho": str(trabalho),
            "entrega": str(entrega),
            "versao": f"v{versao}",
            "observacoes": (observacoes or "").strip() or "nenhuma",
        }
        if modo == MODO_AUTO:
            prompt = f"(modo automático, sem IA) {values['tarefa']} · {values['video']}"
        elif modo == MODO_CORTE and edicao:
            prompt = f"(corte vertical: edição da timeline → ffmpeg) {values['video']}"
        elif modo == MODO_CORTE:
            prompt = f"(corte vertical: whisper → LLM → ffmpeg) {values['video']}"
        else:
            prompt = fill_template(cfg.get("prompt_projeto") or DEFAULT_PROMPT_PROJETO, values)
        add_dirs = [str(perfil_dir)] if perfil_dir and perfil_dir.is_dir() else []
        if modo == MODO_IA:  # o claude roda na pasta do projeto: as referências são pastas irmãs
            add_dirs += identidades.referencias_texto(client_id, project_id)[1]
        # Vídeo de fora da pasta de trabalho (escolhido no Finder): o claude
        # precisa poder ler a pasta dele.
        for media in (video_path, transcript_path):
            if media and base_dir.resolve() not in media.parents and str(media.parent) not in add_dirs:
                add_dirs.append(str(media.parent))
        job = _new_job(
            tipo=TIPO_PROJETO,
            modo=modo,
            client_id=client_id,
            project_id=project_id,
            destino="projeto" if project_id else "downloads",
            cliente_nome=values["cliente"] if client_id else None,
            projeto_nome=projeto_nome,
            tarefa=tarefa,
            tarefa_rotulo=task.get("rotulo", tarefa),
            video=_rel_or_abs(video_path, base_dir),
            transcricao=_rel_or_abs(transcript_path, base_dir) if transcript_path else None,
            observacoes=observacoes or "",
            gcs=gcs_ok or None,
            cwd=str(base_dir),
            add_dirs=add_dirs,
            pasta_base=pasta_base,
            versao=versao,
            entrega=str(entrega),
            trabalho=str(trabalho),
            edicao_de=(edicao or {}).get("origem"),
            inserts=ins,
            legendas=leg,
            perfil_status=identidades.get_status(client_id) if client_id else None,
            prompt=prompt,
        )
        args = {
            "video": str(video_path), "transcricao": str(transcript_path) if transcript_path else "",
            "perfil": str(perfil_dir) if perfil_dir and perfil_dir.is_dir() else "",
            "entrega": str(entrega), "versao": f"v{versao}", "tarefa": values["tarefa"],
            "cliente": values["cliente"], "projeto": str(base_dir), "gcs": gcs_ok,
            "asr_modelo": cfg.get("asr_modelo_legendas") if tarefa == "legendas" else "small",
            "legendas": leg,
        }
        if modo == MODO_IA:
            name = f"Clientela · {job.get('tarefa_rotulo')} · {job.get('projeto_nome')}"
            args["claude"] = build_command(binary, add_dirs, cfg, name)
        args_path = trabalho / "auto-args.json"
        if modo == MODO_CORTE:
            # Sem as chaves: o auto-args.json fica na pasta do projeto (no Drive).
            corte_args = {k: v for k, v in corte_cfg.items() if k not in CORTE_SEGREDOS}
            # o do modal vale para este job; sem ele, o padrão salvo (o que os clipes usam)
            layout_job = clean_layout(layout) if layout else corte_cfg.get("layout")
            if edicao and edicao.get("layout"):
                layout_job = edicao["layout"]
            args = {
                "video": str(video_path),
                "transcricao": str(transcript_path) if transcript_path else "",
                "entrega": str(entrega), "versao": f"v{versao}", "projeto": str(base_dir),
                "observacoes": (observacoes or "").strip(), "corte": corte_args, "claude": binary,
                "layout": layout_job,
                "edicao": edicao,
            }
            safe_write_json(args_path, args)
            env = {env_key: corte_cfg[key] for key, env_key in CORTE_SEGREDOS.items() if corte_cfg.get(key)}
            cmd = [sys.executable or "python3", str(AUTOCORTE_SCRIPT), "--args", str(args_path)]
            _launch(job, cmd, cwd=str(trabalho), env_extra=env)
            return load_job(job["id"]) or job
        safe_write_json(args_path, args)
        cmd = _auto_cmd(kit, ("bancada" if ins and ins["manual"] else tarefa) if modo == MODO_AUTO else "motion", args_path)
        _launch(job, cmd, cwd=str(trabalho))
    return load_job(job["id"]) or job


def _valid_analysis_folders(pastas: list[str] | None) -> list[str]:
    out = []
    for raw in pastas or []:
        p = Path(str(raw or "")).expanduser()
        if not str(raw or "").strip():
            continue
        if not p.is_absolute() or not p.is_dir():
            raise EditorError(f"pasta não encontrada: {raw}")
        resolved = str(p.resolve())
        if resolved not in out:
            out.append(resolved)
    return out


def start_analysis_job(
    client_id: str, observacoes: str = "", pastas: list[str] | None = None, confirmar_tokens: bool = False
) -> dict[str, Any]:
    """Onboarding de identidade. `pastas` são as pastas que o estagiário deve
    estudar — qualquer lugar do disco (ex.: o projeto aprovado + a pasta de
    renders de referência); vazio = a pasta inteira do cliente. As que ficam
    fora da pasta do cliente entram como --add-dir pra ele poder lê-las."""
    binary = find_claude()
    if not binary:
        raise EditorError(availability()["motivo"], 503)
    check_tokens(confirmar_tokens, "A análise da identidade visual")
    client = clients_module.get_client(client_id)
    if not client:
        raise EditorError("cliente não encontrado", 404)
    if not clients_module.folder_available(client):
        raise EditorError("pasta do cliente não encontrada no disco", 409)
    cfg = get_config()
    pastas_ok = _valid_analysis_folders(pastas)
    client_root = Path(client["path"]).resolve()
    perfil_dir = identidades.ensure_profile(client_id)
    if pastas_ok:
        lista_pastas = "\n".join(f"- {p}" for p in pastas_ok)
    else:
        lista_pastas = f"- {client['path']} (a pasta inteira do cliente)"
    refs = [r["pasta"] for r in identidades.referencias_projetos(client_id) if r["pasta"]]
    if refs:
        lista_pastas += ("\nProjetos que o editor marcou como referência visual aprovada (estude as entregas finais "
                         "deles com prioridade):\n" + "\n".join(f"- {r}" for r in refs))
    fora = [p for p in pastas_ok if Path(p) != client_root and client_root not in Path(p).parents]
    with _lock:
        _ensure_single_job()
        values = {
            "cliente": client["name"],
            "pasta_cliente": client["path"],
            "pastas_analise": lista_pastas,
            "perfil": str(perfil_dir),
            "observacoes": (observacoes or "").strip() or "nenhuma",
        }
        prompt = fill_template(cfg.get("prompt_analise") or DEFAULT_PROMPT_ANALISE, values)
        job = _new_job(
            tipo=TIPO_ANALISE,
            client_id=client_id,
            project_id=None,
            cliente_nome=client["name"],
            projeto_nome=None,
            tarefa="analise",
            tarefa_rotulo="Análise de identidade",
            observacoes=observacoes or "",
            cwd=client["path"],
            add_dirs=fora,
            perfil=str(perfil_dir),
            pastas_analise=pastas_ok,
            prompt=prompt,
        )
        identidades.mark_analysis_started(client_id, job["id"], pastas_ok)
        _launch(job, _claude_cmd(job, binary, cfg))
    return load_job(job["id"]) or job


def start_adjust_job(
    project_id: str, pasta: str, chave: str, pedido: str, confirmar_tokens: bool = False
) -> dict[str, Any]:
    """Refaz um elemento (insert, GC, legendas) de uma entrega já feita, na
    mesma pasta, com a bancada que a criou. O pedido do editor é o prompt."""
    binary = find_claude()
    if not binary:
        raise EditorError(availability()["motivo"], 503)
    check_tokens(confirmar_tokens, "O ajuste do elemento")
    pedido = (pedido or "").strip()
    if not pedido:
        raise EditorError("escreva o que mudar nesse elemento")
    project, folder, _, item = _delivery_item(project_id, pasta, chave)
    project_dir = Path(project["path"])
    trabalho = _delivery_origin(project_dir, folder)
    if not trabalho:
        raise EditorError(
            "essa entrega não tem a bancada do estagiário (.estagiario/…), então não dá para ajustar "
            "um elemento só. Gere uma versão nova com o pedido nas observações.",
            409,
        )
    client = clients_module.get_client(project["client_id"])
    cfg = get_config()
    base, versao = DELIVERY_DIR_RE.match(folder.name).groups()
    task_key, task = next(
        ((k, t) for k, t in cfg["tarefas"].items() if t.get("pasta") == base), (None, {"rotulo": base})
    )
    perfil_dir = identidades.profile_dir(project["client_id"])
    nome = " ".join(x for x in (item["numero"], item["titulo"]) if x)
    elemento = f"{nome} (entra em {item['timecode']}, {item['tipo']})"
    with _lock:
        _ensure_single_job()
        values = {
            "cliente": client["name"] if client else project["client_id"],
            "perfil": str(perfil_dir),
            "projeto": str(project_dir),
            "tarefa": task.get("rotulo", base),
            "versao": f"v{versao}",
            "trabalho": str(trabalho),
            "entrega": str(folder),
            "elemento": elemento,
            "arquivos": "\n".join(f"- {a['name']}" for a in item["arquivos"]),
            "pedido": pedido,
        }
        prompt = fill_template(cfg.get("prompt_ajuste") or DEFAULT_PROMPT_AJUSTE, values)
        job = _new_job(
            tipo=TIPO_AJUSTE,
            client_id=project["client_id"],
            project_id=project_id,
            cliente_nome=values["cliente"],
            projeto_nome=project.get("folder_name") or project.get("name"),
            tarefa=task_key or "ajuste",
            tarefa_rotulo=f"Ajuste · {nome}",
            observacoes=pedido,
            pedido=pedido,
            elemento={"chave": item["chave"], "numero": item["numero"], "titulo": item["titulo"],
                      "timecode": item["timecode"], "pasta": folder.name},
            cwd=str(project_dir),
            add_dirs=[str(perfil_dir)] if perfil_dir and perfil_dir.is_dir() else [],
            pasta_base=base,
            versao=int(versao),
            entrega=str(folder),
            trabalho=str(trabalho),
            perfil_status=identidades.get_status(project["client_id"]),
            prompt=prompt,
        )
        _launch(job, _claude_cmd(job, binary, cfg))
    return load_job(job["id"]) or job


def start_inserts_export_job(project_id: str, pasta: str, recorte: bool = True) -> dict[str, Any]:
    """Renderiza e entrega só os inserts aprovados na prévia que ainda não estão (atualizados) na entrega.
    Sem IA: kit/auto.py exportar, na mesma bancada; a entrega é a Motion - Inserts vN dela."""
    from . import previa_inserts

    project, trabalho, itens = previa_inserts.para_exportar(project_id, pasta)
    if not itens:
        raise EditorError("nenhum insert aprovado para exportar (ou os aprovados já estão na entrega)", 409)
    job_info = safe_read_json(trabalho / "job.json", {})
    entrega = Path(job_info.get("entrega") or "")
    comps = [c["comp"] for i in itens for c in i["camadas"]]
    # o recorte vai uma vez só: se a entrega já tem um, não repete
    ja_tem = entrega.is_dir() and any(f.name.startswith("00-00-00 - Recorte") for f in entrega.glob("*.mov"))
    with _lock:
        _ensure_single_job()
        kit = trabalho / "kit"
        for nome in ("auto.py", "render.py", "entregar.py"):  # bancadas antigas: o modo exportar é novo
            shutil.copy2(KIT_DIR / nome, kit / nome)
        args_path = trabalho / "exportar-args.json"
        safe_write_json(args_path, {"comps": comps, "recorte": bool(recorte) and not ja_tem, "pessoa": "Apresentador"})
        nomes = ", ".join(i["nome"] for i in itens)
        job = _new_job(
            tipo=TIPO_EXPORTAR, modo=MODO_AUTO, client_id=project["client_id"], project_id=project_id,
            destino="projeto", projeto_nome=project.get("folder_name") or project.get("name"),
            tarefa="inserts", tarefa_rotulo=f"Exportar inserts · {len(itens)}", observacoes=nomes,
            cwd=str(trabalho), add_dirs=[], pasta_base=DELIVERY_DIR_RE.match(entrega.name).group(1) if DELIVERY_DIR_RE.match(entrega.name) else entrega.name,
            versao=int(previa_inserts.PASTA_RE.match(pasta).group(1)), entrega=str(entrega), trabalho=str(trabalho),
            previa=pasta, prompt=f"(sem IA) exportar {len(itens)} inserts aprovados: {nomes}",
        )
        _launch(job, _auto_cmd(kit, "exportar", args_path), cwd=str(trabalho))
    return load_job(job["id"]) or job


def start_preview_adjust_job(
    project_id: str, pasta: str, chave: str, pedido: str, confirmar_tokens: bool = False
) -> dict[str, Any]:
    """Pede à IA para refazer um insert ainda na prévia: muda o HTML e para, sem render."""
    from . import previa_inserts

    binary = find_claude()
    if not binary:
        raise EditorError(availability()["motivo"], 503)
    check_tokens(confirmar_tokens, "O ajuste do insert")
    pedido = (pedido or "").strip()
    if not pedido:
        raise EditorError("escreva o que mudar nesse insert")
    project, trabalho = previa_inserts._bancada(project_id, pasta)
    item = next((i for i in previa_inserts._itens(trabalho) if i["chave"] == chave), None)
    if not item:
        raise EditorError("insert não encontrado na prévia", 404)
    client = clients_module.get_client(project["client_id"])
    perfil_dir = identidades.profile_dir(project["client_id"])
    cfg = get_config()
    with _lock:
        _ensure_single_job()
        values = {
            "cliente": client["name"] if client else project["client_id"],
            "perfil": str(perfil_dir),
            "projeto": str(project["path"]),
            "trabalho": str(trabalho),
            "elemento": f"{item['nome']} (entra em {item['inicio']:.2f} s do bruto, {item['dur']:.1f} s)",
            "arquivos": ", ".join(f"hf/{c['comp']}.html" for c in item["camadas"]),
            "pedido": pedido,
        }
        prompt = fill_template(cfg.get("prompt_ajuste_previa") or DEFAULT_PROMPT_AJUSTE_PREVIA, values)
        job = _new_job(
            tipo=TIPO_AJUSTE, client_id=project["client_id"], project_id=project_id,
            cliente_nome=values["cliente"], projeto_nome=project.get("folder_name") or project.get("name"),
            tarefa="inserts", tarefa_rotulo=f"Ajuste na prévia · {item['nome']}", observacoes=pedido, pedido=pedido,
            elemento={"chave": chave, "titulo": item["nome"], "previa": pasta},
            cwd=str(project["path"]), add_dirs=[str(perfil_dir)] if perfil_dir and perfil_dir.is_dir() else [],
            trabalho=str(trabalho), previa=pasta, perfil_status=identidades.get_status(project["client_id"]),
            prompt=prompt,
        )
        _launch(job, _claude_cmd(job, binary, cfg))
    return load_job(job["id"]) or job


def start_animate_job(project_id: str, pasta: str, confirmar_tokens: bool = False) -> dict[str, Any]:
    """Tela fixa primeiro: as telas aprovadas na prévia ganham a animação, cada uma no nível do card.
    Muda só o HTML e para; o editor confere na prévia e exporta de lá."""
    from . import previa_inserts

    binary = find_claude()
    if not binary:
        raise EditorError(availability()["motivo"], 503)
    project, trabalho = previa_inserts._bancada(project_id, pasta)
    e = previa_inserts.estado(project_id, pasta)
    itens = [i for i in e["itens"] if i["aprovado"] and not i.get("animada")]
    if not itens:
        raise EditorError("aprove na prévia as telas que devem ser animadas", 409)
    check_tokens(confirmar_tokens, "Animar as telas aprovadas")
    client = clients_module.get_client(project["client_id"])
    perfil_dir = identidades.profile_dir(project["client_id"])
    cfg = get_config()
    pedido_telas = "; ".join(
        f"{i['nome']} ({NIVEIS_ANIMACAO[i['tela']['nivel']] if i.get('tela') else NIVEIS_ANIMACAO[2]}"
        + (f"; pedido: {i['tela']['descricao']}" if i.get("tela") else "") + ")" for i in itens)
    with _lock:
        _ensure_single_job()
        values = {
            "cliente": client["name"] if client else project["client_id"],
            "perfil": str(perfil_dir),
            "projeto": str(project["path"]),
            "trabalho": str(trabalho),
            "elemento": ", ".join(i["nome"] for i in itens),
            "arquivos": ", ".join(f"hf/{c['comp']}.html" for i in itens for c in i["camadas"]),
            "pedido": ("Estas telas foram aprovadas paradas. Anime cada uma sem mudar o layout aprovado: a tela "
                       "aprovada é o estado de repouso, para onde a entrada chega e de onde a saída parte. "
                       "Nível de cada uma: " + pedido_telas + ". " + CRIATIVIDADE + " "
                       + aprendizado.licoes("inserts", project["client_id"])).strip(),
        }
        prompt = fill_template(cfg.get("prompt_ajuste_previa") or DEFAULT_PROMPT_AJUSTE_PREVIA, values)
        job = _new_job(
            tipo=TIPO_AJUSTE, client_id=project["client_id"], project_id=project_id,
            cliente_nome=values["cliente"], projeto_nome=project.get("folder_name") or project.get("name"),
            tarefa="inserts", tarefa_rotulo=f"Animar telas · {len(itens)}", observacoes=values["elemento"],
            pedido=values["pedido"], elemento={"chave": itens[0]["chave"], "titulo": values["elemento"], "previa": pasta},
            cwd=str(project["path"]), add_dirs=[str(perfil_dir)] if perfil_dir and perfil_dir.is_dir() else [],
            trabalho=str(trabalho), previa=pasta, perfil_status=identidades.get_status(project["client_id"]),
            prompt=prompt,
        )
        _launch(job, _claude_cmd(job, binary, cfg))
        telas = safe_read_json(trabalho / TELAS_JSON, {}) if (trabalho / TELAS_JSON).exists() else {}
        telas["animar"] = sorted(set(telas.get("animar") or []) | {i["chave"] for i in itens})
        safe_write_json(trabalho / TELAS_JSON, telas)
    return load_job(job["id"]) or job


def start_templates_job(client_id: str, observacoes: str = "", confirmar_tokens: bool = False) -> dict[str, Any]:
    """Escreve os templates do cliente (marca.json + templates/gc) a partir de
    um perfil que já existe. Barato: só lê o perfil. Quando termina bem, o app
    gera as amostras sozinho."""
    binary = find_claude()
    if not binary:
        raise EditorError(availability()["motivo"], 503)
    check_tokens(confirmar_tokens, "Gerar os templates")
    client = clients_module.get_client(client_id)
    if not client:
        raise EditorError("cliente não encontrado", 404)
    perfil_dir = identidades.profile_dir(client_id)
    if not perfil_dir or not perfil_dir.is_dir() or not (perfil_dir / "marca.md").is_file():
        raise EditorError("o perfil ainda não foi analisado: rode a análise de identidade primeiro", 409)
    cfg = get_config()
    with _lock:
        _ensure_single_job()
        values = {"cliente": client["name"], "perfil": str(perfil_dir),
                  "observacoes": (observacoes or "").strip() or "nenhuma"}
        prompt = fill_template(cfg.get("prompt_templates") or DEFAULT_PROMPT_TEMPLATES, values)
        job = _new_job(
            tipo=TIPO_TEMPLATES, client_id=client_id, project_id=None, cliente_nome=client["name"],
            projeto_nome=None, tarefa="templates", tarefa_rotulo="Templates do cliente",
            observacoes=observacoes or "", cwd=str(perfil_dir), add_dirs=[], perfil=str(perfil_dir), prompt=prompt,
        )
        _launch(job, _claude_cmd(job, binary, cfg))
    return load_job(job["id"]) or job


def start_samples_job(client_id: str) -> dict[str, Any]:
    """Amostras dos templates (legenda e GC sobre fundo neutro) em
    <perfil>/amostras/. Sem IA: kit/auto.py amostras."""
    client = clients_module.get_client(client_id)
    if not client:
        raise EditorError("cliente não encontrado", 404)
    perfil_dir = identidades.profile_dir(client_id)
    if not perfil_dir or not (perfil_dir / "marca.json").is_file():
        raise EditorError("o perfil ainda não tem o template (marca.json): gere os templates primeiro", 409)
    with _lock:
        _ensure_single_job()
        trabalho = perfil_dir / SAMPLES_WORK
        if trabalho.is_dir():  # cada rodada começa limpa: as amostras refletem o template de agora
            shutil.rmtree(trabalho)
        trabalho.mkdir(parents=True)
        kit = _copy_kit(trabalho)
        args_path = trabalho / "auto-args.json"
        safe_write_json(args_path, {"perfil": str(perfil_dir), "saida": str(perfil_dir / SAMPLES_DIR),
                                    "cliente": client["name"]})
        job = _new_job(
            tipo=TIPO_AMOSTRAS, modo=MODO_AUTO, client_id=client_id, project_id=None, cliente_nome=client["name"],
            projeto_nome=None, tarefa="amostras", tarefa_rotulo="Amostras dos templates", observacoes="",
            cwd=str(trabalho), add_dirs=[], perfil=str(perfil_dir), prompt="(modo automático, sem IA) amostras",
        )
        _launch(job, _auto_cmd(kit, "amostras", args_path))
    return load_job(job["id"]) or job


def _ensure_single_job() -> None:
    active = active_job_id()
    if active:
        other = load_job(active) or {}
        what = other.get("projeto_nome") or other.get("cliente_nome") or "outro projeto"
        raise EditorError(
            f"o estagiário já está trabalhando ({other.get('tarefa_rotulo', 'job')} · {what}). "
            "Espere terminar ou cancele.",
            409,
        )


def _new_job(**fields: Any) -> dict[str, Any]:
    job = {
        "id": uuid.uuid4().hex,
        "status": STATUS_PENDENTE,
        "criado_em": _now_iso(),
        "iniciado_em": None,
        "terminado_em": None,
        "pid": None,
        # Máquina que roda o processo. Com data/ compartilhado entre Mac e
        # Windows, o `pid` só significa algo nela (_job_is_local).
        "host": machine.hostname(),
        "comando": None,
        "erro": None,
        "aviso": None,
        "resultado": None,
        "cancelar": False,
        **fields,
    }
    _save_job(job)
    return job


def _launch(job: dict[str, Any], cmd: list[str], cwd: str | None = None, env_extra: dict[str, str] | None = None) -> None:
    """`cmd` é o `claude` (análise, ajuste, templates) ou o kit/auto.py (tarefas
    automáticas e a preparação que antecede a IA, que termina num exec do
    claude). Os dois escrevem stream-json no mesmo .ndjson."""
    job_id = job["id"]
    paths.EDITOR_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_job_prompt(job_id), "w", encoding="utf-8") as f:
        f.write(job["prompt"])
    try:
        with open(_job_prompt(job_id), "rb") as stdin, \
                open(_job_stream(job_id), "ab") as stdout, \
                open(_job_stderr(job_id), "ab") as stderr:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd or job["cwd"],
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                env={**child_env(), **(env_extra or {})},
                start_new_session=True,
            )
    except OSError as exc:
        _update_job(
            job_id, status=STATUS_ERRO, erro=f"não consegui iniciar o job: {exc}",
            terminado_em=_now_iso(), comando=cmd,
        )
        _after_finish(load_job(job_id) or job)
        raise EditorError(f"não consegui iniciar o job: {exc}", 500)
    _procs[job_id] = proc
    _update_job(job_id, status=STATUS_RODANDO, pid=proc.pid, iniciado_em=_now_iso(), comando=cmd)
    _start_monitor(job_id)


def _claude_cmd(job: dict[str, Any], binary: str, cfg: dict[str, Any]) -> list[str]:
    name = f"Clientela · {job.get('tarefa_rotulo')} · {job.get('projeto_nome') or job.get('cliente_nome')}"
    return build_command(binary, job.get("add_dirs") or [], cfg, name)


# --------------------------------------------------------------- acompanhamento
def _short(text: str, n: int = 220) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _describe_tool(block: dict[str, Any]) -> str:
    name = block.get("name") or "ferramenta"
    inp = block.get("input") or {}
    if name == "Bash":
        return f"Bash · {_short(inp.get('description') or inp.get('command') or '', 160)}"
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return f"{name} · {Path(str(inp.get('file_path') or '')).name}"
    if name == "Skill":
        return f"Skill · {inp.get('skill') or inp.get('command') or ''}"
    if name in ("Agent", "Task"):
        return f"Subagente · {_short(inp.get('description') or '', 120)}"
    if name in ("Glob", "Grep"):
        return f"{name} · {_short(inp.get('pattern') or '', 120)}"
    return name


def parse_event(raw: dict[str, Any], live: dict[str, Any]) -> list[dict[str, Any]]:
    """Traduz uma linha do stream-json em zero ou mais eventos legíveis e
    atualiza o resumo do job (`live`). Puro — fácil de testar sem processo."""
    out: list[dict[str, Any]] = []
    t = raw.get("type")
    sub = raw.get("subtype")
    if t == "system" and sub == "init":
        live["sessao_id"] = raw.get("session_id")
        live["modelo"] = raw.get("model")
        live["permission_mode"] = raw.get("permissionMode")
        skills = raw.get("skills") or []
        live["skill_carregada"] = SKILL_NAME in skills
        msg = f"Sessão iniciada · {raw.get('model') or 'modelo padrão'}"
        out.append({"tipo": "sistema", "texto": msg})
        if not live["skill_carregada"]:
            out.append({
                "tipo": "aviso",
                "texto": f"a skill {SKILL_NAME} não está instalada — o estagiário vai trabalhar sem o manual",
            })
    elif t == "system" and sub == "auto" and raw.get("modo") == MODO_SEQUENCIA:
        live["modelo"] = "export da sequência (sem IA)"
        live["skill_carregada"] = True
        out.append({"tipo": "sistema", "texto": "Sequência: as trilhas vão para o ffmpeg"})
    elif t == "system" and sub == "auto" and raw.get("modo") == MODO_CORTE:
        # autocorte.py: script do app; a IA é uma chamada só, para escolher os trechos.
        live["modelo"] = "corte vertical (LLM só nos trechos)"
        live["skill_carregada"] = True
        out.append({"tipo": "sistema", "texto": "Corte vertical: whisper → locutores → LLM escolhe os trechos → ffmpeg"})
    elif t == "system" and sub == "auto":
        # kit/auto.py: o pipeline sem IA. A skill (o kit) está ali por definição.
        live["modelo"] = "automático (sem IA)"
        live["skill_carregada"] = True
        out.append({"tipo": "sistema", "texto": "Modo automático: template do cliente, sem IA e sem custo de tokens"})
    elif t == "system" and sub == "api_retry":
        out.append({"tipo": "aviso", "texto": f"API instável, tentando de novo ({raw.get('attempt')}/{raw.get('max_retries')})"})
    elif t == "system" and sub == "permission_denied":
        live["negacoes"] = live.get("negacoes", 0) + 1
        out.append({"tipo": "negado", "texto": _short(f"permissão negada: {raw.get('tool_name') or ''} {raw.get('reason') or ''}")})
    elif t == "assistant" and not raw.get("parent_tool_use_id"):
        for block in (raw.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                live["fala"] = _short(block["text"], 400)
                out.append({"tipo": "texto", "texto": _short(block["text"], 400)})
            elif block.get("type") == "tool_use":
                live["passos"] = live.get("passos", 0) + 1
                out.append({"tipo": "ferramenta", "texto": _describe_tool(block)})
    elif t == "assistant":
        # Mensagens de subagente: só contam passo, pra não afogar o log.
        for block in (raw.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use":
                live["passos"] = live.get("passos", 0) + 1
    elif t == "user" and not raw.get("parent_tool_use_id"):
        for block in (raw.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                out.append({"tipo": "erro", "texto": _short(content or "a ferramenta falhou", 220)})
    elif t == "result":
        live["resultado"] = {
            "is_error": bool(raw.get("is_error")),
            "texto": raw.get("result"),
            "custo_usd": raw.get("total_cost_usd"),
            "duracao_ms": raw.get("duration_ms"),
            "turnos": raw.get("num_turns"),
            "sessao_id": raw.get("session_id"),
            "negacoes": len(raw.get("permission_denials") or []),
            "motivo": raw.get("terminal_reason"),
        }
        kind = "erro" if raw.get("is_error") else "resultado"
        out.append({"tipo": kind, "texto": _short(raw.get("result") or "terminou", 400)})
    return out


def _live_state(job_id: str) -> dict[str, Any]:
    with _lock:
        return _live.setdefault(job_id, {"offset": 0, "eventos": [], "buffer": b""})


def _read_new(job_id: str) -> bool:
    """Lê o que entrou no .ndjson desde a última vez. True se mudou algo."""
    with _lock:  # o monitor e as requisições leem o mesmo offset
        return _read_new_locked(job_id)


def _read_new_locked(job_id: str) -> bool:
    live = _live_state(job_id)
    stream = _job_stream(job_id)
    if not stream.exists():
        return False
    with open(stream, "rb") as f:
        f.seek(live["offset"])
        chunk = f.read()
    if not chunk:
        return False
    live["offset"] += len(chunk)
    data = live["buffer"] + chunk
    lines = data.split(b"\n")
    live["buffer"] = lines.pop()  # linha incompleta fica pra próxima leitura
    changed = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        for ev in parse_event(raw, live):
            ev["at"] = _now_iso()
            live["eventos"].append(ev)
            changed = True
        if len(live["eventos"]) > MAX_EVENTS:
            del live["eventos"][: len(live["eventos"]) - MAX_EVENTS]
    return changed


def _job_is_local(job: dict[str, Any]) -> bool:
    """O processo do job roda nesta máquina? Job sem ``host`` é de antes do
    Windows, quando só existia o Mac."""
    host = job.get("host")
    if host:
        return host == machine.hostname()
    return machine.current_os() == machine.MAC


def _pid_alive_windows(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if machine.current_os() == machine.WINDOWS:
        # No Windows, os.kill(pid, 0) não testa nada: chama TerminateProcess
        # e mata o processo com código 0.
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_done(job_id: str, pid: int | None) -> tuple[bool, int | None]:
    proc = _procs.get(job_id)
    if proc is not None:
        code = proc.poll()
        return code is not None, code
    return not _pid_alive(pid), None


def _stderr_tail(job_id: str, n: int = 600) -> str:
    p = _job_stderr(job_id)
    if not p.exists():
        return ""
    with open(p, "rb") as f:
        data = f.read()[-n:]
    return data.decode("utf-8", "replace").strip()


_AUTH_HINT = (
    "o Claude Code CLI não está logado. Abra o Terminal, rode `claude`, faça /login "
    "e tente de novo."
)


def _finish(job_id: str, exit_code: int | None) -> None:
    _read_new(job_id)
    live = _live_state(job_id)
    job = load_job(job_id)
    if not job or job.get("status") not in ACTIVE_STATUSES:
        return
    resultado = live.get("resultado")
    fields: dict[str, Any] = {"terminado_em": _now_iso(), "resultado": resultado, "exit_code": exit_code}
    if job.get("cancelar"):
        fields["status"] = STATUS_CANCELADO
    elif resultado and not resultado.get("is_error"):
        fields["status"] = STATUS_CONCLUIDO
    else:
        fields["status"] = STATUS_ERRO
        texto = (resultado or {}).get("texto") or ""
        if "authenticate" in texto.lower() or "/login" in texto:
            fields["erro"] = _AUTH_HINT
        elif texto:
            fields["erro"] = _short(texto, 400)
        else:
            tail = _stderr_tail(job_id)
            fields["erro"] = _short(tail, 400) if tail else (
                "o processo do claude terminou sem resultado"
                + (f" (código {exit_code})" if exit_code is not None else "")
            )
    job = _update_job(job_id, **fields) or job
    _procs.pop(job_id, None)
    _after_finish(job)


def _after_finish(job: dict[str, Any]) -> None:
    try:
        _record_finish(job)
    finally:
        # A vaga do "um job por vez" abriu: o próximo clipe da fila entra.
        _drain_clip_queue()


def _record_finish(job: dict[str, Any]) -> None:
    ok = job.get("status") == STATUS_CONCLUIDO
    if job.get("tipo") in (TIPO_ANALISE, TIPO_TEMPLATES):
        if job.get("tipo") == TIPO_ANALISE:
            identidades.mark_analysis_finished(job["client_id"], ok)
        perfil = identidades.profile_dir(job["client_id"])
        if ok and perfil and (perfil / "marca.json").is_file():
            # a IA escreveu o template: as amostras saem sem IA, para o editor conferir o estilo
            try:
                start_samples_job(job["client_id"])
            except Exception as exc:  # amostra é cortesia; não pode derrubar o fim do job
                logger.warning("amostras depois do job %s: %s", job.get("id"), exc)
        return
    if job.get("tipo") == TIPO_AMOSTRAS:
        return
    if not job.get("project_id"):
        return
    entrega = Path(job.get("entrega") or "")
    arquivos = len([p for p in entrega.glob("*") if p.is_file()]) if entrega.is_dir() else 0
    details = {
        "tarefa": job.get("tarefa_rotulo"),
        "status": job.get("status"),
        "entrega": entrega.name if arquivos else None,
        "arquivos": arquivos,
    }
    if job.get("tipo") == TIPO_AJUSTE:
        details["pedido"] = job.get("pedido")
    try:
        history.append_event(job["project_id"], "editor_job", details)
    except Exception as exc:  # histórico é cortesia; não pode derrubar o job
        logger.warning("history event for editor job %s failed: %s", job.get("id"), exc)
    if ok and job.get("clipe") and arquivos:
        revisao = {"pasta": entrega.name, "estado": REVISAO_PENDENTE, "desde": _now_iso(), "job": job["id"]}
        try:
            store.update(job["project_id"], lambda rec: rec.__setitem__("revisao_corte", revisao))
        except Exception as exc:
            logger.warning("revisão do corte do clipe %s não foi marcada: %s", job.get("project_id"), exc)


# ------------------------------------------------- revisão do corte automático
REVISAO_PENDENTE = "pendente"
REVISAO_ACOES = {"aprovar": "aprovado", "ajustar": "em_ajuste", "descartar": "descartado"}
DESCARTADOS_DIR = "_descartados"


def list_reviews(dias: int = 45) -> list[dict[str, Any]]:
    """Revisões de corte automático de todos os clientes: as pendentes primeiro, depois as decididas nos
    últimos `dias` (a tela de revisão do celular lista isto)."""
    limite = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    out = []
    for client in clients_module.list_clients():
        for p in projects_module.list_projects(client["id"]):
            r = p.get("revisao_corte")
            if not r or (r.get("estado") != REVISAO_PENDENTE and str(r.get("decidido_em") or r.get("desde") or "") < limite):
                continue
            out.append({**r, "project_id": p["id"], "num": p["num"], "projeto": p["name"], "client_id": client["id"],
                        "cliente": client["name"], "folder_available": bool(p.get("folder_available"))})
    out.sort(key=lambda r: (r["estado"] != REVISAO_PENDENTE, ""), reverse=False)
    pend = [r for r in out if r["estado"] == REVISAO_PENDENTE]
    resto = sorted((r for r in out if r["estado"] != REVISAO_PENDENTE), key=lambda r: r.get("decidido_em") or "", reverse=True)
    return sorted(pend, key=lambda r: r.get("desde") or "", reverse=True) + resto


def review_cut(project_id: str, acao: str, nota: str = "") -> dict[str, Any]:
    """A pessoa decide o corte que a IA fez sozinha de um clipe: aprovar (fica como está), ajustar (o
    estúdio abre nele; a revisão sai da frente) ou descartar — a entrega vai para
    <projeto>/.estagiario/_descartados/, escondida e recuperável, nunca apagada."""
    estado = REVISAO_ACOES.get(acao)
    if not estado:
        raise EditorError("ação inválida (aprovar, ajustar ou descartar)")
    project = projects_module.get_project(project_id)
    if not project:
        raise EditorError("projeto não encontrado", 404)
    revisao = project.get("revisao_corte") or {}
    if revisao.get("estado") != REVISAO_PENDENTE:
        raise EditorError("este projeto não tem corte esperando revisão", 409)
    movido = None
    if acao == "descartar" and project.get("folder_available"):
        base = Path(project["path"])
        origem = base / str(revisao.get("pasta") or "")
        if revisao.get("pasta") and DELIVERY_DIR_RE.match(revisao["pasta"]) and origem.is_dir():
            destino_dir = base / WORK_DIR / DESCARTADOS_DIR
            destino_dir.mkdir(parents=True, exist_ok=True)
            alvo = destino_dir / f"{origem.name} {datetime.now().strftime('%Y%m%d-%H%M%S')}"
            shutil.move(str(origem), str(alvo))
            movido = alvo.relative_to(base).as_posix()
    nova = {**revisao, "estado": estado, "decidido_em": _now_iso()}
    if movido:
        nova["descartado_em"] = movido
    nota = (nota or "").strip() if acao == "ajustar" else ""   # só o pedido de ajuste leva texto
    if nota:
        nova["nota"] = nota[:1000]
    store.update(project_id, lambda rec: rec.__setitem__("revisao_corte", nova))
    history.append_event(project_id, "revisao_corte", {"acao": acao, "pasta": revisao.get("pasta"),
                                                       "movido_para": movido, "nota": nota or None})
    if nota:   # o que pedir de ajuste vai para "Ajustes pedidos" do projeto, onde a edição já olha
        feed_module.add_item(project_id, "comments", f"Revisão do {revisao.get('pasta')}: {nota}")
    return nova


# ---------------------------------------------------- corte automático do clipe
# Clipes que chegaram com o estagiário ocupado: (project_id, vídeo relativo).
# Fica na memória — se o app fechar antes, o projeto existe e o corte se pede
# pelo modal do Editor.
_clip_queue: list[tuple[str, str]] = []


def autocut_from_clip(event: dict[str, Any]) -> None:
    """Callback de ``clips.on_clip_ready``: com o corte automático ligado nas
    configurações dos clipes, o clipe novo já sai cortado em 9:16."""
    if not clips_module.get_config().get("autocorte"):
        return
    with _lock:
        _clip_queue.append((event["project_id"], event["video_rel"]))
    _drain_clip_queue()


def _drain_clip_queue() -> None:
    with _lock:
        while _clip_queue and not active_job_id():
            project_id, video = _clip_queue.pop(0)
            try:
                # Ligar "Já fazer o corte vertical" nos clipes é a confirmação, dada uma vez.
                job = start_project_job(project_id, TAREFA_CORTE, video, confirmar_tokens=True)
                # o fim do job pede a revisão (review_cut) só para os cortes que ninguém pediu à mão
                _update_job(job["id"], clipe=True)
            except Exception as exc:  # roda dentro do fim de outro job: nada pode escapar daqui
                logger.warning("corte automático do clipe %s não começou: %s", project_id, exc)
                try:
                    history.append_event(project_id, "editor_job", {
                        "tarefa": DEFAULT_CONFIG["tarefas"][TAREFA_CORTE]["rotulo"],
                        "status": STATUS_ERRO, "erro": str(exc),
                    })
                except Exception:
                    pass


def clip_queue() -> list[str]:
    with _lock:
        return [pid for pid, _ in _clip_queue]


def _monitor(job_id: str) -> None:
    job = load_job(job_id) or {}
    pid = job.get("pid")
    while True:
        try:
            _read_new(job_id)
            done, code = _process_done(job_id, pid)
            if done:
                _finish(job_id, code)
                return
        except Exception as exc:
            logger.warning("editor monitor %s: %s", job_id, exc)
        time.sleep(0.5)


def _start_monitor(job_id: str) -> None:
    threading.Thread(target=_monitor, args=(job_id,), daemon=True, name=f"editor-{job_id[:8]}").start()


def resume_jobs() -> None:
    """Chamado no boot: reata os jobs que estavam rodando quando o app caiu."""
    for job in list_jobs():
        if job.get("status") not in ACTIVE_STATUSES:
            continue
        # Job de outra máquina (data/ compartilhado): o pid é de lá. Testá-lo
        # aqui daria "morto" e fecharia um job que está rodando de verdade.
        if not _job_is_local(job):
            continue
        if _pid_alive(job.get("pid")):
            _start_monitor(job["id"])
        else:
            _finish(job["id"], None)


# ---------------------------------------------------------------- cancelamento
def _signal_group(pid: int, sig: int) -> None:
    if not hasattr(os, "killpg"):
        # Windows: sem grupo de processos POSIX; os.kill com qualquer sinal
        # que não seja CTRL_* vira TerminateProcess.
        try:
            os.kill(pid, sig)
        except OSError:
            pass
        return
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, OSError):
            pass


def cancel_job(job_id: str) -> dict[str, Any] | None:
    """SIGINT primeiro (o CLI fecha o turno direito), SIGTERM depois de
    alguns segundos, SIGKILL por último. Vai pro grupo inteiro pra levar
    junto render e ffmpeg que o estagiário tenha disparado."""
    job = load_job(job_id)
    if not job:
        return None
    if job.get("status") not in ACTIVE_STATUSES:
        return job
    if not _job_is_local(job):
        raise EditorError(f"o job está rodando em outra máquina ({job.get('host')}); cancele por lá", 409)
    job = _update_job(job_id, cancelar=True) or job
    pid = job.get("pid")
    if not pid:
        _finish(job_id, None)
        return load_job(job_id)

    def escalate() -> None:
        # SIGKILL não existe no Windows (lá SIGTERM já é TerminateProcess).
        sigs = (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            pgid = os.getpgid(pid) if hasattr(os, "killpg") else None
        except OSError:
            pgid = None
        for sig in sigs:
            if not _pid_alive(pid) or _process_done(job_id, pid)[0]:
                break
            _signal_group(pid, sig)
            deadline = time.time() + CANCEL_GRACE_SECONDS
            while time.time() < deadline:
                if _process_done(job_id, pid)[0]:
                    break
                time.sleep(0.2)
            else:
                continue
            break
        # O líder sai no SIGINT, mas um filho travado fica no grupo: um ffmpeg em livelock ignora
        # SIGINT/SIGTERM e ficava órfão a 100% de CPU depois de cancelar.
        if pgid and pgid != os.getpgrp():
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

    threading.Thread(target=escalate, daemon=True, name=f"editor-cancel-{job_id[:8]}").start()
    return load_job(job_id)


# --------------------------------------------------------------------- leitura
def job_view(job_id: str, max_events: int = 150) -> dict[str, Any] | None:
    job = load_job(job_id)
    if not job:
        return None
    with _lock:
        # Job que este processo não acompanha (terminou antes de um restart):
        # a primeira leitura parseia o stream inteiro e o resto fica em cache.
        _read_new(job_id)
        live = _live.get(job_id) or {}
        view = dict(job)
        view["eventos"] = list(live.get("eventos") or [])[-max_events:]
        view["fala"] = live.get("fala")
        view["passos"] = live.get("passos", 0)
        view["modelo"] = live.get("modelo")
        view["skill_carregada"] = live.get("skill_carregada")
        view["permission_mode"] = live.get("permission_mode")
        view["negacoes"] = live.get("negacoes", 0)
    view["avaliacao"] = aprendizado.da_job(job_id)
    return view


# --------------------------------------------------------------------- entregas
# "00-11-09 - 3 Debate (camada ATRAS).mov" → timecode MM-SS-FF, número, título,
# qualificadores. Os arquivos aprovados do 049 v3 usam MM-SS-FF (minutos,
# segundos, quadros a 30 fps) — o brief escreveu "HH-SS-FF", os nomes não.
_DELIVERY_FILE_RE = re.compile(
    r"^(?P<tc>\d{2}-\d{2}-\d{2})\s*-\s*(?P<nome>.+?)(?:\s*\((?P<qual>[^()]*)\))?$"
)
_NUM_RE = re.compile(r"^(?P<num>\d+[a-z]?)\s+(?P<titulo>.+)$", re.IGNORECASE)
DELIVERY_EXTS = {".mov", ".mp4", ".webm", ".wav", ".ass", ".srt", ".jpg"}  # legendas e capa soltas do corte vertical


def parse_delivery_name(filename: str) -> dict[str, Any]:
    stem = Path(filename).stem
    m = _DELIVERY_FILE_RE.match(stem)
    if not m:
        return {"tipo": "outro", "titulo": stem}
    tc = m.group("tc")
    nome = m.group("nome").strip()
    qual = [q.strip().lower() for q in (m.group("qual") or "").split(",") if q.strip()]
    if nome.lower().startswith("recorte"):
        return {"tipo": "recorte", "timecode": tc, "titulo": nome, "qualificadores": qual}
    camada = None
    if "camada atras" in qual or "camada atrás" in qual:
        camada = "ATRÁS"
    elif "camada frente" in qual:
        camada = "FRENTE"
    nm = _NUM_RE.match(nome)
    return {
        "tipo": "insert",
        "timecode": tc,
        "numero": nm.group("num") if nm else None,
        "titulo": nm.group("titulo") if nm else nome,
        "camada": camada,
        "tela_cheia": "tela cheia" in qual,
        "com_transicao": "com transicao" in qual or "com transição" in qual,
    }


def _tc_display(tc: str) -> str:
    return tc.replace("-", ":") if tc else ""


def _tc_seconds(tc: str) -> float:
    """MM-SS-FF a 30 fps → segundos no clipe."""
    mm, ss, ff = (int(x) for x in tc.split("-"))
    return round(mm * 60 + ss + ff / 30, 3)


def group_delivery(folder: Path) -> dict[str, Any]:
    inserts: dict[tuple, dict[str, Any]] = {}
    recortes: list[dict[str, Any]] = []
    outros: list[dict[str, Any]] = []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.suffix.lower() not in DELIVERY_EXTS:
            continue
        info = parse_delivery_name(p.name)
        entry = {"name": p.name, "size": p.stat().st_size}
        if info["tipo"] == "recorte":
            recortes.append({**entry, "titulo": info["titulo"]})
        elif info["tipo"] == "insert":
            key = (info["timecode"], info["numero"], info["titulo"])
            item = inserts.setdefault(key, {
                "chave": "|".join(x or "" for x in key),
                "inicio": _tc_seconds(info["timecode"]),
                "timecode": _tc_display(info["timecode"]),
                "numero": info["numero"],
                "titulo": info["titulo"],
                "camadas": [],
                "tela_cheia": False,
                "com_transicao": False,
                "arquivos": [],
            })
            item["arquivos"].append({**entry, "camada": info["camada"]})
            if info["camada"]:
                item["camadas"].append(info["camada"])
            item["tela_cheia"] = item["tela_cheia"] or info["tela_cheia"]
            item["com_transicao"] = item["com_transicao"] or info["com_transicao"]
        else:
            outros.append(entry)
    items = []
    for item in sorted(inserts.values(), key=lambda i: i["timecode"]):
        ordem = {"ATRÁS": 0, "FRENTE": 1}
        item["arquivos"].sort(key=lambda a: ordem.get(a["camada"], 2))
        camadas = sorted(set(item["camadas"]), key=lambda c: ordem.get(c, 2))
        item["camadas"] = camadas
        if camadas == ["ATRÁS", "FRENTE"]:
            tipo = "ATRÁS + FRENTE"
        elif camadas:
            tipo = "camada " + camadas[0]
        elif item["tela_cheia"]:
            tipo = "tela cheia"
        else:
            tipo = "camada única"
        if item["com_transicao"]:
            tipo += ", com transição"
        item["tipo"] = tipo
        items.append(item)
    return {"inserts": items, "recortes": recortes, "outros": outros}


def list_deliveries(project_id: str) -> list[dict[str, Any]] | None:
    project = projects_module.get_project(project_id)
    if not project:
        return None
    if not project.get("folder_available"):
        return []
    project_dir = Path(project["path"])
    versions = []
    for child in project_dir.iterdir():
        m = DELIVERY_DIR_RE.match(child.name)
        if not child.is_dir() or not m:
            continue
        grouped = group_delivery(child)
        total = sum(len(i["arquivos"]) for i in grouped["inserts"]) + len(grouped["recortes"]) + len(grouped["outros"])
        versions.append({
            "pasta": child.name,
            "base": m.group(1),
            "versao": int(m.group(2)),
            "total_arquivos": total,
            # botões por elemento: Prévia precisa do vídeo bruto; Ajustar, da bancada
            "previa_disponivel": _delivery_video(project_dir, child) is not None,
            "ajustavel": _delivery_origin(project_dir, child) is not None,
            "modificado": datetime.fromtimestamp(child.stat().st_mtime, timezone.utc).isoformat(),
            **grouped,
        })
    versions.sort(key=lambda v: (v["modificado"]), reverse=True)
    return versions


def delivery_path(project_id: str, rel: str) -> Path | None:
    """Caminho (pasta ou arquivo) dentro de uma entrega `Motion - … vN`,
    validado contra fuga da pasta do projeto."""
    project = projects_module.get_project(project_id)
    if not project or not project.get("folder_available") or not rel:
        return None
    base = Path(project["path"]).resolve()
    target = (base / rel).resolve()
    if base not in target.parents or not target.exists():
        return None
    top = target.relative_to(base).parts[0]
    if not DELIVERY_DIR_RE.match(top):
        return None
    return target


def _delivery_origin(project_dir: Path, folder: Path) -> Path | None:
    """A bancada que criou a entrega, pelo marcador que o kit/entregar.py
    grava. Só vale se ela ainda existe e mora em <projeto>/.estagiario/."""
    marker = folder / ORIGIN_MARKER
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    trabalho = Path(raw)
    work_root = (project_dir / WORK_DIR).resolve()
    try:
        resolved = trabalho.resolve()
    except OSError:
        return None
    if work_root not in resolved.parents or not (resolved / "job.json").is_file():
        return None
    return resolved


def _delivery_video(project_dir: Path, folder: Path) -> Path | None:
    """O vídeo bruto da entrega: o job.json da bancada, senão o job do app
    que entregou nessa pasta."""
    trabalho = _delivery_origin(project_dir, folder)
    if trabalho:
        info = safe_read_json(trabalho / "job.json", {}) or {}
        caminho = ((info.get("video") or {}).get("caminho")) if isinstance(info, dict) else None
        if caminho and Path(caminho).is_file():
            return Path(caminho)
    for job in list_jobs():
        if job.get("entrega") == str(folder) and job.get("video"):
            p = project_dir / job["video"]
            if p.is_file():
                return p
    return None


def _delivery_item(project_id: str, pasta: str, chave: str) -> tuple[dict, Path, dict, dict]:
    project = projects_module.get_project(project_id)
    if not project:
        raise EditorError("projeto não encontrado", 404)
    if not project.get("folder_available"):
        raise EditorError("pasta do projeto não encontrada no disco", 409)
    project_dir = Path(project["path"])
    if not pasta or not DELIVERY_DIR_RE.match(pasta) or "/" in pasta:
        raise EditorError("entrega inválida")
    folder = project_dir / pasta
    if not folder.is_dir():
        raise EditorError("entrega não encontrada", 404)
    grouped = group_delivery(folder)
    item = next((i for i in grouped["inserts"] if i["chave"] == chave), None)
    if not item:
        raise EditorError("esse elemento não está mais na entrega (atualize o painel)", 404)
    return project, folder, grouped, item


def _media_info(binary: str | None, path: Path) -> dict[str, Any]:
    if not binary:
        return {}
    try:
        out = subprocess.run(
            [binary, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout
        d = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    stream = (d.get("streams") or [{}])[0]
    try:
        dur = float((d.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        dur = None
    return {"largura": stream.get("width"), "altura": stream.get("height"), "duracao": dur}


# ------------------------------------------------- quadro para o enquadramento
FRAMES_DIR = "quadros"   # data/editor/quadros/: JPEGs do editor de enquadramento (cache)
FRAMES_KEEP = 240     # o estúdio pede uma tira de miniaturas por vídeo
FRAME_WIDTH = 960
FRAME_WIDTHS = (160, 320, 960)


def _media_for_frame(project_id: str | None, video: str) -> Path:
    base = None
    if project_id:
        project = projects_module.get_project(project_id)
        if not project:
            raise EditorError("projeto não encontrado", 404)
        if project.get("folder_available"):
            base = Path(project["path"])
    path = _resolve_media(base, video, VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS)
    if not path:
        raise EditorError("vídeo não encontrado", 404)
    return path


def media_info(project_id: str | None, video: str) -> dict[str, Any]:
    """Largura, altura e duração do vídeo: o editor de enquadramento desenha
    as caixas na proporção do quadro e a barra de tempo pela duração. O
    estúdio usa também as faixas de áudio (uma trilha por faixa) e o codec
    (se o navegador não toca, ele pede um proxy)."""
    path = _media_for_frame(project_id, video)
    ffprobe = shutil.which("ffprobe", path=_search_path())
    info = _media_info(ffprobe, path)
    if not info.get("largura"):
        raise EditorError("não consegui ler o vídeo (ffprobe)", 500)
    info.update(_streams_info(ffprobe, path))
    info["proxy"] = proxy_status(path)
    return info


# Codecs que Chrome e Safari tocam no <video>; o resto (ProRes no Chrome, MXF,
# AVI, PCM…) vai por um proxy H.264. O navegador ainda tem a palavra final: se
# ele falhar com um destes, o estúdio pede o proxy do mesmo jeito.
PLAYABLE_VIDEO = {"h264", "hevc", "vp8", "vp9", "av1"}
PLAYABLE_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
PLAYABLE_EXTS = {".mp4", ".m4v", ".mov", ".mkv"}


def _streams_info(ffprobe: str | None, path: Path) -> dict[str, Any]:
    if not ffprobe:
        return {"audios": [], "reproduzivel": False}
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=index,codec_type,codec_name,channels,r_frame_rate:stream_tags=title,handler_name", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout
        streams = json.loads(out or "{}").get("streams") or []
    except (OSError, subprocess.SubprocessError, ValueError):
        streams = []
    video = next((x for x in streams if x.get("codec_type") == "video"), {})
    audios = []
    for x in (x for x in streams if x.get("codec_type") == "audio"):
        tags = x.get("tags") or {}
        titulo = (tags.get("title") or tags.get("handler_name") or "").strip()
        if titulo.lower() in ("soundhandler", "sound handler", "core media audio"):
            titulo = ""
        audios.append({"faixa": len(audios) + 1, "titulo": titulo, "codec": x.get("codec_name"), "canais": x.get("channels")})
    fps = None
    try:
        num, den = (float(v) for v in str(video.get("r_frame_rate") or "0/1").split("/"))
        fps = round(num / den, 3) if den else None
    except ValueError:
        pass
    reproduzivel = (
        path.suffix.lower() in PLAYABLE_EXTS
        and video.get("codec_name") in PLAYABLE_VIDEO
        and (not audios or audios[0].get("codec") in PLAYABLE_AUDIO)
    )
    return {"audios": audios, "codec": video.get("codec_name"), "fps": fps, "reproduzivel": reproduzivel}


def frame_path(project_id: str | None, video: str, t: float, largura: int = FRAME_WIDTH) -> Path:
    """Um quadro do vídeo em JPEG (960 px de largura, ou miniatura de 160/320
    para a tira da timeline), guardado em data/editor/quadros/ — fora da
    pasta do projeto, que está no Drive."""
    largura = largura if largura in FRAME_WIDTHS else FRAME_WIDTH
    path = _media_for_frame(project_id, video)
    ffmpeg = shutil.which("ffmpeg", path=_search_path())
    if not ffmpeg:
        raise EditorError("ffmpeg não encontrado (brew install ffmpeg)", 503)
    t = max(0.0, float(t or 0))
    st = path.stat()
    extra = "" if largura == FRAME_WIDTH else f"|{largura}"
    key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}|{t:.2f}{extra}".encode()).hexdigest()[:20]
    folder = paths.EDITOR_DIR / FRAMES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{key}.jpg"
    if out.is_file():
        return out
    # temporário próprio de cada pedido: duas miniaturas iguais pedidas juntas (a tira do estúdio e a da
    # sequência) dividiam o mesmo .tmp, e a que chegava depois achava o arquivo já movido → 500
    tmp = folder / f".{key}.{uuid.uuid4().hex[:8]}.jpg"
    # imagem parada não leva -ss: num JPEG único o "-ss 0" descarta o único quadro e o ffmpeg sai com 0
    # sem escrever nada (PNG passava) — as miniaturas de JPEG do painel IDV davam 500
    busca = [] if path.suffix.lower() in IMAGE_EXTS else ["-ss", f"{t:.2f}"]
    r = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-y", *busca, "-i", str(path), "-frames:v", "1",
         "-vf", f"scale={largura}:-2", "-q:v", "4" if largura == FRAME_WIDTH else "6", str(tmp)],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise EditorError(f"o ffmpeg não tirou o quadro: {_short(r.stderr, 300)}", 500)
    tmp.replace(out)
    # só os quadros prontos (os .ocultos são temporários de pedidos em andamento); um que outro pedido
    # apagou no meio do caminho não é erro
    old = []
    for p in folder.glob("[!.]*.jpg"):
        try:
            old.append((p.stat().st_mtime, p))
        except FileNotFoundError:
            pass
    for _, p in sorted(old)[:-FRAMES_KEEP]:
        p.unlink(missing_ok=True)
    return out


def preview_layers(grouped: dict[str, Any], item: dict[str, Any], folder: Path) -> list[tuple[Path, bool]]:
    """Camadas na ordem da edição: ATRÁS → recorte → FRENTE → peças únicas.
    O bool diz se a camada é do vídeo inteiro (recorte: precisa do mesmo
    corte de tempo que o bruto)."""
    by = {"ATRÁS": [], "FRENTE": [], None: []}
    for a in item["arquivos"]:
        by.get(a["camada"], by[None]).append((folder / a["name"], False))
    recorte = []
    if by["ATRÁS"] and by["FRENTE"] and grouped.get("recortes"):
        recorte = [(folder / grouped["recortes"][0]["name"], True)]
    return by["ATRÁS"] + recorte + by["FRENTE"] + by[None]


def preview_command(
    ffmpeg: str, video: Path, layers: list[tuple[Path, bool]], inicio: float, dur: float,
    size: tuple[int, int], out: Path,
) -> list[str]:
    w, h = size
    cmd = [ffmpeg, "-v", "error", "-y", "-ss", f"{inicio:.3f}", "-t", f"{dur:.3f}", "-i", str(video)]
    fc = [f"[0:v]scale={w}:{h},setsar=1,fps=30[b0]"]
    last = "b0"
    for i, (path, inteiro) in enumerate(layers, start=1):
        if inteiro:
            cmd += ["-ss", f"{inicio:.3f}"]
        cmd += ["-t", f"{dur:.3f}", "-i", str(path)]
        fc.append(f"[{i}:v]scale={w}:{h},format=yuva420p[o{i}]")
        fc.append(f"[{last}][o{i}]overlay=eof_action=pass:format=auto[b{i}]")
        last = f"b{i}"
    fc.append(f"[{last}]format=yuv420p[out]")
    return cmd + [
        "-filter_complex", ";".join(fc), "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-t", f"{dur:.3f}", str(out),
    ]


def build_preview(project_id: str, pasta: str, chave: str) -> dict[str, Any]:
    """MP4 leve do elemento sobre o trecho do vídeo bruto, com áudio. Fica
    em cache pela combinação de arquivos e mtimes: ajustou o elemento, a
    chave muda e a prévia sai de novo."""
    project, folder, grouped, item = _delivery_item(project_id, pasta, chave)
    project_dir = Path(project["path"])
    video = _delivery_video(project_dir, folder)
    if not video:
        raise EditorError("não achei o vídeo bruto desta entrega (a bancada do estagiário foi apagada?)", 409)
    ffmpeg = shutil.which("ffmpeg", path=_search_path())
    ffprobe = shutil.which("ffprobe", path=_search_path())
    if not ffmpeg or not ffprobe:
        raise EditorError("ffmpeg não encontrado (brew install ffmpeg)", 503)
    layers = preview_layers(grouped, item, folder)
    stats = []
    for path, _ in [(video, True)] + layers:
        st = path.stat()
        stats.append([path.name, st.st_size, st.st_mtime])
    key = hashlib.sha1(json.dumps([str(video), item["inicio"], stats, PREVIEW_MAX_SIDE]).encode()).hexdigest()[:16]
    out_dir = project_dir / WORK_DIR / PREVIEW_DIR
    out = out_dir / f"{key}.mp4"
    durations = [_media_info(ffprobe, p).get("duracao") for p, inteiro in layers if not inteiro]
    dur = max([d for d in durations if d] or [5.0])
    if not out.exists():
        info = _media_info(ffprobe, video)
        vw, vh = info.get("largura") or 1080, info.get("altura") or 1920
        scale = min(1.0, PREVIEW_MAX_SIDE / max(vw, vh))
        size = (int(vw * scale) // 2 * 2, int(vh * scale) // 2 * 2)
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / f"{key}.tmp.mp4"
        cmd = preview_command(ffmpeg, video, layers, item["inicio"], dur, size, tmp)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            raise EditorError("a prévia demorou demais para sair", 500)
        if r.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise EditorError(f"o ffmpeg falhou na prévia: {_short(r.stderr, 300)}", 500)
        os.replace(tmp, out)
        _prune_previews(out_dir)
    return {"arquivo": out.name, "duracao": round(dur, 2), "inicio": item["inicio"],
            "camadas": [p.name for p, _ in layers]}


def _prune_previews(out_dir: Path) -> None:
    files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[PREVIEW_KEEP:]:
        old.unlink(missing_ok=True)


def preview_path(project_id: str, arquivo: str) -> Path | None:
    project = projects_module.get_project(project_id)
    if not project or not project.get("folder_available"):
        return None
    if not re.fullmatch(r"[0-9a-f]{16}\.mp4", arquivo or ""):
        return None
    p = Path(project["path"]) / WORK_DIR / PREVIEW_DIR / arquivo
    return p if p.is_file() else None


# --------------------------------------------------------------------- entradas
def list_candidates(project_id: str) -> dict[str, Any] | None:
    """Vídeos e transcrições da pasta do projeto, pra os seletores do painel.
    Pula pastas ocultas (inclui .estagiario/), auto-save do Premiere, Links/
    e as próprias entregas — um MOV entregue não é vídeo bruto."""
    project = projects_module.get_project(project_id)
    if not project:
        return None
    if not project.get("folder_available"):
        return {"videos": [], "transcricoes": []}
    base = Path(project["path"])
    videos, transcripts = [], []
    for root, dirs, files in os.walk(base):
        rel_root = Path(root).relative_to(base)
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".")
            and "auto-save" not in d.lower()
            and "autosave" not in d.lower()
            and not d.startswith("Adobe Premiere Pro")
            # Links/links.txt é a lista de links do projeto, não transcrição
            and not (rel_root == Path(".") and (DELIVERY_DIR_RE.match(d) or d == "Links"))
        )
        for name in files:
            if name.startswith("."):
                continue
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_EXTS and ext not in TRANSCRIPT_EXTS:
                continue
            p = Path(root) / name
            try:
                st = p.stat()
            except OSError:
                continue
            item = {"path": (rel_root / name).as_posix(), "name": name, "size": st.st_size, "mtime": st.st_mtime}
            (videos if ext in VIDEO_EXTS else transcripts).append(item)
        if len(videos) + len(transcripts) > 500:
            break
    # Brutos/ primeiro (é onde mora o vídeo bruto por convenção), depois o
    # mais recente.
    def key(i: dict[str, Any]) -> tuple:
        return (0 if i["path"].lower().startswith("brutos/") else 1, -i["mtime"])

    videos.sort(key=key)
    transcripts.sort(key=lambda i: -i["mtime"])
    return {"videos": videos, "transcricoes": transcripts}


# ------------------------------------------------- estúdio: vídeo, proxy, onda, cortes
# A janela de Edição IA virou um editor com timeline: o navegador toca o vídeo
# bruto (ou um proxy leve dele), desenha a forma de onda de cada faixa e reabre
# um corte já entregue para o ajuste fino. Tudo que é cache fica em
# data/editor/, fora da pasta do projeto (que está no Drive).
PROXIES_DIR = "proxies"
PROXIES_KEEP = 4
PROXIES_ALFA_KEEP = 40
WAVES_DIR = "ondas"
WAVES_KEEP = 80
WAVE_RATE = 8000
WAVE_MAX_POINTS = 24000
ENVELOPE_PASSO = 0.02     # janela do envelope dos cortes inteligentes (s)
TRANSCRICOES_DIR = "transcricoes"
VIDEO_MIMES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4", ".mkv": "video/webm",
               ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
               ".flac": "audio/flac", ".ogg": "audio/ogg", ".aif": "audio/aiff", ".aiff": "audio/aiff",
               ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".webm": "video/webm"}

_proxies: dict[str, dict[str, Any]] = {}
_proxies_lock = threading.Lock()


def _media_key(path: Path, extra: str = "") -> str:
    st = path.stat()
    return hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}{extra}".encode()).hexdigest()[:20]


_ALFA = re.compile(r"^(yuva|rgba|argb|bgra|abgr|gbrap|ya)")
_alfa_cache: dict[str, bool] = {}


def has_alpha(path: Path) -> bool:
    """O vídeo tem canal alfa? (os inserts de motion são ProRes 4444 yuva444p12le)"""
    key = _media_key(path)
    if key not in _alfa_cache:
        ffprobe = shutil.which("ffprobe", path=_search_path())
        fmt = ""
        if ffprobe:
            try:
                fmt = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=pix_fmt",
                                      "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30,
                                     stdin=subprocess.DEVNULL).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                fmt = ""
        _alfa_cache[key] = bool(_ALFA.match(fmt))
    return _alfa_cache[key]


def _proxy_out(path: Path) -> Path:
    """Proxy H.264 .mp4; com alfa, VP9 .webm com alfa — o Chrome toca e o monitor compõe a transparência
    (um H.264 viraria uma caixa preta por cima do vídeo)."""
    return paths.EDITOR_DIR / PROXIES_DIR / f"{_media_key(path)}{'.webm' if has_alpha(path) else '.mp4'}"


def video_file(project_id: str | None, video: str, proxy: bool = False) -> tuple[Path, str]:
    """O arquivo que o <video> do estúdio toca: o bruto, ou o proxy pronto."""
    path = _media_for_frame(project_id, video)
    if proxy:
        out = _proxy_out(path)
        if not out.is_file():
            raise EditorError("o proxy deste vídeo ainda não está pronto", 404)
        return out, VIDEO_MIMES[out.suffix]
    return path, VIDEO_MIMES.get(path.suffix.lower(), "video/mp4")


def proxy_status(path: Path) -> dict[str, Any]:
    out = _proxy_out(path)
    if out.is_file():
        return {"estado": "pronto", "progresso": 1.0}
    with _proxies_lock:
        st = _proxies.get(str(out))
    return dict(st) if st else {"estado": "nenhum", "progresso": 0.0}


def request_proxy(project_id: str | None, video: str) -> dict[str, Any]:
    """H.264 540p com o áudio da faixa 1, feito em segundo plano. Serve para
    o que o navegador não toca (ProRes no Chrome, MXF, AVI) e para lives
    longas pesadas demais para buscar no meio. Um por vídeo; os mais velhos
    saem do cache."""
    path = _media_for_frame(project_id, video)
    ffmpeg = shutil.which("ffmpeg", path=_search_path())
    if not ffmpeg:
        raise EditorError("ffmpeg não encontrado (brew install ffmpeg)", 503)
    folder = paths.EDITOR_DIR / PROXIES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    out = _proxy_out(path)
    if out.is_file():
        return {"estado": "pronto", "progresso": 1.0}
    with _proxies_lock:
        st = _proxies.get(str(out))
        if st and st["estado"] == "gerando":
            return dict(st)
        _proxies[str(out)] = {"estado": "gerando", "progresso": 0.0}
    dur = (_media_info(shutil.which("ffprobe", path=_search_path()), path) or {}).get("duracao") or 0

    def run() -> None:
        tmp = folder / f".{out.name}"
        if out.suffix == ".webm":
            cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
                   "-vf", "scale=-2:540,format=yuva420p", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0",
                   "-crf", "36", "-deadline", "realtime", "-cpu-used", "8", "-auto-alt-ref", "0", "-g", "30",
                   "-c:a", "libopus", "-b:a", "96k", "-f", "webm", "-progress", "pipe:1", str(tmp)]
        else:
            cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
                   "-vf", "scale=-2:540", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-g", "30",
                   "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", "-f", "mp4",
                   "-progress", "pipe:1", str(tmp)]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    stdin=subprocess.DEVNULL)
            for line in proc.stdout:
                if line.startswith("out_time_ms=") and dur:
                    try:
                        prog = min(0.99, int(line.split("=", 1)[1]) / 1e6 / dur)
                    except ValueError:
                        continue
                    with _proxies_lock:
                        _proxies[str(out)]["progresso"] = round(prog, 3)
            err = proc.stderr.read()
            if proc.wait() != 0 or not tmp.is_file():
                raise RuntimeError(_short(err, 300) or "ffmpeg falhou")
            tmp.replace(out)
            with _proxies_lock:
                _proxies.pop(str(out), None)
            # os .webm são inserts de poucos segundos (uma sequência usa vários); os .mp4, lives inteiras
            for ext, manter in (("mp4", PROXIES_KEEP), ("webm", PROXIES_ALFA_KEEP)):
                old = sorted(folder.glob(f"[!.]*.{ext}"), key=lambda p: p.stat().st_mtime)
                for p in old[:-manter]:
                    p.unlink(missing_ok=True)
        except Exception as exc:  # o estúdio mostra o erro e tenta o original de novo
            tmp.unlink(missing_ok=True)
            logger.warning("proxy falhou para %s: %s", path, exc)
            with _proxies_lock:
                _proxies[str(out)] = {"estado": "erro", "progresso": 0.0, "erro": str(exc)}

    threading.Thread(target=run, daemon=True, name="editor-proxy").start()
    return {"estado": "gerando", "progresso": 0.0}


def wave_peaks(samples: Any, per_point: int) -> list[int]:
    """Amostras s16 → um pico (0–100) por ponto. min/max de fatias rodam em C:
    uma live de 2 h a 8 kHz sai em ~1 s."""
    out = []
    for i in range(0, len(samples), per_point):
        chunk = samples[i:i + per_point]
        if not chunk:
            break
        out.append(min(100, round(max(max(chunk), -min(chunk)) * 100 / 32768)))
    return out


def _decode_s16(path: Path, faixa: int) -> Any:
    """Uma faixa de áudio do arquivo em s16 mono a WAVE_RATE (array "h")."""
    from array import array

    ffmpeg = shutil.which("ffmpeg", path=_search_path())
    if not ffmpeg:
        raise EditorError("ffmpeg não encontrado (brew install ffmpeg)", 503)
    r = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-map", f"0:a:{faixa - 1}", "-vn", "-ac", "1",
         "-ar", str(WAVE_RATE), "-f", "s16le", "-"],
        capture_output=True, timeout=900, stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        raise EditorError(f"não consegui ler a faixa {faixa}: {_short(r.stderr.decode('utf-8', 'replace'), 200)}", 404)
    samples = array("h")
    samples.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _cached_audio(path: Path, faixa: int, kind: str, build: Any) -> dict[str, Any]:
    folder = paths.EDITOR_DIR / WAVES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / f"{_media_key(path, f'|a{faixa}{kind}')}.json"
    cached = safe_read_json(cache, None)
    if cached:
        return cached
    result = build(_decode_s16(path, faixa))
    safe_write_json(cache, result)
    old = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in old[:-WAVES_KEEP]:
        p.unlink(missing_ok=True)
    return result


def waveform(project_id: str | None, video: str, faixa: int = 1) -> dict[str, Any]:
    """Forma de onda de uma faixa de áudio para a trilha da timeline."""
    path = _media_for_frame(project_id, video)
    faixa = max(1, int(faixa or 1))

    def build(samples: Any) -> dict[str, Any]:
        dur = len(samples) / WAVE_RATE
        por_segundo = max(1, min(50, int(WAVE_MAX_POINTS / max(dur, 1))))
        return {"faixa": faixa, "por_segundo": por_segundo, "duracao": round(dur, 3),
                "picos": wave_peaks(samples, max(1, WAVE_RATE // por_segundo))}

    return _cached_audio(path, faixa, "", build)


def envelope_db(samples: Any, per_point: int) -> list[float]:
    """Amostras s16 → pico em dBFS por janela (piso -90). É o que os cortes inteligentes leem: os picos
    0–100 da onda são grossos demais embaixo (1 = -40 dB, 2 = -34 dB)."""
    out = []
    for i in range(0, len(samples), per_point):
        chunk = samples[i:i + per_point]
        if not chunk:
            break
        pico = max(max(chunk), -min(chunk))
        out.append(round(20 * math.log10(pico / 32768), 1) if pico > 1 else -90.0)
    return out


def audio_envelope(project_id: str | None, video: str, faixa: int = 1) -> dict[str, Any]:
    """Envelope em dBFS a cada ENVELOPE_PASSO s de uma faixa (cacheado com as ondas)."""
    path = _media_for_frame(project_id, video)
    faixa = max(1, int(faixa or 1))

    def build(samples: Any) -> dict[str, Any]:
        return {"faixa": faixa, "passo": ENVELOPE_PASSO, "duracao": round(len(samples) / WAVE_RATE, 3),
                "db": envelope_db(samples, int(WAVE_RATE * ENVELOPE_PASSO))}

    return _cached_audio(path, faixa, "|env", build)


# ----------------------------------------------------------- transcrição avulsa (estúdio)
_transcricoes: dict[str, dict[str, Any]] = {}
_transcricoes_lock = threading.Lock()


def _transcription_file(path: Path, modelo: str) -> Path:
    return paths.EDITOR_DIR / TRANSCRICOES_DIR / f"{_media_key(path, '|' + modelo)}.json"


def transcription_status(project_id: str | None, video: str) -> dict[str, Any]:
    """Estado da transcrição do bruto; pronta, vem com as palavras."""
    path = _media_for_frame(project_id, video)
    out = _transcription_file(path, get_config()["corte"].get("asr_modelo") or autocorte.DEFAULTS["asr_modelo"])
    if out.is_file():
        palavras = safe_read_json(out, None)
        if isinstance(palavras, list):
            return {"estado": "pronta", "palavras": palavras}
    with _transcricoes_lock:
        st = _transcricoes.get(str(out))
    return dict(st) if st else {"estado": "nenhuma"}


def request_transcription(project_id: str | None, video: str) -> dict[str, Any]:
    """Transcreve o bruto com o whisper local (o mesmo do corte vertical), em segundo plano: roda o
    autocorte.py como script, com o PATH do app, e guarda as palavras em data/editor/transcricoes/.
    Sem LLM, sem token. Uma de cada vez — o whisper large ocupa a máquina toda."""
    path = _media_for_frame(project_id, video)
    corte = get_config()["corte"]
    modelo = corte.get("asr_modelo") or autocorte.DEFAULTS["asr_modelo"]
    out = _transcription_file(path, modelo)
    if out.is_file():
        return transcription_status(project_id, video)
    with _transcricoes_lock:
        if any(st.get("estado") == "transcrevendo" for st in _transcricoes.values()):
            raise EditorError("já tem uma transcrição rodando; espere ela terminar", 409)
        _transcricoes[str(out)] = {"estado": "transcrevendo"}
    work = out.with_suffix("")
    work.mkdir(parents=True, exist_ok=True)

    def run() -> None:
        try:
            r = subprocess.run(
                [sys.executable or "python3", str(AUTOCORTE_SCRIPT), "--transcrever", str(path), str(out), modelo,
                 corte.get("idioma") or ""],
                cwd=str(work), env=child_env(), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                timeout=6 * 3600,
            )
            if r.returncode != 0 or not out.is_file():
                raise RuntimeError(_short(r.stderr or r.stdout or "o whisper não devolveu nada", 300))
            with _transcricoes_lock:
                _transcricoes.pop(str(out), None)
        except Exception as exc:  # o estúdio mostra o erro; tentar de novo é outro clique
            logger.warning("transcrição falhou para %s: %s", path, exc)
            with _transcricoes_lock:
                _transcricoes[str(out)] = {"estado": "erro", "erro": str(exc)}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    threading.Thread(target=run, daemon=True, name="editor-transcricao").start()
    return {"estado": "transcrevendo"}


def smart_cuts(project_id: str | None, video: str, *, faixa: int = 1, palavras: list[dict[str, Any]] | None = None,
               segmentos: list[dict[str, Any]] | None = None, params: dict[str, Any] | None = None,
               ignorar: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Cortes inteligentes do estúdio (autocorte.cortes_inteligentes) sobre o envelope da faixa. As palavras
    são as que o estúdio tem (revisadas); sem elas, só o silêncio entra."""
    env = audio_envelope(project_id, video, faixa)
    try:
        palavras = [{"texto": str(w.get("texto") or ""), "t": float(w["t"]), "fim": float(w["fim"]),
                     "duvidosa": bool(w.get("duvidosa"))} for w in palavras or []]
        segmentos = [{"inicio": float(s["inicio"]), "fim": float(s["fim"])} for s in segmentos or []]
        ignorar = [{"inicio": float(s["inicio"]), "fim": float(s["fim"])} for s in ignorar or []]
        return autocorte.cortes_inteligentes(env["duracao"], env["db"], env["passo"], palavras, segmentos,
                                             params or {}, ignorar)
    except (KeyError, TypeError, ValueError) as exc:
        raise EditorError(f"parâmetros inválidos para os cortes: {exc}") from exc


CUT_BASE = "Corte 9x16"


def studio_base_dir(project_id: str | None, destino: str | None = None) -> Path:
    """Onde mora o trabalho do estúdio: a pasta do projeto ou a ~/Downloads."""
    if destino == "downloads" or not project_id:
        return paths.USER_DOWNLOADS_DIR
    project = projects_module.get_project(project_id)
    if not project:
        raise EditorError("projeto não encontrado", 404)
    if not project.get("folder_available"):
        raise EditorError("pasta do projeto não encontrada no disco", 409)
    return Path(project["path"])


def _cut_work_dir(base: Path, versao: int) -> Path:
    return base / WORK_DIR / f"{TAREFA_CORTE} v{versao}"


def list_cuts(base: Path) -> list[dict[str, Any]]:
    """Os cortes verticais já entregues em `base` (Corte 9x16 vN/ com
    corte.json), mais novo primeiro. `editavel` = dá para reabrir com as
    palavras (a bancada ainda tem o palavras.json)."""
    if not base.is_dir():
        return []
    out = []
    for child in base.iterdir():
        m = DELIVERY_DIR_RE.match(child.name)
        if not child.is_dir() or not m or m.group(1) != CUT_BASE:
            continue
        corte = safe_read_json(child / "corte.json", None)
        if not isinstance(corte, dict):
            continue
        versao = int(m.group(2))
        out.append({
            "pasta": child.name, "versao": versao,
            "titulo": corte.get("titulo") or "", "duracao": corte.get("duracao"),
            "trechos": len(corte.get("segmentos") or []),
            "video": corte.get("video"), "video_existe": bool(corte.get("video")) and Path(corte["video"]).is_file(),
            "com_palavras": (_cut_work_dir(base, versao) / "palavras.json").is_file(),
            "da_timeline": bool(corte.get("edicao")),
            "modificado": datetime.fromtimestamp(child.stat().st_mtime, timezone.utc).isoformat(),
        })
    out.sort(key=lambda c: c["versao"], reverse=True)
    return out


def load_cut(base: Path, pasta: str) -> dict[str, Any]:
    """Tudo o que o estúdio precisa para reabrir um corte na timeline: os
    trechos, as palavras no tempo do bruto (da bancada), o enquadramento como
    foi desenhado e, se ele já veio de uma edição, os ajustes dela."""
    m = DELIVERY_DIR_RE.match(pasta or "")
    if not m or m.group(1) != CUT_BASE or "/" in pasta or "\\" in pasta:
        raise EditorError("corte inválido")
    folder = base / pasta
    corte = safe_read_json(folder / "corte.json", None)
    if not isinstance(corte, dict):
        raise EditorError("esse corte não tem corte.json", 404)
    versao = int(m.group(2))
    trab = _cut_work_dir(base, versao)
    palavras = safe_read_json(trab / "palavras.json", None)
    args = safe_read_json(trab / "auto-args.json", {}) or {}
    layout = corte.get("layout_editor") or args.get("layout") or (args.get("corte") or {}).get("layout")
    video = corte.get("video") or args.get("video") or ""
    return {
        "pasta": pasta, "versao": versao,
        "video": video, "video_existe": bool(video) and Path(video).is_file(),
        "titulo": corte.get("titulo") or "", "resumo": corte.get("resumo") or "",
        "gancho": corte.get("gancho") or "", "legenda": corte.get("legenda", True),
        "segmentos": corte.get("segmentos") or [], "descartado": corte.get("descartado") or [],
        "locutores": corte.get("locutores") or [], "origem_locutores": corte.get("origem_locutores"),
        "palavras": palavras if isinstance(palavras, list) else None,
        "layout": layout, "edicao": corte.get("edicao"),
        "observacoes": args.get("observacoes") or "",
    }


# ---------------------------------------------------------------- sequências (.clientela)
# A edição multipista mora num arquivo do projeto, como um .prproj: <projeto>/<nome>.clientela (ou na
# ~/Downloads no trabalho avulso). Mídia de dentro da pasta-base fica com caminho relativo (posix) — o
# mesmo arquivo abre no Mac e no Windows pelo Drive; de fora, absoluto. O modelo e o render são do
# sequencia.py; aqui é só disco, mídia e o job de export.
SEQ_NOME_RE = re.compile(r"^[^/\\.][^/\\]*\.clientela$")
SEQ_PASTA = "Sequência"
SEQ_WORK = "sequencia"
SEQUENCIA_SCRIPT = Path(sequencia.__file__).resolve()


def _seq_file(base: Path, arquivo: str) -> Path:
    if not SEQ_NOME_RE.match(arquivo or ""):
        raise EditorError("arquivo de sequência inválido")
    return base / arquivo


def _seq_media_path(base: Path, caminho: str) -> Path:
    p = Path(caminho)
    return p if p.is_absolute() else base / Path(caminho.replace("\\", "/"))


def _seq_media_caminho(base: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def list_sequences(base: Path) -> list[dict[str, Any]]:
    out = []
    for f in sorted(base.glob(f"*{sequencia.EXTENSAO}")) if base.is_dir() else []:
        if not SEQ_NOME_RE.match(f.name):
            continue
        doc = safe_read_json(f, None)
        if not isinstance(doc, dict) or doc.get("formato") != sequencia.FORMATO:
            continue
        try:
            dur = sequencia.duracao(sequencia.normalizar(doc))
        except ValueError:
            dur = None
        out.append({"arquivo": f.name, "nome": doc.get("nome") or f.stem, "config": doc.get("config") or {},
                    "duracao": dur, "clipes": len(doc.get("clipes") or []), "midias": len(doc.get("midias") or []),
                    "modificado": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()})
    out.sort(key=lambda x: x["modificado"], reverse=True)
    return out


def _seq_view(base: Path, arquivo: str, doc: dict[str, Any]) -> dict[str, Any]:
    offline = [m["id"] for m in doc["midias"] if not _seq_media_path(base, m["caminho"]).is_file()]
    return {"arquivo": arquivo, "doc": doc, "offline": offline, "duracao": sequencia.duracao(doc),
            "sobreposicoes": sequencia.sobreposicoes(doc)}


def create_sequence(base: Path, nome: str, preset: str = "completo", largura: int | None = None,
                    altura: int | None = None, fps: float = 30) -> dict[str, Any]:
    nome = (nome or "").strip()
    limpo = paths.sanitize_folder_component(nome)
    if not limpo:
        raise EditorError("dê um nome à sequência")
    try:
        doc = sequencia.nova(nome, preset, largura, altura, fps)
    except ValueError as exc:
        raise EditorError(str(exc)) from exc
    arquivo = f"{limpo}{sequencia.EXTENSAO}"
    path = _seq_file(base, arquivo)
    if path.exists():
        raise EditorError(f"já existe uma sequência chamada {limpo}", 409)
    base.mkdir(parents=True, exist_ok=True)
    safe_write_json(path, {**doc, "salvo_em": _now_iso(), "host": machine.hostname()})
    return _seq_view(base, arquivo, doc)


def sequence_from_cut(base: Path, pasta: str) -> dict[str, Any]:
    """Um corte vertical entregue (`Corte 9x16 vN/`) vira uma sequência 9:16 que se ajusta no editor: o
    bruto como take, cada trecho um par V1+A1 vinculado com o enquadramento dele, as palavras no take (a
    legenda da sequência sai delas) e o estilo da legenda. O arquivo é `<pasta>.clientela` na mesma
    pasta-base; abrir de novo devolve o que já existe (nunca sobrescreve um ajuste). O gancho volta na
    resposta: o estúdio é que tem os templates de gráfico e o põe em V2."""
    cut = load_cut(base, pasta)
    arquivo = f"{paths.sanitize_folder_component(pasta)}{sequencia.EXTENSAO}"
    path = _seq_file(base, arquivo)
    extra = {"gancho": "", "gancho_duracao": 0.0, "novo": False}
    if path.is_file():
        return {**load_sequence(base, arquivo), **extra}
    if not cut["video_existe"]:
        raise EditorError(f"o vídeo bruto deste corte não está neste computador: {cut['video']}", 409)
    ed = cut.get("edicao") or {}
    segs = ed.get("segmentos") or cut["segmentos"]
    if not segs:
        raise EditorError("esse corte não tem trechos", 409)
    media = sequence_media(base, cut["video"])
    palavras = ed.get("palavras") if ed.get("palavras") is not None else (cut["palavras"] or [])
    midia = {"id": "bruto", **{k: media[k] for k in ("caminho", "nome", "duracao", "largura", "altura", "video", "audios", "modificado")},
             "palavras": [{k: w[k] for k in ("texto", "t", "fim", "duvidosa", "quebra", "junto") if w.get(k) is not None}
                          for w in palavras if isinstance(w, dict)]}
    padrao = ed.get("layout") or cut.get("layout") or {"modo": "inteiro"}
    clipes, t = [], 0.0
    for i, s in enumerate(segs):
        dur = float(s["fim"]) - float(s["inicio"])
        if dur <= sequencia.MIN_CLIPE:
            continue
        comum = {"midia": "bruto", "trilha": 1, "inicio": round(t, 4), "entrada": float(s["inicio"]), "saida": float(s["fim"])}
        if media["video"]:
            clipes.append({"id": f"v{i}", "tipo": "video", "vinculo": f"l{i}", **comum,
                           "enquadramento": {k: v for k, v in (s.get("layout") or padrao).items() if k != "capa"}})
        if media["audios"]:
            clipes.append({"id": f"a{i}", "tipo": "audio", "vinculo": f"l{i}", "faixa": 1, **comum})
        t += dur
    est = ed.get("estilo") or {}
    legenda = {"ativa": cut.get("legenda", True) is not False and bool(midia["palavras"]),
               "modo": "fixa" if est.get("estatica") else "animada"}
    for k in ("tamanho", "margem", "palavras", "maiusculas", "fonte"):
        if est.get(k) is not None:
            legenda[k] = est[k]
    if est.get("cores"):
        legenda["cor_destaque"] = est["cores"][0]
    doc = {"formato": sequencia.FORMATO, "versao": sequencia.VERSAO, "nome": pasta,
           "config": {"largura": 1080, "altura": 1920, "fps": 30, "preset": "vertical"},
           "midias": [midia], "trilhas": {"video": 2, "audio": 2}, "clipes": clipes, "legenda": legenda}
    if ed.get("cor"):
        for c in doc["clipes"]:
            if c["tipo"] == "video":
                c["cor"] = ed["cor"]
    try:
        doc = sequencia.normalizar(doc)
    except ValueError as exc:
        raise EditorError(f"não consegui montar a sequência do corte: {exc}") from exc
    base.mkdir(parents=True, exist_ok=True)
    safe_write_json(path, {**doc, "salvo_em": _now_iso(), "host": machine.hostname(), "origem": {"corte": pasta}})
    gancho = ed.get("gancho") if ed.get("gancho") is not None else cut.get("gancho")
    return {**_seq_view(base, arquivo, doc), "gancho": gancho or "",
            "gancho_duracao": float(ed.get("gancho_duracao") or 3.0), "novo": True}


def load_sequence(base: Path, arquivo: str) -> dict[str, Any]:
    path = _seq_file(base, arquivo)
    raw = safe_read_json(path, None)
    if not isinstance(raw, dict):
        raise EditorError("sequência não encontrada", 404)
    try:
        doc = sequencia.normalizar(raw)
    except ValueError as exc:
        raise EditorError(f"{arquivo} está com problema: {exc}", 422) from exc
    return _seq_view(base, arquivo, doc)


def save_sequence(base: Path, arquivo: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Grava a sequência inteira (o estúdio salva sozinho a cada mudança). Último a gravar ganha."""
    path = _seq_file(base, arquivo)
    if not path.is_file():
        raise EditorError("sequência não encontrada (foi renomeada ou apagada fora do app?)", 404)
    try:
        doc = sequencia.normalizar(doc)
    except ValueError as exc:
        raise EditorError(str(exc)) from exc
    safe_write_json(path, {**doc, "salvo_em": _now_iso(), "host": machine.hostname()})
    return _seq_view(base, arquivo, doc)


def _probe_media(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe", path=_search_path())
    if not ffprobe:
        raise EditorError("ffprobe não encontrado (brew install ffmpeg)", 503)
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate:"
             "stream_disposition=attached_pic:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        ).stdout
        d = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise EditorError(f"não consegui ler {path.name}", 422) from exc
    streams = d.get("streams") or []
    # capa de MP3 aparece como stream de vídeo: não é vídeo
    video = next((x for x in streams if x.get("codec_type") == "video"
                  and not (x.get("disposition") or {}).get("attached_pic")), None)
    audios = [x for x in streams if x.get("codec_type") == "audio"]
    try:
        dur = float((d.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        dur = 0.0
    if not video and not audios:
        raise EditorError(f"{path.name} não tem vídeo nem áudio", 422)
    fps = 0.0
    if video and "/" in str(video.get("r_frame_rate") or ""):
        n, q = video["r_frame_rate"].split("/")
        fps = round(float(n) / float(q), 3) if float(q) else 0.0
    return {"duracao": round(dur, 4), "video": bool(video), "largura": int((video or {}).get("width") or 0),
            "altura": int((video or {}).get("height") or 0), "fps": fps, "audios": len(audios),
            "reproduzivel": path.suffix.lower() in PLAYABLE_EXTS | {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
            and (not video or video.get("codec_name") in PLAYABLE_VIDEO)}


def sequence_media(base: Path, caminho: str) -> dict[str, Any]:
    """Uma mídia para entrar na sequência: arquivo de vídeo ou de áudio, dentro da pasta-base (relativo)
    ou em qualquer lugar do disco (absoluto). Devolve a entrada pronta para `midias`, sem o id."""
    p = Path(caminho or "")
    path = p if p.is_absolute() else _safe_rel(base, caminho)
    if not path or not path.is_file() or path.suffix.lower() not in VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS:
        raise EditorError("arquivo de vídeo, áudio ou imagem não encontrado", 404)
    info = _probe_media(path)
    if path.suffix.lower() in IMAGE_EXTS:   # logo, textura: sem duração, sem áudio
        info.update(duracao=0.0, video=True, imagem=True, audios=0, fps=0.0, reproduzivel=True)
    st = path.stat()
    return {"caminho": _seq_media_caminho(base, path), "nome": path.name,
            "modificado": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(), **info}


def start_sequence_job(project_id: str | None, destino: str | None, arquivo: str) -> dict[str, Any]:
    """Exporta a sequência salva em MP4 H.264/AAC: <base>/Sequência - <nome> vN/<nome>.mp4, com uma
    cópia do .clientela de como estava. Só ffmpeg, sem token; entra na fila de um job por vez."""
    base = studio_base_dir(project_id, destino)
    view = load_sequence(base, arquivo)
    doc = view["doc"]
    if not doc["clipes"]:
        raise EditorError("a sequência está vazia: ponha clipes na timeline", 409)
    vivos = sequencia.clipes_no_render(doc)   # desativados, trilhas ocultas/mudas: nem precisam estar online
    usados = {c["midia"] for c in vivos if c.get("midia")}
    offline = [m["nome"] for m in doc["midias"] if m["id"] in usados and m["id"] in view["offline"]]
    if offline:
        raise EditorError("mídia offline: " + ", ".join(offline), 409)
    graficos = {}
    for c in vivos:
        if c["tipo"] != "grafico":
            continue
        st = graphic_status(c.get("render") or "")
        if not st["pronto"]:
            raise EditorError(f"o gráfico {c['grafico']['def'].get('nome') or c['id']} ainda não foi renderizado", 409)
        graficos[c["id"]] = str(_graphic_dir(c["render"]))
    legenda_dir = None
    leg = doc["legenda"]
    if leg["ativa"] and leg["queimar"]:
        if not graphic_status(leg.get("render") or "")["pronto"]:
            raise EditorError("a legenda queimada ainda não foi renderizada", 409)
        legenda_dir = str(_graphic_dir(leg["render"]))
    nome = paths.sanitize_folder_component(doc["nome"]) or "Sequência"
    pasta_base = f"{SEQ_PASTA} - {nome}"
    project = projects_module.get_project(project_id) if project_id and destino != "downloads" else None
    with _lock:
        _ensure_single_job()
        versao = next_version(base, pasta_base)
        entrega = base / f"{pasta_base} v{versao}"
        trabalho = base / WORK_DIR / f"{SEQ_WORK} v{versao}"
        trabalho.mkdir(parents=True, exist_ok=True)
        args_path = trabalho / "auto-args.json"
        safe_write_json(args_path, {
            "doc": doc, "entrega": str(entrega), "nome": f"{nome}.mp4",
            "midias": {m["id"]: str(_seq_media_path(base, m["caminho"]).resolve()) for m in doc["midias"] if m["id"] in usados},
            "graficos": graficos, "legenda_dir": legenda_dir,
        })
        job = _new_job(
            tipo=TIPO_SEQUENCIA, modo=MODO_SEQUENCIA,
            client_id=project["client_id"] if project else None,
            project_id=project["id"] if project else None,
            destino="projeto" if project else "downloads",
            cliente_nome=None, projeto_nome=(project.get("folder_name") or project.get("name")) if project else "Downloads",
            tarefa=SEQ_WORK, tarefa_rotulo="Sequência", observacoes="", sequencia=arquivo,
            cwd=str(base), add_dirs=[], pasta_base=pasta_base, versao=versao, entrega=str(entrega),
            trabalho=str(trabalho), prompt=f"(sequência: trilhas → ffmpeg) {arquivo}",
        )
        _launch(job, [sys.executable or "python3", str(SEQUENCIA_SCRIPT), "--args", str(args_path)], cwd=str(trabalho))
    return load_job(job["id"]) or job


# ---------------------------------------------------------- gráficos (templates) renderizados pelo estúdio
# O template de um clipe de gráfico (camadas + expressões) é desenhado pelo mesmo código JS na prévia e no
# export: antes de exportar, o estúdio renderiza os quadros PNG (com alfa) e manda para cá, uma pasta por
# chave (hash do template, parâmetros, formato e duração) em data/editor/graficos/. O job usa a pasta.
GRAFICOS_DIR = "graficos"
GRAFICOS_KEEP = 80
GRAFICO_MAX_QUADRO = 25 * 1024 * 1024
_KEY_RE = re.compile(r"^[0-9a-f]{16,64}$")


def _graphic_dir(key: str) -> Path:
    if not _KEY_RE.match(key or ""):
        raise EditorError("chave de gráfico inválida")
    return paths.EDITOR_DIR / GRAFICOS_DIR / key


def graphic_status(key: str) -> dict[str, Any]:
    try:
        d = _graphic_dir(key)
    except EditorError:
        return {"pronto": False, "quadros": 0}
    marca = safe_read_json(d / "pronto.json", None) if d.is_dir() else None
    return {"pronto": bool(marca), "quadros": (marca or {}).get("quadros", 0)}


def save_graphic_frame(key: str, n: int, data: bytes) -> None:
    d = _graphic_dir(key)
    if not 0 <= n < 200_000:
        raise EditorError("quadro fora do intervalo")
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > GRAFICO_MAX_QUADRO:
        raise EditorError("o quadro precisa ser um PNG")
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{n:05d}.{uuid.uuid4().hex[:6]}.png"
    tmp.write_bytes(data)
    tmp.replace(d / f"{n:05d}.png")


def finish_graphic(key: str, quadros: int, estados: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Fecha um gráfico. Com `estados` ([{"n", "duracao"}], a legenda queimada), escreve também a lista do
    concat do ffmpeg: um PNG por mudança visível, cada um com a sua duração — não um PNG por quadro."""
    d = _graphic_dir(key)
    if quadros < 1 or any(not (d / f"{n:05d}.png").is_file() for n in range(quadros)):
        raise EditorError("faltam quadros do gráfico", 409)
    if estados is not None:
        linhas = ["ffconcat version 1.0"]
        try:
            itens = [(int(e["n"]), float(e["duracao"])) for e in estados]
        except (KeyError, TypeError, ValueError) as exc:
            raise EditorError("estados da legenda inválidos") from exc
        if not itens or any(not 0 <= n < quadros or not 0 < dur < 36000 for n, dur in itens):
            raise EditorError("estados da legenda inválidos")
        for n, dur in itens:
            linhas += [f"file '{n:05d}.png'", f"duration {dur:.4f}"]
        linhas.append(f"file '{itens[-1][0]:05d}.png'")   # o concat ignora a duração do último sem repetir
        persistence.safe_write_text(d / "lista.ffconcat", "\n".join(linhas) + "\n")
    safe_write_json(d / "pronto.json", {"quadros": quadros, "em": _now_iso(), "estados": len(estados or [])})
    folder = d.parent
    antigas = sorted((p for p in folder.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for p in antigas[:-GRAFICOS_KEEP]:
        if p != d:
            shutil.rmtree(p, ignore_errors=True)
    return graphic_status(key)


# ---------------------------------------------------------------- Auto-SFX
SFX_INDEX = "sfx-index.json"   # data/editor/: duração de cada efeito (ffprobe uma vez por arquivo)
SFX_MAX = 3000


def sfx_folder() -> Path | None:
    cfg = get_config().get("sfx") or {}
    raw = (cfg.get("pasta_windows") if machine.current_os() == "windows" else cfg.get("pasta_mac")) or ""
    if raw.strip():
        return Path(raw).expanduser()
    return None


def _audio_duration(ffprobe: str, path: Path) -> float:
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).stdout.strip()
        return round(float(out), 4)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def sfx_library() -> dict[str, Any]:
    """Os efeitos da pasta configurada (recursivo), com categoria (sequencia.categoria_sfx) e duração."""
    from concurrent.futures import ThreadPoolExecutor

    pasta = sfx_folder()
    if not pasta or not pasta.is_dir():
        return {"pasta": str(pasta or ""), "existe": False, "arquivos": [], "categorias": {}}
    arquivos = []
    for p in sorted(pasta.rglob("*")):
        if len(arquivos) >= SFX_MAX:
            break
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not any(x.startswith(".") for x in p.relative_to(pasta).parts):
            arquivos.append(p)
    cache_path = paths.EDITOR_DIR / SFX_INDEX
    cache = safe_read_json(cache_path, {}) or {}
    ffprobe = shutil.which("ffprobe", path=_search_path())
    faltam = []
    for p in arquivos:
        st = p.stat()
        c = cache.get(str(p))
        if not (c and c.get("mtime") == st.st_mtime and c.get("size") == st.st_size):
            faltam.append((p, st))
    if faltam and ffprobe:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for (p, st), dur in zip(faltam, ex.map(lambda x: _audio_duration(ffprobe, x[0]), faltam)):
                cache[str(p)] = {"mtime": st.st_mtime, "size": st.st_size, "duracao": dur}
        paths.EDITOR_DIR.mkdir(parents=True, exist_ok=True)
        safe_write_json(cache_path, {k: v for k, v in cache.items() if Path(k).is_file()})
    out, cats = [], {}
    for p in arquivos:
        rel = p.relative_to(pasta).as_posix()
        cat = sequencia.categoria_sfx(rel)
        cats[cat] = cats.get(cat, 0) + 1
        out.append({"caminho": str(p), "nome": p.name, "rel": rel, "categoria": cat,
                    "duracao": (cache.get(str(p)) or {}).get("duracao", 0.0)})
    return {"pasta": str(pasta), "existe": True, "arquivos": out, "categorias": cats}


MOVIMENTO_DIR = "movimento"   # data/editor/: trocas de cena por mídia (scdet), para o Auto-SFX
LIMIAR_CENA = 12.0            # = autocorte.LIMIAR_CENA


def scene_changes(path: Path) -> list[float]:
    """Instantes em que a imagem do take muda de vez (ffmpeg scdet a 5 fps), com cache por mídia."""
    folder = paths.EDITOR_DIR / MOVIMENTO_DIR
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / f"{_media_key(path)}.json"
    feito = safe_read_json(cache, None)
    if isinstance(feito, list):
        return feito
    ffmpeg = shutil.which("ffmpeg", path=_search_path())
    if not ffmpeg:
        raise EditorError("ffmpeg não encontrado (brew install ffmpeg)", 503)
    r = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-an", "-vf",
                        f"fps=5,scale=320:-2,scdet=threshold={LIMIAR_CENA},metadata=mode=print:file=-", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL)
    tempos = [round(t, 3) for t, d in autocorte.ler_metadata(r.stdout) if t > 0 and d.get("lavfi.scd.score", 0) >= LIMIAR_CENA]
    safe_write_json(cache, tempos)
    return tempos


def sfx_plan(doc: dict[str, Any], params: dict[str, Any] | None = None, base: Path | None = None) -> dict[str, Any]:
    try:
        doc = sequencia.normalizar(doc)
        params = dict(params or {})
        params.setdefault("ganho", (get_config().get("sfx") or {}).get("ganho", -10))
        lib = sfx_library()
        if not lib["existe"]:
            raise EditorError("pasta de efeitos não encontrada: escolha a pasta na aba SFX", 409)
        sufixo = uuid.uuid4().hex[:6]
        movimentos = {}
        if params.get("movimento") and base is not None:   # trocas de cena dos takes usados em V1
            por_id = {m["id"]: m for m in doc["midias"]}
            for mid in {c["midia"] for c in doc["clipes"] if c["tipo"] == "video" and c["trilha"] == 1}:
                path = _seq_media_path(base, por_id[mid]["caminho"])
                if path.is_file() and not por_id[mid].get("imagem"):
                    movimentos[mid] = scene_changes(path)
        return sequencia.planejar_sfx(doc, lib["arquivos"], params, novo_id=lambda pre, k: f"{pre}x{sufixo}{k}",
                                      movimentos=movimentos)
    except ValueError as exc:
        raise EditorError(str(exc)) from exc


# ---------------------------------------------------------------- IDV no painel da sequência
FONT_EXTS = {".otf", ".ttf", ".woff", ".woff2"}
IDV_MAX = 400
SEQ_TEMPLATES_DIR = "templates/sequencia"   # dentro de <cliente>/_Estagiário/


def _idv_files(folder: Path, profundidade: int = 4, limite: int = IDV_MAX) -> list[Path]:
    out = []
    if not folder.is_dir():
        return out
    for raiz, dirs, nomes in os.walk(folder):
        rel = Path(raiz).relative_to(folder)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != identidades.PROFILE_DIR_NAME
                         and len(rel.parts) < profundidade)
        for n in sorted(nomes):
            p = Path(raiz) / n
            if not n.startswith(".") and p.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS | {".webm"}:
                out.append(p)
                if len(out) >= limite:
                    return out
    return out


def _idv_item(p: Path) -> dict[str, Any]:
    return {"caminho": str(p), "nome": p.name, "tipo": "imagem" if p.suffix.lower() in IMAGE_EXTS else "video"}


def idv_library(project_id: str | None, client_id: str | None = None) -> dict[str, Any]:
    """O que o painel IDV da sequência oferece: logos, texturas e referências do perfil (_Estagiário/), as
    pastas "00 - …" do cliente (a biblioteca de assets, pela convenção do estúdio), os Motion já entregues
    neste projeto, as fontes e o marca.json (cores, degradês, papéis de tipografia) — os templates de
    gráfico usam como padrão."""
    project = projects_module.get_project(project_id) if project_id else None
    client_id = (project or {}).get("client_id") or client_id
    client = clients_module.get_client(client_id) if client_id else None
    vazio = {"cliente": None, "client_id": None, "perfil": False, "marca": None, "fontes": [], "grupos": []}
    if not client:
        return vazio
    out = {**vazio, "cliente": client["name"], "client_id": client["id"]}
    perfil = identidades.profile_dir(client["id"])
    grupos = []
    if perfil and perfil.is_dir():
        out["perfil"] = True
        marca = safe_read_json(perfil / "marca.json", None)
        out["marca"] = marca if isinstance(marca, dict) else None
        for rotulo, cat in (("Logos", "logos"), ("Texturas", "texturas"), ("Referências", "referencias")):
            itens = [_idv_item(p) for p in _idv_files(perfil / identidades.ASSET_CATEGORIES[cat], 3, 120)]
            if itens:
                grupos.append({"nome": rotulo, "itens": itens})
        fontes_dir = perfil / identidades.ASSET_CATEGORIES["fonts"]
        arquivos = sorted(p.name for p in fontes_dir.iterdir() if p.suffix.lower() in FONT_EXTS) if fontes_dir.is_dir() else []
        declaradas = {f.get("arquivo"): f for f in ((out["marca"] or {}).get("tipografia") or {}).get("fontes") or []
                      if isinstance(f, dict)}
        for nome in arquivos:
            f = declaradas.get(nome) or {}
            out["fontes"].append({"familia": f.get("familia") or Path(nome).stem, "peso": f.get("peso") or 400,
                                  "estilo": f.get("estilo") or "normal", "arquivo": nome, "declarada": bool(f)})
    cdir = clients_module.client_dir(client)
    if cdir and cdir.is_dir():
        for pasta in sorted(p for p in cdir.iterdir() if p.is_dir() and re.match(r"^00\s*-\s*", p.name)):
            itens = [_idv_item(p) for p in _idv_files(pasta)]
            if itens:
                grupos.append({"nome": pasta.name, "itens": itens})
    if project and project.get("folder_available"):
        pdir = Path(project["path"])
        for entrega in sorted((p for p in pdir.iterdir() if p.is_dir() and DELIVERY_DIR_RE.match(p.name)
                               and p.name.startswith("Motion - ")), key=lambda p: p.name):
            itens = [_idv_item(p) for p in _idv_files(entrega, 1, 80)]
            if itens:
                grupos.append({"nome": entrega.name, "itens": itens})
    out["grupos"] = grupos
    return out


def idv_font(client_id: str, arquivo: str) -> Path | None:
    p = identidades.asset_path(client_id, "fonts", arquivo)
    return p if p and p.suffix.lower() in FONT_EXTS else None


def _templates_dir(client_id: str | None, sub: str = SEQ_TEMPLATES_DIR) -> Path | None:
    perfil = identidades.profile_dir(client_id) if client_id else None
    return perfil / sub if perfil else None


def list_seq_templates(client_id: str | None) -> list[dict[str, Any]]:
    """Pacote de templates de gráfico do cliente: <cliente>/_Estagiário/templates/sequencia/*.json."""
    d = _templates_dir(client_id)
    out = []
    for f in sorted(d.glob("*.json")) if d and d.is_dir() else []:
        t = safe_read_json(f, None)
        if isinstance(t, dict) and isinstance(t.get("camadas"), list):
            out.append({**t, "id": f"cliente:{f.stem}", "pacote": "cliente"})
    return out


def save_seq_template(client_id: str | None, template: dict[str, Any]) -> dict[str, Any]:
    d = _templates_dir(client_id)
    if not d:
        raise EditorError("este destino não tem cliente com pasta no disco", 409)
    if not isinstance(template, dict) or not isinstance(template.get("camadas"), list) or not template.get("nome"):
        raise EditorError("template inválido")
    if len(json.dumps(template, ensure_ascii=False)) > sequencia.MAX_GRAFICO:
        raise EditorError("template grande demais")
    slug = paths.sanitize_folder_component(str(template["nome"]))[:60]
    if not slug:
        raise EditorError("dê um nome ao template")
    d.mkdir(parents=True, exist_ok=True)
    limpo = {k: v for k, v in template.items() if k not in ("id", "pacote")}
    safe_write_json(d / f"{slug}.json", limpo)
    return {**limpo, "id": f"cliente:{slug}", "pacote": "cliente"}


def project_editions(project_id: str) -> dict[str, Any]:
    """O relatório de edição de um projeto para o pipeline: as sequências .clientela (formato, duração,
    clipes, takes, mídia offline) com o histórico de export de cada uma (os jobs de sequência), e os
    cortes verticais entregues."""
    project = projects_module.get_project(project_id)
    if not project:
        raise EditorError("projeto não encontrado", 404)
    if not project.get("folder_available"):
        return {"sequencias": [], "cortes": [], "exports": 0}
    pdir = Path(project["path"])
    jobs = [j for j in list_jobs(project_id=project_id) if j.get("tipo") == TIPO_SEQUENCIA]
    sequencias = []
    for s in list_sequences(pdir):
        try:
            v = load_sequence(pdir, s["arquivo"])
            offline, takes = len(v["offline"]), len([m for m in v["doc"]["midias"] if not m.get("sfx")])
            graficos = sum(1 for c in v["doc"]["clipes"] if c["tipo"] == "grafico")
        except EditorError:
            offline, takes, graficos = None, None, None
        exports = [{"versao": j.get("versao"), "status": j.get("status"), "entrega": Path(j.get("entrega") or "").name,
                    "em": j.get("terminado_em") or j.get("criado_em"), "erro": j.get("erro")}
                   for j in jobs if j.get("sequencia") == s["arquivo"]]
        sequencias.append({**s, "offline": offline, "takes": takes, "graficos": graficos, "exports": exports})
    cortes = [{"pasta": c["pasta"], "versao": c.get("versao"), "titulo": c.get("titulo")} for c in list_cuts(pdir)]
    return {"sequencias": sequencias, "cortes": cortes, "exports": len(jobs)}


# ---------------------------------------------------------------- estilos de legenda (pacote do cliente)
LEGENDA_ESTILOS_DIR = "templates/legendas"   # dentro de <cliente>/_Estagiário/
LEGENDA_ESTILO_CHAVES = {"fonte", "tamanho", "cor_texto", "cor_destaque", "margem", "maiusculas", "palavras", "modo"}


def list_caption_styles(client_id: str | None) -> list[dict[str, Any]]:
    d = _templates_dir(client_id, LEGENDA_ESTILOS_DIR)
    out = []
    for f in sorted(d.glob("*.json")) if d and d.is_dir() else []:
        e = safe_read_json(f, None)
        if isinstance(e, dict):
            out.append({**{k: v for k, v in e.items() if k in LEGENDA_ESTILO_CHAVES}, "nome": e.get("nome") or f.stem,
                        "id": f"cliente:{f.stem}"})
    return out


def save_caption_style(client_id: str | None, estilo: dict[str, Any]) -> dict[str, Any]:
    d = _templates_dir(client_id, LEGENDA_ESTILOS_DIR)
    if not d:
        raise EditorError("este destino não tem cliente com pasta no disco", 409)
    if not isinstance(estilo, dict) or not str(estilo.get("nome") or "").strip():
        raise EditorError("dê um nome ao estilo")
    slug = paths.sanitize_folder_component(str(estilo["nome"]).strip())[:60]
    if not slug:
        raise EditorError("dê um nome ao estilo")
    limpo = {k: v for k, v in estilo.items() if k in LEGENDA_ESTILO_CHAVES}
    limpo["nome"] = str(estilo["nome"]).strip()[:80]
    d.mkdir(parents=True, exist_ok=True)
    safe_write_json(d / f"{slug}.json", limpo)
    return {**limpo, "id": f"cliente:{slug}"}


# ---------------------------------------------------------------- passada semântica da legenda (LLM)
SEMANTICA_SCHEMA = {
    "type": "object",
    "properties": {"juntos": {"type": "array", "items": {"type": "integer"}},
                   "quebras": {"type": "array", "items": {"type": "integer"}}},
    "required": ["juntos", "quebras"],
}
SEMANTICA_LOTE = 500   # palavras por chamada


def _prompt_semantica(lote: list[tuple[int, str]], max_palavras: int) -> str:
    texto = " ".join(f"{k}:{t}" for k, t in lote)
    return (
        "Você diagrama legendas de vídeo em português (telas curtas, uma frase ou pedaço de frase por vez, "
        f"até ~{max_palavras} palavras por tela). Abaixo, a transcrição palavra por palavra, cada uma com o "
        "seu número (número:palavra).\n\n"
        "Devolva só JSON com duas listas de números:\n"
        '- "juntos": palavras que NÃO podem ficar no fim de uma tela separadas da palavra seguinte — nomes '
        "próprios compostos, termos que só fazem sentido juntos (\"Banco Central\", \"segundo turno\"), "
        "número com unidade, artigo/preposição com o que vem depois, verbo com a partícula, negação com o verbo.\n"
        '- "quebras": palavras que devem ABRIR uma tela nova — começo de frase ou de ideia, depois de pausa '
        "de sentido, antes de \"mas\", \"porque\", \"então\" quando mudam o rumo.\n"
        "Não invente números fora da lista. Não marque o que já está óbvio pela pontuação.\n\n"
        f"Transcrição:\n{texto}\n"
    )


def caption_semantics(palavras: list[dict[str, Any]], max_palavras: int = 4,
                      confirmar_tokens: bool = False) -> dict[str, Any]:
    """Marca, nas palavras da legenda, o que fica junto e onde abre tela nova (autocorte.blocos lê as
    marcas). OpenRouter, se for o provedor do corte vertical; senão o Claude Code — com a trava de tokens."""
    cfg = get_config()["corte"]
    lista = [(k, str(w.get("texto") or "")[:40]) for k, w in enumerate(palavras or []) if str(w.get("texto") or "").strip()]
    if not lista:
        raise EditorError("não há palavras: transcreva os takes primeiro")
    usa_openrouter = cfg.get("provedor_llm") == "openrouter"
    binario = None
    if not usa_openrouter:
        binario = find_claude()
        if not binario:
            raise EditorError(availability()["motivo"], 503)
        check_tokens(confirmar_tokens, "Revisar o agrupamento da legenda com IA")
    validos = {k for k, _ in lista}
    juntos, quebras, custo = set(), set(), 0.0
    for ini in range(0, len(lista), SEMANTICA_LOTE):
        lote = lista[ini:ini + SEMANTICA_LOTE]
        prompt = _prompt_semantica(lote, max_palavras)
        try:
            if usa_openrouter:
                resp, c = autocorte.llm_openrouter(prompt, cfg)
            else:
                resp, c = autocorte.llm_claude(prompt, cfg, binario, timeout=600, schema=SEMANTICA_SCHEMA, env=child_env())
        except autocorte.Falha as exc:
            raise EditorError(str(exc), 502) from exc
        custo += c or 0.0
        resp = resp if isinstance(resp, dict) else {}
        juntos |= {int(k) for k in resp.get("juntos") or [] if isinstance(k, (int, float)) and int(k) in validos}
        quebras |= {int(k) for k in resp.get("quebras") or [] if isinstance(k, (int, float)) and int(k) in validos}
    quebras -= {k + 1 for k in juntos}   # o LLM às vezes pede as duas coisas no mesmo ponto: junto ganha
    return {"juntos": sorted(juntos), "quebras": sorted(quebras), "custo": round(custo, 4),
            "provedor": "openrouter" if usa_openrouter else "claude"}
