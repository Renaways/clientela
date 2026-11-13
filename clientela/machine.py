"""Em que máquina e em que sistema o app está rodando.

O Clientela roda no MacBook e em desktops Windows, com a mesma pasta data/
(compartilhada pelo Google Drive). Tudo que depende do sistema passa por
aqui: qual caminho da pasta do cliente vale nesta máquina (cada cliente
guarda um por sistema, ``path_mac`` / ``path_windows``), e de onde veio cada
projeto (``origem`` no registro, gravada na criação) — que vira o ícone do
card.
"""

import functools
import ipaddress
import os
import socket
import subprocess
import sys
from typing import Any

MAC = "mac"
WINDOWS = "windows"
LINUX = "linux"

# Chave do clients.json com a pasta do cliente em cada sistema. Linux não tem
# chave própria: não é um ambiente do estúdio, e os testes rodam no Mac.
PATH_KEYS = {MAC: "path_mac", WINDOWS: "path_windows"}

ICONE_APPLE = "apple"
ICONE_WINDOWS = "windows"
ICONE_NUVEM = "nuvem"


def current_os(platform: str | None = None) -> str:
    p = platform if platform is not None else sys.platform
    if p == "darwin":
        return MAC
    if p.startswith("win"):
        return WINDOWS
    return LINUX


@functools.lru_cache(maxsize=1)
def hostname() -> str:
    """Nome do computador, estável entre redes: identifica a máquina dona de
    um job do editor e aparece no card ("Windows - Desktop-Main").

    ``socket.gethostname()`` não serve sozinho: no macOS ele vira o que o DHCP
    mandar ("192.168.0.12", "MacBook-Pro.local", o domínio da rede). Por isso
    o LocalHostName do macOS (Ajustes > Geral > Compartilhamento) e o
    COMPUTERNAME do Windows vêm primeiro."""
    os_name = current_os()
    if os_name == WINDOWS:
        name = os.environ.get("COMPUTERNAME", "").strip()
        if name:
            return name
    elif os_name == MAC:
        try:
            out = subprocess.run(
                ["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    name = (socket.gethostname() or "").strip()
    try:
        ipaddress.ip_address(name)
        return name
    except ValueError:
        return name.split(".")[0] or "desconhecido"


def origin() -> dict[str, str]:
    """O que vai em ``origem`` de um registro de projeto criado agora."""
    return {"os": current_os(), "host": hostname()}


def path_key(os_name: str | None = None) -> str:
    """Chave do caminho da pasta do cliente neste sistema. Fora de Mac e
    Windows cai no do Mac (é o ambiente onde os testes e o dev rodam)."""
    return PATH_KEYS.get(os_name or current_os(), PATH_KEYS[MAC])


def is_shared(client: dict[str, Any] | None) -> bool:
    """Cliente com pasta mapeada nos dois sistemas: o projeto abre de
    qualquer máquina (o Drive sincroniza a pasta entre elas)."""
    return bool(client) and all((client.get(k) or "").strip() for k in PATH_KEYS.values())


def project_badge(origem: dict[str, Any] | None, client: dict[str, Any] | None) -> dict[str, str]:
    """Ícone do projeto na UI.

    - Cliente mapeado nos dois sistemas → nuvem (acessível pelos dois).
    - Senão, onde o projeto nasceu: Windows → logo do Windows + nome do
      computador ("Windows - Desktop-Main"); Mac → logo da Apple.

    Registro sem ``origem`` é anterior a esta mudança, quando o app só rodava
    no Mac — conta como Mac.
    """
    origem = origem or {}
    host = (origem.get("host") or "").strip()
    if is_shared(client):
        rotulo = "Drive · Mac e Windows"
        return {"icone": ICONE_NUVEM, "rotulo": rotulo, "host": host}
    if origem.get("os") == WINDOWS:
        rotulo = f"Windows - {host}" if host else "Windows"
        return {"icone": ICONE_WINDOWS, "rotulo": rotulo, "host": host}
    return {"icone": ICONE_APPLE, "rotulo": f"Mac - {host}" if host else "Mac", "host": host}
