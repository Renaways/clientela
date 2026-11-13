"""Perfil do usuário: nome e foto.

O app é de usuário único e sem login, então "perfil" aqui não tem nada de
identidade — é só o rosto que ancora o menu de configurações na topbar, no
lugar dos três cards que ocupavam a coluna direita da Home inteira.

Mora no config.json em vez de arquivo próprio porque é exatamente o tipo de
preferência solta que já vive lá, e a foto vai pra covers/ porque essa pasta já
é servida pelo app em /covers/<arquivo>.
"""

from pathlib import Path
from typing import Any

from werkzeug.utils import secure_filename

from . import paths
from .persistence import safe_read_json, safe_write_json

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# Sem underscore inicial: secure_filename() o remove, e aí o nome gravado não
# bateria com o usado na limpeza das outras extensões. Não colide com capa de
# cliente, que é sempre o id de 32 hex.
PHOTO_STEM = "perfil"
DEFAULT_NAME = "Estúdio"


def get_profile() -> dict[str, Any]:
    config = safe_read_json(paths.CONFIG_JSON, {})
    profile = config.get("profile") or {}
    return {
        "name": profile.get("name") or DEFAULT_NAME,
        "photo": profile.get("photo"),
    }


def set_profile(name: str | None = None, photo: str | None = None) -> dict[str, Any]:
    config = safe_read_json(paths.CONFIG_JSON, {})
    profile = dict(config.get("profile") or {})
    if name is not None:
        profile["name"] = name.strip() or DEFAULT_NAME
    if photo is not None:
        profile["photo"] = photo
    config["profile"] = profile
    safe_write_json(paths.CONFIG_JSON, config)
    return get_profile()


def save_photo_upload(filename: str, file_obj) -> str | None:
    """Grava a foto em covers/ e devolve o nome do arquivo, ou None se a
    extensão não for de imagem."""
    ext = Path(filename).suffix.lower()
    if ext not in PHOTO_EXTENSIONS:
        return None
    safe_name = secure_filename(f"{PHOTO_STEM}{ext}")
    paths.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    dest = paths.COVERS_DIR / safe_name
    file_obj.save(dest)
    # Trocar de .jpg pra .png deixaria o arquivo antigo pra trás e o navegador
    # poderia continuar servindo o velho do cache; apaga as outras extensões.
    for other in PHOTO_EXTENSIONS - {ext}:
        (paths.COVERS_DIR / secure_filename(f"{PHOTO_STEM}{other}")).unlink(missing_ok=True)
    set_profile(photo=safe_name)
    return safe_name
