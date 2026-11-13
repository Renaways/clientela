from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import clients as clients_module
from . import financials, paths
from . import history as history_module
from . import projects as projects_module
from . import stages as stages_module
from . import store, timer

FORGOTTEN_TIMER_HOURS = 3
STALE_BACKLOG_DAYS = 14
WAITING_EXTERNAL_DAYS = 3
DUE_SOON_DAYS = 2


# ---------------------------------------------------------------- períodos
# As estatísticas olham um mês, um ano ou o histórico inteiro. O ano e o total
# são a soma dos meses (financials.compute_client_period), então batem com o
# que se vê navegando mês a mês.

PERIOD_KINDS = ("mes", "ano", "total")
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]
MESES_CURTOS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
# Mais antigo que isso não existe na conta; serve só de piso pro "total".
_INICIO_DOS_TEMPOS = "2000-01"
_MESES_NA_SERIE_MENSAL = 6


def _valid_month(value: str | None) -> bool:
    return bool(value) and len(value) == 7 and value[4] == "-" and value[:4].isdigit() and value[5:].isdigit() \
        and 1 <= int(value[5:]) <= 12


def _shift_month(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:]) - 1 + delta
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def _month_label(month: str) -> str:
    return f"{MESES[int(month[5:]) - 1]} de {month[:4]}"


def resolve_period(periodo: str | None = None, ref: str | None = None,
                   month: str | None = None) -> dict[str, Any]:
    """Normaliza o período pedido. `month` é o parâmetro antigo (só mês) e
    continua valendo. Nada passa do mês/ano corrente."""
    hoje = financials.current_month()
    kind = periodo if periodo in PERIOD_KINDS else "mes"
    if kind == "mes":
        ref = month if _valid_month(month) else ref if _valid_month(ref) else hoje
        ref = min(ref, hoje)
        return {
            "kind": "mes", "ref": ref, "label": _month_label(ref), "months": [ref],
            "prev_ref": _shift_month(ref, -1), "next_ref": None if ref >= hoje else _shift_month(ref, 1),
        }
    if kind == "ano":
        ano = ref if ref and len(ref) == 4 and ref.isdigit() else hoje[:4]
        ano = min(ano, hoje[:4])
        return {
            "kind": "ano", "ref": ano, "label": ano,
            "months": financials.month_range(f"{ano}-01", min(f"{ano}-12", hoje)),
            "prev_ref": str(int(ano) - 1), "next_ref": None if ano >= hoje[:4] else str(int(ano) + 1),
        }
    return {
        "kind": "total", "ref": None, "label": "todo o histórico",
        "months": financials.month_range(_INICIO_DOS_TEMPOS, hoje),
        "prev_ref": None, "next_ref": None,
    }


def _visible(client_id: str) -> list[dict[str, Any]]:
    return [p for p in projects_module.list_projects(client_id) if not p.get("oculto_estatisticas")]


def _breakdown(client_id: str, period: dict[str, Any], visible: list[dict[str, Any]]) -> dict[str, Any]:
    if period["kind"] == "mes":
        return financials.compute_client_month(client_id, period["ref"], visible)
    return financials.compute_client_period(client_id, period["months"], visible)


def _series_keys(period: dict[str, Any], first_month: str | None) -> tuple[str, list[str]]:
    """Eixo do gráfico: os 6 meses até o mês escolhido; os 12 meses do ano;
    ou um ponto por ano no total (mês a mês desde 2020 seriam 80 barras)."""
    hoje = financials.current_month()
    if period["kind"] == "mes":
        return "mes", financials.month_range(_shift_month(period["ref"], -(_MESES_NA_SERIE_MENSAL - 1)), period["ref"])
    if period["kind"] == "ano":
        return "mes", financials.month_range(f"{period['ref']}-01", f"{period['ref']}-12")
    first_year = int((first_month or hoje)[:4])
    return "ano", [str(y) for y in range(first_year, int(hoje[:4]) + 1)]


