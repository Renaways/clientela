import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from werkzeug.utils import secure_filename

from . import machine, paths, store
from .persistence import safe_read_json, safe_write_json

COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

TIPO_CLIENTE = "cliente"
TIPO_PESSOAL = "pessoal"


def _normalize_tipo(tipo: Any) -> str:
    return TIPO_PESSOAL if tipo == TIPO_PESSOAL else TIPO_CLIENTE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_paths(client: dict[str, Any]) -> dict[str, Any]:
    """A pasta do cliente é guardada uma vez por sistema (``path_mac``,
    ``path_windows``); ``path`` é derivado na leitura com a desta máquina e é
    o que o resto do app usa. Vazio quando o cliente não foi mapeado neste
    sistema — e aí ``folder_available`` é False.

    Cadastro sem nenhuma das chaves é de antes do Windows, quando ``path`` era
    sempre o caminho do Mac."""
    if not any(k in client for k in machine.PATH_KEYS.values()):
        client[machine.PATH_KEYS[machine.MAC]] = client.get("path") or ""
    for key in machine.PATH_KEYS.values():
        client.setdefault(key, "")
    client["path"] = client.get(machine.path_key()) or ""
    return client


def _load_all() -> list[dict[str, Any]]:
    return [_resolve_paths(c) for c in safe_read_json(paths.CLIENTS_JSON, [])]


def _save_all(clients: list[dict[str, Any]]) -> None:
    # ``path`` gravado é sempre o do Mac, não o desta máquina: com data/
    # compartilhado, o Windows gravaria o caminho dele por cima e um app
    # antigo no Mac (ou um rollback) leria um caminho que lá não existe.
    stored = [{**c, "path": c.get(machine.PATH_KEYS[machine.MAC]) or ""} for c in clients]
    safe_write_json(paths.CLIENTS_JSON, stored)


def _apply_path_fields(client: dict[str, Any], fields: dict[str, Any]) -> None:
    """``path`` sozinho (API antiga, ou o campo do sistema atual) vale para
    esta máquina; ``path_mac`` / ``path_windows`` valem para cada sistema."""
    if "path" in fields:
        client[machine.path_key()] = (fields["path"] or "").strip()
    for key in machine.PATH_KEYS.values():
        if key in fields:
            client[key] = (fields[key] or "").strip()
    client["path"] = client.get(machine.path_key()) or ""


def list_clients() -> list[dict[str, Any]]:
    return _load_all()


def get_client(client_id: str) -> dict[str, Any] | None:
    for c in _load_all():
        if c["id"] == client_id:
            return c
    return None


def create_client(
    name: str,
    path: str | None = None,
    frameio: str | None = None,
    orcamento_mensal: float = 0.0,
    contrato_inicio: str | None = None,
    contrato_fim: str | None = None,
    template_path_premiere: str | None = None,
    template_path_ae: str | None = None,
    tipo: str = TIPO_CLIENTE,
    path_mac: str | None = None,
    path_windows: str | None = None,
) -> dict[str, Any]:
    clients = _load_all()
    client = {
        "id": uuid.uuid4().hex,
        "name": name,
        "path": "",
        "path_mac": "",
        "path_windows": "",
        "frameio": frameio or "",
        "cover": None,
        "created_at": _now_iso(),
        "orcamento_mensal": orcamento_mensal,
        "contrato_inicio": contrato_inicio,
        "contrato_fim": contrato_fim,
        "template_path_premiere": template_path_premiere,
        "template_path_ae": template_path_ae,
        "ativo": True,
        "tipo": _normalize_tipo(tipo),
    }
    path_fields = {"path_mac": path_mac, "path_windows": path_windows}
    if path is not None:
        path_fields["path"] = path
    _apply_path_fields(client, {k: v for k, v in path_fields.items() if v is not None})
    clients.append(client)
    _save_all(clients)
    # Só a pasta deste sistema: a do outro é um caminho que daqui não existe.
    if client["path"]:
        Path(client["path"]).mkdir(parents=True, exist_ok=True)
    detect_cover(client["id"])
    return client


