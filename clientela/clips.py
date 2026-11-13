"""Clipes de live: uma pasta de gravação vigiada vira projeto sozinha.

O OBS (ou o que for) grava o clipe bruto numa pasta definida nas
configurações. Quando aparece ali um vídeo novo e ele termina de ser escrito,
o vigia:

1. cria um projeto no cliente configurado (``projects.create_project``, com a
   estrutura de pasta de sempre) e marca o clipe como ``midia_original`` no
   registro;
2. move o clipe para ``<projeto>/Brutos/``;
3. emite o evento ``clipe_pronto`` — para os callbacks registrados com
   ``on_clip_ready`` (no processo) e para o frontend, que busca
   ``/api/clipes/eventos`` e avisa que o projeto está pronto para o pipeline
   de IA. O vigia não dispara o editor sozinho: quem escolhe a tarefa é quem
   recebe o evento (com ``autocorte`` ligado, o editor registra um callback
   que pede o corte vertical de cada clipe).

"Terminou de ser escrito" = tamanho e data de modificação parados por
``estavel_segundos``. Antes de criar o projeto o arquivo é renomeado, na
mesma pasta, para um nome oculto: isso o reserva (o próximo ciclo não o vê
de novo) e, no Windows, falha enquanto o gravador ainda segura o arquivo —
nesse caso ele só volta para a fila. O watchdog, quando instalado, só acorda
o ciclo mais cedo; sem ele a pasta é lida a cada poucos segundos e tudo
funciona igual.

A configuração fica em ``config.json`` sob ``clipes``, com a pasta por
sistema (``pasta_mac`` / ``pasta_windows``): o config.json é o mesmo nas
duas máquinas, a pasta de gravação não.
"""

import logging
import shutil
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import clients as clients_module
from . import history, machine, paths, store
from . import projects as projects_module
from .persistence import safe_read_json, safe_write_json
from .stages import STAGES

logger = logging.getLogger("clientela.clips")

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

CONFIG_KEY = "clipes"
OS_FOLDER_KEYS = {machine.MAC: "pasta_mac", machine.WINDOWS: "pasta_windows"}

DEFAULT_CLIP_CONFIG: dict[str, Any] = {
    "ativo": False,
    "pasta_mac": "",
    "pasta_windows": "",
    "client_id": "",
    "etapa_inicial": "backlog",
    # {arquivo} = nome do clipe sem extensão; {data} = AAAA-MM-DD e
    # {hora} = HHhMM da última gravação do arquivo.
    "nome_modelo": "Clipe {arquivo}",
    "tags": [],
    # Os mesmos formatos que o editor aceita como vídeo bruto.
    "extensoes": [".mp4", ".mov", ".mkv", ".m4v"],
    "estavel_segundos": 10,
    # Desligado: apontar o vigia para a pasta de saída do OBS, com meses de
    # gravações antigas, não pode virar centenas de projetos de uma vez.
    "processar_existentes": False,
    # Ligado: cada clipe que vira projeto já entra no corte vertical do editor
    # (editor.autocut_from_clip, registrado no app.py). Na fila, se o
    # estagiário estiver ocupado.
    "autocorte": False,
}

MIN_STABLE_SECONDS = 2
EVENT = "clipe_pronto"
CLAIM_PREFIX = ".clientela-"
POLL_SECONDS_PENDING = 2
POLL_SECONDS_IDLE = 5
POLL_SECONDS_IDLE_WATCHDOG = 30
MAX_EVENTS = 50

_lock = threading.RLock()
# Um clipe por vez: o ciclo da thread e o "Verificar agora" da API não
# processam o mesmo arquivo em paralelo. Separado de _lock para que mover um
# clipe grande (entre discos é cópia) não trave o /status.
_process_lock = threading.Lock()
_wake = threading.Event()
_callbacks: list[Callable[[dict[str, Any]], Any]] = []
_events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
_seq = 0

# Estado do vigia, todo protegido por _lock.
_folder: Path | None = None
_folder_key: tuple | None = None
_started_at = 0.0
_baseline: set[str] = set()                         # o que já estava na pasta quando o vigia começou
_pending: dict[str, tuple[tuple[int, float], float]] = {}  # caminho → (assinatura, parada desde)
_failures: dict[str, dict[str, Any]] = {}           # caminho → {erro, assinatura, em}
_last_reason: str | None = None
_thread: threading.Thread | None = None
_observer: "Observer | None" = None
_observer_watch: Any = None


class ClipError(Exception):
    pass


class _NotReady(Exception):
    """O gravador ainda segura o arquivo (Windows): volta para a fila."""


# ------------------------------------------------------------------ config
def folder_key(os_name: str | None = None) -> str:
    return OS_FOLDER_KEYS.get(os_name or machine.current_os(), OS_FOLDER_KEYS[machine.MAC])