def _build_series(period: dict[str, Any], hours_maps: dict[str, dict[str, float]]) -> dict[str, Any]:
    """`hours_maps` é {client_id: {"YYYY-MM": horas}}. Cada ponto traz o total
    e a quebra por cliente (a tela geral empilha; a do cliente usa o total)."""
    hoje = financials.current_month()
    meses_com_hora = [m for hm in hours_maps.values() for m, h in hm.items() if h > 0]
    granularity, keys = _series_keys(period, min(meses_com_hora) if meses_com_hora else None)
    points = []
    for key in keys:
        by_client = {}
        for cid, hm in hours_maps.items():
            h = sum(v for m, v in hm.items() if m.startswith(key)) if granularity == "ano" else hm.get(key, 0.0)
            if h:
                by_client[cid] = h
        if granularity == "mes":
            label, future = MESES_CURTOS[int(key[5:]) - 1], key > hoje
            selected = key == period["ref"] if period["kind"] == "mes" else False
        else:
            label, future, selected = key, False, False
        points.append({
            "key": key, "label": label, "hours": sum(by_client.values()),
            "by_client": by_client, "future": future, "selected": selected,
        })
    return {"granularity": granularity, "points": points}


def _comparison(period: dict[str, Any], hours_maps: dict[str, dict[str, float]]) -> dict[str, Any] | None:
    """Horas do período anterior equivalente: o mês anterior, ou o mesmo
    trecho do ano anterior (jan até o mês corrente, se o ano está em curso —
    comparar o ano parcial com o ano cheio diria sempre que caiu)."""
    if period["kind"] == "mes":
        months, label = [period["prev_ref"]], _month_label(period["prev_ref"])
    elif period["kind"] == "ano":
        ultimo = period["months"][-1][5:] if period["months"] else "12"
        months = financials.month_range(f"{period['prev_ref']}-01", f"{period['prev_ref']}-{ultimo}")
        label = period["prev_ref"] if ultimo == "12" else \
            f"{MESES_CURTOS[0]}–{MESES_CURTOS[int(ultimo) - 1]} de {period['prev_ref']}"
    else:
        return None
    hours = sum(hm.get(m, 0.0) for hm in hours_maps.values() for m in months)
    return {"label": label, "hours": hours}


def _all_client_projects():
    """Só clientes ativos: é o que alimenta a Home (pendências, KPIs). As
    estatísticas históricas (global_stats) varrem todos por conta própria."""
    for client in clients_module.list_clients():
        if not clients_module.is_active(client):
            continue
        for project in projects_module.list_projects(client["id"]):
            yield client, project


def destinations() -> dict:
    """Destinos do "Baixar mídia" e do "Edição IA" do topo: a ~/Downloads e os
    projetos ativos — clientes ativos, fora de Concluído, sem a pasta de assets
    `00 - …`. Projeto sem pasta no disco vem marcado (a tela mostra, mas não
    deixa escolher: não há onde gravar). O cronômetro rodando sobe para o topo:
    é quase sempre o projeto em que se está mexendo."""
    items = []
    for client, project in _all_client_projects():
        if project.get("stage") == stages_module.FINAL_STAGE or project.get("pasta_de_assets"):
            continue
        items.append({
            "id": project["id"],
            "num": project["num"],
            "name": project["name"],
            "client_id": client["id"],
            "client_name": client["name"],
            "stage": project.get("stage"),
            "path": project.get("path"),
            "folder_available": bool(project.get("folder_available")),
            "sem_pasta": bool(project.get("sem_pasta")),
            "timer_running": project.get("timer_status") == "running",
        })
    items.sort(key=lambda i: (not i["timer_running"], i["client_name"].lower(), -_num_key(i["num"])))
    downloads = paths.USER_DOWNLOADS_DIR
    return {
        "downloads": {"path": str(downloads), "exibir": _tilde(downloads), "disponivel": downloads.is_dir()},
        "projetos": items,
    }


