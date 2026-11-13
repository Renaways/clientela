import os
import shutil
from pathlib import Path
from typing import Any

from . import clients as clients_module
from . import projects as projects_module
from .paths import sanitize_folder_component

ADOBE_EXTS = {".aep", ".prproj", ".mogrt", ".ai", ".psd", ".psb", ".indd", ".prfpset"}
SHORTCUT_EXTS = {".url", ".webloc", ".lnk", ".desktop"}

# extension -> suggested subfolder, for the "other" bucket only (adobe always -> Edit)
_SUGGESTION_HINTS: dict[str, str] = {}
for ext in (".mp4", ".mov", ".mxf"):
    _SUGGESTION_HINTS[ext] = "Export"
for ext in (".wav", ".mp3", ".aac", ".m4a"):
    _SUGGESTION_HINTS[ext] = "Audio"
for ext in (".png", ".jpg", ".jpeg", ".tiff", ".tif"):
    _SUGGESTION_HINTS[ext] = "References"


def categorize_file(path: Path) -> str:
    """§5.3: extension-based bucket used to drive organizer suggestions —
    Adobe project files, link shortcuts, or everything else."""
    ext = path.suffix.lower()
    if ext in ADOBE_EXTS:
        return "adobe"
    if ext in SHORTCUT_EXTS or (ext == ".txt" and "link" in path.stem.lower()):
        return "links"
    return "other"


FILE_KIND_EXTS: dict[str, str] = {}
for ext in (".prproj", ".aep"):
    FILE_KIND_EXTS[ext] = "projeto"
for ext in (".mogrt", ".ai", ".psd", ".psb", ".indd", ".prfpset"):
    FILE_KIND_EXTS[ext] = "design"
for ext in (".mp4", ".mov", ".mxf"):
    FILE_KIND_EXTS[ext] = "video"
for ext in (".wav", ".mp3", ".aac", ".m4a"):
    FILE_KIND_EXTS[ext] = "audio"
for ext in (".png", ".jpg", ".jpeg", ".tiff", ".tif"):
    FILE_KIND_EXTS[ext] = "imagem"
for ext in SHORTCUT_EXTS:
    FILE_KIND_EXTS[ext] = "link"


def _is_autosave_path(path: Path) -> bool:
    """True if any folder in `path` looks like a Premiere/After Effects
    auto-save folder (e.g. "Adobe Premiere Pro Auto-Save")."""
    return any("auto-save" in part.lower() or "autosave" in part.lower() for part in path.parts)


def file_kind(path: Path) -> str:
    """Richer per-file classification driving the file-type filter UI —
    separate from `categorize_file`, which only feeds the 3-bucket
    organizer-suggestion logic and shouldn't be repurposed for display.
    `path` should be relative to the project folder so the auto-save check
    only looks at folders inside the project, not ancestor directories."""
    if _is_autosave_path(path):
        return "autosave"
    if path.suffix.lower() == ".txt" and "link" in path.stem.lower():
        return "link"
    return FILE_KIND_EXTS.get(path.suffix.lower(), "outro")


def suggest_folder(path: Path) -> str | None:
    ext = path.suffix.lower()
    target = "Edit" if categorize_file(path) == "adobe" else _SUGGESTION_HINTS.get(ext)
    if not target or path.parent.name == target:
        return None
    return target


