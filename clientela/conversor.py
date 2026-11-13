"""Conversor de entrega: versões do vídeo final (Cliente H.264, WhatsApp, reenquadrado, só áudio) com o ffmpeg,
sem abrir o Premiere/Media Encoder.

`build_args` é a única tradução preset → argumentos. A saída vai ao lado do original (ou na ~/Downloads) com
nome livre, e o ffmpeg roda com `-n`: nunca sobrescreve nada, muito menos a origem. O reenquadramento usa
`sequencia.cadeia_enquadramento` (as regras do corte vertical em qualquer tamanho) — `autocorte.cadeia_video`
é fixo em 1080x1920 a 30 fps. Progresso por `-progress pipe:1` (out_time ÷ duração do ffprobe).

Um job por vez, fila em memória, no molde de `transcricao.py`. Cancelar mata o ffmpeg e apaga só o arquivo
que ele estava escrevendo."""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import editor, paths, sequencia
from .transcricao import _nome_livre

logger = logging.getLogger("clientela.conversor")

PRESETS = ("cliente", "whatsapp", "reenquadrar", "audio")
FORMATOS = {"9x16": (1080, 1920), "1x1": (1080, 1080), "16x9": (1920, 1080)}
PADRAO = {"formato": "9x16", "modo": "recorte", "posicao": 0.5, "audio": "mp3"}
WHATSAPP_LIMITE = 100 * 1024 * 1024
ENTRADA_EXTS = editor.VIDEO_EXTS | {".webm"}

FINISHED = ("done", "error", "cancelled")
_MAX_JOBS = 50
_jobs: dict[str, dict[str, Any]] = {}
_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()
_fila = threading.Condition(_lock)
_worker: threading.Thread | None = None


class OptionError(ValueError):
    pass


def normalizar(preset: str, opcoes: dict[str, Any] | None) -> dict[str, Any]:
    if preset not in PRESETS:
        raise OptionError(f"preset desconhecido: {preset}")
    o = {**PADRAO, **{k: v for k, v in (opcoes or {}).items() if k in PADRAO}}
    if o["formato"] not in FORMATOS:
        raise OptionError(f"formato desconhecido: {o['formato']}")
    if o["modo"] not in ("recorte", "desfocado"):
        raise OptionError(f"modo desconhecido: {o['modo']}")
    if o["audio"] not in ("mp3", "wav"):
        raise OptionError(f"áudio desconhecido: {o['audio']}")
    try:
        o["posicao"] = min(1.0, max(0.0, float(o["posicao"])))
    except (TypeError, ValueError):
        raise OptionError("posição precisa ser um número de 0 a 1") from None
    return o


def nome_saida(entrada: str | Path, preset: str, o: dict[str, Any]) -> str:
    stem = Path(entrada).stem
    if preset == "audio":
        return f"{stem} - Áudio.{o['audio']}"
    sufixo = {"cliente": "Cliente", "whatsapp": "WhatsApp"}.get(preset) or o["formato"]
    return f"{stem} - {sufixo}.mp4"


_H264 = ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]


