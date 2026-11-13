"""Integração com o sistema: abrir arquivo, mostrar no Finder/Explorer,
seletor de pasta/arquivo, navegadores. Uma implementação por sistema —
macOS (`open`, AppleScript) e Windows (`explorer`, `os.startfile`, diálogos
do Windows Forms via PowerShell). Em outro sistema as funções devolvem
False/None/[] e a tela trata como "não disponível"."""

import os
import subprocess
import sys
from pathlib import Path

# (nome do app no macOS, nome exibido, chave que o yt-dlp entende em
# --cookies-from-browser). A terceira coluna existe pra que só o backend
# conheça o vocabulário do yt-dlp: a tela monta o seletor com o que vier daqui.
_KNOWN_BROWSERS = [
    ("Google Chrome", "Google Chrome", "chrome"),
    ("Safari", "Safari", "safari"),
    ("Firefox", "Firefox", "firefox"),
    ("Microsoft Edge", "Microsoft Edge", "edge"),
    ("Brave Browser", "Brave Browser", "brave"),
    ("Opera", "Opera", "opera"),
    ("Vivaldi", "Vivaldi", "vivaldi"),
]

# Onde cada navegador costuma estar instalado no Windows, relativo a
# %ProgramFiles%, %ProgramFiles(x86)% ou %LocalAppData% (instalação por
# usuário). Chave = nome do app em _KNOWN_BROWSERS, que é o que a tela manda
# de volta em open_url.
_WINDOWS_BROWSER_EXES = {
    "Google Chrome": [r"Google\Chrome\Application\chrome.exe"],
    "Firefox": [r"Mozilla Firefox\firefox.exe"],
    "Microsoft Edge": [r"Microsoft\Edge\Application\msedge.exe"],
    "Brave Browser": [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
    "Opera": [r"Programs\Opera\opera.exe", r"Opera\opera.exe"],
    "Vivaldi": [r"Vivaldi\Application\vivaldi.exe"],
}

_IS_MAC = sys.platform == "darwin"
_IS_WINDOWS = sys.platform.startswith("win")


# ------------------------------------------------------------------- Windows
def _powershell(script: str, env_extra: dict[str, str]) -> str | None:
    """Roda um script PowerShell em STA (os diálogos do Windows Forms exigem)
    e devolve o stdout. Texto do usuário (título, pasta inicial) vai por
    variável de ambiente, nunca interpolado no script."""
    env = {**os.environ, **env_extra}
    prelude = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; "
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", prelude + script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# Dono do diálogo sempre no topo: sem ele o seletor abre atrás do navegador.
_PS_OWNER = "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}; "


def _windows_browser_exe(app_name: str) -> str | None:
    bases = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LocalAppData"),
    ]
    for rel in _WINDOWS_BROWSER_EXES.get(app_name, []):
        for base in bases:
            if base and (Path(base) / rel).is_file():
                return str(Path(base) / rel)
    return None


# ------------------------------------------------------------------ comuns
def reveal_in_finder(path: str) -> bool:
    """Mostra o arquivo (ou a pasta) selecionado no Finder / Explorer."""
    if _IS_MAC:
        try:
            subprocess.run(["open", "-R", path], check=True)
            return True
        except (subprocess.CalledProcessError, OSError):
            return False
    if _IS_WINDOWS:
        if not Path(path).exists():
            return False
        try:
            # O explorer devolve 1 mesmo quando abre: não dá pra usar check=True.
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
            return True
        except OSError:
            return False
    return False


def move_to_trash(path: str) -> bool:
    """Manda a pasta/arquivo pra Lixeira (macOS) / Lixeira do Windows, de onde
    dá pra recuperar. True só se o caminho sumiu de onde estava."""
    if _IS_MAC:
        # NSFileManager (pyobjc, que vem com o pynput) não pede permissão de
        # Automação; o AppleScript do Finder é só o plano B.
        try:
            from Foundation import NSFileManager, NSURL

            NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
                NSURL.fileURLWithPath_(path), None, None
            )
        except ImportError:
            try:
                subprocess.run(
                    ["osascript", "-e", "on run argv", "-e", "set alvo to POSIX file (item 1 of argv)",
                     "-e", 'tell application "Finder" to delete alvo', "-e", "end run", path],
                    check=True, capture_output=True, timeout=120,
                )
            except (subprocess.SubprocessError, OSError):
                return False
    elif _IS_WINDOWS:
        script = (
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            "$p = $env:CLIENTELA_ALVO; "
            "if (Test-Path -LiteralPath $p -PathType Container) "
            "{ [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p, 'OnlyErrorDialogs', 'SendToRecycleBin') } "
            "else { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p, 'OnlyErrorDialogs', 'SendToRecycleBin') }; 'ok'"
        )
        if _powershell(script, {"CLIENTELA_ALVO": os.path.normpath(path)}) != "ok":
            return False
    else:
        return False
    return not Path(path).exists()