def get_config() -> dict[str, Any]:
    stored = safe_read_json(paths.CONFIG_JSON, {}).get(CONFIG_KEY, {})
    cfg = {**DEFAULT_CLIP_CONFIG, **stored}
    cfg["pasta"] = cfg.get(folder_key()) or ""
    return cfg


def _normalize_exts(raw: Any) -> list[str]:
    items = raw.replace(",", " ").split() if isinstance(raw, str) else list(raw or [])
    out = []
    for item in items:
        ext = str(item).strip().lower()
        if not ext:
            continue
        ext = ext if ext.startswith(".") else f".{ext}"
        if ext not in out:
            out.append(ext)
    return out


def set_config(**fields: Any) -> dict[str, Any]:
    """Grava os campos conhecidos. ``pasta`` vai para a chave do sistema atual."""
    config = safe_read_json(paths.CONFIG_JSON, {})
    current = {**DEFAULT_CLIP_CONFIG, **config.get(CONFIG_KEY, {})}
    if "pasta" in fields:
        pasta = str(fields.pop("pasta") or "").strip()
        if pasta and not Path(pasta).expanduser().is_absolute():
            raise ClipError("a pasta de gravação precisa ser um caminho completo")
        current[folder_key()] = str(Path(pasta).expanduser()) if pasta else ""
    for key, value in fields.items():
        if key not in DEFAULT_CLIP_CONFIG or key in OS_FOLDER_KEYS.values():
            continue
        if key == "client_id":
            value = str(value or "")
            if value and not clients_module.get_client(value):
                raise ClipError("cliente não encontrado")
        elif key == "etapa_inicial":
            if value not in STAGES:
                raise ClipError("etapa inválida")
        elif key == "estavel_segundos":
            try:
                value = max(MIN_STABLE_SECONDS, int(value))
            except (TypeError, ValueError):
                raise ClipError("tempo de espera inválido") from None
        elif key == "extensoes":
            value = _normalize_exts(value)
            if not value:
                raise ClipError("informe ao menos uma extensão de vídeo")
        elif key == "tags":
            value = [t.strip() for t in (value.split(",") if isinstance(value, str) else value or []) if str(t).strip()]
        elif key == "nome_modelo":
            value = str(value or "").strip() or DEFAULT_CLIP_CONFIG["nome_modelo"]
        elif key in ("ativo", "processar_existentes", "autocorte"):
            value = bool(value)
        current[key] = value
    config[CONFIG_KEY] = current
    safe_write_json(paths.CONFIG_JSON, config)
    _wake.set()
    return get_config()


def _inactive_reason(cfg: dict[str, Any]) -> str | None:
    if not cfg["ativo"]:
        return "desligado"
    if not cfg["pasta"]:
        return f"sem pasta de gravação definida neste computador ({machine.current_os()})"
    if not Path(cfg["pasta"]).is_dir():
        return f"a pasta de gravação não existe: {cfg['pasta']}"
    client = clients_module.get_client(cfg["client_id"]) if cfg["client_id"] else None
    if not client:
        return "escolha o cliente que recebe os clipes"
    if clients_module.client_dir(client) is None:
        return f"o cliente {client['name']} não tem pasta neste computador"
    return None


# ------------------------------------------------------------------ eventos
def on_clip_ready(callback: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    """Registra quem quer saber que um clipe virou projeto. Recebe o evento
    (ver ``_emit``); uma exceção no callback é logada e não afeta os outros.
    Devolve o próprio callback, então serve de decorador."""
    with _lock:
        if callback not in _callbacks:
            _callbacks.append(callback)
    return callback


def remove_callback(callback: Callable[[dict[str, Any]], Any]) -> None:
    with _lock:
        if callback in _callbacks:
            _callbacks.remove(callback)


def _emit(event: dict[str, Any]) -> dict[str, Any]:
    global _seq
    with _lock:
        _seq += 1
        event = {"seq": _seq, **event}
        _events.append(event)
        callbacks = list(_callbacks)
    for callback in callbacks:
        try:
            callback(dict(event))
        except Exception:
            logger.exception("callback de clipe falhou: %r", callback)
    return event


def events_after(seq: int = 0) -> dict[str, Any]:
    with _lock:
        return {"ultimo": _seq, "eventos": [dict(e) for e in _events if e["seq"] > seq]}


# ------------------------------------------------------------------ varredura
def _signature(path: Path) -> tuple[int, float] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime)


def _is_candidate_name(path: Path, cfg: dict[str, Any]) -> bool:
    return not path.name.startswith(".") and path.suffix.lower() in cfg["extensoes"]


def _list_videos(folder: Path, cfg: dict[str, Any]) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir() if p.is_file() and _is_candidate_name(p, cfg))
    except OSError:
        return []