def list_project_files(project_id: str) -> list[dict[str, Any]]:
    project = projects_module.get_project(project_id)
    if not project or not project["folder_available"]:
        return []
    project_dir = Path(project["path"])
    subs = projects_module.subproject_dirs(project_dir)
    out = []
    for p in project_dir.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(project_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if subs and (project_dir / rel.parts[0]) in subs:
            continue
        out.append(
            {
                "name": p.name,
                "ext": p.suffix.lstrip(".").upper(),
                "path": str(rel),
                "category": categorize_file(p),
                "kind": file_kind(rel),
                "size": p.stat().st_size,
                "suggest_folder": suggest_folder(p),
            }
        )
    return out


# Perfil design: estes também viram botão de abrir no cartão (no perfil vídeo um .psd é só asset).
DESIGN_OPEN_EXTS = {".psd", ".psb", ".ai", ".indd", ".fig", ".afdesign", ".afphoto", ".afpub", ".sketch", ".xd", ".cdr"}


def list_open_files(project_dir: Path, design: bool = False) -> list[dict[str, Any]]:
    """Os .prproj/.aep da pasta, o mais recente primeiro — é o que o rodapé de
    ações rápidas do cartão mostra.

    Anda a árvore com os.walk em vez de rglob pra podar as pastas de auto-save
    (dezenas de cópias do mesmo .prproj) e as ocultas antes de entrar nelas;
    o quadro da Home chama isto pra todos os projetos de todos os clientes a
    cada abertura. Ordenado por modificação porque, num projeto com três
    versões do .prproj, o que interessa abrir é o que foi mexido por último.
    """
    if not project_dir.is_dir():
        return []
    found: list[tuple[float, dict[str, Any]]] = []
    subs = projects_module.subproject_dirs(project_dir)
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and not _is_autosave_path(Path(d)) and Path(root) / d not in subs
        ]
        for name in files:
            if name.startswith("."):
                continue
            path = Path(root) / name
            ext = path.suffix.lower()
            if FILE_KIND_EXTS.get(ext) != "projeto" and not (design and ext in DESIGN_OPEN_EXTS):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            found.append(
                (
                    mtime,
                    {
                        "name": name,
                        "ext": path.suffix.lstrip(".").upper(),
                        "path": str(path.relative_to(project_dir)),
                    },
                )
            )
    found.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in found]


def attach_quick_actions(client_id: str, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enriquece a lista do quadro com o que o rodapé do cartão precisa:
    `atalhos.arquivos` (Premiere/After, mais recente primeiro) e
    `atalhos.links` (os do projeto mais o Frame.io do cliente, igual ao
    painel de detalhe). Fica fora de `list_projects` de propósito — aquele é
    chamado pelo financeiro, pela busca e pelo cronômetro, que não têm por que
    varrer pastas."""
    client = clients_module.get_client(client_id)
    from . import stages  # aqui dentro: stages importa projects, que este módulo também importa
    design = stages.perfil_uso() == "design"
    for project in projects:
        folder = Path(project["path"]) if project.get("folder_available") else None
        project["atalhos"] = {
            "arquivos": list_open_files(folder, design) if folder else [],
            # sequências do estúdio (.clientela na raiz): só o nome — ler o JSON de cada uma pesaria na Home
            "sequencias": sorted(p.name for p in folder.glob("*.clientela") if not p.name.startswith(".")) if folder else [],
            "links": projects_module.merge_client_links(project.get("links") or [], client),
        }
    return projects


def _safe_join(project_dir: Path, relative: str) -> Path | None:
    """Resolve a client-supplied relative path/folder name and ensure it
    stays inside the project directory."""
    cleaned = "/".join(sanitize_folder_component(part) for part in relative.split("/") if part)
    if not cleaned:
        return None
    candidate = (project_dir / cleaned).resolve()
    try:
        candidate.relative_to(project_dir.resolve())
    except ValueError:
        return None
    return candidate


def _unique_destination(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    i = 1
    while True:
        candidate = dest.parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def apply_organization(project_id: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
    """actions: [{"type": "create", "folder": "Edit"}, {"type": "move", "file": "cam_a.mov", "to": "Footage"}]"""
    project = projects_module.get_project(project_id)
    if not project or not project["folder_available"]:
        return {"applied": []}
    project_dir = Path(project["path"]).resolve()
    applied = []

    for action in actions:
        if action.get("type") == "create":
            folder = _safe_join(project_dir, action.get("folder", ""))
            if not folder:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            applied.append({"type": "create", "folder": action["folder"]})
        elif action.get("type") == "move":
            src = _safe_join(project_dir, action.get("file", ""))
            dest_dir = _safe_join(project_dir, action.get("to", ""))
            if not src or not dest_dir or not src.is_file():
                continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_destination(dest_dir / src.name)
            shutil.move(str(src), str(dest))
            applied.append(
                {"type": "move", "file": action["file"], "to": str(dest.relative_to(project_dir))}
            )

    return {"applied": applied}