def open_with_default_app(path: str) -> bool:
    if _IS_MAC:
        try:
            subprocess.run(["open", path], check=True)
            return True
        except (subprocess.CalledProcessError, OSError):
            return False
    if _IS_WINDOWS:
        try:
            os.startfile(os.path.normpath(path))  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    return False


def pick_folder_dialog(prompt: str = "Escolha a pasta") -> str | None:
    """Seletor de pasta nativo. None se o sistema não tem um ou o usuário
    cancelou."""
    if _IS_WINDOWS:
        script = (
            _PS_OWNER
            + "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            + "$d.Description = $env:CLIENTELA_PROMPT; $d.ShowNewFolderButton = $true; "
            + "if ($d.ShowDialog($owner) -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        return _powershell(script, {"CLIENTELA_PROMPT": prompt})
    if not _IS_MAC:
        return None
    script = f"POSIX path of (choose folder with prompt {_applescript_str(prompt)})"
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=300
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    return path or None


def _applescript_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pick_files_dialog(prompt: str = "Escolha os arquivos", start_dir: str | None = None) -> list[str]:
    """Seletor nativo com seleção múltipla (os takes de uma sequência). Lista vazia se cancelou."""
    start = start_dir if start_dir and Path(start_dir).is_dir() else ""
    if _IS_WINDOWS:
        script = (
            _PS_OWNER
            + "$d = New-Object System.Windows.Forms.OpenFileDialog; $d.Multiselect = $true; "
            + "$d.Title = $env:CLIENTELA_PROMPT; "
            + "if ($env:CLIENTELA_START) { $d.InitialDirectory = $env:CLIENTELA_START }; "
            + "if ($d.ShowDialog($owner) -eq 'OK') { $d.FileNames | ForEach-Object { Write-Output $_ } }"
        )
        out = _powershell(script, {"CLIENTELA_PROMPT": prompt, "CLIENTELA_START": start})
        return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not _IS_MAC:
        return []
    escolha = f"choose file with prompt {_applescript_str(prompt)} with multiple selections allowed"
    if start:
        escolha += f" default location (POSIX file {_applescript_str(start)})"
    script = (f"set fs to ({escolha})\nset out to \"\"\n"
              "repeat with f in fs\nset out to out & POSIX path of f & linefeed\nend repeat\nreturn out")
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def pick_file_dialog(prompt: str = "Escolha o arquivo", start_dir: str | None = None) -> str | None:
    """Seletor de arquivo nativo. None se o sistema não tem um ou o usuário
    cancelou. `start_dir`, quando existe, é onde o diálogo abre (e.g. o
    projeto de destino)."""
    start = start_dir if start_dir and Path(start_dir).is_dir() else ""
    if _IS_WINDOWS:
        script = (
            _PS_OWNER
            + "$d = New-Object System.Windows.Forms.OpenFileDialog; "
            + "$d.Title = $env:CLIENTELA_PROMPT; "
            + "if ($env:CLIENTELA_START) { $d.InitialDirectory = $env:CLIENTELA_START }; "
            + "if ($d.ShowDialog($owner) -eq 'OK') { Write-Output $d.FileName }"
        )
        return _powershell(script, {"CLIENTELA_PROMPT": prompt, "CLIENTELA_START": start})
    if not _IS_MAC:
        return None
    script = f"POSIX path of (choose file with prompt {_applescript_str(prompt)}"
    if start:
        script += f" default location (POSIX file {_applescript_str(start)})"
    script += ")"
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=300
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    return path or None


def detect_browsers() -> list[dict[str, str]]:
    if _IS_WINDOWS:
        return [
            {"name": display_name, "app": app_name, "cookies_key": cookies_key}
            for app_name, display_name, cookies_key in _KNOWN_BROWSERS
            if _windows_browser_exe(app_name)
        ]
    if not _IS_MAC:
        return []
    found = []
    for app_name, display_name, cookies_key in _KNOWN_BROWSERS:
        try:
            # `open -Ra` so pergunta ao LaunchServices se o app existe, sem
            # abrir nada. O `osascript -e 'id of application ...'` de antes
            # mostrava um dialogo "Where is Firefox?" para cada navegador
            # ausente em vez de responder que nao esta instalado.
            result = subprocess.run(
                ["open", "-Ra", app_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                found.append(
                    {"name": display_name, "app": app_name, "cookies_key": cookies_key}
                )
        except (subprocess.SubprocessError, OSError):
            continue
    return found


def open_url(url: str, browser_app: str | None = None) -> bool:
    if _IS_WINDOWS:
        exe = _windows_browser_exe(browser_app) if browser_app else None
        try:
            if exe:
                subprocess.Popen([exe, url])
            else:
                os.startfile(url)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    if not _IS_MAC:
        return False
    try:
        if browser_app:
            subprocess.run(["open", "-a", browser_app, url], check=True)
        else:
            subprocess.run(["open", url], check=True)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False
