import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# Antes de qualquer import do app: o python3 que vem no macOS é 3.9 e quebraria com um TypeError sem explicação.
if sys.version_info < (3, 10):
    sys.exit(f"O Clientela precisa do Python 3.10 ou mais novo (este é o {sys.version.split()[0]}, em {sys.executable}).\n"
             "Mac: dê dois cliques em instalar.command (ele acha um Python novo e cria o .venv) e abra pelo Clientela.app,\n"
             "ou rode: .venv/bin/python app.py · Windows: start_windows.bat")
try:
    import flask  # noqa: F401
except ImportError:
    sys.exit("Faltam os pacotes do Clientela neste Python. Mac: rode instalar.command e abra pelo Clientela.app "
             "(ou .venv/bin/python app.py) · Windows: start_windows.bat")

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory

from clientela import acesso, ambiente, automation, backup, clips, editor, paths, startup
from clientela import projects as projects_module
from clientela import stages as stages_module
from clientela import tags as tags_module
from clientela.api import api as api_blueprint


def create_app() -> Flask:
    paths.ensure_data_dirs()
    # Toda pasta de projeto ainda sem registro em data/projects/ é registrada
    # aqui, importando os metadados antigos que ela tiver em LOG/. Idempotente
    # e barato (só checa existência de arquivo), então roda no boot.
    projects_module.import_all_from_disk()
    # "Concluído" virou a etapa terminal e "Liberado" passou a ser o degrau
    # anterior. Quem estava em "liberado" com o sentido antigo vai pro arquivo
    # uma única vez (marcador em data/migrations.json).
    stages_module.migrate_liberado_to_concluido()
    # O registro global de tags é só um índice do que os projetos já usam, e
    # ele pode sumir (data/ não vem de nenhum clone) enquanto as tags seguem
    # nos registros dos projetos. Sem esta reconciliação, /api/tags nasce vazio
    # e leva junto o seletor de tags, o filtro e os pesos do rateio.
    tags_module.rebuild_registry_from_disk()
    # ffmpeg, Node, yt-dlp…: checados numa thread para não segurar o boot. O
    # frontend busca o resultado em /api/startup/status e avisa/bloqueia.
    startup.start_background_check()
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(api_blueprint)

    # Senha e acesso remoto (acesso.py): do próprio computador passa direto, a menos que o usuário tenha
    # pedido senha também aqui; de fora, só com o acesso ligado. Nos dois casos, com o cookie que a tela de
    # login entrega. Vale para a página, as capas e a API.
    @app.before_request
    def _acesso_remoto():
        local = acesso.eh_local(request.remote_addr)
        if local and not acesso.exige_local():
            return None
        api_req = request.path.startswith("/api/")
        if not local and not acesso.ativo():
            return (jsonify({"error": "o acesso remoto está desligado"}), 403) if api_req else (acesso.DESLIGADO, 403)
        if request.path == "/acesso/entrar" or acesso.cookie_valido(request.cookies.get(acesso.COOKIE)):
            return None
        if api_req:
            return jsonify({"error": "entre com a senha", "pin": True}), 401
        return acesso.pagina_pin(), 401

    @app.post("/acesso/entrar")
    def acesso_entrar():
        local = acesso.eh_local(request.remote_addr)
        if local and not acesso.exige_local():
            return redirect("/")
        try:
            valor = acesso.conferir_pin(request.remote_addr, request.form.get("pin", ""),
                                        usuario=request.form.get("usuario"), local=local)
        except acesso.AcessoError as exc:
            return acesso.pagina_pin(str(exc), request.form.get("usuario", "")), exc.status
        resp = redirect("/")
        resp.set_cookie(acesso.COOKIE, valor, max_age=acesso.COOKIE_MAX_AGE, httponly=True, samesite="Strict")
        return resp

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/covers/<path:filename>")
    def cover(filename):
        return send_from_directory(paths.COVERS_DIR, filename)

    return app


def _setup_logging() -> None:
    paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(paths.LOGS_DIR / "app.log", maxBytes=2_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("clientela")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)


app = create_app()

def _senha_pelo_terminal() -> None:
    """`python app.py --redefinir-senha` / `--remover-senha`: quem esqueceu a senha e está na máquina."""
    import getpass
    if "--remover-senha" in sys.argv:
        acesso.remover_senha()
        print("Senha removida. O app volta a abrir sem senha neste computador e o acesso remoto ficou desligado.")
        return
    senha = getpass.getpass("Nova senha: ")
    if senha != getpass.getpass("Repita a senha: "):
        sys.exit("As senhas não batem; nada mudou.")
    try:
        acesso.configurar(senha=senha)
    except acesso.AcessoError as exc:
        sys.exit(f"Não mudei a senha: {exc}")
    print("Senha trocada. Todos os aparelhos precisam entrar de novo.")


if __name__ == "__main__":
    if "--redefinir-senha" in sys.argv or "--remover-senha" in sys.argv:
        _senha_pelo_terminal()
        sys.exit(0)
    _setup_logging()
    automation.start_all()
    # Pasta de gravação vigiada: clipe novo e finalizado vira projeto (clips.py)
    # e, com "corte automático" ligado, entra no corte vertical do editor.
    clips.on_clip_ready(editor.autocut_from_clip)
    clips.start_watcher()
    # Checklist de ambiente (fontes, pastas, mídias linkadas) dos clientes ativos, em silêncio.
    ambiente.start_background_scan()
    # Jobs do estagiário que estavam rodando quando o app fechou: o `claude`
    # roda numa sessão própria e sobrevive; aqui o app volta a acompanhar.
    editor.resume_jobs()
    backup.start_daily_scheduler()
    # Com o acesso remoto ligado (e PIN cadastrado) escuta em todas as interfaces; senão, só aqui.
    acesso.escutando = acesso.host_de_escuta()
    acesso.argv_boot = [sys.executable, os.path.abspath(__file__)]
    app.run(host=acesso.escutando, port=acesso.PORTA, threaded=True)
