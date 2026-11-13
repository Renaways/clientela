"""Verificação do ambiente na abertura do app.

Até aqui cada módulo descobria sozinho, e só na hora do uso, que faltava uma
ferramenta: o yt-dlp no import de downloads.py, o ffmpeg quando a prévia do
estagiário já estava sendo montada, o `npx` quando o render falhava no meio do
job. Este módulo junta essas checagens num registro só, roda tudo numa thread
assim que o app sobe e entrega um relatório que o frontend lê para avisar e
bloquear o que não vai funcionar.

Dois registros, e é só neles que se mexe para crescer:

- ``FEATURES``: os recursos do app que podem ficar indisponíveis, com o rótulo
  que o usuário vê e os ``data-action`` do frontend que o recurso dispara. As
  rotas do backend se marcam com ``@_requires("<recurso>")`` em api.py.
- ``DEPENDENCIES``: cada ferramenta, de que tipo ela é (executável no PATH,
  módulo Python ou uma checagem própria), se é obrigatória e quais recursos ela
  alimenta.

Obrigatória que falta → o recurso fica **bloqueado** e o frontend abre o modal
com a lista. Opcional que falta → o recurso fica **desativado** e o aviso é só
um toast. Nos dois casos as ações do recurso deixam de rodar.

Sem mexer no código dá para acrescentar ferramentas ou mudar a classificação
das padrão em ``data/dependencies.json`` (ver ``load_registry``).
"""

from __future__ import annotations

import functools
import importlib
import importlib.metadata
import logging
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from . import editor, machine, paths
from .persistence import safe_read_json

logger = logging.getLogger("clientela.startup")

KIND_EXECUTABLE = "executable"
KIND_PYTHON = "python"
KIND_CUSTOM = "custom"
# Do data/dependencies.json só vêm os tipos declarativos: uma checagem própria
# é uma função, e função só existe no código.
_JSON_KINDS = (KIND_EXECUTABLE, KIND_PYTHON)

LEVEL_OK = "ok"
LEVEL_BLOCKED = "blocked"     # falta obrigatória
LEVEL_DISABLED = "disabled"   # falta só opcional

VERSION_TIMEOUT = 5  # s; `node --version` frio leva ~0,1s, isto é só o teto


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    # data-action do frontend que este recurso dispara: clicar num deles com o
    # recurso fora do ar vira um aviso em vez da ação.
    actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dependency:
    key: str
    label: str
    kind: str
    target: str = ""                       # executável ou módulo Python
    required: bool = False
    features: tuple[str, ...] = ()
    hint: str = ""                         # como instalar no Mac (e no Linux), mostrado ao usuário
    hint_windows: str = ""                 # como instalar no Windows; vazio = o mesmo hint
    url: str = ""                          # página oficial com as instruções
    nota: str = ""                         # passo depois de instalar, se houver
    version_args: tuple[str, ...] = ()     # executável: argumentos que imprimem a versão
    # KIND_CUSTOM: devolve {"ok", "path"?, "version"?, "detail"?}
    check: Callable[[], dict[str, Any]] | None = field(default=None, compare=False)


def _check_claude() -> dict[str, Any]:
    # Respeita o `claude_path` da configuração do Editor, que o which não vê.
    binary = editor.find_claude()
    if binary:
        return {"ok": True, "path": binary}
    return {"ok": False, "detail": "não encontrado no PATH nem no caminho configurado no Editor"}


FEATURES: dict[str, Feature] = {f.key: f for f in (
    Feature(
        "editor",
        "Estagiário de edição (gerar, ajustar e pré-visualizar motion)",
        actions=(
            "editor-generate", "editor-ajuste-enviar", "editor-elemento", "acao-editor-gerar",
            "ident-analyze", "ident-templates", "ident-amostras",
        ),
    ),
    Feature("editor_ia", "Tarefas com IA do estagiário (inserts, motion curto, análise de identidade)"),
    Feature("downloads", "Download de vídeos (YouTube, Instagram…)", actions=("download-start",)),
    Feature("auto_pause", "Auto-pausa do cronômetro por inatividade"),
    Feature("file_activity", "Início do cronômetro por atividade nos arquivos"),
)}

# O pip do próprio Python que roda o app: no Mac é o .venv, no Windows o .venv-win.
_PIP = f'"{sys.executable}" -m pip install'