def build_args(preset: str, entrada: str, saida: str, opcoes: dict[str, Any] | None = None,
               info: dict[str, Any] | None = None) -> list[str]:
    """Argumentos do ffmpeg (sem o executável). `info` (largura/altura da origem) só o reenquadrar usa."""
    if Path(saida).expanduser().resolve() == Path(entrada).expanduser().resolve():
        raise OptionError("a saída não pode ser o próprio arquivo de origem")
    o = normalizar(preset, opcoes)
    args = ["-nostdin", "-hide_banner", "-v", "error", "-n", "-i", entrada, "-progress", "pipe:1", "-nostats"]
    if preset == "audio":
        cod = ["-c:a", "libmp3lame", "-b:a", "320k"] if o["audio"] == "mp3" else ["-c:a", "pcm_s16le", "-ar", "48000"]
        return [*args, "-map", "0:a:0", "-vn", *cod, saida]
    audio = ["-map", "0:a:0?", "-c:a", "aac"]
    fim = ["-movflags", "+faststart", saida]
    if preset == "cliente":
        return [*args, "-map", "0:v:0", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", *_H264, "-crf", "20",
                *audio, "-b:a", "192k", *fim]
    if preset == "whatsapp":
        return [*args, "-map", "0:v:0", "-vf",
                "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                *_H264, "-profile:v", "main", "-crf", "26", *audio, "-b:a", "128k", *fim]
    sw, sh = int((info or {}).get("largura") or 0), int((info or {}).get("altura") or 0)
    if not sw or not sh:
        raise OptionError("não consegui ler o tamanho do vídeo (ffprobe)")
    W, H = FORMATOS[o["formato"]]
    if o["modo"] == "desfocado":
        e = {"modo": "desfocado", "escala": 1, "posicao": 0.5}
    else:  # caixa da largura que cabe, deslizando na horizontal por `posicao`
        frac = min(1.0, sh * W / H / sw)
        e = {"modo": "inteiro", "principal": {"x": (1 - frac) * o["posicao"], "y": 0, "w": frac, "h": 1}}
    cadeia = sequencia.cadeia_enquadramento(e, sw, sh, W, H, 0)
    return [*args, "-filter_complex", f"[0:v:0]{cadeia},setsar=1,format=yuv420p[v]", "-map", "[v]",
            *_H264, "-crf", "20", *audio, "-b:a", "192k", *fim]


def ler_progresso(linha: str, duracao: float | None) -> float | None:
    """Uma linha do `-progress` → % (ou None). `out_time_ms` também vem em microssegundos (bug antigo do ffmpeg)."""
    chave, _, valor = linha.strip().partition("=")
    if chave == "progress" and valor == "end":
        return 100.0
    if chave in ("out_time_us", "out_time_ms") and duracao:
        try:
            return round(min(100.0, max(0.0, int(valor) / 1e6 / duracao * 100)), 1)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------- jobs
def _bin(nome: str) -> str | None:
    return shutil.which(nome, path=editor._search_path())


def _upd(job_id: str, **campos: Any) -> None:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(campos)


def start(arquivo: str, preset: str, opcoes: dict[str, Any] | None = None, destino: str = "") -> dict[str, Any]:
    """`destino` vazio = ao lado do original; "downloads" = ~/Downloads."""
    src = Path(arquivo).expanduser()
    if not src.is_file() or src.suffix.lower() not in ENTRADA_EXTS:
        raise OptionError(f"vídeo não encontrado: {arquivo}")
    o = normalizar(preset, opcoes)
    if not _bin("ffmpeg") or not _bin("ffprobe"):
        raise OptionError("ffmpeg não encontrado (instale o ffmpeg)")
    pasta = paths.USER_DOWNLOADS_DIR if destino == "downloads" else src.parent
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "id": job_id, "status": "pending", "etapa": "Na fila", "pct": 0.0, "arquivo": str(src),
            "titulo": src.name, "preset": preset, "opcoes": o, "pasta": str(pasta), "saida": None, "nome": None,
            "tamanho": None, "aviso": None, "error": None, "criado_em": time.time(), "terminado_em": None,
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
        _worker = threading.Thread(target=_loop, daemon=True, name="conversor")
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
            prox["status"], prox["etapa"] = "running", "Convertendo"
            job_id = prox["id"]
        try:
            _rodar(job_id)
        except Exception as exc:  # um job quebrado não derruba a fila
            logger.exception("conversão %s falhou", job_id)
            _upd(job_id, status="error", etapa="Falhou", error=str(exc), terminado_em=time.time())


def _rodar(job_id: str) -> None:
    with _lock:
        job = dict(_jobs[job_id])
    src = Path(job["arquivo"])
    info = editor._media_info(_bin("ffprobe"), src)
    pasta = Path(job["pasta"])
    pasta.mkdir(parents=True, exist_ok=True)
    saida = _nome_livre(pasta, nome_saida(src, job["preset"], job["opcoes"]))
    args = build_args(job["preset"], str(src), str(saida), job["opcoes"], info)
    _upd(job_id, saida=str(saida), nome=saida.name)
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([_bin("ffmpeg"), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=err, env=editor.child_env(), text=True, encoding="utf-8", errors="replace")
        with _lock:
            _procs[job_id] = proc
            if _jobs[job_id]["status"] == "cancelled":  # cancelado entre a fila e o Popen
                proc.terminate()
        for linha in proc.stdout:
            pct = ler_progresso(linha, info.get("duracao"))
            if pct is not None:
                _upd(job_id, pct=pct)
        rc = proc.wait()
        err.seek(0)
        erro = err.read().decode("utf-8", "replace").strip()
    with _lock:
        _procs.pop(job_id, None)
        cancelado = _jobs[job_id]["status"] == "cancelled"
    if cancelado or rc != 0:
        saida.unlink(missing_ok=True)  # o nome era livre: este arquivo é só o parcial deste job
        if cancelado:
            _upd(job_id, etapa="Cancelado", terminado_em=time.time())
        else:
            _upd(job_id, status="error", etapa="Falhou", error=(erro.splitlines() or [f"ffmpeg saiu com {rc}"])[-1],
                 terminado_em=time.time())
        return
    tamanho = saida.stat().st_size
    aviso = ("Passou de 100 MB: o WhatsApp pode recusar. Mande como documento."
             if job["preset"] == "whatsapp" and tamanho > WHATSAPP_LIMITE else None)
    _upd(job_id, status="done", etapa="Pronto", pct=100.0, tamanho=tamanho, aviso=aviso, terminado_em=time.time())


def cancel(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] in FINISHED:
            return False
        job["status"], job["etapa"], job["terminado_em"] = "cancelled", "Cancelado", time.time()
        proc = _procs.get(job_id)
    if proc:
        proc.terminate()
    return True


def get_status(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def list_jobs() -> list[dict[str, Any]]:
    with _lock:
        return [dict(j) for j in sorted(_jobs.values(), key=lambda j: j["criado_em"], reverse=True)]


def saida_de(job_id: str) -> Path | None:
    job = get_status(job_id)
    if not job or job["status"] != "done" or not job["saida"]:
        return None
    return Path(job["saida"])


def reset() -> None:
    with _lock:
        for p in _procs.values():
            p.terminate()
        _jobs.clear()
        _procs.clear()