def _reset_folder(folder: Path | None, cfg: dict[str, Any], now: float) -> None:
    global _folder, _started_at, _baseline
    _folder = folder
    _started_at = now
    _pending.clear()
    _failures.clear()
    _baseline = set()
    if folder is not None and not cfg["processar_existentes"]:
        _baseline = {str(p) for p in _list_videos(folder, cfg)}
    _schedule_observer(folder)


def scan_once(now: float | None = None) -> list[dict[str, Any]]:
    """Um ciclo do vigia. Devolve os eventos ``clipe_pronto`` emitidos.

    Um arquivo entra na fila quando aparece; a cada ciclo a assinatura
    (tamanho, mtime) é comparada com a anterior e, parada por
    ``estavel_segundos``, o clipe é processado. O que já estava na pasta
    quando o vigia começou fica de fora (salvo ``processar_existentes``),
    a menos que continue mudando — é um clipe sendo gravado."""
    global _last_reason, _folder_key
    now = time.time() if now is None else now
    cfg = get_config()
    reason = _inactive_reason(cfg)
    with _lock:
        _last_reason = reason
        folder = None if reason else Path(cfg["pasta"])
        key = (folder, cfg["processar_existentes"], tuple(cfg["extensoes"]))
        if key != _folder_key:
            _folder_key = key
            _reset_folder(folder, cfg, now)
        if folder is None:
            return []
        ready = _collect_ready(folder, cfg, now)

    emitted = []
    for path in ready:
        with _process_lock:
            try:
                emitted.append(process_clip(path, cfg))
            except _NotReady:
                sig = _signature(path)
                if sig:
                    with _lock:
                        _pending[str(path)] = (sig, now)
            except Exception as exc:
                logger.exception("clipe %s não virou projeto", path)
                sig = _signature(path)
                with _lock:
                    _failures[str(path)] = {
                        "arquivo": path.name,
                        "erro": str(exc) or exc.__class__.__name__,
                        "assinatura": list(sig) if sig else [0, 0],
                        "em": datetime.now().isoformat(timespec="seconds"),
                    }
    return emitted


def _collect_ready(folder: Path, cfg: dict[str, Any], now: float) -> list[Path]:
    """Atualiza a fila com o que está na pasta e tira dela o que já parou de
    mudar. Chamado com _lock."""
    seen = set()
    ready = []
    for path in _list_videos(folder, cfg):
        key = str(path)
        sig = _signature(path)
        if sig is None:
            continue
        if key in _baseline:
            if sig[1] < _started_at:
                continue
            _baseline.discard(key)
        seen.add(key)
        failure = _failures.get(key)
        if failure and tuple(failure["assinatura"]) == sig:
            continue
        _failures.pop(key, None)
        previous = _pending.get(key)
        if not previous or previous[0] != sig:
            _pending[key] = (sig, now)
            continue
        if sig[0] > 0 and now - previous[1] >= cfg["estavel_segundos"]:
            ready.append(path)
            _pending.pop(key)
    for key in list(_pending):
        if key not in seen:
            _pending.pop(key)
    return ready


