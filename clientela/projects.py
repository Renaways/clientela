import logging
import re
import shutil
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

from werkzeug.utils import secure_filename

from . import clients as clients_module
from . import files_native, history, machine, store
from .paths import sanitize_folder_component

logger = logging.getLogger("clientela.projects")

PROJECT_DIR_RE = re.compile(r"^(\d+)\s*-\s*(.+)$")

# Convenção do estúdio: uma pasta "00 - …" não é entrega, é biblioteca de assets
# do cliente (identidade visual, elementos, templates, footage base). Ela segue
# a nomenclatura de projeto e por isso vira "projeto" na varredura, mas somar as
# horas dela junto com as entregas distorce toda estatística.
#
# Exige DOIS zeros exatos, não `int(num) == 0`: "000 - Eventos" é um agrupador
# com projetos reais aninhados dentro, e cair na mesma regra apagaria horas de
# trabalho de verdade das estatísticas.
ASSET_FOLDER_RE = re.compile(r"^00\s*-\s*")


def is_asset_folder(folder_name: str) -> bool:
    """True para as pastas "00 - …" de assets/projeto-base do cliente."""
    return bool(ASSET_FOLDER_RE.match(folder_name))


# Mantidos como nome público: outros módulos e testes leem daqui.
DEFAULT_META = {"tags": [], "due_date": None, "notes": ""}
DEFAULT_STAGE = {"stage": "backlog"}
DEFAULT_TIME = store.DEFAULT_TIMER
DEFAULT_COST = store.DEFAULT_COST


def _webloc_content(url: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict><key>URL</key>'
        f"<string>{url}</string></dict></plist>\n"
    )


def _url_shortcut_content(url: str) -> str:
    return f"[InternetShortcut]\nURL={url}\n"


def is_sub(num: str) -> bool:
    """Subprojeto: número "<pai>.<NN>" (pasta "NN - …" dentro da do pai)."""
    return "." in str(num)


def parent_id(project_id: str) -> str | None:
    parts = store.split_id(project_id)
    if not parts or not is_sub(parts[1]):
        return None
    return store.make_id(parts[0], parts[1].split(".")[0])


def _parent_name(project_id: str) -> str | None:
    pid = parent_id(project_id)
    parent = store.load(pid) if pid else None
    return parent.get("name") if parent else None


def _numbered_subdirs(folder: Path) -> list[tuple[str, Path]]:
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    out = []
    for entry in entries:
        m = PROJECT_DIR_RE.match(entry.name)
        if m and entry.is_dir():
            out.append((m.group(1), entry))
    return out


def _sub_folders(parent_num: str, parent_dir: Path) -> list[tuple[str, Path]]:
    """Subpastas "NN - …" de um projeto que são subprojetos. A pasta interna do
    Premiere ("012 - Nome" dentro de "012 - Nome") repete o número do pai, como
    texto, e não conta; dentro de "00 - …" (biblioteca de assets) nada conta.
    Texto e não int: o subprojeto "01" e o pai "001" são o mesmo int."""
    if is_asset_folder(parent_dir.name):
        return []
    return [(f"{parent_num}.{n}", d) for n, d in _numbered_subdirs(parent_dir) if n != parent_num]


def subproject_dirs(project_dir: Path) -> set[Path]:
    """Pastas dos subprojetos dentro da pasta de um projeto, pra quem anda a
    árvore do pai (lista de arquivos, atalhos, estimativa de horas) não pegar
    os arquivos dos filhos. Pasta que já é de subprojeto não tem filhos."""
    m = PROJECT_DIR_RE.match(project_dir.name)
    if not m or PROJECT_DIR_RE.match(project_dir.parent.name):
        return set()
    return {d for _, d in _sub_folders(m.group(1), project_dir)}


def _scan_folders(client_path: Path | None) -> dict[str, list[Path]]:
    """Pastas de projeto do cliente, agrupadas por número — incluindo os
    subprojetos, um nível abaixo, como "012.01". Duas pastas com o mesmo
    número são um conflito que a tela de estatísticas avisa. None = cliente
    sem pasta neste sistema (``clients.client_dir``)."""
    found: dict[str, list[Path]] = {}
    if client_path is None or not client_path.is_dir():
        return found
    for num, entry in _numbered_subdirs(client_path):
        first = num not in found
        found.setdefault(num, []).append(entry)
        if first:
            for sub_num, sub_dir in _sub_folders(num, entry):
                found.setdefault(sub_num, []).append(sub_dir)
    return found