DEPENDENCIES: list[Dependency] = [
    # O kit do estagiário extrai áudio, compõe prévias e renderiza com ffmpeg, e
    # o HyperFrames (render, transcrição) roda via npx.
    Dependency("ffmpeg", "FFmpeg", KIND_EXECUTABLE, "ffmpeg", required=True,
               features=("editor",), hint="brew install ffmpeg", hint_windows="winget install Gyan.FFmpeg",
               url="https://ffmpeg.org/download.html", version_args=("-version",)),
    Dependency("ffprobe", "FFprobe", KIND_EXECUTABLE, "ffprobe", required=True,
               features=("editor",), hint="brew install ffmpeg", hint_windows="winget install Gyan.FFmpeg",
               url="https://ffmpeg.org/download.html", version_args=("-version",)),
    Dependency("node", "Node.js", KIND_EXECUTABLE, "node", required=True,
               features=("editor",), hint="brew install node", hint_windows="winget install OpenJS.NodeJS.LTS",
               url="https://nodejs.org/pt/download", version_args=("--version",)),
    Dependency("npx", "npx", KIND_EXECUTABLE, "npx", required=True,
               features=("editor",), hint="brew install node", hint_windows="winget install OpenJS.NodeJS.LTS",
               url="https://nodejs.org/pt/download", version_args=("--version",)),
    Dependency("claude", "Claude Code CLI", KIND_CUSTOM, required=False,
               features=("editor_ia",),
               hint="curl -fsSL https://claude.ai/install.sh | bash",
               hint_windows="irm https://claude.ai/install.ps1 | iex",
               url="https://docs.claude.com/pt/docs/claude-code/setup",
               nota="Depois de instalar, abra o terminal e rode `claude` uma vez para entrar na sua conta.",
               check=_check_claude),
    Dependency("yt-dlp", "yt-dlp", KIND_PYTHON, "yt_dlp", required=False,
               features=("downloads",), hint=f"{_PIP} \"yt-dlp[default]\"",
               url="https://github.com/yt-dlp/yt-dlp#installation"),
    Dependency("pynput", "pynput", KIND_PYTHON, "pynput", required=False,
               features=("auto_pause",), hint=f"{_PIP} pynput"),
    Dependency("watchdog", "watchdog", KIND_PYTHON, "watchdog", required=False,
               features=("file_activity",), hint=f"{_PIP} watchdog"),
]


def register_feature(feature: Feature) -> None:
    FEATURES[feature.key] = feature


def register(dep: Dependency) -> None:
    """Acrescenta uma dependência ao registro (ou troca a de mesma chave)."""
    DEPENDENCIES[:] = [d for d in DEPENDENCIES if d.key != dep.key] + [dep]


# ---------------------------------------------------------------- registro
def _tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in (value or ()))


def load_registry() -> tuple[list[Dependency], dict[str, Feature]]:
    """O registro do código mais o que ``data/dependencies.json`` acrescenta.

    Formato do arquivo (tudo opcional)::

        {
          "features": {"legendas_asr": {"label": "...", "actions": ["..."]}},
          "dependencies": [
            {"key": "whisper", "label": "Whisper", "kind": "executable",
             "target": "whisper", "required": false, "features": ["legendas_asr"],
             "hint": "pip install openai-whisper", "version_args": ["--help"]},
            {"key": "node", "required": false}
          ]
        }

    Uma entrada com a chave de uma dependência que já existe só ajusta os campos
    que trouxer (o exemplo acima rebaixa o Node a opcional); uma chave nova
    precisa de ``kind`` executable ou python e de ``target``.
    """
    deps = list(DEPENDENCIES)
    features = dict(FEATURES)
    raw = safe_read_json(paths.DEPENDENCIES_JSON, {})
    if not isinstance(raw, dict):
        return deps, features

    for key, spec in (raw.get("features") or {}).items():
        if not isinstance(spec, dict):
            continue
        base = features.get(key) or Feature(key, key)
        features[key] = replace(
            base,
            label=str(spec.get("label") or base.label),
            actions=_tuple(spec["actions"]) if "actions" in spec else base.actions,
        )

    by_key = {d.key: i for i, d in enumerate(deps)}
    for spec in raw.get("dependencies") or []:
        if not isinstance(spec, dict) or not spec.get("key"):
            logger.warning("dependencies.json: entrada sem key ignorada: %r", spec)
            continue
        key = str(spec["key"])
        changes: dict[str, Any] = {}
        for name in ("label", "target", "hint", "hint_windows", "url", "nota"):
            if name in spec:
                changes[name] = str(spec[name])
        if "required" in spec:
            changes["required"] = bool(spec["required"])
        for name in ("features", "version_args"):
            if name in spec:
                changes[name] = _tuple(spec[name])
        if key in by_key:
            if "kind" in spec and spec["kind"] != deps[by_key[key]].kind:
                logger.warning("dependencies.json: %s não pode trocar de tipo, ignorado", key)
            deps[by_key[key]] = replace(deps[by_key[key]], **changes)
            continue
        kind = spec.get("kind")
        if kind not in _JSON_KINDS or not changes.get("target"):
            logger.warning("dependencies.json: %s precisa de kind (%s) e target", key, "/".join(_JSON_KINDS))
            continue
        deps.append(Dependency(key=key, label=changes.pop("label", key), kind=kind, **changes))
        by_key[key] = len(deps) - 1

    return deps, features


