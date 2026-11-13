"""Senha do app e acesso remoto (celular pela rede local ou ZeroTier).

Uma credencial só (usuário opcional + senha; o PIN de antes é uma senha só de dígitos e continua valendo)
protege o acesso remoto e, se o usuário ligar ``exigir_local``, também o próprio computador. Esqueceu a
senha: ``python app.py --redefinir-senha`` (ou ``--remover-senha``), só quem está na máquina.

O app continua sem login no próprio computador: tudo que chega de 127.0.0.1 / ::1 passa direto. Com o
acesso remoto ligado e um PIN cadastrado, o servidor passa a escutar em todas as interfaces (0.0.0.0) e
qualquer requisição de outro endereço precisa do cookie que a tela do PIN entrega. Sem o cookie, a página
mostra a tela do PIN e a API responde 401.

Mora em ``config.json`` → ``acesso_remoto`` (que vai pelo Drive para o Windows também, então o mesmo PIN
vale nas duas máquinas). O PIN é guardado só como PBKDF2 com sal; o cookie é um HMAC do hash com um
segredo que muda a cada troca de PIN, então trocar o PIN desloga todos os aparelhos. Errar o PIN
`MAX_TENTATIVAS` vezes seguidas bloqueia aquele endereço por `BLOQUEIO_S`.

O endereço em que o servidor escuta é decidido no boot (``host_de_escuta``): ligar ou desligar o acesso
pede reiniciar o app, e a tela de configuração diz isso.
"""

from __future__ import annotations

import gc
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from typing import Any

from . import machine, paths
from .persistence import safe_read_json, safe_write_json

CONFIG_KEY = "acesso_remoto"
COOKIE = "clientela_acesso"
COOKIE_MAX_AGE = 180 * 24 * 3600
PIN_RE = re.compile(r"^\d{4,12}$")
SENHA_MIN, SENHA_MAX = 6, 128
USUARIO_MAX = 40
ITERACOES = 200_000
MAX_TENTATIVAS = 5
BLOQUEIO_S = 300
PORTA = 8000

# O endereço em que este processo está escutando de verdade e como relançá-lo (app.py preenche no boot;
# nos testes fica None e "reiniciar" é recusado).
escutando: str = "127.0.0.1"
argv_boot: list[str] | None = None

_lock = threading.Lock()
_falhas: dict[str, list[float]] = {}   # endereço → instantes das falhas recentes


class AcessoError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ config
def _ler() -> dict[str, Any]:
    cfg = safe_read_json(paths.CONFIG_JSON, {}).get(CONFIG_KEY)
    return cfg if isinstance(cfg, dict) else {}


def _gravar(cfg: dict[str, Any]) -> None:
    config = safe_read_json(paths.CONFIG_JSON, {})
    config[CONFIG_KEY] = cfg
    safe_write_json(paths.CONFIG_JSON, config)


def _hash(pin: str, sal: str, iteracoes: int = ITERACOES) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(sal), iteracoes).hex()


def pin_definido(cfg: dict[str, Any] | None = None) -> bool:
    cfg = _ler() if cfg is None else cfg
    pin = cfg.get("pin") or {}
    return bool(pin.get("hash") and pin.get("sal") and cfg.get("segredo"))


def ativo(cfg: dict[str, Any] | None = None) -> bool:
    """Ligado *e* com PIN: sem PIN o acesso remoto nunca abre."""
    cfg = _ler() if cfg is None else cfg
    return bool(cfg.get("ativo")) and pin_definido(cfg)


def exige_local(cfg: dict[str, Any] | None = None) -> bool:
    """Pedir a senha também neste computador: só vale com uma senha cadastrada."""
    cfg = _ler() if cfg is None else cfg
    return bool(cfg.get("exigir_local")) and pin_definido(cfg)


def _guardar_senha(cfg: dict[str, Any], segredo_novo: str) -> None:
    sal = secrets.token_hex(16)
    cfg["pin"] = {"sal": sal, "hash": _hash(segredo_novo, sal), "iteracoes": ITERACOES}
    cfg["segredo"] = secrets.token_hex(32)   # trocar a senha = derruba os aparelhos já liberados


def configurar(ativo: bool | None = None, pin: str | None = None, senha: str | None = None,
               usuario: str | None = None, exigir_local: bool | None = None) -> dict[str, Any]:
    with _lock:
        cfg = _ler()
        if pin is not None:
            pin = str(pin).strip()
            if not PIN_RE.match(pin):
                raise AcessoError("o PIN precisa ter de 4 a 12 dígitos")
            _guardar_senha(cfg, pin)
        if senha is not None:
            senha = str(senha)
            if not SENHA_MIN <= len(senha) <= SENHA_MAX:
                raise AcessoError(f"a senha precisa ter de {SENHA_MIN} a {SENHA_MAX} caracteres")
            _guardar_senha(cfg, senha)
        if usuario is not None:
            usuario = str(usuario).strip()
            if len(usuario) > USUARIO_MAX:
                raise AcessoError(f"o usuário pode ter até {USUARIO_MAX} caracteres")
            cfg["usuario"] = usuario
        if ativo is not None:
            if ativo and not pin_definido(cfg):
                raise AcessoError("cadastre uma senha antes de ligar o acesso remoto")
            cfg["ativo"] = bool(ativo)
        if exigir_local is not None:
            if exigir_local and not pin_definido(cfg):
                raise AcessoError("cadastre uma senha antes de pedir senha neste computador")
            cfg["exigir_local"] = bool(exigir_local)
        _gravar(cfg)
    return status()