def _rel_folder_name(client_path: Path | None, folder_dir: Path) -> str:
    """``folder_name`` do registro: caminho relativo à pasta do cliente (posix),
    então "012 - Campanha/01 - Teaser" para um subprojeto."""
    if client_path is not None:
        try:
            return folder_dir.relative_to(client_path).as_posix()
        except ValueError:
            pass
    return folder_dir.name


def next_project_number(client_path: Path | None, client_id: str | None = None) -> str:
    """Próximo número livre, olhando o disco E o registro.

    Sem o registro, apagar a pasta "012 - X" e criar um projeto novo reusaria
    o 012 — e o projeto novo herdaria o histórico inteiro do antigo, porque o
    id é o número.
    """
    max_n = 0
    for num in _scan_folders(client_path):
        if not is_sub(num):
            max_n = max(max_n, int(num))
    if client_id:
        for record in store.list_for_client(client_id):
            if is_sub(record.get("num", "")):
                continue
            try:
                max_n = max(max_n, int(record["num"]))
            except (TypeError, ValueError):
                continue
    return f"{max_n + 1:03d}"


def next_sub_number(parent: dict[str, Any]) -> str:
    """Próximo "<pai>.NN" livre, olhando a pasta do pai e o registro (mesmo
    motivo do next_project_number)."""
    max_n = 0
    if parent["folder_available"]:
        for num, _ in _sub_folders(parent["num"], Path(parent["path"])):
            max_n = max(max_n, int(num.split(".")[1]))
    prefix = f"{parent['num']}."
    for record in store.list_for_client(parent["client_id"]):
        if record.get("num", "").startswith(prefix):
            try:
                max_n = max(max_n, int(record["num"][len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{max_n + 1:02d}"


def _find_project_dir(client_path: Path | None, num: str) -> Path | None:
    if client_path is None or not client_path.is_dir():
        return None
    if is_sub(num):
        parent_num, local = num.split(".", 1)
        parent_dir = _find_project_dir(client_path, parent_num)
        if parent_dir is None:
            return None
        for sub_num, sub_dir in _sub_folders(parent_num, parent_dir):
            if sub_num == num:
                return sub_dir
        return None
    for entry in client_path.iterdir():
        if entry.is_dir() and entry.name.startswith(f"{num} - "):
            return entry
    return None


def _sem_pasta_neste_sistema(client: dict[str, Any]) -> str:
    sistema = "Windows" if machine.current_os() == machine.WINDOWS else "Mac"
    return f"{client.get('name', 'O cliente')} não tem pasta configurada no {sistema}. Edite o cliente e preencha a pasta deste sistema."


def _register_folder(client_id: str, num: str, folder_dir: Path, client_path: Path | None) -> dict[str, Any]:
    m = PROJECT_DIR_RE.match(folder_dir.name)
    name = m.group(2) if m else folder_dir.name
    return store.register_from_folder(
        client_id, num, name, folder_dir, folder_name=_rel_folder_name(client_path, folder_dir)
    )


def _sync_record_with_folder(
    record: dict[str, Any], folder_dir: Path, client_path: Path | None
) -> dict[str, Any]:
    """Enquanto a pasta existe, o nome dela manda: renomear no Finder continua
    funcionando. Grava só quando algo mudou, pra não girar o .bak a cada
    listagem.

    ``last_known_path`` (absoluto) não entra na comparação: com data/
    compartilhado entre Mac e Windows, cada máquina veria o caminho da outra
    e regravaria todos os registros a cada listagem. O que identifica a pasta
    é ``folder_name``, relativo à pasta do cliente de cada sistema."""
    m = PROJECT_DIR_RE.match(folder_dir.name)
    name = m.group(2) if m else folder_dir.name
    folder_name = _rel_folder_name(client_path, folder_dir)
    if record.get("name") == name and record.get("folder_name") == folder_name:
        return record

    def apply(rec: dict[str, Any]) -> None:
        rec["name"] = name
        rec["folder_name"] = folder_name
        rec["last_known_path"] = str(folder_dir)

    return store.update(record["id"], apply) or record


def _missing_folder_path(record: dict[str, Any], client: dict[str, Any] | None) -> str | None:
    """Onde a pasta estaria nesta máquina: pasta do cliente deste sistema +
    ``folder_name``. ``last_known_path`` só como último recurso — pode ser o
    caminho de outro sistema."""
    base = clients_module.client_dir(client)
    if base is not None and record.get("folder_name"):
        return str(base / record["folder_name"])
    return record.get("last_known_path")


def _build_project_dict(
    record: dict[str, Any],
    folder_dir: Path | None,
    conflicting: list[str] | None = None,
    client: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timer_data = record["timer"]
    cost = record["cost"]
    folder_name = record.get("folder_name") or (folder_dir.name if folder_dir else "")
    return {
        "id": record["id"],
        "client_id": record["client_id"],
        "num": record["num"],
        # Subprojeto: id do projeto-pai (derivado do número "012.01").
        "pai": parent_id(record["id"]),
        "pai_nome": _parent_name(record["id"]),
        "name": record["name"],
        "folder_name": folder_name,
        "path": str(folder_dir) if folder_dir else _missing_folder_path(record, client),
        "folder_available": folder_dir is not None,
        "pastas_conflitantes": list(conflicting or []),
        "stage": record.get("stage", "backlog"),
        "tags": list(record.get("tags") or []),
        "due_date": record.get("due_date"),
        "notes": record.get("notes", ""),
        "link_postagem": record.get("link_postagem", ""),
        "aguardando_externo": record.get("aguardando_externo"),
        # Sem pasta por escolha (≠ pasta perdida): só vale enquanto não existe
        # pasta com esse número — se uma aparecer, ela se liga ao registro.
        "sem_pasta": bool(record.get("sem_pasta")) and folder_dir is None,
        "links": list(record.get("links") or []),
        "midia_original": record.get("midia_original"),
        "revisao_corte": record.get("revisao_corte"),
        "timer_status": timer_data.get("status", "idle"),
        "running_start": timer_data.get("running_start"),
        "sessions": list(timer_data.get("sessions") or []),
        "valor_individual": cost.get("valor_individual", 0.0),
        "custos_extra": list(cost.get("custos_extra") or []),
        # Derivado na leitura, nunca gravado no registro: a pasta "00 - …"
        # continua aparecendo e clicável na lista, mas fica fora de horas,
        # contagem, pipeline, tags e financeiro — todos os agregadores filtram
        # por esta chave. Usa o nome guardado, então vale mesmo sem a pasta.
        "oculto_estatisticas": bool(cost.get("oculto_estatisticas", False))
        or is_asset_folder(folder_name),
        "pasta_de_assets": is_asset_folder(folder_name),
        "created_at": record.get("created_at"),
        # Onde o projeto nasceu (sistema + nome do computador) e o ícone que
        # isso vira no card: Apple, Windows + máquina, ou nuvem quando o
        # cliente tem pasta nos dois sistemas (machine.project_badge).
        "origem": record.get("origem"),
        "maquina": machine.project_badge(record.get("origem"), client),
        "concluido_em": (
            _concluded_at(record.get("history") or [])
            if record.get("stage") == "concluido"
            else None
        ),
    }


def _concluded_at(events: list[dict[str, Any]]) -> str | None:
    """Quando o projeto entrou em "concluido" — ou em "liberado", se nunca
    entrou em "concluido".

    "Liberado" foi a etapa terminal até setembro de 2026; os projetos que
    estavam nela foram movidos pra "concluido" em bloco pela migração, que de
    propósito não grava evento no histórico (gravaria a data da migração em
    todos eles e o arquivo perderia a ordem cronológica real). O evento de
    liberação antigo é, portanto, a data de conclusão desses projetos.
    """
    return history.last_stage_change_to(events, "concluido") or history.last_stage_change_to(
        events, "liberado"
    )


def list_projects(client_id: str) -> list[dict[str, Any]]:
    """União do que está no disco com o que está no registro.

    Pasta sem registro → é registrada na hora (é assim que a importação dos
    metadados antigos acontece, e que uma pasta criada no Finder entra no app).
    Registro sem pasta → continua na lista, marcado ``folder_available=False``.
    """
    client = clients_module.get_client(client_id)
    if not client:
        return []
    client_path = clients_module.client_dir(client)
    folders = _scan_folders(client_path)
    for num, dirs in folders.items():
        _register_folder(client_id, num, dirs[0], client_path)

    out = []
    for record in store.list_for_client(client_id):
        dirs = folders.get(record["num"]) or []
        folder_dir = dirs[0] if dirs else None
        if folder_dir is not None:
            record = _sync_record_with_folder(record, folder_dir, client_path)
        conflicting = [d.name for d in dirs] if len(dirs) > 1 else []
        out.append(_build_project_dict(record, folder_dir, conflicting, client))
    out.sort(key=lambda p: p["num"])
    return out


def get_project(project_id: str) -> dict[str, Any] | None:
    parts = store.split_id(project_id)
    if not parts:
        return None
    client_id, num = parts
    client = clients_module.get_client(client_id)
    if not client:
        return None
    client_path = clients_module.client_dir(client)
    folder_dir = _find_project_dir(client_path, num)
    record = store.load(project_id)
    if record is None:
        if folder_dir is None:
            return None
        record = _register_folder(client_id, num, folder_dir, client_path)
    elif folder_dir is not None:
        record = _sync_record_with_folder(record, folder_dir, client_path)
    return _build_project_dict(record, folder_dir, client=client)


def get_project_dir(project_id: str) -> Path | None:
    """Pasta do projeto no disco, ou None se ela não está disponível."""
    project = get_project(project_id)
    if not project or not project["folder_available"]:
        return None
    return Path(project["path"])


def import_all_from_disk() -> int:
    """Registra toda pasta de projeto que ainda não tem registro, em todos os
    clientes. Chamada no boot. Nunca levanta exceção: uma pasta ilegível não
    pode impedir o app de subir. Devolve quantos registros novos criou."""
    novos = 0
    for client in clients_module.list_clients():
        try:
            client_path = clients_module.client_dir(client)
            folders = _scan_folders(client_path)
            for num, dirs in folders.items():
                if not store.exists(store.make_id(client["id"], num)):
                    _register_folder(client["id"], num, dirs[0], client_path)
                    novos += 1
        except Exception:
            logger.exception("falha ao importar projetos do cliente %s", client.get("name"))
    if novos:
        logger.info("%s projeto(s) registrado(s) a partir do disco", novos)
    return novos


def _write_link_shortcut(links_dir: Path, label: str, url: str) -> None:
    shortcut_stem = sanitize_folder_component(label) or "link"
    if sys.platform == "darwin":
        (links_dir / f"{shortcut_stem}.webloc").write_text(_webloc_content(url))
    else:
        (links_dir / f"{shortcut_stem}.url").write_text(_url_shortcut_content(url))


def _build_project_folder(
    client: dict[str, Any],
    project_dir: Path,
    links: list[dict[str, str]],
    uploaded_files: list | None = None,
    use_template: bool = False,
) -> None:
    """Estrutura padrão de um projeto no disco: pasta interna com .prproj/.aep
    (do modelo do cliente, se pedido), Brutos/ e Links/ com um atalho por link
    e o links.txt. Falha se a pasta já existe."""
    folder_name = project_dir.name
    project_dir.mkdir(parents=False, exist_ok=False)

    inner = project_dir / folder_name
    inner.mkdir()

    premiere_src = (
        Path(client["template_path_premiere"])
        if use_template and client.get("template_path_premiere")
        else None
    )
    if premiere_src and premiere_src.is_file():
        shutil.copy2(premiere_src, inner / f"{folder_name}.prproj")
    else:
        (inner / f"{folder_name}.prproj").touch()

    ae_src = (
        Path(client["template_path_ae"]) if use_template and client.get("template_path_ae") else None
    )
    if ae_src and ae_src.is_file():
        shutil.copy2(ae_src, inner / f"{folder_name}.aep")
    else:
        (inner / f"{folder_name}.aep").touch()

    (project_dir / "Brutos").mkdir()
    links_dir = project_dir / "Links"
    links_dir.mkdir()

    link_lines = []
    for link in links:
        _write_link_shortcut(links_dir, link["label"], link["url"])
        link_lines.append(f"{link['label']}: {link['url']}")
    (links_dir / "links.txt").write_text("\n".join(link_lines) + ("\n" if link_lines else ""))

    for f in uploaded_files or []:
        dest = project_dir / "Brutos" / secure_filename(f.filename)
        f.save(dest)


def create_project(
    client_id: str,
    name: str,
    tags: list[str] | None = None,
    due_date: str | None = None,
    links: list[dict[str, str]] | None = None,
    initial_stage: str = "backlog",
    uploaded_files: list | None = None,
    valor_individual: float = 0.0,
    use_template: bool = False,
    notes: str = "",
    criar_pasta: bool = True,
    pai: str | None = None,
) -> dict[str, Any]:
    """Cria o projeto. Com ``criar_pasta=False`` nada é tocado no disco: o
    projeto existe só no registro (``sem_pasta``), com etapa, cronômetro,
    notas, feed e links funcionando — é o caso das tarefas de um cliente
    pessoal, que dividem a pasta do cliente. Uploads e modelo são ignorados.

    ``pai`` = id de um projeto do mesmo cliente: cria um subprojeto
    "<pai>.NN", com a pasta "NN - nome" dentro da pasta do pai."""
    client = clients_module.get_client(client_id)
    if not client:
        raise ValueError(f"unknown client {client_id}")
    client_path = clients_module.client_dir(client)
    if criar_pasta:
        if client_path is None:
            raise ValueError(_sem_pasta_neste_sistema(client))
        client_path.mkdir(parents=True, exist_ok=True)

    clean_name = sanitize_folder_component(name) or "Sem nome"
    if pai:
        parent = get_project(pai)
        if not parent or parent["client_id"] != client_id:
            raise ValueError("projeto-pai não encontrado neste cliente")
        if parent["pai"]:
            raise ValueError("um subprojeto não pode ter subprojetos")
        num = next_sub_number(parent)
        local_name = f"{num.split('.')[1]} - {clean_name}"
        if criar_pasta and not parent["folder_available"]:
            raise ValueError("o projeto-pai não tem pasta neste computador; crie o subprojeto sem pasta")
        project_dir = Path(parent["path"]) / local_name if parent["folder_available"] else None
        folder_name = f"{parent['folder_name']}/{local_name}"
    else:
        num = next_project_number(client_path, client_id)
        folder_name = f"{num} - {clean_name}"
        project_dir = client_path / folder_name if client_path is not None else None
    project_id = store.make_id(client_id, num)

    saved_links: list[dict[str, str]] = []
    for link in links or []:
        url = link.get("url") or link.get("href")
        if not url:
            continue
        label = link.get("label") or link.get("nome") or "link"
        saved_links.append({"label": label, "url": url})

    try:
        if criar_pasta:
            _build_project_folder(client, project_dir, saved_links, uploaded_files, use_template)
            created_at = project_dir.stat().st_ctime
        else:
            created_at = time.time()

        record = store.new_record(
            client_id,
            num,
            clean_name,
            folder_name,
            str(project_dir) if criar_pasta else None,
            created_at,
            initial_stage,
            origem=machine.origin(),
        )
        record["sem_pasta"] = not criar_pasta
        record["tags"] = list(tags or [])
        record["due_date"] = due_date
        record["notes"] = notes or ""
        record["links"] = saved_links
        record["cost"]["valor_individual"] = valor_individual
        store.save(record)
        history.append_event(project_id, "criado", {"stage": initial_stage})

        # A tag também entra no índice global — set_project_tags já fazia isso,
        # a criação não fazia. Sem isso, uma tag criada na hora do projeto só
        # apareceria no seletor depois do próximo boot, quando a reconciliação
        # do tags.py varre o registro. Import tardio porque tags.py importa este
        # módulo; a dependência só existe neste sentido, e em tempo de execução.
        if tags:
            from . import tags as tags_module

            for tag in tags:
                tags_module.add_tag(tag)
    except Exception:
        store.delete(project_id)
        if criar_pasta and project_dir is not None and project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
        raise

    return get_project(project_id)


def create_folder_for(project_id: str) -> dict[str, Any] | None:
    """Dá pasta a um projeto criado sem ela (a tarefa cresceu e agora tem
    arquivos). Usa a mesma estrutura da criação, com os links que o registro
    já tem. Recusa se a pasta já existe."""
    project = get_project(project_id)
    if not project:
        return None
    if project["folder_available"]:
        raise ValueError("o projeto já tem pasta")
    client = clients_module.get_client(project["client_id"])
    client_path = clients_module.client_dir(client)
    if client_path is None:
        raise ValueError(_sem_pasta_neste_sistema(client))
    client_path.mkdir(parents=True, exist_ok=True)
    if project["pai"]:
        parent = get_project(project["pai"])
        if not parent or not parent["folder_available"]:
            raise ValueError("o projeto-pai não tem pasta neste computador")
        local_name = f"{project['num'].split('.')[1]} - {project['name']}"
        project_dir = Path(parent["path"]) / local_name
        folder_name = f"{parent['folder_name']}/{local_name}"
    else:
        folder_name = f"{project['num']} - {project['name']}"
        project_dir = client_path / folder_name
    try:
        _build_project_folder(client, project_dir, project["links"])
    except FileExistsError:
        raise ValueError(f"já existe uma pasta chamada {folder_name}") from None

    def apply(rec: dict[str, Any]) -> None:
        rec["sem_pasta"] = False
        rec["folder_name"] = folder_name
        rec["last_known_path"] = str(project_dir)

    store.update(project_id, apply)
    history.append_event(project_id, "pasta_criada", {"pasta": folder_name})
    return get_project(project_id)


def rename_project(project_id: str, new_name: str) -> dict[str, Any] | None:
    project = get_project(project_id)
    if not project:
        return None
    num = project["num"]
    clean_name = sanitize_folder_component(new_name) or project["name"]
    local_name = f"{num.split('.')[-1]} - {clean_name}"
    old_folder_name = project.get("folder_name") or local_name
    new_folder_name = str(PurePosixPath(old_folder_name).parent / local_name)

    if project["folder_available"]:
        old_dir = Path(project["path"])
        new_dir = old_dir.parent / local_name
        if new_dir != old_dir:
            old_dir.rename(new_dir)
            old_inner = new_dir / old_dir.name
            new_inner = new_dir / local_name
            if old_inner.is_dir() and old_inner != new_inner:
                old_inner.rename(new_inner)
        new_path = str(new_dir)
    else:
        new_path = project["path"]

    def apply(rec: dict[str, Any]) -> None:
        rec["name"] = clean_name
        rec["folder_name"] = new_folder_name
        rec["last_known_path"] = new_path

    store.update(project_id, apply)
    if old_folder_name != new_folder_name:
        history.append_event(project_id, "renomeado", {"de": old_folder_name, "para": new_folder_name})
    return get_project(project_id)


def _refuse_if_has_children(project_id: str) -> None:
    """Apagar o pai levaria a pasta dos filhos pra Lixeira e deixaria os
    registros deles órfãos — horas incluídas. Melhor pedir pra ir um a um."""
    parts = store.split_id(project_id)
    if parts and not is_sub(parts[1]):
        prefix = f"{parts[1]}."
        if any(r.get("num", "").startswith(prefix) for r in store.list_for_client(parts[0])):
            raise ValueError("este projeto tem subprojetos; exclua os subprojetos primeiro")


def remove_project_record(project_id: str) -> bool:
    """Apaga o registro de um projeto cuja pasta não existe mais.

    Com a pasta presente o registro voltaria na próxima listagem (vazio), então
    apagar seria só perder o histórico — por isso recusa.
    """
    project = get_project(project_id)
    if not project:
        return False
    if project["folder_available"]:
        raise ValueError("a pasta do projeto ainda existe; o registro só pode ser removido sem ela")
    _refuse_if_has_children(project_id)
    return store.delete(project_id)


def delete_project(project_id: str) -> bool:
    """Exclui o projeto de vez: a pasta vai pra Lixeira e o registro (horas,
    histórico, ajustes, links) é apagado. Sem pasta, só o registro."""
    project = get_project(project_id)
    if not project:
        return False
    _refuse_if_has_children(project_id)
    if project["folder_available"]:
        folder = Path(project["path"])
        client_dir = clients_module.client_dir(clients_module.get_client(project["client_id"]))
        if client_dir is None or folder.resolve() == client_dir.resolve() or client_dir.resolve() not in folder.resolve().parents:
            raise ValueError("a pasta do projeto não está dentro da pasta do cliente; não vou apagá-la")
        if not files_native.move_to_trash(str(folder)):
            raise RuntimeError(f"não consegui mandar a pasta pra Lixeira: {folder}")
    return store.delete(project_id)


def add_link(project_id: str, label: str, url: str) -> dict[str, Any] | None:
    project = get_project(project_id)
    if not project:
        return None
    label = (label or "").strip() or "link"
    url = (url or "").strip()
    if not url:
        return project

    def apply(rec: dict[str, Any]) -> None:
        rec.setdefault("links", []).append({"label": label, "url": url})

    store.update(project_id, apply)

    if project["folder_available"]:
        project_dir = Path(project["path"])
        links_dir = project_dir / "Links"
        try:
            links_dir.mkdir(exist_ok=True)
            _write_link_shortcut(links_dir, label, url)
            links_txt = links_dir / "links.txt"
            existing = links_txt.read_text() if links_txt.exists() else ""
            links_txt.write_text(existing + f"{label}: {url}\n")
        except OSError as exc:
            logger.warning("não consegui gravar o atalho do link em %s: %s", links_dir, exc)
    return get_project(project_id)


def list_links(project_id: str) -> list[dict[str, str]]:
    project = get_project(project_id)
    if not project:
        return []
    return [
        {"label": link.get("label", ""), "url": link.get("url", "")}
        for link in project.get("links") or []
        if link.get("url")
    ]


# O Frame.io é uma conta por cliente, não por projeto: ele é preenchido em
# "Editar cliente" e nunca chega nos links do projeto. Sem isso o card de Links
# do projeto abria sem o botão do Frame.io mesmo com o link cadastrado.
FRAMEIO_KEYWORDS = ("frame.io", "frameio", "frame io")


def is_frameio_label(label: str) -> bool:
    low = (label or "").lower()
    return any(k in low for k in FRAMEIO_KEYWORDS)


def merge_client_links(
    links: list[dict[str, Any]], client: dict[str, Any] | None
) -> list[dict[str, str]]:
    """Links do projeto mais o Frame.io do cadastro do cliente.

    Um Frame.io salvo no próprio projeto sempre ganha: o do cliente só entra
    quando o projeto não tem nenhum, para não aparecer botão repetido. Função
    pura porque o quadro chama isto pra cada cartão com o cliente já em mãos —
    ir buscar o cliente e o projeto de novo por id seria uma leitura do disco
    por cartão."""
    out = [
        {"label": link.get("label", ""), "url": link.get("url", "")}
        for link in links
        if link.get("url")
    ]
    if any(is_frameio_label(link["label"]) for link in out):
        return out
    url = (client or {}).get("frameio")
    if url:
        out.append({"label": "Frame.io", "url": url})
    return out


def list_links_with_client_defaults(project_id: str) -> list[dict[str, str]]:
    project = get_project(project_id)
    if not project:
        return []
    client = clients_module.get_client(project["client_id"])
    return merge_client_links(project.get("links") or [], client)


def set_notes(project_id: str, notes: str) -> dict[str, Any] | None:
    if not get_project(project_id):
        return None

    def apply(rec: dict[str, Any]) -> None:
        rec["notes"] = notes

    store.update(project_id, apply)
    return get_project(project_id)


def set_link_postagem(project_id: str, url: str) -> dict[str, Any] | None:
    if not get_project(project_id):
        return None
    url = url.strip()
    # Só http(s): o link vira window.open, e "javascript:" não pode passar.
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    store.update(project_id, lambda rec: rec.__setitem__("link_postagem", url))
    return get_project(project_id)
