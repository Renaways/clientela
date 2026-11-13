"""Transcrição com o openai-whisper (https://github.com/openai/whisper).

O whisper (PyTorch) não mora no .venv do app: roda como `python -m whisper`
no primeiro Python da máquina que consegue importá-lo (ou o configurado), do
mesmo jeito que o terminal rodaria. Toda opção da CLI dele tem campo no modal;
`build_args` é a única tradução opções → argumentos, e o comando equivalente
fica no job (copiável, como no Baixar mídia).

O progresso vem da barra do tqdm que o whisper desenha no stderr com
`--verbose False` (quadros de mel, 100 por segundo de áudio) e da barra do
download do modelo na primeira vez. A saída é gerada numa pasta temporária e
só depois movida para o destino com nome livre: transcrever de novo o mesmo
vídeo nunca apaga a transcrição anterior.

Um job por vez (o modelo ocupa a CPU/GPU inteira); os outros esperam na fila.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import editor

logger = logging.getLogger("clientela.transcricao")

MODELOS = ("tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en",
           "large-v1", "large-v2", "large-v3", "large", "large-v3-turbo", "turbo")
# Tamanho aproximado do download e da memória, da tabela do README do whisper.
MODELO_INFO = {
    "tiny": ("39 M", "~1 GB", "~10x"), "base": ("74 M", "~1 GB", "~7x"), "small": ("244 M", "~2 GB", "~4x"),
    "medium": ("769 M", "~5 GB", "~2x"), "large": ("1550 M", "~10 GB", "1x"), "turbo": ("809 M", "~6 GB", "~8x"),
}
FORMATOS = ("txt", "srt", "vtt", "tsv", "json")
TAREFAS = ("transcribe", "translate")
DISPOSITIVOS = ("cpu", "mps", "cuda")
MIDIA_EXTS = {".mp4", ".mov", ".mxf", ".m4v", ".avi", ".mkv", ".webm", ".wav", ".mp3", ".m4a", ".aac",
              ".flac", ".ogg", ".opus", ".aif", ".aiff", ".wma", ".mts", ".ts"}

PADRAO: dict[str, Any] = {
    "modelo": "turbo", "idioma": "", "tarefa": "transcribe", "dispositivo": "cpu",
    "formatos": ["txt", "srt", "vtt", "json"],
    "temperatura": 0.0, "temperatura_incremento": 0.2, "best_of": 5, "beam_size": 5,
    "patience": None, "length_penalty": None, "suppress_tokens": "-1",
    "initial_prompt": "", "condition_on_previous_text": True, "fp16": True,
    "compression_ratio_threshold": 2.4, "logprob_threshold": -1.0, "no_speech_threshold": 0.6,
    "word_timestamps": False, "prepend_punctuations": "\"'“¿([{-", "append_punctuations": "\"'.。,，!！?？:：”)]}、",
    "highlight_words": False, "max_line_width": None, "max_line_count": None, "max_words_per_line": None,
    "threads": 0, "clip_timestamps": "0", "hallucination_silence_threshold": None,
    "model_dir": "", "python": "",
}

_PY_CANDIDATOS = ("python3", "/Library/Developer/CommandLineTools/usr/bin/python3",
                  "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3")
_INFO_SCRIPT = (
    "import json,whisper,torch\n"
    "from whisper.tokenizer import LANGUAGES\n"
    "from whisper.version import __version__ as v\n"
    "print(json.dumps({'versao': v, 'torch': torch.__version__, 'idiomas': LANGUAGES,"
    " 'mps': bool(getattr(torch.backends,'mps',None) and torch.backends.mps.is_available()),"
    " 'cuda': torch.cuda.is_available()}))"
)

FINISHED = ("done", "error", "cancelled")
_MAX_JOBS = 50
_jobs: dict[str, dict[str, Any]] = {}
_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()
_fila = threading.Condition(_lock)
_worker: threading.Thread | None = None
_info_cache: dict[str, Any] = {}


class OptionError(ValueError):
    pass


# ------------------------------------------------------------------ ferramenta
def _rodar_info(python: str) -> dict[str, Any] | None:
    try:
        r = subprocess.run([python, "-c", _INFO_SCRIPT], capture_output=True, text=True, timeout=60,
                           env=editor.child_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def tool_info(python: str = "", forcar: bool = False) -> dict[str, Any]:
    """O Python que tem o whisper, versões, idiomas, modelos já baixados."""
    chave = python or "*"
    if not forcar and chave in _info_cache:
        info = dict(_info_cache[chave])
    else:
        candidatos = [python] if python else [shutil.which(c, path=editor._search_path()) or c for c in _PY_CANDIDATOS]
        info = {"ok": False, "python": None}
        for c in dict.fromkeys(candidatos):
            dados = _rodar_info(str(Path(c).expanduser()))
            if dados:
                info = {"ok": True, "python": str(Path(c).expanduser()), **dados}
                break
        _info_cache[chave] = dict(info)
    info["ffmpeg"] = shutil.which("ffmpeg", path=editor._search_path())
    info["modelos"] = modelos_baixados()
    info["hint"] = "pip3 install -U openai-whisper (precisa do ffmpeg: brew install ffmpeg)"
    return info


def _pasta_modelos(model_dir: str = "") -> Path:
    if model_dir:
        return Path(model_dir).expanduser()
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "whisper"


# O nome do arquivo .pt de cada modelo é o fim da URL em whisper._MODELS.
_ARQUIVO_MODELO = {"large": "large-v3", "turbo": "large-v3-turbo"}


def modelos_baixados(model_dir: str = "") -> list[str]:
    pasta = _pasta_modelos(model_dir)
    return [m for m in MODELOS if (pasta / f"{_ARQUIVO_MODELO.get(m, m)}.pt").is_file()]


# ------------------------------------------------------------------- opções
def _num(o: dict, k: str, tipo=float, minimo=None, maximo=None, opcional=False):
    v = o.get(k, PADRAO[k])
    if v in (None, "") and opcional:
        return None
    try:
        v = tipo(v)
    except (TypeError, ValueError):
        raise OptionError(f"{k}: valor inválido ({v!r})")
    if (minimo is not None and v < minimo) or (maximo is not None and v > maximo):
        raise OptionError(f"{k}: fora do intervalo ({v})")
    return v


def normalizar(opcoes: dict[str, Any] | None) -> dict[str, Any]:
    o = {**PADRAO, **(opcoes or {})}
    if o["modelo"] not in MODELOS and not Path(str(o["modelo"])).expanduser().is_file():
        raise OptionError(f"modelo desconhecido: {o['modelo']}")
    if o["tarefa"] not in TAREFAS:
        raise OptionError("tarefa: transcribe ou translate")
    if o["dispositivo"] not in DISPOSITIVOS:
        raise OptionError("dispositivo: cpu, mps ou cuda")
    if not re.fullmatch(r"[A-Za-z][A-Za-z \-]{0,30}", o["idioma"] or "x"):
        raise OptionError("idioma inválido")
    fmts = [f for f in FORMATOS if f in (o.get("formatos") or [])]
    if not fmts:
        raise OptionError("escolha ao menos um formato de saída")
    if not re.fullmatch(r"-?\d+(,-?\d+)*", str(o["suppress_tokens"]).replace(" ", "") or "-1"):
        raise OptionError("suppress_tokens: ids separados por vírgula (ex.: -1)")
    if not re.fullmatch(r"\d+(\.\d+)?(,\d+(\.\d+)?)*", str(o["clip_timestamps"]).replace(" ", "") or "0"):
        raise OptionError("clip_timestamps: início,fim,início,fim… em segundos (ex.: 0,60,120,180)")
    return {
        **o, "formatos": fmts, "idioma": (o["idioma"] or "").strip(),
        "temperatura": _num(o, "temperatura", float, 0, 1),
        "temperatura_incremento": _num(o, "temperatura_incremento", float, 0, 1, opcional=True),
        "best_of": _num(o, "best_of", int, 1, 20), "beam_size": _num(o, "beam_size", int, 1, 20),
        "patience": _num(o, "patience", float, 0.1, 10, opcional=True),
        "length_penalty": _num(o, "length_penalty", float, 0, 1, opcional=True),
        "compression_ratio_threshold": _num(o, "compression_ratio_threshold", float, 0, 10, opcional=True),
        "logprob_threshold": _num(o, "logprob_threshold", float, -20, 0, opcional=True),
        "no_speech_threshold": _num(o, "no_speech_threshold", float, 0, 1, opcional=True),
        "max_line_width": _num(o, "max_line_width", int, 1, 500, opcional=True),
        "max_line_count": _num(o, "max_line_count", int, 1, 20, opcional=True),
        "max_words_per_line": _num(o, "max_words_per_line", int, 1, 50, opcional=True),
        "threads": _num(o, "threads", int, 0, 128),
        "hallucination_silence_threshold": _num(o, "hallucination_silence_threshold", float, 0, 60, opcional=True),
        "suppress_tokens": str(o["suppress_tokens"]).replace(" ", "") or "-1",
        "clip_timestamps": str(o["clip_timestamps"]).replace(" ", "") or "0",
        "initial_prompt": str(o["initial_prompt"] or "")[:2000],
        "condition_on_previous_text": bool(o["condition_on_previous_text"]),
        "fp16": bool(o["fp16"]), "word_timestamps": bool(o["word_timestamps"]),
        "highlight_words": bool(o["highlight_words"]),
    }


def build_args(opcoes: dict[str, Any], arquivo: str, saida: str) -> list[str]:
    """Os argumentos de `python -m whisper`, depois do `-m whisper`."""
    o = normalizar(opcoes)
    palavras = o["word_timestamps"]
    a = [arquivo, "--model", o["modelo"], "--output_dir", saida,
         # o whisper só gera um formato ou todos; com mais de um, os não pedidos ficam na pasta temporária
         "--output_format", o["formatos"][0] if len(o["formatos"]) == 1 else "all",
         "--verbose", "False", "--task", o["tarefa"], "--device", o["dispositivo"]]
    if o["model_dir"]:
        a += ["--model_dir", str(Path(o["model_dir"]).expanduser())]
    if o["idioma"]:
        a += ["--language", o["idioma"]]
    # fp16 não existe na CPU: o whisper avisaria e cairia para fp32 de qualquer jeito
    a += ["--temperature", str(o["temperatura"]), "--best_of", str(o["best_of"]), "--beam_size", str(o["beam_size"]),
          "--suppress_tokens", o["suppress_tokens"],
          "--condition_on_previous_text", str(o["condition_on_previous_text"]),
          "--fp16", str(o["fp16"] and o["dispositivo"] != "cpu"),
          "--word_timestamps", str(palavras), "--clip_timestamps", o["clip_timestamps"],
          "--threads", str(o["threads"])]
    for k in ("patience", "length_penalty", "compression_ratio_threshold", "logprob_threshold",
              "no_speech_threshold", "hallucination_silence_threshold"):
        if o[k] is not None:
            a += [f"--{k}", str(o[k])]
    if o["temperatura_incremento"] is not None:
        a += ["--temperature_increment_on_fallback", str(o["temperatura_incremento"])]
    if o["initial_prompt"]:
        a += ["--initial_prompt", o["initial_prompt"]]
    if palavras:  # o whisper recusa estas sem --word_timestamps True
        a += ["--prepend_punctuations", o["prepend_punctuations"], "--append_punctuations", o["append_punctuations"],
              "--highlight_words", str(o["highlight_words"])]
        for k in ("max_line_width", "max_line_count", "max_words_per_line"):
            if o[k] is not None:
                a += [f"--{k}", str(o[k])]
    return a


def comando(opcoes: dict[str, Any], arquivo: str, saida: str, python: str = "python3") -> str:
    return shlex.join([python, "-m", "whisper", *build_args(opcoes, arquivo, saida)])


# ----------------------------------------------------------------- progresso
_TQDM = re.compile(r"(\d+(?:\.\d+)?)([kMG]?)(i?B|frames)?/(\d+(?:\.\d+)?)([kMG]?)(i?B|frames)?\s*\[([^\]<]*)<?([^,\]]*)")
_MULT = {"": 1, "k": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def ler_progresso(linha: str) -> dict[str, Any] | None:
    """Uma atualização do tqdm → {etapa, pct, eta, feito_s, total_s}."""
    m = _TQDM.search(linha)
    if not m:
        return None
    feito, total = float(m.group(1)) * _MULT[m.group(2)], float(m.group(4)) * _MULT[m.group(5)]
    if total <= 0:
        return None
    baixando = "frames" not in linha  # a barra do download é em bytes (160M/1.51G … MiB/s)
    p = {"etapa": "Baixando o modelo" if baixando else "Transcrevendo",
         "pct": round(min(100.0, feito / total * 100), 1), "eta": m.group(8).strip().strip("?") or None}
    if not baixando:
        p["feito_s"], p["total_s"] = feito / 100, total / 100  # 100 quadros de mel por segundo
    return p


# ---------------------------------------------------------------------- jobs
def _nome_livre(destino: Path, nome: str) -> Path:
    alvo = destino / nome
    stem, ext, n = Path(nome).stem, Path(nome).suffix, 2
    while alvo.exists():
        alvo = destino / f"{stem} ({n}){ext}"
        n += 1
    return alvo


def _upd(job_id: str, **campos: Any) -> None:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(campos)


def start(arquivo: str, opcoes: dict[str, Any] | None = None, saida: str = "") -> dict[str, Any]:
    src = Path(arquivo).expanduser()
    if not src.is_file():
        raise OptionError(f"arquivo não encontrado: {arquivo}")
    if src.suffix.lower() not in MIDIA_EXTS:
        raise OptionError(f"não é vídeo nem áudio: {src.name}")
    destino = Path(saida).expanduser() if saida else src.parent
    if not destino.is_dir():
        raise OptionError(f"pasta de saída não existe: {destino}")
    o = normalizar(opcoes)
    info = tool_info(o["python"])
    if not info["ok"]:
        raise OptionError(f"openai-whisper não encontrado — {info['hint']}")
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "id": job_id, "status": "pending", "etapa": "Na fila", "pct": 0.0, "eta": None,
            "feito_s": None, "total_s": None, "idioma_detectado": None, "arquivo": str(src), "titulo": src.name,
            "pasta": str(destino), "arquivos": [], "error": None, "log": [], "opcoes": o,
            "comando": comando(o, str(src), str(destino), info["python"]),
            "criado_em": time.time(), "terminado_em": None,
        }
        finished = sorted((j for j in _jobs.values() if j["status"] in FINISHED), key=lambda j: j["criado_em"])
        for j in finished[: max(0, len(_jobs) - _MAX_JOBS)]:
            _jobs.pop(j["id"], None)
        _fila.notify()
        _garantir_worker()
    return get_status(job_id)


def _garantir_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, daemon=True, name="transcricao")
        _worker.start()


def _loop() -> None:
    while True:
        with _lock:
            prox = None
            while prox is None:
                pend = sorted((j for j in _jobs.values() if j["status"] == "pending"), key=lambda j: j["criado_em"])
                prox = pend[0] if pend else None
                if prox is None:
                    _fila.wait()
            prox["status"], prox["etapa"] = "running", "Carregando o modelo"
            job_id = prox["id"]
        try:
            _rodar(job_id)
        except Exception as exc:  # um job quebrado não derruba a fila
            logger.exception("transcrição %s falhou", job_id)
            _upd(job_id, status="error", error=str(exc), terminado_em=time.time())


def _rodar(job_id: str) -> None:
    with _lock:
        job = dict(_jobs[job_id])
    o = job["opcoes"]
    info = tool_info(o["python"])
    with tempfile.TemporaryDirectory(prefix="clientela-whisper-") as tmp:
        cmd = [info["python"], "-u", "-m", "whisper", *build_args(o, job["arquivo"], tmp)]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env={**editor.child_env(), "PYTHONIOENCODING": "utf-8"}, cwd=tmp)
        with _lock:
            _procs[job_id] = proc
            if _jobs[job_id]["status"] == "cancelled":  # cancelado entre a fila e o Popen
                proc.terminate()
        log: list[str] = []
        buf = b""
        while True:
            ch = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
            if not ch:
                break
            buf += ch
            *partes, buf = re.split(rb"[\r\n]", buf)
            for p in partes:
                _linha(job_id, p.decode("utf-8", "replace").strip(), log)
        if buf:
            _linha(job_id, buf.decode("utf-8", "replace").strip(), log)
        rc = proc.wait()
        with _lock:
            _procs.pop(job_id, None)
            cancelado = _jobs[job_id]["status"] == "cancelled"
        if cancelado:
            _upd(job_id, etapa="Cancelado", terminado_em=time.time())
            return
        stem = Path(job["arquivo"]).stem
        gerados = [Path(tmp) / f"{stem}.{f}" for f in o["formatos"] if (Path(tmp) / f"{stem}.{f}").is_file()]
        # o whisper imprime "Skipping …" e sai com 0 quando a transcrição quebra
        falha = next((l for l in log if l.startswith("Skipping ")), None)
        if rc != 0 or falha or not gerados:
            erro = falha or next((l for l in reversed(log) if "Error" in l), None) or (log[-1] if log else f"saiu com {rc}")
            _upd(job_id, status="error", error=erro, etapa="Falhou", log=log[-40:], terminado_em=time.time())
            return
        destino = Path(job["pasta"])
        movidos = []
        for g in gerados:
            alvo = _nome_livre(destino, g.name)
            shutil.move(str(g), alvo)
            movidos.append(alvo.name)
    _upd(job_id, status="done", etapa="Pronto", pct=100.0, eta=None, arquivos=movidos,
         log=log[-40:], terminado_em=time.time())


def _linha(job_id: str, linha: str, log: list[str]) -> None:
    if not linha:
        return
    p = ler_progresso(linha)
    if p:
        _upd(job_id, **p)
        return
    log.append(linha)
    if linha.startswith("Detected language:"):
        _upd(job_id, idioma_detectado=linha.split(":", 1)[1].strip())


def cancel(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] in FINISHED:
            return False
        job["status"], job["etapa"] = "cancelled", "Cancelado"
        job["terminado_em"] = time.time()
        proc = _procs.get(job_id)
    if proc:
        proc.terminate()
    return True


def retry(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        return None
    return start(job["arquivo"], job["opcoes"], job["pasta"])


def get_status(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return {k: v for k, v in job.items() if k != "opcoes"} if job else None


def list_jobs() -> list[dict[str, Any]]:
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["criado_em"], reverse=True)
        return [{k: v for k, v in j.items() if k != "opcoes"} for j in jobs]


def arquivo_de(job_id: str, nome: str = "") -> Path | None:
    """Um arquivo gerado pelo job (ou o primeiro), só entre os que ele gerou."""
    with _lock:
        job = _jobs.get(job_id)
    if not job or not job["arquivos"]:
        return None
    nome = nome or job["arquivos"][0]
    return Path(job["pasta"]) / nome if nome in job["arquivos"] else None


def reset() -> None:
    with _lock:
        for p in _procs.values():
            p.terminate()
        _jobs.clear()
        _procs.clear()
        _info_cache.clear()