# ---------------------------------------------------------------- checagens
def _first_line(text: str | None) -> str | None:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return None


def _check_executable(dep: Dependency) -> dict[str, Any]:
    # O mesmo PATH alargado que o estagiário usa: aberto pelo Clientela.app o
    # PATH chega só com /usr/bin:/bin e o Homebrew ficaria de fora.
    binary = shutil.which(dep.target, path=editor._search_path())
    if not binary:
        return {"ok": False, "detail": f"`{dep.target}` não encontrado no PATH"}
    if not dep.version_args:
        return {"ok": True, "path": binary}
    # Achar o binário não basta: um Node quebrado por upgrade do Homebrew
    # (libicu trocada) existe no PATH e morre ao abrir.
    try:
        out = subprocess.run(
            [binary, *dep.version_args], capture_output=True, text=True,
            timeout=VERSION_TIMEOUT, env=editor.child_env(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "path": binary, "detail": f"`{dep.target}` não respondeu em {VERSION_TIMEOUT}s"}
    except OSError as exc:
        return {"ok": False, "path": binary, "detail": f"`{dep.target}` não executa: {exc}"}
    if out.returncode != 0:
        erro = _first_line(out.stderr) or f"código {out.returncode}"
        return {"ok": False, "path": binary, "detail": f"`{dep.target}` encontrado, mas falhou: {erro}"}
    return {"ok": True, "path": binary, "version": _first_line(out.stdout) or _first_line(out.stderr)}


@functools.lru_cache(maxsize=1)
def _packages_distributions() -> dict[str, list[str]]:
    # Varre todos os pacotes instalados; uma vez por processo basta.
    return importlib.metadata.packages_distributions()


def _python_version(module_name: str, module: Any) -> str | None:
    # O nome do módulo nem sempre é o do pacote (yt_dlp vem de yt-dlp).
    top = module_name.split(".")[0]
    for dist in _packages_distributions().get(top, []):
        try:
            return importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            continue
    version = getattr(module, "__version__", None)
    return str(version) if version else None


def _check_python(dep: Dependency) -> dict[str, Any]:
    # Import de verdade, não find_spec: um pacote instalado pela metade é
    # encontrado e quebra no import, e é o import que os módulos fazem.
    try:
        module = importlib.import_module(dep.target)
    except Exception as exc:  # ImportError e o que o __init__ do pacote levantar
        return {"ok": False, "detail": f"import {dep.target} falhou: {exc}"}
    return {"ok": True, "path": getattr(module, "__file__", None), "version": _python_version(dep.target, module)}


_CHECKERS: dict[str, Callable[[Dependency], dict[str, Any]]] = {
    KIND_EXECUTABLE: _check_executable,
    KIND_PYTHON: _check_python,
    KIND_CUSTOM: lambda dep: dep.check() if dep.check else {"ok": False, "detail": "sem função de checagem"},
}


def check_dependency(dep: Dependency) -> dict[str, Any]:
    checker = _CHECKERS.get(dep.kind)
    try:
        result = checker(dep) if checker else {"ok": False, "detail": f"tipo desconhecido: {dep.kind}"}
    except Exception as exc:
        # Uma checagem com defeito vira "faltando", nunca derruba a verificação inteira.
        logger.exception("checagem de %s falhou", dep.key)
        result = {"ok": False, "detail": f"a checagem falhou: {exc}"}
    return {
        "key": dep.key,
        "label": dep.label,
        "kind": dep.kind,
        "target": dep.target,
        "required": dep.required,
        "features": list(dep.features),
        "hint": dep.hint_windows if machine.current_os() == machine.WINDOWS and dep.hint_windows else dep.hint,
        "url": dep.url,
        "nota": dep.nota,
        "ok": bool(result.get("ok")),
        "path": result.get("path"),
        "version": result.get("version"),
        "detail": result.get("detail"),
    }


def run_checks(deps: list[Dependency] | None = None,
               features: dict[str, Feature] | None = None) -> dict[str, Any]:
    """Checa tudo (em paralelo: cada `--version` é um processo) e monta o relatório."""
    if deps is None or features is None:
        loaded_deps, loaded_features = load_registry()
        deps = loaded_deps if deps is None else deps
        features = loaded_features if features is None else features
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(deps)))) as pool:
        results = list(pool.map(check_dependency, deps))

    feature_report: dict[str, dict[str, Any]] = {}
    for key in dict.fromkeys(k for r in results for k in r["features"]):
        feature = features.get(key) or Feature(key, key)
        missing = [r for r in results if key in r["features"] and not r["ok"]]
        if any(r["required"] for r in missing):
            level = LEVEL_BLOCKED
        elif missing:
            level = LEVEL_DISABLED
        else:
            level = LEVEL_OK
        feature_report[key] = {
            "label": feature.label,
            "actions": list(feature.actions),
            "level": level,
            "available": level == LEVEL_OK,
            "missing": [r["key"] for r in missing],
        }

    return {
        "status": "done",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "dependencies": results,
        "missing_required": [r["key"] for r in results if r["required"] and not r["ok"]],
        "missing_optional": [r["key"] for r in results if not r["required"] and not r["ok"]],
        "features": feature_report,
    }