def _tilde(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def _num_key(num) -> int:
    try:
        return int(num)
    except (TypeError, ValueError):
        return 0


def _parse_due(due: str) -> datetime | None:
    if not due:
        return None
    try:
        return datetime.fromisoformat(due)
    except ValueError:
        try:
            return datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            return None


def _days_since(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        desde = datetime.fromisoformat(iso).astimezone().replace(tzinfo=None)
    except ValueError:
        return None
    return (now.date() - desde.date()).days


def _sub_label(client: dict[str, Any], project: dict[str, Any]) -> str:
    # Subprojeto não tem card no quadro: a pendência diz de qual projeto ele é.
    return f"{client['name']} · {project['pai_nome']}" if project.get("pai_nome") else client["name"]


def pendencias(limit: int = 8) -> list[dict[str, Any]]:
    now = datetime.now()
    items: list[dict[str, Any]] = []

    for client, project in _all_client_projects():
        if project.get("oculto_estatisticas") or project["stage"] == stages_module.FINAL_STAGE:
            continue

        due_dt = _parse_due(project.get("due_date"))
        if due_dt:
            delta_days = (due_dt.date() - now.date()).days
            if delta_days < 0:
                items.append(
                    {
                        "kind": "Atrasado",
                        "urgency": 0,
                        "client_id": client["id"],
                        "project_id": project["id"],
                        "title": f"{project['num']} - {project['name']}",
                        "sub": _sub_label(client, project),
                        "value": f"{abs(delta_days)}d",
                    }
                )
            elif delta_days == 0:
                items.append(
                    {
                        "kind": "Hoje",
                        "urgency": 1,
                        "client_id": client["id"],
                        "project_id": project["id"],
                        "title": f"{project['num']} - {project['name']}",
                        "sub": _sub_label(client, project),
                        "value": due_dt.strftime("%d/%m"),
                    }
                )
            elif delta_days <= DUE_SOON_DAYS:
                items.append(
                    {
                        "kind": "Em breve",
                        "urgency": 2,
                        "client_id": client["id"],
                        "project_id": project["id"],
                        "title": f"{project['num']} - {project['name']}",
                        "sub": _sub_label(client, project),
                        "value": f"em {delta_days} dias",
                    }
                )

        esperando = project.get("aguardando_externo")
        if esperando:
            # Um projeto parado de propósito não é "esquecido no backlog": o
            # aviso útil é quanto tempo faz que a equipe de fora não responde.
            dias = _days_since(esperando.get("desde"), now)
            if dias is not None and dias >= WAITING_EXTERNAL_DAYS:
                motivo = esperando.get("motivo") or "equipe externa"
                items.append(
                    {
                        "kind": "Aguardando",
                        "urgency": 2,
                        "client_id": client["id"],
                        "project_id": project["id"],
                        "title": f"{project['num']} - {project['name']}",
                        "sub": f"{client['name']} · {motivo} — cobrar?",
                        "value": f"{dias}d",
                    }
                )
            continue

        if project["stage"] == "backlog" and project.get("created_at"):
            age_days = (now.timestamp() - project["created_at"]) / 86400
            if age_days > STALE_BACKLOG_DAYS:
                items.append(
                    {
                        "kind": "Sem etapa",
                        "urgency": 3,
                        "client_id": client["id"],
                        "project_id": project["id"],
                        "title": f"{project['num']} - {project['name']}",
                        "sub": f"{client['name']} · no backlog há {int(age_days)} dias",
                        "value": f"{int(age_days)}d",
                    }
                )

    active = timer.get_active()
    if active:
        started = datetime.fromisoformat(active["running_start"])
        elapsed_hours = (now - started).total_seconds() / 3600
        if elapsed_hours > FORGOTTEN_TIMER_HOURS:
            project = projects_module.get_project(active["project_id"])
            if project:
                client = clients_module.get_client(project["client_id"])
                items.append(
                    {
                        "kind": "Cronômetro",
                        "urgency": 0,
                        "client_id": project["client_id"],
                        "project_id": project["id"],
                        "title": f"{project['num']} - {project['name']}",
                        "sub": f"{client['name'] if client else ''} · rodando desde {started.strftime('%H:%M')} — esquecido?",
                        "value": f"{elapsed_hours:.1f}h",
                    }
                )

    items.sort(key=lambda i: i["urgency"])
    return items[:limit]


def client_summary(client: dict[str, Any]) -> dict[str, Any]:
    month = financials.current_month()
    breakdown = financials.compute_client_month(client["id"], month)
    all_projects = projects_module.list_projects(client["id"])
    visible = [p for p in all_projects if not p.get("oculto_estatisticas")]
    return {
        "id": client["id"],
        "name": client["name"],
        "cover": client.get("cover"),
        "created_at": client.get("created_at"),
        "folder_available": clients_module.folder_available(client),
        "ativo": clients_module.is_active(client),
        "tipo": "pessoal" if clients_module.is_personal(client) else "cliente",
        "budget": client.get("orcamento_mensal", 0.0),
        "hours": breakdown["hours"],
        "profit": breakdown["profit"],
        "project_count": len(visible),
        "stages": [p["stage"] for p in visible],
    }


def recent_sessions(limit: int = 6) -> list[dict[str, Any]]:
    rows = []
    for client, project in _all_client_projects():
        for session in project.get("sessions", []):
            if not session.get("end"):
                continue
            rows.append(
                {
                    "client_id": client["id"],
                    "project_id": project["id"],
                    "project_title": f"{project['num']} - {project['name']}",
                    "end": session["end"],
                    "seconds": session.get("seconds", 0),
                }
            )
    rows.sort(key=lambda r: r["end"], reverse=True)
    return rows[:limit]


def home() -> dict[str, Any]:
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    week_start_iso = week_ago.isoformat()

    hours_this_week = 0.0
    active_projects = 0
    delivered_this_week = 0
    all_clients = clients_module.list_clients()

    for client, project in _all_client_projects():
        if project.get("oculto_estatisticas"):
            continue
        for session in project.get("sessions", []):
            if session.get("start", "") >= week_start_iso:
                hours_this_week += timer.effective_seconds(session) / 3600.0
        if project["stage"] != stages_module.FINAL_STAGE:
            active_projects += 1

    for client, project in _all_client_projects():
        if project["stage"] == stages_module.FINAL_STAGE and not project.get("oculto_estatisticas"):
            events = history_module.get_history(project["id"])
            for event in events:
                if (
                    event.get("action") == "etapa_alterada"
                    and event.get("details", {}).get("para") == stages_module.FINAL_STAGE
                ):
                    if event.get("at", "") >= week_start_iso:
                        delivered_this_week += 1
                    break

    month = financials.current_month()
    profit_this_month = sum(
        financials.compute_client_month(c["id"], month)["profit"]
        for c in all_clients
        if clients_module.is_active(c)
    )

    return {
        "stats": {
            "hours_this_week": hours_this_week,
            "active_projects": active_projects,
            "profit_this_month": profit_this_month,
            "delivered_this_week": delivered_this_week,
        },
        "pendencias": pendencias(),
        "clients": [client_summary(c) for c in all_clients],
        "recent_sessions": recent_sessions(),
    }


def client_stats(client_id: str, month: str | None = None, periodo: str | None = None,
                 ref: str | None = None) -> dict[str, Any]:
    period = resolve_period(periodo, ref, month)
    projects_list = _visible(client_id)
    breakdown = _breakdown(client_id, period, projects_list)
    settings = financials.get_cost_settings()
    hourly = financials.hourly_cost(settings)
    hours_maps = {client_id: financials.hours_by_month(projects_list)}

    hours_by_project = [
        {
            "project_id": p["id"],
            "name": f"{p['num']} - {p['name']}",
            "hours": breakdown["projects"].get(p["id"], {}).get("horas", 0.0),
            "stage": p["stage"],
        }
        for p in projects_list
    ]
    hours_by_project.sort(key=lambda r: r["hours"], reverse=True)

    margins = []
    for p in projects_list:
        fin = breakdown["projects"].get(p["id"], {"valor": 0.0, "custo": 0.0, "lucro": 0.0, "horas": 0.0})
        margin_pct = (fin["lucro"] / fin["valor"] * 100) if fin["valor"] else 0.0
        margins.append(
            {
                "project_id": p["id"],
                "name": f"{p['num']} - {p['name']}",
                "hours": fin["horas"],
                "cost": fin["custo"],
                "value": fin["valor"],
                "margin_pct": margin_pct,
            }
        )

    tag_hours: dict[str, float] = {}
    for p in projects_list:
        hours = breakdown["projects"].get(p["id"], {}).get("horas", 0.0)
        tags = p["tags"] or []
        if not tags:
            continue
        for t in tags:
            tag_hours[t] = tag_hours.get(t, 0.0) + hours / len(tags)

    ledger = financials.list_ledger_for_client(client_id)
    budget_history = sorted(ledger.items(), reverse=True)[:3]

    return {
        "client_id": client_id,
        "month": period["ref"] if period["kind"] == "mes" else None,
        "period": period | {"months": None},
        "series": _build_series(period, hours_maps),
        "comparison": _comparison(period, hours_maps),
        "kpis": {
            "hours": breakdown["hours"],
            "value": breakdown["value_total"],
            "cost": breakdown["cost"],
            "profit": breakdown["profit"],
        },
        "hourly_cost": hourly,
        "hours_by_project": hours_by_project,
        "margins": margins,
        "tag_distribution": [
            {"tag": t, "hours": h} for t, h in sorted(tag_hours.items(), key=lambda kv: -kv[1])
        ],
        "budget_history": [{"month": m, **entry} for m, entry in budget_history],
    }


def financeiro_configurado() -> bool:
    """True quando o motor financeiro consegue produzir número que signifique algo.

    Testa o lado do **custo**, não o da receita: é `pro_labore_mensal` e
    `custos_fixos` que fazem `hourly_cost` sair de zero. Só com orçamento
    preenchido o custo é sempre 0 e o "lucro" vira uma cópia do orçamento —
    número que parece real e não é. A UI usa isso para esconder os painéis de
    dinheiro até os dois lados existirem.
    """
    settings = financials.get_cost_settings()
    if settings.get("pro_labore_mensal"):
        return True
    return any(c.get("valor_mensal") for c in settings.get("custos_fixos", []))


def global_stats(month: str | None = None, periodo: str | None = None,
                 ref: str | None = None) -> dict[str, Any]:
    """Agregação de todos os clientes juntos, para a tela de estatísticas gerais.

    Mesma forma do `client_stats`, mas o eixo principal é o cliente em vez do
    projeto. Projetos `oculto_estatisticas` ficam de fora, como em toda agregação.
    """
    period = resolve_period(periodo, ref, month)
    all_clients = clients_module.list_clients()
    hours_maps: dict[str, dict[str, float]] = {}

    hours_by_client: list[dict[str, Any]] = []
    conflitos: list[dict[str, Any]] = []
    top_projects: list[dict[str, Any]] = []
    tag_hours: dict[str, float] = {}
    stage_counts: dict[str, int] = {s: 0 for s in stages_module.STAGES}
    nomes_etapa = stages_module.labels()
    totals = {"hours": 0.0, "value": 0.0, "cost": 0.0, "profit": 0.0}
    project_total = 0

    for client in all_clients:
        visible = _visible(client["id"])
        breakdown = _breakdown(client["id"], period, visible)
        hours_maps[client["id"]] = financials.hours_by_month(visible)

        # Duas pastas com o mesmo número ("00 - A" e "00 - B") produzem o mesmo
        # project_id: só a primeira é lida como a pasta do projeto, e a outra
        # fica invisível. Em vez de esconder o problema, a tela avisa qual pasta
        # renomear (`list_projects` devolve as duas em `pastas_conflitantes`).
        for project in visible:
            if project.get("pastas_conflitantes"):
                conflitos.append(
                    {
                        "client_id": client["id"],
                        "client_name": client["name"],
                        "project_id": project["id"],
                        "folders": sorted(project["pastas_conflitantes"]),
                    }
                )

        client_hours = 0.0
        for project in visible:
            fin = breakdown["projects"].get(project["id"], {})
            hours = fin.get("horas", 0.0)
            client_hours += hours
            project_total += 1
            if project["stage"] in stage_counts:
                stage_counts[project["stage"]] += 1

            top_projects.append(
                {
                    "client_id": client["id"],
                    "client_name": client["name"],
                    "project_id": project["id"],
                    "name": f"{project['num']} - {project['name']}",
                    "stage": project["stage"],
                    "hours": hours,
                }
            )

            tags = project["tags"] or []
            for tag in tags:
                tag_hours[tag] = tag_hours.get(tag, 0.0) + hours / len(tags)

        hours_by_client.append(
            {
                "client_id": client["id"],
                "name": client["name"],
                "cover": client.get("cover"),
                "hours": client_hours,
                "project_count": len(visible),
                "value": breakdown["value_total"],
                "cost": breakdown["cost"],
                "profit": breakdown["profit"],
            }
        )

        totals["hours"] += breakdown["hours"]
        totals["value"] += breakdown["value_total"]
        totals["cost"] += breakdown["cost"]
        totals["profit"] += breakdown["profit"]

    hours_by_client.sort(key=lambda r: r["hours"], reverse=True)
    top_projects.sort(key=lambda r: r["hours"], reverse=True)

    return {
        "month": period["ref"] if period["kind"] == "mes" else None,
        "period": period | {"months": None},
        "series": _build_series(period, hours_maps),
        "comparison": _comparison(period, hours_maps),
        "financeiro_configurado": financeiro_configurado(),
        "conflitos_de_numero": conflitos,
        "kpis": {
            "hours": totals["hours"],
            "value": totals["value"],
            "cost": totals["cost"],
            "profit": totals["profit"],
            "client_count": len(all_clients),
            "project_count": project_total,
            "active_projects": project_total - stage_counts.get(stages_module.FINAL_STAGE, 0),
        },
        "hourly_cost": financials.hourly_cost(),
        "hours_by_client": hours_by_client,
        "top_projects": top_projects,
        "tag_distribution": [
            {"tag": t, "hours": h} for t, h in sorted(tag_hours.items(), key=lambda kv: -kv[1])
        ],
        "stage_distribution": [
            {"stage": s, "label": nomes_etapa[s], "count": stage_counts[s]}
            for s in stages_module.STAGES
        ],
    }


def costs_history(client_id: str) -> list[dict[str, Any]]:
    ledger = financials.list_ledger_for_client(client_id)
    return [{"month": m, **entry} for m, entry in sorted(ledger.items(), reverse=True)]


def today_overview() -> dict[str, Any]:
    home_data = home()
    return {
        "pendencias": home_data["pendencias"],
        "active_projects": home_data["stats"]["active_projects"],
    }


def active_projects() -> list[dict[str, Any]]:
    out = []
    for client, project in _all_client_projects():
        if project["stage"] != stages_module.FINAL_STAGE and not project.get("oculto_estatisticas"):
            out.append({**project, "client_name": client["name"]})
    return out



# ------------------------------------------------------------------ calendário gerencial
def _dia(valor: Any) -> date | None:
    """Data local de um created_at (epoch ou ISO), de um evento (ISO UTC) ou do início de uma sessão
    (ISO local sem fuso)."""
    if valor in (None, ""):
        return None
    try:
        if isinstance(valor, (int, float)):
            return datetime.fromtimestamp(valor).date()
        d = datetime.fromisoformat(str(valor))
        return (d.astimezone() if d.tzinfo else d).date()
    except (ValueError, OSError, OverflowError):
        return None


def calendario(mes: str | None = None) -> dict[str, Any]:
    """O mês num calendário: em cada dia, as horas editadas (as mesmas das estatísticas: timer.effective_seconds,
    no dia em que a sessão começou), os projetos que entraram e os que foram liberados; e a lista dos projetos
    do mês com entrada → liberação (ou hoje, se ainda aberto) para as barras. Inclui clientes desativados —
    é histórico — e deixa de fora o que as estatísticas deixam (pastas "00 - …", ocultos)."""
    hoje = date.today()
    try:
        ano, m = (int(x) for x in (mes or "").split("-")) if mes else (hoje.year, hoje.month)
        ini = date(ano, m, 1)
    except ValueError:
        ini = date(hoje.year, hoje.month, 1)
    fim = (date(ini.year + (ini.month == 12), ini.month % 12 + 1, 1) - timedelta(days=1))
    dias = {}
    d = ini
    while d <= fim:
        dias[d.isoformat()] = {"segundos": 0, "entradas": [], "liberacoes": [], "projetos": {}}
        d += timedelta(days=1)
    projetos = []
    for client in clients_module.list_clients():
        for p in projects_module.list_projects(client["id"]):
            if p.get("oculto_estatisticas"):
                continue
            eventos = (store.load(p["id"]) or {}).get("history") or []
            # entrada = o mais antigo entre criação, primeiro evento e primeira sessão: os projetos importados
            # em set/2026 ganharam a data da importação como created_at, mas o histórico e as horas são de antes
            # (com só o created_at, setembro tinha 130 "entradas" e lead médio negativo)
            candidatas = [_dia(p.get("created_at"))] + [_dia(e.get("at") or e.get("ts")) for e in eventos[:3]]
            candidatas += [_dia(s.get("start")) for s in (p.get("sessions") or [])[:3]]
            entrada = min((c for c in candidatas if c), default=None)
            liberado = None
            if p.get("stage") in ("liberado", stages_module.FINAL_STAGE):
                liberado = _dia(history_module.last_stage_change_to(eventos, "liberado")
                                or history_module.last_stage_change_to(eventos, stages_module.FINAL_STAGE))
            por_dia: dict[str, int] = {}
            total = 0
            for sess in p.get("sessions") or []:
                seg = timer.effective_seconds(sess)
                dia = _dia(sess.get("start"))
                if not seg or not dia:
                    continue
                total += seg
                if ini <= dia <= fim:
                    por_dia[dia.isoformat()] = por_dia.get(dia.isoformat(), 0) + seg
            aberto = p.get("stage") not in ("liberado", stages_module.FINAL_STAGE)
            no_mes = (bool(por_dia) or (entrada and ini <= entrada <= fim) or (liberado and ini <= liberado <= fim)
                      or (aberto and entrada and entrada <= fim and ini <= hoje))
            if not no_mes:
                continue
            for k, seg in por_dia.items():
                dias[k]["segundos"] += seg
                dias[k]["projetos"][p["id"]] = seg
            if entrada and ini <= entrada <= fim:
                dias[entrada.isoformat()]["entradas"].append(p["id"])
            if liberado and ini <= liberado <= fim:
                dias[liberado.isoformat()]["liberacoes"].append(p["id"])
            projetos.append({
                "id": p["id"], "client_id": client["id"], "cliente": client["name"], "num": p["num"], "nome": p["name"],
                "stage": p.get("stage"), "entrada": entrada.isoformat() if entrada else None,
                "liberado": liberado.isoformat() if liberado else None,
                "segundos_mes": sum(por_dia.values()), "segundos_total": total, "dias": por_dia,
                "lead_dias": (liberado - entrada).days if liberado and entrada else None,
            })
    projetos.sort(key=lambda x: (x["entrada"] or "9999", x["cliente"], x["num"]))
    liberados_mes = [x for x in projetos if x["liberado"] and ini.isoformat() <= x["liberado"] <= fim.isoformat()]
    leads = [x["lead_dias"] for x in liberados_mes if x["lead_dias"] is not None]
    return {
        "mes": ini.strftime("%Y-%m"), "inicio": ini.isoformat(), "fim": fim.isoformat(), "hoje": hoje.isoformat(),
        "dias": dias, "projetos": projetos,
        "resumo": {"segundos": sum(v["segundos"] for v in dias.values()),
                   "entradas": sum(len(v["entradas"]) for v in dias.values()),
                   "liberacoes": len(liberados_mes),
                   "lead_medio": round(sum(leads) / len(leads), 1) if leads else None,
                   "dias_trabalhados": sum(1 for v in dias.values() if v["segundos"])},
    }