def update_client(client_id: str, **fields: Any) -> dict[str, Any] | None:
    clients = _load_all()
    for c in clients:
        if c["id"] == client_id:
            _apply_path_fields(c, fields)
            for key in (
                "name",
                "frameio",
                "orcamento_mensal",
                "contrato_inicio",
                "contrato_fim",
                "cover",
                "template_path_premiere",
                "template_path_ae",
            ):
                if key in fields:
                    c[key] = fields[key]
            if "ativo" in fields:
                c["ativo"] = bool(fields["ativo"])
            if "tipo" in fields:
                c["tipo"] = _normalize_tipo(fields["tipo"])
            _save_all(clients)
            return c
    return None


def client_dir(client: dict[str, Any] | None) -> Path | None:
    """Pasta do cliente nesta máquina, ou None se ele não foi mapeado neste
    sistema. Nunca ``Path("")``: isso é o diretório atual, e escanear ou
    gravar nele seria escanear ou gravar dentro do app."""
    raw = (client or {}).get("path") or ""
    return Path(raw) if raw.strip() else None


def folder_available(client: dict[str, Any]) -> bool:
    d = client_dir(client)
    return d is not None and d.is_dir()


def is_active(client: dict[str, Any]) -> bool:
    """Cliente desativado: contrato encerrado, mas o histórico fica. Sai do
    quadro, das pendências e dos KPIs de "ativo"; continua abrível e nas
    estatísticas históricas. Cadastros antigos não têm a chave → ativo."""
    return bool(client.get("ativo", True))


def is_personal(client: dict[str, Any] | None) -> bool:
    """Cliente pessoal: trabalho próprio (o visual da Twitch, o portfólio),
    sem contrato. As horas contam como as de qualquer cliente; valor, custo
    e lucro não — horas sem receita derrubariam o lucro do mês. Cadastros
    antigos não têm a chave → cliente comum."""
    return bool(client) and client.get("tipo") == TIPO_PESSOAL


def with_folder_status(client: dict[str, Any]) -> dict[str, Any]:
    return {
        **client,
        "folder_available": folder_available(client),
        "ativo": is_active(client),
        "tipo": TIPO_PESSOAL if is_personal(client) else TIPO_CLIENTE,
        "mapeado_neste_sistema": client_dir(client) is not None,
        "compartilhado": machine.is_shared(client),
    }


def delete_client(client_id: str) -> bool:
    """Remove o cadastro e os registros de projeto dele em data/projects/.
    A pasta no disco não é tocada."""
    clients = _load_all()
    remaining = [c for c in clients if c["id"] != client_id]
    if len(remaining) == len(clients):
        return False
    _save_all(remaining)
    store.delete_client(client_id)
    return True


def set_budget(client_id: str, orcamento_mensal: float) -> dict[str, Any] | None:
    return update_client(client_id, orcamento_mensal=orcamento_mensal)


def save_cover_upload(client_id: str, filename: str, file_obj) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext not in COVER_EXTENSIONS:
        return None
    safe_name = secure_filename(f"{client_id}{ext}")
    dest = paths.COVERS_DIR / safe_name
    paths.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    file_obj.save(dest)
    update_client(client_id, cover=safe_name)
    return safe_name


def detect_cover(client_id: str) -> str | None:
    """If the client folder has a `Fotos/` subfolder, use the most recently
    modified image in it as the cover, without requiring a manual upload."""
    client = get_client(client_id)
    if not client:
        return None
    base = client_dir(client)
    if base is None:
        return None
    fotos_dir = base / "Fotos"
    if not fotos_dir.is_dir():
        return None
    candidates = [
        p for p in fotos_dir.iterdir() if p.is_file() and p.suffix.lower() in COVER_EXTENSIONS
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    safe_name = secure_filename(f"{client_id}{newest.suffix.lower()}")
    dest = paths.COVERS_DIR / safe_name
    paths.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(newest.read_bytes())
    update_client(client_id, cover=safe_name)
    return safe_name