# ---------------------------------------------------------------- estado do app
_lock = threading.Lock()
_report: dict[str, Any] | None = None
_thread: threading.Thread | None = None


def _store(report: dict[str, Any]) -> None:
    global _report
    with _lock:
        _report = report
    faltando = report["missing_required"] + report["missing_optional"]
    if faltando:
        logger.warning("ambiente: faltando %s (obrigatórias: %s)",
                       ", ".join(faltando), ", ".join(report["missing_required"]) or "nenhuma")
    else:
        logger.info("ambiente: todas as %d dependências ok (%d ms)",
                    len(report["dependencies"]), report["duration_ms"])


def _run_in_background() -> None:
    try:
        _store(run_checks())
    except Exception:
        logger.exception("verificação de ambiente falhou")


def start_background_check() -> threading.Thread:
    """Dispara a verificação numa thread daemon; não espera. Chamado no boot."""
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return _thread
        _thread = threading.Thread(target=_run_in_background, name="startup-check", daemon=True)
        _thread.start()
        return _thread


def recheck() -> dict[str, Any]:
    """Roda de novo, agora no próprio request: é o botão "Verificar de novo"."""
    report = run_checks()
    _store(report)
    return report


def status() -> dict[str, Any]:
    with _lock:
        if _report is not None:
            return _report
        running = bool(_thread and _thread.is_alive())
    return {"status": "running" if running else "pending"}


def reset() -> None:
    global _report, _thread
    with _lock:
        _report = None
        _thread = None


def feature_problem(feature_key: str) -> dict[str, Any] | None:
    """O motivo de o recurso estar fora do ar, ou None se ele pode rodar.

    Enquanto a verificação não terminou nada é bloqueado: na dúvida o recurso
    roda e, se faltar algo, falha do jeito que já falhava antes."""
    with _lock:
        report = _report
    if not report:
        return None
    feature = report["features"].get(feature_key)
    if not feature or feature["available"]:
        return None
    deps = {d["key"]: d for d in report["dependencies"]}
    missing = [deps[k] for k in feature["missing"] if k in deps]
    nomes = ", ".join(d["label"] for d in missing)
    return {
        "feature": feature_key,
        "level": feature["level"],
        "message": f"{feature['label']} indisponível: falta {nomes}",
        "missing": [{"key": d["key"], "label": d["label"], "hint": d["hint"]} for d in missing],
    }
