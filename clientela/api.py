import functools
import logging
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from . import acesso, ambiente, aprendizado, atualizacao, automation, backup, clients as clients_module, clips, conversor, dashboard, downloads
from . import editor, feed, feedback, files_native, financials, history, identidades, messages
from . import machine, metadados_ia, organizer, paths, previa_inserts
from . import profile as profile_module
from . import projects as projects_module
from . import search as search_module
from . import projeto_ia, silencio, stages, startup, tags, timer, transcricao, vitrine
from .persistence import safe_read_json, safe_write_json

api = Blueprint("api", __name__, url_prefix="/api")
BOOT_ID = __import__("uuid").uuid4().hex  # muda a cada vez que o app sobe: a página recarrega sozinha
logger = logging.getLogger(__name__)


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _project_id() -> str | None:
    return request.values.get("project_id") or _body().get("project_id")


def _not_found(label: str = "not found"):
    return jsonify({"error": label}), 404


def _bad_request(label: str):
    return jsonify({"error": label}), 400


def _flag(value) -> bool:
    if isinstance(value, str):
        return value in ("true", "on", "1")
    return bool(value)


def _folder_unavailable():
    return jsonify({"error": "pasta do projeto não encontrada no disco"}), 409


def _requires(feature: str):
    """Recusa a rota com 503 quando a verificação de ambiente (startup.py)
    marcou o recurso como fora do ar. O frontend já bloqueia o clique; isto
    cobre quem chama a API direto e o caso de a página estar desatualizada."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            problem = startup.feature_problem(feature)
            if problem:
                return jsonify({"error": problem["message"], **problem}), 503
            return fn(*args, **kwargs)
        return inner
    return wrap


# ---------------------------------------------------------------- Clientes
@api.get("/clients")
def get_clients():
    return jsonify([clients_module.with_folder_status(c) for c in clients_module.list_clients()])


@api.post("/clients")
def post_clients():
    body = _body()
    has_path = any(body.get(k) for k in ("path", "path_mac", "path_windows"))
    if not body.get("name") or not has_path:
        return _bad_request("name and path are required")
    client = clients_module.create_client(
        name=body["name"],
        path=body.get("path"),
        path_mac=body.get("path_mac"),
        path_windows=body.get("path_windows"),
        frameio=body.get("frameio"),
        orcamento_mensal=body.get("orcamento_mensal", 0.0),
        contrato_inicio=body.get("contrato_inicio"),
        contrato_fim=body.get("contrato_fim"),
        template_path_premiere=body.get("template_path_premiere"),
        template_path_ae=body.get("template_path_ae"),
        tipo=body.get("tipo") or clients_module.TIPO_CLIENTE,
    )
    return jsonify(client), 201


@api.put("/clients/<client_id>")
def put_client(client_id: str):
    client = clients_module.update_client(client_id, **_body())
    return jsonify(client) if client else _not_found()


@api.delete("/clients/<client_id>")
def delete_client(client_id: str):
    return jsonify({"deleted": clients_module.delete_client(client_id)})


@api.post("/clients/<client_id>/cover")
def post_client_cover(client_id: str):
    file = request.files.get("file")
    if not file or not file.filename:
        return _bad_request("file is required")
    saved = clients_module.save_cover_upload(client_id, file.filename, file)
    if not saved:
        return _bad_request("unsupported image type")
    return jsonify({"cover": saved})


@api.put("/clients/<client_id>/budget")
def put_client_budget(client_id: str):
    client = clients_module.set_budget(client_id, _body().get("orcamento_mensal", 0.0))
    return jsonify(client) if client else _not_found()


# ---------------------------------------------------------------- Projetos
@api.get("/clients/<client_id>/projects")
def get_client_projects(client_id: str):
    return jsonify(
        organizer.attach_quick_actions(client_id, projects_module.list_projects(client_id))
    )


@api.post("/clients/<client_id>/projects")
def post_client_projects(client_id: str):
    body = request.form if request.form else _body()
    name = body.get("name")
    if not name:
        return _bad_request("name is required")
    tags_value = body.get("tags")
    if isinstance(tags_value, str):
        tags_value = [t for t in tags_value.split(",") if t]
    links = body.get("links") if not request.form else None
    use_template = _flag(body.get("use_template"))
    # Ausente = cria (o comportamento de sempre); o formulário manda "0"/"1".
    criar_pasta = _flag(body.get("criar_pasta", True))
    try:
        project = projects_module.create_project(
            client_id=client_id,
            name=name,
            tags=tags_value or [],
            due_date=body.get("due_date"),
            links=links or [],
            initial_stage=body.get("initial_stage", "backlog"),
            uploaded_files=request.files.getlist("files"),
            valor_individual=float(body.get("valor_individual") or 0.0),
            use_template=use_template,
            notes=body.get("notes") or "",
            criar_pasta=criar_pasta,
            pai=body.get("pai") or None,
        )
    except ValueError as exc:
        return _bad_request(str(exc))
    return jsonify(project), 201


@api.post("/projects/create-folder")
def post_project_create_folder():
    try:
        project = projects_module.create_folder_for(_project_id() or "")
    except ValueError as exc:
        return _bad_request(str(exc))
    return jsonify(project) if project else _not_found()


@api.get("/clients/<client_id>/next-number")
def get_next_number(client_id: str):
    client = clients_module.get_client(client_id)
    if not client:
        return _not_found()
    return jsonify(
        {
            "next_number": projects_module.next_project_number(
                clients_module.client_dir(client), client_id
            )
        }
    )


@api.post("/projects/rename")
def post_project_rename():
    project = projects_module.rename_project(_project_id(), _body().get("new_name", ""))
    return jsonify(project) if project else _not_found()


@api.delete("/projects")
def delete_project_record():
    """Apaga o registro de um projeto cuja pasta sumiu. Com a pasta presente o
    registro voltaria (vazio) na próxima listagem, então recusa com 409 — a não
    ser com `excluir: true`, que manda a pasta pra Lixeira e apaga tudo."""
    body = _body()
    project_id = body.get("project_id") or _project_id() or ""
    try:
        if body.get("excluir"):
            timer.discard_running_session(project_id)
            removed = projects_module.delete_project(project_id)
        else:
            removed = projects_module.remove_project_record(project_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except RuntimeError as exc:
        logger.error("excluir projeto %s: %s", project_id, exc)
        return jsonify({"error": str(exc)}), 500
    if not removed:
        return _not_found()
    return jsonify({"removed": True})


@api.post("/projects/add-link")
def post_project_add_link():
    body = _body()
    project = projects_module.add_link(_project_id(), body.get("label", ""), body.get("url", ""))
    return jsonify(project) if project else _not_found()


# ------------------------------------------------------------ Etapa/histórico
@api.get("/projects/stage")
def get_project_stage():
    stage = stages.get_stage(_project_id())
    return jsonify({"stage": stage}) if stage else _not_found()


@api.post("/projects/stage")
def post_project_stage():
    try:
        project = stages.set_stage(_project_id(), _body().get("stage", ""))
    except ValueError as exc:
        return _bad_request(str(exc))
    return jsonify(project) if project else _not_found()


@api.get("/projects/history")
def get_project_history():
    return jsonify(stages.get_history(_project_id()))


# ------------------------------------------------------------- Tags/prazo
@api.get("/projects/tags")
def get_project_tags():
    return jsonify(tags.get_project_tags(_project_id()))


@api.post("/projects/tags")
def post_project_tags():
    project = tags.set_project_tags(_project_id(), _body().get("tags", []))
    return jsonify(project) if project else _not_found()


@api.get("/projects/due-date")
def get_project_due_date():
    return jsonify({"due_date": tags.get_due_date(_project_id())})


@api.post("/projects/due-date")
def post_project_due_date():
    project = tags.set_due_date(_project_id(), _body().get("due_date"))
    return jsonify(project) if project else _not_found()


@api.post("/projects/aguardando-externo")
def post_project_waiting_external():
    body = _body()
    motivo = (body.get("motivo") or "") if body.get("ativo") else None
    project = tags.set_waiting_external(_project_id(), motivo)
    return jsonify(project) if project else _not_found()


@api.post("/projects/notes")
def post_project_notes():
    project = projects_module.set_notes(_project_id(), _body().get("notes", ""))
    return jsonify(project) if project else _not_found()


@api.post("/projects/link-postagem")
def post_project_link_postagem():
    project = projects_module.set_link_postagem(_project_id(), str(_body().get("url") or ""))
    return jsonify(project) if project else _not_found()


# ------------------------------------------------- Ajustes e checklist
# Toda mutação devolve o feed inteiro (as duas listas): o detalhe redesenha
# os dois cartões a partir de uma resposta só.
def _feed_response(project_id: str | None):
    data = feed.get_feed(project_id)
    return jsonify(data) if data is not None else _not_found()


@api.get("/projects/feed")
def get_project_feed():
    return _feed_response(_project_id())


@api.post("/projects/feed/items")
def post_project_feed_item():
    body = _body()
    try:
        items = feed.add_item(_project_id(), body.get("kind", ""), body.get("text", ""))
    except ValueError as exc:
        return _bad_request(str(exc))
    if items is None:
        return _not_found()
    return _feed_response(_project_id())


@api.patch("/projects/feed/items")
def patch_project_feed_item():
    body = _body()
    try:
        items = feed.set_done(_project_id(), body.get("kind", ""), body.get("id", ""), body.get("done", False))
    except ValueError as exc:
        return _bad_request(str(exc))
    except KeyError:
        return _not_found("item not found")
    if items is None:
        return _not_found()
    return _feed_response(_project_id())


@api.delete("/projects/feed/items")
def delete_project_feed_item():
    # DELETE vem sem corpo: project_id, kind e id chegam na query string.
    try:
        items = feed.delete_item(_project_id(), request.args.get("kind", ""), request.args.get("id", ""))
    except ValueError as exc:
        return _bad_request(str(exc))
    except KeyError:
        return _not_found("item not found")
    if items is None:
        return _not_found()
    return _feed_response(_project_id())


@api.get("/tags")
def get_tags():
    return jsonify(tags.list_tags())


@api.post("/tags")
def post_tags():
    return jsonify(tags.add_tag(_body().get("name", "")))


# ------------------------------------------------------------- Cronômetro
@api.get("/projects/timer/status")
def get_timer_status():
    status = timer.get_status(_project_id())
    return jsonify(status) if status else _not_found()


@api.post("/projects/timer/start")
def post_timer_start():
    status = timer.start_timer(_project_id())
    return jsonify(status) if status else _not_found()


@api.post("/projects/timer/pause")
def post_timer_pause():
    status = timer.pause_timer(_project_id())
    return jsonify(status) if status else _not_found()


@api.post("/projects/timer/stop")
def post_timer_stop():
    status = timer.stop_timer(_project_id())
    return jsonify(status) if status else _not_found()


@api.post("/projects/timer/manual")
def post_timer_manual():
    body = _body()
    status = timer.log_manual(
        _project_id(), body.get("start", ""), body.get("end", ""), body.get("note", "")
    )
    return jsonify(status) if status else _not_found()


@api.post("/projects/timer/estimate")
def post_timer_estimate():
    project_id = _project_id()
    gap_minutes = int(_body().get("gap_minutes", 90))
    sessions = timer.estimate_sessions(project_id, gap_minutes)
    status = timer.apply_estimate(project_id, sessions)
    return jsonify(status) if status else _not_found()


@api.delete("/projects/timer/estimate")
def delete_timer_estimate():
    status = timer.remove_estimate(_project_id(), request.values.get("session_id", ""))
    return jsonify(status) if status else _not_found()


@api.post("/timer/estimate-all")
def post_timer_estimate_all():
    return jsonify(timer.estimate_all())


@api.get("/timer/stale")
def get_timer_stale():
    return jsonify(timer.stale_running_timer() or {})


@api.post("/projects/timer/discard")
def post_timer_discard():
    status = timer.discard_running_session(_project_id())
    return jsonify(status) if status else _not_found()


@api.get("/timer/active")
def get_timer_active():
    return jsonify(timer.get_active())


# -------------------------------------------------------- Custos/financeiro
@api.get("/cost-settings")
def get_cost_settings():
    return jsonify(financials.get_cost_settings())


@api.put("/cost-settings")
def put_cost_settings():
    return jsonify(financials.set_cost_settings(**_body()))


@api.get("/projects/cost")
def get_project_cost():
    month = request.args.get("month")
    breakdown = financials.project_cost(_project_id(), month)
    return jsonify(breakdown) if breakdown is not None else _not_found()


@api.post("/projects/cost")
def post_project_cost():
    project = financials.set_valor_individual(_project_id(), float(_body().get("valor_individual", 0.0)))
    return jsonify(project) if project else _not_found()


@api.post("/projects/cost/extra")
def post_project_cost_extra():
    body = _body()
    project = financials.add_extra_cost(
        _project_id(), body.get("nome", ""), float(body.get("valor", 0.0)), body.get("month")
    )
    return jsonify(project) if project else _not_found()


@api.delete("/projects/cost/extra")
def delete_project_cost_extra():
    project = financials.remove_extra_cost(_project_id(), request.values.get("cost_id", ""))
    return jsonify(project) if project else _not_found()


@api.post("/projects/stats-visibility")
def post_project_stats_visibility():
    project = financials.set_stats_visibility(_project_id(), bool(_body().get("oculto", False)))
    return jsonify(project) if project else _not_found()


# ---------------------------------------------------------------- Dashboards
@api.get("/dashboard/home")
def get_dashboard_home():
    return jsonify(dashboard.home())


@api.get("/dashboard")
def get_dashboard():
    client_id = request.args.get("client_id")
    if not client_id:
        return _bad_request("client_id is required")
    return jsonify(dashboard.client_stats(
        client_id, request.args.get("month"), request.args.get("periodo"), request.args.get("ref")))


@api.get("/dashboard/global")
def get_dashboard_global():
    return jsonify(dashboard.global_stats(
        request.args.get("month"), request.args.get("periodo"), request.args.get("ref")))


@api.get("/dashboard/costs")
def get_dashboard_costs():
    client_id = request.args.get("client_id")
    if not client_id:
        return _bad_request("client_id is required")
    return jsonify(dashboard.client_stats(client_id, request.args.get("month")))


@api.get("/dashboard/costs-history")
def get_dashboard_costs_history():
    client_id = request.args.get("client_id")
    if not client_id:
        return _bad_request("client_id is required")
    return jsonify(dashboard.costs_history(client_id))


@api.get("/today-overview")
def get_today_overview():
    return jsonify(dashboard.today_overview())


@api.get("/active-projects")
def get_active_projects():
    return jsonify(dashboard.active_projects())


# --------------------------------------------------------- Arquivos/organização
@api.get("/project-files")
def get_project_files():
    return jsonify(organizer.list_project_files(_project_id()))


@api.get("/project/contents")
def get_project_contents():
    project = projects_module.get_project(_project_id())
    if not project:
        return _not_found()
    if not project["folder_available"]:
        return _folder_unavailable()
    project_dir = Path(project["path"])
    out = [
        {"name": p.name, "is_dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else None}
        for p in sorted(project_dir.iterdir())
        if not p.name.startswith(".")
    ]
    return jsonify(out)


@api.get("/projects/metadados-ia")
def get_project_metadados_ia():
    project = projects_module.get_project(_project_id())
    if not project:
        return _not_found()
    if not project["folder_available"]:
        return _folder_unavailable()
    return jsonify(metadados_ia.analisar_pasta(Path(project["path"])))


# ---- IA gratuita lendo o projeto (projeto_ia.py): do que se trata, o que se espera e sugestões
def _ia_erro(exc: Exception):
    if isinstance(exc, LookupError):
        return jsonify({"error": str(exc)}), 409
    if isinstance(exc, PermissionError):
        return jsonify({"error": str(exc)}), 503
    return jsonify({"error": str(exc)}), 502  # a OpenRouter recusou, caiu ou devolveu lixo


@api.get("/projects/ia")
def get_project_ia():
    try:
        return jsonify(projeto_ia.estado(_project_id() or ""))
    except LookupError as exc:
        return _ia_erro(exc)


@api.post("/projects/ia")
def post_project_ia():
    try:
        return jsonify(projeto_ia.gerar_resumo(_project_id() or ""))
    except (LookupError, PermissionError, projeto_ia.autocorte.Falha) as exc:
        return _ia_erro(exc)


@api.get("/projects/descricao-revisada")
def get_descricao_revisada():
    try:
        return jsonify(projeto_ia.revisao_salva(_project_id() or ""))
    except LookupError as exc:
        return _ia_erro(exc)


@api.post("/projects/descricao-revisada")
def post_descricao_revisada():
    body = _body()
    try:
        return jsonify(projeto_ia.revisar_descricao(
            _project_id() or "", body.get("descricao"), bool(body.get("avancada")), bool(body.get("confirmar_tokens"))))
    except editor.EditorError as exc:
        return _editor_error(exc)
    except (LookupError, PermissionError, projeto_ia.autocorte.Falha) as exc:
        return _ia_erro(exc)


@api.put("/projects/descricao-revisada")
def put_descricao_revisada():
    body = _body()
    try:
        return jsonify(projeto_ia.editar_revisao(_project_id() or "", body.get("revisao")))
    except LookupError as exc:
        return _ia_erro(exc)


@api.post("/projects/marcadores-premiere")
def post_marcadores_premiere():
    body = _body()
    try:
        return jsonify(projeto_ia.marcadores_premiere(_project_id() or "", body.get("video") or None))
    except (LookupError, PermissionError) as exc:
        return _ia_erro(exc)


@api.post("/projects/cortar-silencio")
def post_cortar_silencio():
    try:
        return jsonify(silencio.cortar_silencio(_project_id() or "", _body()))
    except editor.EditorError as exc:
        return _editor_error(exc)


@api.get("/ia/modelos")
def get_ia_modelos():
    try:
        return jsonify(projeto_ia.listar_modelos_openrouter())
    except projeto_ia.autocorte.Falha as exc:
        return _ia_erro(exc)


@api.post("/ia/testar")
def post_ia_testar():
    body = _body()
    try:
        return jsonify(projeto_ia.testar_modelo(body.get("modelo"), body.get("temperatura")))
    except (ValueError, PermissionError, projeto_ia.autocorte.Falha) as exc:
        return _ia_erro(exc)


@api.get("/projects/chat")
def get_project_chat():
    try:
        return jsonify(projeto_ia.conversa(_project_id() or ""))
    except LookupError as exc:
        return _ia_erro(exc)


@api.post("/projects/chat")
def post_project_chat():
    body = _body()
    try:
        return jsonify(projeto_ia.perguntar(_project_id() or "", body.get("mensagem") or "", body.get("modo") or "duvida"))
    except (LookupError, PermissionError, projeto_ia.autocorte.Falha) as exc:
        return _ia_erro(exc)


@api.delete("/projects/chat")
def delete_project_chat():
    try:
        projeto_ia.limpar_conversa(_project_id() or "")
    except LookupError as exc:
        return _ia_erro(exc)
    return jsonify({"ok": True})


@api.get("/projects/referencia")
def get_project_referencia():
    project = projects_module.get_project(_project_id() or "")
    if not project:
        return _not_found()
    refs = identidades.referencias_projetos(project["client_id"])
    return jsonify({"ativo": any(r["id"] == project["id"] for r in refs), "referencias": refs})


@api.put("/projects/referencia")
def put_project_referencia():
    body = _body()
    try:
        refs = identidades.marcar_referencia(_project_id() or "", bool(body.get("ativo")))
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"ativo": any(r["id"] == _project_id() for r in refs), "referencias": refs})


@api.post("/editor/inserts-sugestoes")
def post_inserts_sugestoes():
    body = _body()
    try:
        return jsonify(projeto_ia.sugerir_telas(_project_id() or "", body.get("transcricao") or None))
    except (LookupError, PermissionError, projeto_ia.autocorte.Falha) as exc:
        return _ia_erro(exc)


# ---- feedback das ferramentas (feedback.py), em data/feedback.json para o diagnóstico
@api.get("/feedback")
def get_feedback():
    return jsonify({"itens": feedback.listar(), "resumo": feedback.resumo(), "destino": feedback.destino()})


@api.post("/feedback")
def post_feedback():
    try:
        return jsonify(feedback.registrar(_body())), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@api.post("/organizer/apply")
def post_organizer_apply():
    result = organizer.apply_organization(_project_id(), _body().get("actions", []))
    return jsonify(result)


@api.post("/files/move")
def post_files_move():
    body = _body()
    result = organizer.apply_organization(
        _project_id(), [{"type": "move", "file": body.get("file", ""), "to": body.get("to", "")}]
    )
    return jsonify(result)


def _client_folder_of(project: dict) -> str | None:
    """Pasta do cliente de um projeto, se ela existe no disco. É onde mora o
    trabalho de uma tarefa criada sem pasta própria."""
    client = clients_module.get_client(project["client_id"])
    if not client or not clients_module.folder_available(client):
        return None
    return client["path"]


@api.post("/files/open")
def post_files_open():
    body = _body()
    project = projects_module.get_project(body.get("project_id", ""))
    if not project:
        return _not_found()
    if project["sem_pasta"] and not body.get("file"):
        # "Abrir pasta" de uma tarefa sem pasta própria abre a do cliente.
        client_dir = _client_folder_of(project)
        if not client_dir:
            return _folder_unavailable()
        return jsonify({"opened": files_native.open_with_default_app(client_dir)})
    if not project["folder_available"]:
        return _folder_unavailable()
    target = Path(project["path"]) / body.get("file", "")
    if body.get("finder"):
        return jsonify({"opened": files_native.reveal_in_finder(str(target))})
    ok = files_native.open_with_default_app(str(target))
    return jsonify({"opened": ok})


@api.post("/files/open-all")
def post_files_open_all():
    project = projects_module.get_project(_project_id())
    if not project:
        return _not_found()
    if project["sem_pasta"]:
        client_dir = _client_folder_of(project)
        if not client_dir:
            return _folder_unavailable()
        return jsonify({"opened": files_native.reveal_in_finder(client_dir)})
    if not project["folder_available"]:
        return _folder_unavailable()
    ok = files_native.reveal_in_finder(project["path"])
    return jsonify({"opened": ok})


# ------------------------------------------------------------ Download de mídia
@api.get("/destinos")
def get_destinations():
    """Destinos do "Baixar mídia" e do "Edição IA" do topo."""
    return jsonify(dashboard.destinations())


@api.post("/projects/download/start")
@_requires("downloads")
def post_download_start():
    body = _body()
    urls = body.get("urls") or ([body["url"]] if body.get("url") else [])
    try:
        job_id = downloads.start_download(
            _project_id(),
            urls,
            destino=body.get("destino"),
            opcoes=body.get("opcoes"),
            meta=body.get("meta"),
            media_format=body.get("media_format", "both"),
            video_quality=body.get("video_quality", "max"),
            codec=body.get("codec", "h264"),
            audio_format=body.get("audio_format", "wav"),
            cookies_from_browser=body.get("cookies_from_browser"),
            start_time=body.get("start_time"),
            end_time=body.get("end_time"),
        )
    except downloads.OptionError as exc:
        return _bad_request(str(exc))
    if not job_id:
        return _bad_request("yt-dlp indisponível, ou o destino não existe (projeto sem pasta no disco)")
    return jsonify({"job_id": job_id, "job": downloads.get_status(job_id)})


@api.get("/projects/download/status")
def get_download_status():
    status = downloads.get_status(request.args.get("job_id", ""))
    if not status:
        return _not_found()
    status.pop("pedido", None)
    return jsonify(status)


@api.post("/projects/download/cancel")
def post_download_cancel():
    ok = downloads.cancel_download(_body().get("job_id", ""))
    return jsonify({"cancelled": ok}) if ok else _not_found()


@api.post("/projects/download/reveal")
def post_download_reveal():
    """Mostra no Finder o arquivo baixado (ou a pasta de destino)."""
    target = downloads.reveal_target(_body().get("job_id", ""))
    if not target:
        return _not_found()
    return jsonify({"opened": files_native.reveal_in_finder(str(target))})


@api.get("/downloads")
def get_downloads():
    """A fila: downloads desta sessão do app, mais novo primeiro."""
    return jsonify(downloads.list_jobs())


@api.post("/downloads/probe")
@_requires("downloads")
def post_download_probe():
    """Lê o link sem baixar: capa, título, alturas, legendas, capítulos, playlist."""
    body = _body()
    try:
        info = downloads.probe(
            body.get("url", ""),
            cookies_from_browser=body.get("cookies") or None,
            playlist=_flag(body.get("playlist")),
        )
    except downloads.ProbeError as exc:
        return jsonify({"error": str(exc)}), 422
    return jsonify(info)


# ------------------------------------------------------- Transcrição (whisper)
@api.get("/transcricao/ferramenta")
def get_transcricao_ferramenta():
    a = request.args
    return jsonify(transcricao.tool_info(a.get("python", ""), forcar=a.get("forcar") == "1"))


@api.get("/conversor")
def get_conversor():
    return jsonify(conversor.list_jobs())


@api.post("/conversor")
def post_conversor():
    """`arquivo` relativo à pasta do projeto (com `project_id`) ou absoluto (escolhido no Finder)."""
    body = _body()
    base = None
    if body.get("project_id"):
        project = projects_module.get_project(body["project_id"])
        if not project:
            return _not_found()
        if not project["folder_available"]:
            return _folder_unavailable()
        base = Path(project["path"])
    src = editor._resolve_media(base, body.get("arquivo"), conversor.ENTRADA_EXTS)
    if not src:
        return jsonify({"error": "vídeo não encontrado"}), 400
    try:
        job = conversor.start(str(src), body.get("preset", ""), body.get("opcoes"), body.get("destino") or "")
    except conversor.OptionError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"job": job})


@api.post("/conversor/<job_id>/cancel")
def post_conversor_cancel(job_id):
    return jsonify({"cancelled": True}) if conversor.cancel(job_id) else _not_found()


@api.post("/conversor/<job_id>/finder")
def post_conversor_finder(job_id):
    alvo = conversor.saida_de(job_id)
    return jsonify({"opened": files_native.reveal_in_finder(str(alvo))}) if alvo else _not_found()


@api.get("/transcricao")
def get_transcricoes():
    return jsonify(transcricao.list_jobs())


@api.post("/transcricao")
def post_transcricao():
    """Um job por arquivo; todos entram na mesma fila."""
    body = _body()
    arquivos = body.get("arquivos") or []
    if not arquivos:
        return _bad_request("arquivos is required")
    try:
        jobs = [transcricao.start(a, body.get("opcoes"), body.get("saida") or "") for a in arquivos]
    except transcricao.OptionError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"jobs": jobs})


@api.post("/transcricao/comando")
def post_transcricao_comando():
    body = _body()
    try:
        info = transcricao.tool_info((body.get("opcoes") or {}).get("python", ""))
        cmd = transcricao.comando(body.get("opcoes") or {}, body.get("arquivo") or "<arquivo>",
                                  body.get("saida") or "<pasta do arquivo>", info.get("python") or "python3")
    except transcricao.OptionError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"comando": cmd})


@api.post("/transcricao/<job_id>/cancel")
def post_transcricao_cancel(job_id):
    return jsonify({"cancelled": True}) if transcricao.cancel(job_id) else _not_found()


@api.post("/transcricao/<job_id>/retry")
def post_transcricao_retry(job_id):
    try:
        job = transcricao.retry(job_id)
    except transcricao.OptionError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"job": job}) if job else _not_found()


@api.post("/transcricao/<job_id>/abrir")
def post_transcricao_abrir(job_id):
    """Abre um arquivo gerado (no app padrão) ou mostra no Finder."""
    body = _body()
    alvo = transcricao.arquivo_de(job_id, body.get("arquivo") or "")
    if not alvo:
        return _not_found()
    if body.get("finder"):
        return jsonify({"opened": files_native.reveal_in_finder(str(alvo))})
    return jsonify({"opened": files_native.open_with_default_app(str(alvo))})


@api.get("/transcricao/<job_id>/texto")
def get_transcricao_texto(job_id):
    """O .txt gerado, para a prévia no modal."""
    job = transcricao.get_status(job_id)
    txt = next((n for n in (job or {}).get("arquivos", []) if n.endswith(".txt")), None)
    alvo = transcricao.arquivo_de(job_id, txt) if txt else None
    if not alvo or not alvo.is_file():
        return _not_found()
    return jsonify({"texto": alvo.read_text(encoding="utf-8", errors="replace")[:200_000]})


@api.post("/downloads/<job_id>/retry")
@_requires("downloads")
def post_download_retry(job_id: str):
    try:
        new_id = downloads.retry_download(job_id)
    except downloads.OptionError as exc:
        return _bad_request(str(exc))
    if not new_id:
        return _not_found()
    return jsonify({"job_id": new_id, "job": downloads.get_status(new_id)})


@api.post("/downloads/comando")
def post_download_command():
    body = _body()
    try:
        cmd = downloads.preview_command(_project_id(), body.get("destino"), body.get("opcoes") or {}, body.get("urls") or [])
    except downloads.OptionError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"comando": cmd})


@api.get("/downloads/ytdlp")
def get_ytdlp_info():
    return jsonify(downloads.tool_info())


@api.post("/downloads/ytdlp/update")
def post_ytdlp_update():
    return jsonify(downloads.update_ytdlp())


@api.post("/projects/download/estimate")
def post_download_estimate():
    body = _body()
    result = downloads.estimate_size(
        body.get("url", ""),
        media_format=body.get("media_format", "both"),
        video_quality=body.get("video_quality", "max"),
        audio_format=body.get("audio_format", "wav"),
        start_time=body.get("start_time"),
        end_time=body.get("end_time"),
        cookies_from_browser=body.get("cookies_from_browser"),
    )
    return jsonify(result)


# --------------------------------------------------------------- Mensagens/links
@api.get("/message/link-candidates")
def get_message_link_candidates():
    data = messages.get_link_candidates(_project_id())
    return jsonify(data) if data is not None else _not_found()


@api.post("/message/generate")
def post_message_generate():
    body = _body()
    message = messages.render_message(
        _project_id(), body.get("links", []), kind=body.get("kind", "whatsapp")
    )
    return jsonify({"message": message}) if message is not None else _not_found()


@api.get("/message-settings")
def get_message_settings():
    return jsonify(messages.get_settings())


@api.put("/message-settings")
def put_message_settings():
    return jsonify(messages.set_settings(**_body()))


@api.get("/message/links-from-project")
def get_links_from_project():
    return jsonify(projects_module.list_links_with_client_defaults(_project_id()))


@api.get("/browsers")
def get_browsers():
    return jsonify(files_native.detect_browsers())


@api.post("/links/open")
def post_links_open():
    body = _body()
    ok = files_native.open_url(body.get("url", ""), body.get("browser_app"))
    return jsonify({"opened": ok})


# --------------------------------------------------------------------- Backup
@api.post("/backup/run")
def post_backup_run():
    zip_path = backup.run_backup()
    return jsonify({"path": str(zip_path)})


@api.get("/backup/list")
def get_backup_list():
    return jsonify(backup.list_backups())


@api.get("/backup/config")
def get_backup_config():
    return jsonify(backup.get_backup_config())


@api.post("/backup/config")
def post_backup_config():
    return jsonify(backup.set_backup_config(**_body()))


# ----------------------------------------------------------------- Automação
@api.get("/sistema")
def get_sistema():
    """Em que máquina este app está rodando: a tela usa pra saber qual campo
    de pasta do cliente é "este computador" e onde fica data/."""
    return jsonify(
        {
            "os": machine.current_os(),
            "host": machine.hostname(),
            "path_key": machine.path_key(),
            "data_dir": str(paths.DATA_DIR),
        }
    )


@api.get("/boot")
def get_boot():
    return jsonify({"id": BOOT_ID})


@api.get("/startup/status")
def get_startup_status():
    return jsonify(startup.status())


@api.post("/startup/recheck")
def post_startup_recheck():
    return jsonify(startup.recheck())


@api.get("/automation")
def get_automation():
    return jsonify(automation.get_automation_config())


@api.put("/automation")
def put_automation():
    return jsonify(automation.set_automation_config(**_body()))


@api.get("/automation/status")
def get_automation_status():
    return jsonify(automation.automation_status())


# ---------------------------------------------------- Clipes (pasta vigiada)
@api.get("/clipes/config")
def get_clips_config():
    return jsonify(clips.get_config())


@api.put("/clipes/config")
def put_clips_config():
    try:
        return jsonify(clips.set_config(**_body()))
    except clips.ClipError as exc:
        return _bad_request(str(exc))


@api.get("/clipes/status")
def get_clips_status():
    return jsonify(clips.status())


@api.get("/clipes/eventos")
def get_clips_events():
    return jsonify(clips.events_after(request.args.get("apos", 0, type=int)))


@api.post("/clipes/verificar")
def post_clips_scan():
    """Esquece as falhas e roda um ciclo agora ("Verificar agora" no modal)."""
    clips.retry_failures()
    clips.scan_once()
    return jsonify(clips.status())


# ------------------------------------------------------------- Busca/config
@api.get("/search")
def get_search():
    return jsonify(search_module.search(request.args.get("q", "")))


def _config_publica(config):
    # o hash do PIN e o segredo do cookie de acesso remoto nunca saem pela API genérica
    return {k: v for k, v in config.items() if k != acesso.CONFIG_KEY}


@api.get("/config")
def get_config():
    return jsonify(_config_publica(safe_read_json(paths.CONFIG_JSON, {})))


@api.put("/config")
def put_config():
    config = safe_read_json(paths.CONFIG_JSON, {})
    config.update({k: v for k, v in _body().items() if k != acesso.CONFIG_KEY})
    safe_write_json(paths.CONFIG_JSON, config)
    return jsonify(_config_publica(config))


# ------------------------------------------------------------- Boas-vindas (primeira abertura)
@api.get("/boas-vindas")
def get_boas_vindas():
    # Só para quem nunca concluiu e ainda não tem cliente: quem já usa o app não vê.
    concluida = safe_read_json(paths.CONFIG_JSON, {}).get("boas_vindas_concluida")
    return jsonify({
        "mostrar": not concluida and not clients_module.list_clients(),
        "concluida": concluida,
        "pasta_dados": str(paths.DATA_DIR),
    })


@api.post("/boas-vindas")
def post_boas_vindas():
    config = safe_read_json(paths.CONFIG_JSON, {})
    config["boas_vindas_concluida"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    safe_write_json(paths.CONFIG_JSON, config)
    return jsonify({"concluida": config["boas_vindas_concluida"]})


# ------------------------------------------------------------- Versão nova no GitHub (atualizacao.py: só avisa)
@api.get("/atualizacao")
def get_atualizacao():
    return jsonify(atualizacao.verificar(forcar=request.args.get("forcar") == "1"))


# ------------------------------------------------------------- Acesso remoto (celular)
def _so_local():
    if not acesso.eh_local(request.remote_addr):
        return jsonify({"error": "o acesso remoto só se configura no próprio computador"}), 403
    return None


@api.get("/acesso")
def get_acesso():
    return jsonify({**acesso.status(), "local": acesso.eh_local(request.remote_addr)})


@api.put("/acesso")
def put_acesso():
    if (negado := _so_local()) is not None:
        return negado
    body = _body()
    try:
        st = acesso.configurar(ativo=body["ativo"] if "ativo" in body else None,
                               pin=body.get("pin") or None, senha=body.get("senha") or None,
                               usuario=body.get("usuario") if "usuario" in body else None,
                               exigir_local=body["exigir_local"] if "exigir_local" in body else None)
    except acesso.AcessoError as exc:
        return jsonify({"error": str(exc)}), exc.status
    resp = jsonify({**st, "local": True})
    # Trocar a senha troca o segredo do cookie: sem isto, quem acabou de ligar "pedir senha neste
    # computador" (ou de trocar a senha com ela ligada) cairia na tela de login na hora.
    if acesso.exige_local() and (valor := acesso.token()):
        resp.set_cookie(acesso.COOKIE, valor, max_age=acesso.COOKIE_MAX_AGE, httponly=True, samesite="Strict")
    return resp


@api.post("/acesso/reiniciar")
def post_acesso_reiniciar():
    if (negado := _so_local()) is not None:
        return negado
    try:
        acesso.reiniciar()
    except acesso.AcessoError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify({"ok": True})


@api.post("/acesso/sair")
def post_acesso_sair():
    resp = jsonify({"ok": True})
    resp.delete_cookie(acesso.COOKIE)
    return resp


@api.get("/profile")
def get_profile():
    return jsonify(profile_module.get_profile())


@api.put("/profile")
def put_profile():
    return jsonify(profile_module.set_profile(name=_body().get("name")))


@api.post("/profile/photo")
def post_profile_photo():
    file = request.files.get("file")
    if not file or not file.filename:
        return _bad_request("file is required")
    saved = profile_module.save_photo_upload(file.filename, file)
    if not saved:
        return _bad_request("unsupported image type")
    return jsonify(profile_module.get_profile())


@api.post("/dialog/folder")
def post_dialog_folder():
    path = files_native.pick_folder_dialog(_body().get("prompt", "Escolha a pasta"))
    return jsonify({"path": path})


@api.post("/dialog/file")
def post_dialog_file():
    body = _body()
    if body.get("multiplos"):
        paths_ = files_native.pick_files_dialog(body.get("prompt", "Escolha os arquivos"), body.get("inicio") or None)
        return jsonify({"paths": paths_, "path": paths_[0] if paths_ else None})
    path = files_native.pick_file_dialog(body.get("prompt", "Escolha o arquivo"), body.get("inicio") or None)
    return jsonify({"path": path})


# ------------------------------------------------- Identidade visual (perfis)
def _client_or_404(client_id: str):
    return clients_module.get_client(client_id)


def _profile_unavailable():
    return jsonify({"error": "pasta do cliente não encontrada no disco: o perfil mora nela (_Estagiário/)"}), 409


@api.get("/clients/<client_id>/identidade")
def get_identidade(client_id: str):
    if not _client_or_404(client_id):
        return _not_found()
    profile = identidades.get_profile(client_id)
    if not profile:
        return _not_found()
    job_id = profile.get("analise_job_id")
    profile["analise_job"] = editor.job_view(job_id, max_events=60) if job_id else None
    return jsonify(profile)


@api.put("/clients/<client_id>/identidade")
def put_identidade(client_id: str):
    if not _client_or_404(client_id):
        return _not_found()
    profile = identidades.save_texts(client_id, _body().get("textos") or {})
    return jsonify(profile) if profile else _profile_unavailable()


@api.post("/clients/<client_id>/identidade/status")
def post_identidade_status(client_id: str):
    if not _client_or_404(client_id):
        return _not_found()
    if not identidades.profile_dir(client_id):
        return _profile_unavailable()
    if identidades.get_status(client_id) == identidades.STATUS_EM_ANALISE:
        return jsonify({"error": "a análise ainda está rodando"}), 409
    profile = identidades.set_status(client_id, _body().get("status", ""))
    return jsonify(profile) if profile else _bad_request("status inválido")


@api.post("/clients/<client_id>/identidade/assets")
def post_identidade_asset(client_id: str):
    if not _client_or_404(client_id):
        return _not_found()
    if not identidades.profile_dir(client_id):
        return _profile_unavailable()
    categoria = request.form.get("categoria", "")
    if categoria not in identidades.ASSET_CATEGORIES:
        return _bad_request("categoria inválida")
    files = request.files.getlist("file")
    if not files:
        return _bad_request("nenhum arquivo")
    saved = []
    for f in files:
        result = identidades.add_asset(client_id, categoria, f.filename or "", f)
        if result:
            saved.append(result)
    return jsonify({"saved": saved, "profile": identidades.get_profile(client_id)})


@api.get("/clients/<client_id>/identidade/assets/<categoria>/<path:name>")
def get_identidade_asset(client_id: str, categoria: str, name: str):
    target = identidades.asset_path(client_id, categoria, name)
    if not target:
        return _not_found()
    return send_file(target, as_attachment=_flag(request.args.get("download", "1")))


@api.delete("/clients/<client_id>/identidade/assets/<categoria>/<path:name>")
def delete_identidade_asset(client_id: str, categoria: str, name: str):
    if not identidades.delete_asset(client_id, categoria, name):
        return _not_found()
    return jsonify(identidades.get_profile(client_id))


@api.post("/clients/<client_id>/identidade/abrir")
def post_identidade_open(client_id: str):
    if not _client_or_404(client_id):
        return _not_found()
    d = identidades.ensure_profile(client_id, textos=False)
    if not d:
        return _profile_unavailable()
    return jsonify({"opened": files_native.open_with_default_app(str(d))})


@api.post("/clients/<client_id>/identidade/analisar")
@_requires("editor")
def post_identidade_analyze(client_id: str):
    try:
        body = _body()
        job = editor.start_analysis_job(client_id, body.get("observacoes", ""), body.get("pastas") or [],
                                        confirmar_tokens=bool(body.get("confirmar_tokens")))
    except editor.EditorError as exc:
        return _editor_error(exc)
    return jsonify(editor.job_view(job["id"]))


# -------------------------------------------------- Editor (estagiário de IA)
def _editor_error(exc: "editor.EditorError"):
    """Erro de um job do editor. O 428 (tokens do Claude sem confirmação) leva a marca que faz o frontend
    perguntar e repetir o pedido com `confirmar_tokens`."""
    body: dict[str, Any] = {"error": str(exc)}
    if exc.status == editor.TOKENS_STATUS:
        body["confirmar_tokens"] = True
    return jsonify(body), exc.status


@api.post("/clients/<client_id>/identidade/templates")
@_requires("editor")
def post_identidade_templates(client_id: str):
    try:
        body = _body()
        job = editor.start_templates_job(client_id, body.get("observacoes", ""),
                                         confirmar_tokens=bool(body.get("confirmar_tokens")))
    except editor.EditorError as exc:
        return _editor_error(exc)
    return jsonify(editor.job_view(job["id"]))


@api.post("/clients/<client_id>/identidade/amostras")
@_requires("editor")
def post_identidade_amostras(client_id: str):
    try:
        job = editor.start_samples_job(client_id)
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(editor.job_view(job["id"]))


@api.get("/clients/<client_id>/identidade/amostras/<name>")
def get_identidade_amostra(client_id: str, name: str):
    target = identidades.sample_path(client_id, name)
    if not target:
        return _not_found()
    return send_file(target, mimetype="video/mp4", conditional=True, max_age=0)


# ------------------------------------------------- Vitrine de identidade (tela do cliente)
@api.get("/clients/<client_id>/vitrine")
def get_vitrine(client_id: str):
    v = vitrine.coletar(client_id)
    return jsonify(v) if v else _not_found()


@api.put("/clients/<client_id>/vitrine/pasta")
def put_vitrine_pasta(client_id: str):
    body = _body()
    try:
        vitrine.escolher_pasta(client_id, body.get("categoria", ""), body.get("pasta") or None)
    except ValueError as exc:
        return _bad_request(str(exc))
    except LookupError:
        return _profile_unavailable()
    return jsonify(vitrine.coletar(client_id))


@api.get("/clients/<client_id>/vitrine/arquivo")
def get_vitrine_arquivo(client_id: str):
    a = request.args
    target = vitrine.arquivo(client_id, a.get("cat", ""), a.get("origem", ""), a.get("nome", ""))
    if not target:
        return _not_found()
    return send_file(target, conditional=True, max_age=3600)


@api.post("/clients/<client_id>/vitrine/resumo")
def post_vitrine_resumo(client_id: str):
    try:
        return jsonify(vitrine.gerar_resumo(client_id))
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 409
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 503
    except vitrine.autocorte.Falha as exc:  # a OpenRouter recusou, caiu ou devolveu lixo
        return jsonify({"error": str(exc)}), 502


@api.get("/editor/status")
def get_editor_status():
    return jsonify(editor.availability())


@api.get("/editor/config")
def get_editor_config():
    return jsonify(editor.public_config())


@api.put("/editor/config")
def put_editor_config():
    body = _body()
    if body.get("reset"):
        editor.reset_config()
        return jsonify(editor.public_config())
    try:
        editor.save_config(body)
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(editor.public_config())


@api.get("/editor/candidatos")
def get_editor_candidates():
    result = editor.list_candidates(_project_id())
    return jsonify(result) if result is not None else _not_found()


@api.get("/editor/jobs")
def get_editor_jobs():
    jobs = editor.list_jobs(project_id=request.args.get("project_id"), client_id=request.args.get("client_id"))
    return jsonify([{k: v for k, v in j.items() if k != "prompt"} for j in jobs[:20]])


@api.post("/editor/jobs")
@_requires("editor")
def post_editor_job():
    body = _body()
    try:
        if body.get("destino") == "downloads":
            job = editor.start_downloads_job(
                body.get("tarefa", ""),
                body.get("video", ""),
                body.get("transcricao") or None,
                body.get("observacoes", ""),
                body.get("gcs") or None,
                client_id=body.get("client_id") or None,
                layout=body.get("layout") or None,
                edicao=body.get("edicao") or None,
                confirmar_tokens=bool(body.get("confirmar_tokens")),
                opcoes=body.get("opcoes") or None,
            )
        else:
            job = editor.start_project_job(
                _project_id(),
                body.get("tarefa", ""),
                body.get("video", ""),
                body.get("transcricao") or None,
                body.get("observacoes", ""),
                body.get("gcs") or None,
                layout=body.get("layout") or None,
                edicao=body.get("edicao") or None,
                confirmar_tokens=bool(body.get("confirmar_tokens")),
                opcoes=body.get("opcoes") or None,
            )
    except editor.EditorError as exc:
        return _editor_error(exc)
    return jsonify(editor.job_view(job["id"]))


@api.get("/editor/jobs/<job_id>")
def get_editor_job(job_id: str):
    view = editor.job_view(job_id)
    return jsonify(view) if view else _not_found()


@api.post("/editor/jobs/<job_id>/avaliacao")
def post_editor_job_avaliacao(job_id: str):
    job = editor.load_job(job_id)
    if not job:
        return _not_found()
    if job.get("status") in editor.ACTIVE_STATUSES:
        return jsonify({"error": "espere o job terminar para avaliar"}), 409
    try:
        return jsonify(aprendizado.avaliar(job, _body()))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@api.get("/editor/aprendizados")
def get_editor_aprendizados():
    return jsonify({"avaliacoes": aprendizado.listar(), "documento": aprendizado.documento()})


@api.post("/editor/jobs/<job_id>/cancel")
def post_editor_job_cancel(job_id: str):
    try:
        job = editor.cancel_job(job_id)
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(job) if job else _not_found()


@api.get("/editor/entregas")
def get_editor_deliveries():
    result = editor.list_deliveries(_project_id())
    return jsonify(result) if result is not None else _not_found()


@api.post("/editor/ajustes")
@_requires("editor")
def post_editor_adjust():
    body = _body()
    try:
        job = editor.start_adjust_job(
            _project_id(), body.get("pasta", ""), body.get("chave", ""), body.get("pedido", ""),
            confirmar_tokens=bool(body.get("confirmar_tokens")),
        )
    except editor.EditorError as exc:
        return _editor_error(exc)
    return jsonify(editor.job_view(job["id"]))


@api.post("/editor/previa")
@_requires("editor")
def post_editor_preview():
    body = _body()
    try:
        result = editor.build_preview(_project_id(), body.get("pasta", ""), body.get("chave", ""))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(result)


# ---- prévia dos inserts em HTML (previa_inserts.py): aprovar, trocar a fonte, templates universais, exportar
@api.get("/editor/inserts")
def get_inserts_previas():
    return jsonify(previa_inserts.listar(_project_id() or ""))


@api.get("/editor/inserts/<pasta>")
def get_inserts_previa(pasta: str):
    try:
        return jsonify(previa_inserts.estado(_project_id() or "", pasta))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.put("/editor/inserts/<pasta>")
def put_inserts_previa(pasta: str):
    try:
        return jsonify(previa_inserts.salvar(_project_id() or "", pasta, _body()))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.post("/editor/inserts/<pasta>/universal")
def post_inserts_universal(pasta: str):
    try:
        return jsonify(previa_inserts.salvar_universal(_project_id() or "", pasta, _body().get("spec") or {}))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.delete("/editor/inserts/<pasta>/universal/<sid>")
def delete_inserts_universal(pasta: str, sid: str):
    try:
        return jsonify(previa_inserts.remover_universal(_project_id() or "", pasta, sid))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


# O projeto vai no caminho (não na query): o HTML da composição pede assets/… relativo a ele.
@api.get("/editor/inserts-hf/<project_id>/<pasta>/<path:rel>")
def get_inserts_hf(project_id: str, pasta: str, rel: str):
    try:
        target = previa_inserts.arquivo(project_id, pasta, rel)
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    if target.suffix == ".html":
        return previa_inserts.html_previa(target), 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}
    return send_file(target, conditional=True, max_age=0)


@api.post("/editor/inserts/<pasta>/exportar")
@_requires("editor")
def post_inserts_exportar(pasta: str):
    try:
        job = editor.start_inserts_export_job(_project_id() or "", pasta, bool(_body().get("recorte", True)))
    except editor.EditorError as exc:
        return _editor_error(exc)
    return jsonify(editor.job_view(job["id"]))


@api.post("/editor/inserts/<pasta>/animar")
@_requires("editor")
def post_inserts_animar(pasta: str):
    try:
        job = editor.start_animate_job(_project_id() or "", pasta, bool(_body().get("confirmar_tokens")))
    except editor.EditorError as exc:
        return _editor_error(exc)
    return jsonify(editor.job_view(job["id"]))


@api.post("/editor/inserts/<pasta>/ajuste")
@_requires("editor")
def post_inserts_ajuste(pasta: str):
    body = _body()
    try:
        job = editor.start_preview_adjust_job(_project_id() or "", pasta, body.get("chave", ""), body.get("pedido", ""),
                                              confirmar_tokens=bool(body.get("confirmar_tokens")))
    except editor.EditorError as exc:
        return _editor_error(exc)
    return jsonify(editor.job_view(job["id"]))


@api.get("/editor/midia")
def get_editor_media_info():
    try:
        return jsonify(editor.media_info(request.args.get("project_id") or None, request.args.get("video", "")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.get("/editor/quadro")
def get_editor_frame():
    """Um quadro do vídeo para o editor de enquadramento do corte vertical."""
    try:
        t = float(request.args.get("t") or 0)
        largura = int(request.args.get("w") or editor.FRAME_WIDTH)
        target = editor.frame_path(request.args.get("project_id") or None, request.args.get("video", ""), t, largura)
    except ValueError:
        return jsonify({"error": "tempo inválido"}), 400
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return send_file(target, mimetype="image/jpeg", max_age=3600)


@api.get("/editor/video")
def get_editor_video():
    """O vídeo bruto (ou o proxy dele) para o <video> do estúdio de Edição IA."""
    try:
        target, mime = editor.video_file(
            request.args.get("project_id") or None, request.args.get("video", ""), request.args.get("proxy") == "1"
        )
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    # conditional: o <video> busca no meio por Range
    return send_file(target, mimetype=mime, conditional=True, max_age=0)


@api.post("/editor/proxy")
def post_editor_proxy():
    body = _body()
    try:
        return jsonify(editor.request_proxy(body.get("project_id") or None, body.get("video", "")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.get("/editor/proxy")
def get_editor_proxy():
    try:
        path = editor._media_for_frame(request.args.get("project_id") or None, request.args.get("video", ""))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(editor.proxy_status(path))


@api.get("/editor/onda")
def get_editor_waveform():
    try:
        faixa = int(request.args.get("faixa") or 1)
        return jsonify(editor.waveform(request.args.get("project_id") or None, request.args.get("video", ""), faixa))
    except ValueError:
        return jsonify({"error": "faixa inválida"}), 400
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.get("/editor/transcricao")
def get_editor_transcription():
    try:
        return jsonify(editor.transcription_status(request.args.get("project_id") or None, request.args.get("video", "")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.post("/editor/transcricao")
def post_editor_transcription():
    """Whisper local no bruto (sem LLM): dá as palavras aos cortes inteligentes e à trilha Fala."""
    body = _body()
    try:
        return jsonify(editor.request_transcription(body.get("project_id") or None, body.get("video", "")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.post("/editor/cortes-inteligentes")
def post_editor_smart_cuts():
    body = _body()
    try:
        return jsonify(editor.smart_cuts(
            body.get("project_id") or None, body.get("video", ""), faixa=int(body.get("faixa") or 1),
            palavras=body.get("palavras"), segmentos=body.get("segmentos"), params=body.get("params"),
            ignorar=body.get("ignorar"),
        ))
    except ValueError:
        return jsonify({"error": "faixa inválida"}), 400
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


# ------------------------------------------------- sequências multipista (.clientela)
def _seq_base():
    src = request.args if request.method == "GET" else _body()
    return editor.studio_base_dir(src.get("project_id") or None, src.get("destino"))


@api.post("/sequencia/de-corte")
def post_sequence_from_cut():
    """Abre um Corte 9x16 vN como sequência 9:16 (cria <pasta>.clientela na primeira vez)."""
    try:
        return jsonify(editor.sequence_from_cut(_seq_base(), _body().get("pasta", "")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.get("/sequencias")
def get_sequences():
    try:
        return jsonify(editor.list_sequences(_seq_base()))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.post("/sequencias")
def post_sequence():
    body = _body()
    try:
        fps = float(body.get("fps") or 30)
        largura = int(body["largura"]) if body.get("largura") else None
        altura = int(body["altura"]) if body.get("altura") else None
        return jsonify(editor.create_sequence(_seq_base(), body.get("nome", ""), body.get("preset") or "completo",
                                              largura, altura, fps))
    except (TypeError, ValueError):
        return jsonify({"error": "formato inválido"}), 400
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.get("/sequencia")
def get_sequence():
    try:
        return jsonify(editor.load_sequence(_seq_base(), request.args.get("arquivo", "")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.put("/sequencia")
def put_sequence():
    body = _body()
    try:
        return jsonify(editor.save_sequence(_seq_base(), body.get("arquivo", ""), body.get("doc")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.post("/sequencia/midia")
def post_sequence_media():
    """Lê um vídeo ou áudio para entrar na sequência (o estúdio põe na lista e salva)."""
    try:
        return jsonify(editor.sequence_media(_seq_base(), _body().get("caminho", "")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.post("/sequencia/exportar")
@_requires("editor")
def post_sequence_export():
    body = _body()
    try:
        job = editor.start_sequence_job(body.get("project_id") or None, body.get("destino"), body.get("arquivo", ""))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(editor.job_view(job["id"]))


@api.get("/sequencia/sfx")
def get_sequence_sfx():
    """A biblioteca de efeitos da pasta configurada (categorias e durações)."""
    return jsonify(editor.sfx_library())


@api.post("/sequencia/sfx")
def post_sequence_sfx():
    """Plano de Auto-SFX para a sequência enviada (o estúdio aplica como um passo de desfazer)."""
    body = _body()
    try:
        base = editor.studio_base_dir(body.get("project_id") or None, body.get("destino"))
        return jsonify(editor.sfx_plan(body.get("doc"), body.get("params"), base))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.get("/sequencia/idv")
def get_sequence_idv():
    return jsonify(editor.idv_library(request.args.get("project_id") or None, request.args.get("client_id") or None))


@api.get("/sequencia/fonte")
def get_sequence_font():
    target = editor.idv_font(request.args.get("client_id", ""), request.args.get("arquivo", ""))
    if not target:
        return _not_found()
    return send_file(target, max_age=3600)


@api.get("/sequencia/templates")
def get_sequence_templates():
    return jsonify(editor.list_seq_templates(request.args.get("client_id") or None))


@api.post("/sequencia/templates")
def post_sequence_template():
    body = _body()
    try:
        return jsonify(editor.save_seq_template(body.get("client_id") or None, body.get("template")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.get("/sequencia/legendas-estilos")
def get_caption_styles():
    return jsonify(editor.list_caption_styles(request.args.get("client_id") or None))


@api.post("/sequencia/legendas-estilos")
def post_caption_style():
    body = _body()
    try:
        return jsonify(editor.save_caption_style(body.get("client_id") or None, body.get("estilo")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.post("/sequencia/legenda/semantica")
def post_caption_semantics():
    """Passada de IA na diagramação da legenda (com a trava de tokens quando é o Claude)."""
    body = _body()
    try:
        return jsonify(editor.caption_semantics(body.get("palavras") or [], int(body.get("max_palavras") or 4),
                                                confirmar_tokens=bool(body.get("confirmar_tokens"))))
    except (TypeError, ValueError):
        return jsonify({"error": "parâmetros inválidos"}), 400
    except editor.EditorError as exc:
        return _editor_error(exc)


@api.get("/sequencia/grafico/<key>")
def get_sequence_graphic(key: str):
    return jsonify(editor.graphic_status(key))


@api.put("/sequencia/grafico/<key>/<int:n>")
def put_sequence_graphic_frame(key: str, n: int):
    """Um quadro PNG (com alfa) de um gráfico, renderizado pelo estúdio antes do export."""
    try:
        editor.save_graphic_frame(key, n, request.get_data(cache=False))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify({"ok": True})


@api.post("/sequencia/grafico/<key>/pronto")
def post_sequence_graphic_done(key: str):
    try:
        body = _body()
        return jsonify(editor.finish_graphic(key, int(body.get("quadros") or 0), body.get("estados")))
    except (TypeError, ValueError):
        return jsonify({"error": "quadros inválido"}), 400
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


# -------------------------------------------------- checklist de ambiente
@api.get("/ambiente")
def get_environment():
    """Resumo da última varredura de cada cliente ativo + o relatório do app (rápido)."""
    return jsonify({**ambiente.resumo_geral(), "app": ambiente.checar_app()})


@api.get("/ambiente/<client_id>")
def get_environment_client(client_id: str):
    try:
        return jsonify(ambiente.relatorio(client_id, forcar=request.args.get("forcar") == "1"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404


@api.post("/ambiente/fonte")
def post_environment_font():
    """Instala (copia para a pasta de fontes do usuário) uma família que o cliente usa e falta aqui."""
    body = _body()
    try:
        feito = ambiente.instalar_fonte(body.get("client_id", ""), body.get("familia", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({**feito, "relatorio": ambiente.checar_cliente(body.get("client_id", ""))})


@api.post("/ambiente/revelar")
def post_environment_reveal():
    """Mostra no Finder um arquivo da checagem — só dentro da pasta do cliente ou um modelo dele."""
    body = _body()
    client = clients_module.get_client(body.get("client_id", ""))
    alvo = Path(str(body.get("caminho") or ""))
    if not client or not alvo.exists():
        return _not_found()
    cdir = clients_module.client_dir(client)
    permitidos = {client.get("template_path_premiere"), client.get("template_path_ae")}
    dentro = bool(cdir) and (alvo.resolve() == cdir.resolve() or cdir.resolve() in alvo.resolve().parents)
    if not dentro and str(alvo) not in permitidos:
        return _not_found()
    return jsonify({"opened": files_native.reveal_in_finder(str(alvo))})


@api.get("/editor/edicoes")
def get_editor_editions():
    """Relatório de edição do projeto (sequências, exports, cortes) para o pipeline."""
    try:
        return jsonify(editor.project_editions(_project_id() or ""))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.get("/revisoes")
def get_reviews():
    return jsonify(editor.list_reviews())


@api.get("/calendario")
def get_calendar():
    return jsonify(dashboard.calendario(request.args.get("mes") or None))


@api.post("/editor/revisao")
def post_editor_review():
    """Aprovar, ajustar ou descartar o corte vertical que a IA fez sozinha de um clipe de live."""
    try:
        body = _body()
        return jsonify(editor.review_cut(_project_id() or "", body.get("acao", ""), body.get("nota", "")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.get("/editor/cortes")
def get_editor_cuts():
    """Cortes verticais já entregues no destino (projeto ou Downloads), para reabrir na timeline."""
    try:
        base = editor.studio_base_dir(request.args.get("project_id") or None, request.args.get("destino"))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify(editor.list_cuts(base))


@api.get("/editor/corte")
def get_editor_cut():
    try:
        base = editor.studio_base_dir(request.args.get("project_id") or None, request.args.get("destino"))
        return jsonify(editor.load_cut(base, request.args.get("pasta", "")))
    except editor.EditorError as exc:
        return jsonify({"error": str(exc)}), exc.status


@api.get("/editor/capa-imagem")
def get_editor_cover_image():
    """A imagem escolhida para a capa do corte, para a prévia (só imagem, caminho completo)."""
    target = editor.cover_image_path(request.args.get("caminho", ""))
    if not target:
        return _not_found()
    return send_file(target, max_age=0)


@api.get("/editor/previa/arquivo")
def get_editor_preview_file():
    target = editor.preview_path(_project_id(), request.args.get("arquivo", ""))
    if not target:
        return _not_found()
    # conditional: o <video> pede por Range para buscar no meio
    return send_file(target, mimetype="video/mp4", conditional=True, max_age=0)


@api.post("/editor/jobs/<job_id>/revelar")
def post_editor_job_reveal(job_id: str):
    """Mostra no Finder a entrega do job (ou a pasta-base, se ela ainda não
    tem nada): o "Edição IA" do topo não tem a lista de entregas do projeto."""
    job = editor.load_job(job_id)
    if not job or not job.get("entrega"):
        return _not_found()
    entrega = Path(job["entrega"])
    target = entrega if entrega.exists() else Path(job.get("cwd") or entrega.parent)
    return jsonify({"opened": files_native.reveal_in_finder(str(target))})


@api.post("/editor/revelar")
def post_editor_reveal():
    body = _body()
    target = editor.delivery_path(body.get("project_id", ""), body.get("rel", ""))
    if not target:
        return _not_found()
    return jsonify({"opened": files_native.reveal_in_finder(str(target))})