def remover_senha() -> None:
    """Apaga a credencial e desliga tudo que dependia dela (o `--remover-senha` do terminal)."""
    with _lock:
        cfg = _ler()
        for k in ("pin", "segredo"):
            cfg.pop(k, None)
        cfg["ativo"] = False
        cfg["exigir_local"] = False
        _gravar(cfg)


def host_de_escuta() -> str:
    return "0.0.0.0" if ativo() else "127.0.0.1"


# ------------------------------------------------------------------ quem pode entrar
def eh_local(endereco: str | None) -> bool:
    try:
        ip = ipaddress.ip_address((endereco or "").split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def token(cfg: dict[str, Any] | None = None) -> str | None:
    cfg = _ler() if cfg is None else cfg
    if not pin_definido(cfg):
        return None
    return hmac.new(bytes.fromhex(cfg["segredo"]), cfg["pin"]["hash"].encode(), hashlib.sha256).hexdigest()


def cookie_valido(valor: str | None) -> bool:
    esperado = token()
    return bool(esperado and valor and hmac.compare_digest(esperado, valor))


def bloqueado_ate(endereco: str, agora: float | None = None) -> float:
    agora = time.time() if agora is None else agora
    with _lock:
        recentes = [t for t in _falhas.get(endereco, []) if agora - t < BLOQUEIO_S]
        _falhas[endereco] = recentes
        return recentes[-1] + BLOQUEIO_S if len(recentes) >= MAX_TENTATIVAS else 0.0


def conferir_pin(endereco: str, pin: str, agora: float | None = None, usuario: str | None = None,
                 local: bool = False) -> str:
    """Devolve o valor do cookie se usuário e senha batem; levanta AcessoError (429 no bloqueio) se não."""
    agora = time.time() if agora is None else agora
    if bloqueado_ate(endereco, agora) > agora:
        raise AcessoError("muitas tentativas erradas: espere alguns minutos", 429)
    cfg = _ler()
    if not (exige_local(cfg) if local else ativo(cfg)):
        raise AcessoError("o acesso remoto está desligado", 403)
    p = cfg["pin"]
    certo = hmac.compare_digest(_hash(str(pin or "").strip(), p["sal"], int(p.get("iteracoes") or ITERACOES)), p["hash"])
    esperado = (cfg.get("usuario") or "").casefold()
    if esperado and (usuario or "").strip().casefold() != esperado:
        certo = False
    if not certo:
        with _lock:
            _falhas.setdefault(endereco, []).append(agora)
        raise AcessoError("usuário ou senha errados" if esperado else "senha errada", 401)
    with _lock:
        _falhas.pop(endereco, None)
    return token(cfg) or ""


def reiniciar() -> None:
    """Relança o processo no lugar (mesmo pid, então o Clientela.app continua dono das permissões), meio
    segundo depois, para a resposta HTTP sair antes. Os jobs do editor rodam em sessão própria e o boot
    volta a acompanhá-los (editor.resume_jobs)."""
    if not argv_boot:
        raise AcessoError("reinicie o app manualmente (feche e abra de novo)", 409)
    if machine.current_os() != machine.MAC:
        raise AcessoError("no Windows, feche e abra o app de novo", 409)
    argv = list(argv_boot)

    def _exec() -> None:
        time.sleep(0.5)
        # O werkzeug deixa o socket que escuta herdável: sem isto, o processo novo nasceria com o
        # 127.0.0.1:8000 antigo aberto e sem ninguém atendendo, e as conexões locais ficariam penduradas.
        # Só os sockets do Python: um os.closerange() fecha descritores protegidos do macOS (EXC_GUARD)
        # e o sistema mata o processo com SIGKILL.
        for obj in gc.get_objects():
            if isinstance(obj, socket.socket):
                try:
                    if obj.fileno() >= 0:
                        obj.set_inheritable(False)
                except OSError:
                    pass
        os.execv(argv[0], argv)

    threading.Thread(target=_exec, daemon=True, name="acesso-reiniciar").start()


def reset() -> None:
    global escutando, argv_boot
    with _lock:
        _falhas.clear()
    escutando = "127.0.0.1"
    argv_boot = None


# ------------------------------------------------------------------ endereços para digitar no celular
def _ifconfig() -> list[tuple[str, str]]:
    """(interface, IPv4) de cada interface com endereço, pelo `ifconfig` (macOS)."""
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    itens, atual = [], ""
    for linha in out.splitlines():
        m = re.match(r"^(\S+?):\s", linha)
        if m:
            atual = m.group(1)
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", linha)
        if m and atual:
            itens.append((atual, m.group(1)))
    return itens


def _rotulo(interface: str, ip: str) -> str:
    # ZeroTier no macOS cria interfaces feth*; no Linux/Windows o nome tem "zt"/"ZeroTier"
    if interface.startswith("feth") or interface.lower().startswith("zt") or "zerotier" in interface.lower():
        return "ZeroTier"
    if interface.startswith("utun") or interface.startswith("tailscale"):
        return "VPN"
    if ipaddress.ip_address(ip).is_private:
        return "Rede local"
    return "Outra rede"


def enderecos() -> list[dict[str, str]]:
    if machine.current_os() == machine.MAC:
        pares = _ifconfig()
    else:
        try:
            pares = [("", ip) for ip in socket.gethostbyname_ex(socket.gethostname())[2]]
        except OSError:
            pares = []
    out, vistos = [], set()
    for interface, ip in pares:
        if ip in vistos or ipaddress.ip_address(ip).is_loopback or ip.startswith("169.254."):
            continue
        vistos.add(ip)
        out.append({"interface": interface, "ip": ip, "rotulo": _rotulo(interface, ip), "url": f"http://{ip}:{PORTA}"})
    ordem = {"ZeroTier": 0, "Rede local": 1, "VPN": 2, "Outra rede": 3}
    return sorted(out, key=lambda e: ordem.get(e["rotulo"], 9))


def status() -> dict[str, Any]:
    cfg = _ler()
    quer = host_de_escuta()
    return {
        "ativo": bool(cfg.get("ativo")), "pin_definido": pin_definido(cfg), "escutando": escutando,
        "usuario": cfg.get("usuario") or "", "exigir_local": exige_local(cfg),
        "precisa_reiniciar": quer != escutando, "porta": PORTA, "enderecos": enderecos(),
        "pode_reiniciar": machine.current_os() == machine.MAC,
    }


# ------------------------------------------------------------------ a tela do PIN
# HTML fora do index.html de propósito: ela aparece antes de qualquer coisa do app, para quem ainda não
# entrou, e precisa funcionar sozinha num celular.
PAGINA = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0e0e10"><title>Clientela</title>
<style>
:root{color-scheme:dark;--bg:#0e0e10;--card:#161619;--borda:#2a2a30;--txt:#f2f2f4;--sub:#9a9aa3;--erro:#ff6b6b;}
*{box-sizing:border-box;margin:0}
body{min-height:100dvh;display:grid;place-items:center;background:var(--bg);color:var(--txt);
  font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;padding:24px 16px}
form{width:100%;max-width:340px;background:var(--card);border:1px solid var(--borda);border-radius:14px;padding:28px 22px;
  display:flex;flex-direction:column;gap:16px}
h1{font-size:20px;letter-spacing:-.02em}
p{color:var(--sub);font-size:13px}
label{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--sub)}
input{width:100%;font:16px/1.2 inherit;padding:12px 12px;
  border-radius:10px;border:1px solid var(--borda);background:#0b0b0d;color:var(--txt);outline:none}
input:focus{border-color:#6b6b75}
button{font:600 15px/1 inherit;padding:14px;border-radius:999px;border:0;background:var(--txt);color:#111;cursor:pointer}
.erro{color:var(--erro);font-size:13px;min-height:1em}
</style></head>
<body><form method="post" action="/acesso/entrar">
<h1>Clientela</h1><p>Entre com o seu usuário e a senha do Clientela.</p>
__USUARIO__
<label>Senha<input name="pin" type="password" autocomplete="current-password" maxlength="128" __FOCO_SENHA__ required></label>
<div class="erro">__ERRO__</div>
<button type="submit">Entrar</button>
</form></body></html>"""

DESLIGADO = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Clientela</title>
<style>body{min-height:100vh;display:grid;place-items:center;background:#0e0e10;color:#9a9aa3;
font:15px/1.5 -apple-system,system-ui,sans-serif;padding:16px;text-align:center}</style></head>
<body><p>O acesso remoto do Clientela está desligado.<br>Ligue em Configurações → Acesso remoto, no computador.</p></body></html>"""


def _esc(texto: str) -> str:
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def pagina_pin(erro: str = "", usuario: str = "") -> str:
    cfg = _ler()
    tem_usuario = bool(cfg.get("usuario"))
    # Sem usuário cadastrado (ou o PIN antigo, só dígitos) a tela pede só a senha.
    campo = ('<label>Usuário<input name="usuario" autocomplete="username" autocapitalize="none" maxlength="40" '
             f'value="{_esc(usuario)}" {"" if usuario else "autofocus"} required></label>') if tem_usuario else ""
    return (PAGINA.replace("__USUARIO__", campo)
            .replace("__FOCO_SENHA__", "autofocus" if not tem_usuario or usuario else "")
            .replace("__ERRO__", _esc(erro)))
