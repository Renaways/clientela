import uuid
from datetime import datetime
from typing import Any

from . import clients as clients_module
from . import paths
from . import projects as projects_module
from . import store, timer
from .persistence import safe_read_json, safe_write_json

DEFAULT_COST_SETTINGS = {
    "pro_labore_mensal": 0.0,
    "horas_capacidade_mensal": 160.0,
    "imposto_percentual": 0.0,
    "custos_fixos": [],
    "tag_weights": {},
}


def _now_iso() -> str:
    return datetime.now().isoformat()


def current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def month_range(start: str, end: str) -> list[str]:
    """Meses "YYYY-MM" de `start` a `end`, inclusive. Vazio se start > end."""
    y, m = int(start[:4]), int(start[5:7])
    end_y, end_m = int(end[:4]), int(end[5:7])
    out: list[str] = []
    while (y, m) <= (end_y, end_m):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def contract_bounds(client: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Primeiro e último mês do contrato ("YYYY-MM"), None no lado em aberto."""
    if not client:
        return None, None
    inicio = (client.get("contrato_inicio") or "")[:7] or None
    fim = (client.get("contrato_fim") or "")[:7] or None
    return inicio, fim


def has_contract(client: dict[str, Any] | None) -> bool:
    return any(contract_bounds(client))


def month_is_billable(client: dict[str, Any] | None, month: str) -> bool:
    """O orçamento mensal só vira receita nos meses do contrato.

    Sem isso, somar um ano ou o histórico inteiro cobraria o orçamento de
    todo mês que tem uma sessão — inclusive as estimativas por atividade de
    pasta de 2020, anos antes do contrato. Um mês conta se qualquer dia dele
    cai dentro do contrato. Cliente sem datas de contrato fatura qualquer mês,
    como sempre foi.
    """
    inicio, fim = contract_bounds(client)
    if inicio and month < inicio:
        return False
    if fim and month > fim:
        return False
    return True


def get_cost_settings() -> dict[str, Any]:
    return safe_read_json(paths.COST_SETTINGS_JSON, dict(DEFAULT_COST_SETTINGS))


def set_cost_settings(**fields: Any) -> dict[str, Any]:
    settings = get_cost_settings()
    for key in (
        "pro_labore_mensal",
        "horas_capacidade_mensal",
        "imposto_percentual",
        "custos_fixos",
        "tag_weights",
    ):
        if key in fields:
            settings[key] = fields[key]
    safe_write_json(paths.COST_SETTINGS_JSON, settings)
    return settings


def add_fixed_cost(nome: str, valor_mensal: float) -> dict[str, Any]:
    settings = get_cost_settings()
    settings.setdefault("custos_fixos", []).append(
        {"id": uuid.uuid4().hex, "nome": nome, "valor_mensal": valor_mensal}
    )
    safe_write_json(paths.COST_SETTINGS_JSON, settings)
    return settings


def remove_fixed_cost(cost_id: str) -> dict[str, Any]:
    settings = get_cost_settings()
    settings["custos_fixos"] = [c for c in settings.get("custos_fixos", []) if c["id"] != cost_id]
    safe_write_json(paths.COST_SETTINGS_JSON, settings)
    return settings


def hourly_cost(settings: dict[str, Any] | None = None) -> float:
    settings = settings or get_cost_settings()
    fixed = sum(c.get("valor_mensal", 0.0) for c in settings.get("custos_fixos", []))
    capacity = settings.get("horas_capacidade_mensal") or 1.0
    return (settings.get("pro_labore_mensal", 0.0) + fixed) / capacity


def project_hours_in_month(project: dict[str, Any], month: str) -> float:
    total_seconds = 0
    for session in project.get("sessions", []):
        start = session.get("start")
        if start and start[:7] == month:
            total_seconds += timer.effective_seconds(session)
    return total_seconds / 3600.0


def _project_weight(project: dict[str, Any], tag_weights: dict[str, float]) -> float:
    tags = project.get("tags") or []
    if not tags:
        return 1.0
    return sum(tag_weights.get(t, 1.0) for t in tags) / len(tags)


def apportion_project_values(
    client_id: str, month: str, visible_projects: list[dict[str, Any]] | None = None
) -> dict[str, float]:
    """valor_individual > 0 is kept as-is; the client's remaining monthly
    budget (after manual values) is split across zero-value projects
    proportional to their tag weight (untagged/unweighted => weight 1)."""
    client = clients_module.get_client(client_id)
    if not client:
        return {}
    settings = get_cost_settings()
    tag_weights = settings.get("tag_weights", {})
    visible = (
        visible_projects
        if visible_projects is not None
        else [p for p in projects_module.list_projects(client_id) if not p.get("oculto_estatisticas")]
    )

    manual = {p["id"]: p["valor_individual"] for p in visible if p.get("valor_individual")}
    zero_value = [p for p in visible if not p.get("valor_individual")]

    budget = client.get("orcamento_mensal", 0.0) or 0.0
    remaining = budget - sum(manual.values())

    result = dict(manual)
    if zero_value:
        total_weight = sum(_project_weight(p, tag_weights) for p in zero_value)
        if remaining > 0 and total_weight > 0:
            for p in zero_value:
                result[p["id"]] = remaining * (_project_weight(p, tag_weights) / total_weight)
        else:
            for p in zero_value:
                result[p["id"]] = 0.0
    return result


def compute_client_month(
    client_id: str, month: str, visible: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """`visible` deixa quem soma vários meses varrer as pastas uma vez só."""
    client = clients_module.get_client(client_id)
    settings = get_cost_settings()
    hourly = hourly_cost(settings)
    tax_pct = settings.get("imposto_percentual", 0.0) / 100.0

    if visible is None:
        visible = [p for p in projects_module.list_projects(client_id) if not p.get("oculto_estatisticas")]
    values = apportion_project_values(client_id, month, visible) if month_is_billable(client, month) else {}
    # Cliente pessoal: as horas contam, o dinheiro não. Sem receita, o custo
    # das horas viraria prejuízo e puxaria o lucro do mês de todo o estúdio.
    sem_financeiro = clients_module.is_personal(client)

    per_project: dict[str, Any] = {}
    total_cost = 0.0
    total_value = 0.0
    total_hours = 0.0
    for p in visible:
        hours = project_hours_in_month(p, month)
        extra = sum(
            e.get("valor", 0.0) for e in p.get("custos_extra", []) if e.get("month") == month
        )
        valor = values.get(p["id"], 0.0)
        tax = valor * tax_pct
        cost = hours * hourly + extra + tax
        if sem_financeiro:
            valor = cost = 0.0
        per_project[p["id"]] = {
            "valor": valor,
            "custo": cost,
            "lucro": valor - cost,
            "horas": hours,
        }
        total_cost += cost
        total_value += valor
        total_hours += hours

    return {
        "client_id": client_id,
        "month": month,
        "orcamento": client.get("orcamento_mensal", 0.0) if client else 0.0,
        "value_total": total_value,
        "cost": total_cost,
        "profit": total_value - total_cost,
        "hours": total_hours,
        "projects": per_project,
    }


def activity_months(visible: list[dict[str, Any]]) -> set[str]:
    """Meses em que algum projeto teve sessão ou custo extra lançado."""
    months: set[str] = set()
    for p in visible:
        for session in p.get("sessions", []):
            if session.get("start"):
                months.add(session["start"][:7])
        for extra in p.get("custos_extra", []):
            if extra.get("month"):
                months.add(extra["month"])
    return months


def client_period_months(
    client: dict[str, Any], visible: list[dict[str, Any]], months: list[str]
) -> list[str]:
    """Dos `months` pedidos, os que podem ter número para este cliente: os com
    atividade e, se há contrato, os meses dele (o orçamento é receita mesmo em
    mês sem sessão). Sem contrato, só os meses com atividade — senão o total
    de um cliente sem datas cobraria o orçamento desde o primeiro mês da conta.
    Nunca passa do mês corrente."""
    hoje = current_month()
    ativos = activity_months(visible)
    contrato = has_contract(client)
    return [
        m for m in months
        if m <= hoje and (m in ativos or (contrato and month_is_billable(client, m)))
    ]


def hours_by_month(visible: list[dict[str, Any]]) -> dict[str, float]:
    """Horas efetivas (piso de 15 min e corte de horário já aplicados) por mês."""
    out: dict[str, float] = {}
    for p in visible:
        for session in p.get("sessions", []):
            start = session.get("start")
            if start:
                out[start[:7]] = out.get(start[:7], 0.0) + timer.effective_seconds(session) / 3600.0
    return out


def compute_client_period(
    client_id: str, months: list[str], visible: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Soma de `compute_client_month` nos meses do período em que o cliente tem
    número (ver `client_period_months`). É a mesma conta de navegar mês a mês
    e somar — ano e total não têm regra própria."""
    client = clients_module.get_client(client_id)
    if visible is None:
        visible = [p for p in projects_module.list_projects(client_id) if not p.get("oculto_estatisticas")]
    per_project: dict[str, dict[str, float]] = {}
    by_month: dict[str, dict[str, float]] = {}
    totals = {"value_total": 0.0, "cost": 0.0, "profit": 0.0, "hours": 0.0}
    for month in client_period_months(client, visible, months) if client else []:
        mes = compute_client_month(client_id, month, visible)
        by_month[month] = {
            "hours": mes["hours"], "value": mes["value_total"],
            "cost": mes["cost"], "profit": mes["profit"],
        }
        for key in totals:
            totals[key] += mes[key]
        for pid, fin in mes["projects"].items():
            acc = per_project.setdefault(pid, {"valor": 0.0, "custo": 0.0, "lucro": 0.0, "horas": 0.0})
            for key in acc:
                acc[key] += fin[key]
    for p in visible:
        per_project.setdefault(p["id"], {"valor": 0.0, "custo": 0.0, "lucro": 0.0, "horas": 0.0})
    return {
        "client_id": client_id,
        "orcamento": client.get("orcamento_mensal", 0.0) if client else 0.0,
        **totals,
        "projects": per_project,
        "by_month": by_month,
    }


def project_cost(project_id: str, month: str | None = None) -> dict[str, Any] | None:
    project = projects_module.get_project(project_id)
    if not project:
        return None
    month = month or current_month()
    breakdown = compute_client_month(project["client_id"], month)
    return breakdown["projects"].get(
        project_id, {"valor": 0.0, "custo": 0.0, "lucro": 0.0, "horas": 0.0}
    )


def set_valor_individual(project_id: str, valor: float) -> dict[str, Any] | None:
    if not projects_module.get_project(project_id):
        return None
    store.update(project_id, lambda rec: rec["cost"].__setitem__("valor_individual", valor))
    return projects_module.get_project(project_id)


def add_extra_cost(
    project_id: str, nome: str, valor: float, month: str | None = None
) -> dict[str, Any] | None:
    if not projects_module.get_project(project_id):
        return None
    extra = {"id": uuid.uuid4().hex, "nome": nome, "valor": valor, "month": month or current_month()}
    store.update(project_id, lambda rec: rec["cost"].setdefault("custos_extra", []).append(extra))
    return projects_module.get_project(project_id)


def remove_extra_cost(project_id: str, cost_id: str) -> dict[str, Any] | None:
    if not projects_module.get_project(project_id):
        return None

    def apply(rec: dict[str, Any]) -> None:
        rec["cost"]["custos_extra"] = [
            e for e in rec["cost"].get("custos_extra", []) if e.get("id") != cost_id
        ]

    store.update(project_id, apply)
    return projects_module.get_project(project_id)


def set_stats_visibility(project_id: str, oculto: bool) -> dict[str, Any] | None:
    if not projects_module.get_project(project_id):
        return None
    store.update(project_id, lambda rec: rec["cost"].__setitem__("oculto_estatisticas", bool(oculto)))
    return projects_module.get_project(project_id)


def close_month(client_id: str, month: str) -> dict[str, Any]:
    result = compute_client_month(client_id, month)
    ledger = safe_read_json(paths.MONTHLY_LEDGER_JSON, {})
    key = f"{client_id}:{month}"
    entry = {
        "orcamento": result["orcamento"],
        "cost": result["cost"],
        "profit": result["profit"],
        "hours": result["hours"],
        "projects": result["projects"],
        "closed_at": _now_iso(),
    }
    ledger[key] = entry
    safe_write_json(paths.MONTHLY_LEDGER_JSON, ledger)
    return entry


def get_ledger_entry(client_id: str, month: str) -> dict[str, Any] | None:
    ledger = safe_read_json(paths.MONTHLY_LEDGER_JSON, {})
    return ledger.get(f"{client_id}:{month}")


def list_ledger_for_client(client_id: str) -> dict[str, Any]:
    ledger = safe_read_json(paths.MONTHLY_LEDGER_JSON, {})
    prefix = f"{client_id}:"
    return {k[len(prefix):]: v for k, v in ledger.items() if k.startswith(prefix)}
