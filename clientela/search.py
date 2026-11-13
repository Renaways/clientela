from typing import Any

from . import clients as clients_module
from . import projects as projects_module
from . import stages


def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    results: list[dict[str, Any]] = []
    if not q:
        return results

    for client in clients_module.list_clients():
        if q in client["name"].lower():
            results.append(
                {
                    "kind": "Cliente",
                    "title": client["name"],
                    "sub": f"{len(projects_module.list_projects(client['id']))} projetos"
                    + ("" if clients_module.is_active(client) else " · desativado"),
                    "client_id": client["id"],
                    "project_id": None,
                }
            )

    for client in clients_module.list_clients():
        for project in projects_module.list_projects(client["id"]):
            haystack = f"{project['num']} {project['name']}".lower()
            if q in haystack:
                results.append(
                    {
                        "kind": "Projeto",
                        "title": f"{project['num']} - {project['name']}",
                        "sub": stages.labels().get(project["stage"], project["stage"]),
                        "client_id": client["id"],
                        "project_id": project["id"],
                    }
                )

    return results[:limit]
