import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import clients as clients_module
from . import paths
from . import projects as projects_module
from . import timer
from .persistence import safe_read_json, safe_write_json
from .projects import PROJECT_DIR_RE

logger = logging.getLogger("clientela.automation")

try:
    from pynput import keyboard, mouse

    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False
    logger.warning("pynput not installed — auto-pause on inactivity disabled")

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    logger.warning("watchdog not installed — auto-start on file activity disabled")

try:
    # Vem junto com o pynput no macOS (pyobjc). Responde direto se o processo
    # tem permissao de Acessibilidade, sem precisar esperar por um evento.
    from ApplicationServices import AXIsProcessTrusted

    AX_TRUST_PROBE_AVAILABLE = True
except ImportError:
    AX_TRUST_PROBE_AVAILABLE = False

DEFAULT_AUTOMATION_CONFIG = {
    "auto_pause_enabled": True,
    "auto_pause_idle_minutes": 15,
    # Desligado por padrao: o autosave do Premiere grava dentro da pasta do
    # projeto a cada poucos minutos e religava o cronometro sozinho. Quem manda
    # no cronometro agora e a etapa (ver `auto_timer_on_producao` em stages.py).
    "auto_start_enabled": False,
    "auto_timer_on_producao": True,
}

INACTIVITY_CHECK_INTERVAL_SECONDS = 20
WATCH_SYNC_INTERVAL_SECONDS = 30
_IGNORED_DIR_NAMES = {"node_modules"}

_last_activity = time.time()
# Prova positiva de que o pynput esta mesmo entregando eventos. Sem permissao de
# Acessibilidade no macOS os listeners sobem, ficam `is_alive()`, nao logam nada
# e nunca disparam o callback — `_last_activity` congelava no import e a
# auto-pausa passava a matar qualquer cronometro a cada 20s.
_activity_seen = False
_untrusted_warned = False


def get_automation_config() -> dict[str, Any]:
    config = safe_read_json(paths.CONFIG_JSON, {})
    return {**DEFAULT_AUTOMATION_CONFIG, **config.get("timer_automation", {})}


def set_automation_config(**fields: Any) -> dict[str, Any]:
    config = safe_read_json(paths.CONFIG_JSON, {})
    automation = {**DEFAULT_AUTOMATION_CONFIG, **config.get("timer_automation", {}), **fields}
    config["timer_automation"] = automation
    safe_write_json(paths.CONFIG_JSON, config)
    return automation


def _on_activity(*_args: Any, **_kwargs: Any) -> None:
    global _last_activity, _activity_seen
    _last_activity = time.time()
    _activity_seen = True


def input_monitoring_ok() -> bool | None:
    """O monitoramento de teclado/mouse esta mesmo funcionando?

    True  = certeza que sim (ja vimos um evento, ou o macOS confirma a permissao)
    False = certeza que nao (macOS diz que o processo nao e confiavel)
    None  = nao da para saber ainda — tratamos como "nao" na hora de pausar.
    """
    if not PYNPUT_AVAILABLE:
        return False
    if _activity_seen:
        return True
    if AX_TRUST_PROBE_AVAILABLE:
        return bool(AXIsProcessTrusted())
    return None


def automation_status() -> dict[str, Any]:
    monitoring = input_monitoring_ok()
    return {
        "pynput_available": PYNPUT_AVAILABLE,
        "watchdog_available": WATCHDOG_AVAILABLE,
        "input_monitoring_ok": monitoring,
        # A auto-pausa so age com prova positiva; sem isso ela fica inerte de
        # proposito, em vez de picotar o cronometro.
        "auto_pause_armed": monitoring is True,
        "config": get_automation_config(),
    }


def _warn_untrusted_once() -> None:
    global _untrusted_warned
    if _untrusted_warned:
        return
    _untrusted_warned = True
    logger.warning(
        "auto-pause inerte: sem permissao de Acessibilidade, o pynput nao entrega "
        "eventos de teclado/mouse. Ajustes > Privacidade e Seguranca > Acessibilidade."
    )


def start_inactivity_watcher() -> threading.Thread | None:
    if not PYNPUT_AVAILABLE:
        return None

    mouse.Listener(on_move=_on_activity, on_click=_on_activity, on_scroll=_on_activity).start()
    keyboard.Listener(on_press=_on_activity).start()

    def loop() -> None:
        while True:
            time.sleep(INACTIVITY_CHECK_INTERVAL_SECONDS)
            cfg = get_automation_config()
            if not cfg["auto_pause_enabled"]:
                continue
            if input_monitoring_ok() is not True:
                _warn_untrusted_once()
                continue
            idle_seconds = time.time() - _last_activity
            if idle_seconds > cfg["auto_pause_idle_minutes"] * 60:
                active = timer.get_active()
                if active:
                    timer.pause_timer(active["project_id"])

    thread = threading.Thread(target=loop, daemon=True, name="inactivity-watcher")
    thread.start()
    return thread


def _resolve_project_id(path: Path) -> str | None:
    for client in clients_module.list_clients():
        client_path = clients_module.client_dir(client)
        if client_path is None:
            continue
        try:
            rel = path.relative_to(client_path)
        except ValueError:
            continue
        if not rel.parts:
            continue
        m = PROJECT_DIR_RE.match(rel.parts[0])
        if m:
            return f"{client['id']}:{m.group(1)}"
    return None


if WATCHDOG_AVAILABLE:

    class _ProjectActivityHandler(FileSystemEventHandler):
        def on_created(self, event: Any) -> None:
            self._handle(event)

        def on_modified(self, event: Any) -> None:
            self._handle(event)

        def _handle(self, event: Any) -> None:
            if event.is_directory:
                return
            path = Path(event.src_path)
            if path.name.startswith(".") or _IGNORED_DIR_NAMES & set(path.parts):
                return
            cfg = get_automation_config()
            if not cfg["auto_start_enabled"]:
                return
            project_id = _resolve_project_id(path)
            if not project_id:
                return
            active = timer.get_active()
            if active and active.get("project_id") == project_id:
                return
            timer.start_timer(project_id)


_observer: "Observer | None" = None
_watches: dict[str, Any] = {}


def _sync_watches() -> None:
    if _observer is None:
        return
    current = {
        client["path"]: client
        for client in clients_module.list_clients()
        if clients_module.folder_available(client)
    }
    for path in list(_watches.keys()):
        if path not in current:
            _observer.unschedule(_watches.pop(path))
    handler = _ProjectActivityHandler()
    for path in current:
        if path not in _watches:
            try:
                _watches[path] = _observer.schedule(handler, path, recursive=True)
            except OSError:
                continue


def start_folder_watcher() -> threading.Thread | None:
    global _observer
    if not WATCHDOG_AVAILABLE:
        return None

    _observer = Observer()
    _observer.start()

    def loop() -> None:
        while True:
            _sync_watches()
            time.sleep(WATCH_SYNC_INTERVAL_SECONDS)

    thread = threading.Thread(target=loop, daemon=True, name="folder-watch-sync")
    thread.start()
    return thread


def start_all() -> None:
    start_inactivity_watcher()
    start_folder_watcher()