def _project_name(path: Path, cfg: dict[str, Any]) -> str:
    sig = _signature(path)
    when = datetime.fromtimestamp(sig[1]) if sig else datetime.now()
    values = {"arquivo": path.stem, "data": when.strftime("%Y-%m-%d"), "hora": when.strftime("%Hh%M")}

    class _Keep(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        name = cfg["nome_modelo"].format_map(_Keep(values))
    except (ValueError, IndexError):
        name = cfg["nome_modelo"]
    return name.strip() or path.stem


def process_clip(path: Path, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Clipe pronto → projeto novo com ele em Brutos/ → evento ``clipe_pronto``.

    Se qualquer passo falhar o clipe volta ao nome e lugar originais e o
    projeto recém-criado (pasta vazia + registro) é desfeito."""
    cfg = cfg or get_config()
    client_id = cfg["client_id"]
    original = Path(path)
    name = _project_name(original, cfg)
    claimed = original.with_name(f"{CLAIM_PREFIX}{uuid.uuid4().hex[:8]}-{original.name}")
    try:
        original.rename(claimed)
    except PermissionError as exc:
        raise _NotReady(str(exc)) from exc
    except FileNotFoundError as exc:
        raise ClipError("o clipe sumiu da pasta antes de ser processado") from exc

    project = None
    try:
        project = projects_module.create_project(
            client_id,
            name,
            tags=list(cfg.get("tags") or []),
            initial_stage=cfg["etapa_inicial"],
            criar_pasta=True,
        )
        project_dir = Path(project["path"])
        brutos = project_dir / "Brutos"
        brutos.mkdir(exist_ok=True)
        dest = brutos / original.name
        size = claimed.stat().st_size
        shutil.move(str(claimed), str(dest))
    except BaseException:
        if claimed.exists():
            try:
                claimed.rename(original)
            except OSError:
                logger.exception("não consegui devolver o clipe %s ao nome original", claimed)
        if project is not None:
            _undo_project(project)
        raise

    rel = dest.relative_to(project_dir).as_posix()
    received_at = datetime.now().isoformat(timespec="seconds")
    midia = {
        "arquivo": rel,
        "nome_original": original.name,
        "pasta_origem": str(original.parent),
        "tamanho": size,
        "recebido_em": received_at,
        "host": machine.hostname(),
    }

    def apply(rec: dict[str, Any]) -> None:
        rec["midia_original"] = midia

    store.update(project["id"], apply)
    history.append_event(project["id"], "clipe_recebido", {"arquivo": rel, "origem": str(original)})
    client = clients_module.get_client(client_id) or {}
    logger.info("clipe %s → projeto %s", original.name, project["id"])
    return _emit({
        "tipo": EVENT,
        "project_id": project["id"],
        "client_id": client_id,
        "cliente_nome": client.get("name", client_id),
        "projeto_nome": project["name"],
        "folder_name": project["folder_name"],
        "video": str(dest),
        "video_rel": rel,
        "em": received_at,
    })


def _undo_project(project: dict[str, Any]) -> None:
    project_dir = Path(project["path"]) if project.get("path") else None
    try:
        if project_dir is not None and project_dir.is_dir():
            shutil.rmtree(project_dir)
        projects_module.remove_project_record(project["id"])
    except Exception:
        logger.exception("não consegui desfazer o projeto %s", project.get("id"))


def retry_failures() -> None:
    """Esquece as falhas: os clipes voltam para a fila no próximo ciclo."""
    with _lock:
        _failures.clear()
    _wake.set()


# ------------------------------------------------------------------ status
def status() -> dict[str, Any]:
    cfg = get_config()
    with _lock:
        pending = [
            {"arquivo": Path(k).name, "tamanho": sig[0], "parado_ha": round(time.time() - since, 1)}
            for k, (sig, since) in _pending.items()
        ]
        failures = [{k: v for k, v in f.items() if k != "assinatura"} for f in _failures.values()]
        recent = [dict(e) for e in list(_events)[-10:]][::-1]
        reason = _last_reason if _thread is not None else _inactive_reason(cfg)
    return {
        "watchdog_disponivel": WATCHDOG_AVAILABLE,
        "rodando": _thread is not None and _thread.is_alive(),
        "vigiando": reason is None and _thread is not None,
        "motivo": reason,
        "pasta": cfg["pasta"],
        "pendentes": pending,
        "falhas": failures,
        "recentes": recent,
        "ultimo": _seq,
        "config": cfg,
    }


# ------------------------------------------------------------------ thread
if WATCHDOG_AVAILABLE:

    class _WakeHandler(FileSystemEventHandler):
        def on_any_event(self, event: Any) -> None:
            if not event.is_directory:
                _wake.set()


def _schedule_observer(folder: Path | None) -> None:
    global _observer_watch
    if _observer is None:
        return
    if _observer_watch is not None:
        try:
            _observer.unschedule(_observer_watch)
        except Exception:
            pass
        _observer_watch = None
    if folder is not None:
        try:
            _observer_watch = _observer.schedule(_WakeHandler(), str(folder), recursive=False)
        except OSError:
            logger.warning("watchdog não conseguiu vigiar %s; seguindo só com a leitura periódica", folder)


def start_watcher() -> threading.Thread:
    global _thread, _observer
    if _thread is not None and _thread.is_alive():
        return _thread
    if WATCHDOG_AVAILABLE:
        _observer = Observer()
        _observer.daemon = True
        _observer.start()

    def loop() -> None:
        while True:
            try:
                scan_once()
            except Exception:
                logger.exception("ciclo do vigia de clipes falhou")
            with _lock:
                busy = bool(_pending)
            idle = POLL_SECONDS_IDLE_WATCHDOG if _observer_watch is not None else POLL_SECONDS_IDLE
            _wake.wait(POLL_SECONDS_PENDING if busy else idle)
            _wake.clear()

    _thread = threading.Thread(target=loop, daemon=True, name="clip-watcher")
    _thread.start()
    return _thread


def reset() -> None:
    """Zera o estado do módulo (testes)."""
    global _seq, _folder, _folder_key, _started_at, _baseline, _last_reason
    with _lock:
        _folder_key = None
        _callbacks.clear()
        _events.clear()
        _seq = 0
        _folder = None
        _started_at = 0.0
        _baseline = set()
        _pending.clear()
        _failures.clear()
        _last_reason = None
