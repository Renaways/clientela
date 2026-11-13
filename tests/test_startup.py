import json
import os
import stat

from clientela import paths, startup
from clientela.startup import Dependency, Feature

# Capturado na coleta, antes de o conftest trocar por um no-op.
_REAL_START = startup.start_background_check

FEATS = {
    "render": Feature("render", "Render", actions=("render-go",)),
    "baixar": Feature("baixar", "Baixar", actions=("baixar-go",)),
}


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_executavel_encontrado_traz_versao(tmp_path):
    exe = _script(tmp_path, "ferramenta", 'echo "ferramenta 1.2.3"')
    r = startup.check_dependency(Dependency("f", "F", startup.KIND_EXECUTABLE, exe, version_args=("--version",)))
    assert r["ok"] and r["path"] == exe and r["version"] == "ferramenta 1.2.3"


def test_executavel_que_existe_mas_quebra_conta_como_faltando(tmp_path):
    exe = _script(tmp_path, "quebrado", 'echo "dyld: Library not loaded" >&2; exit 134')
    r = startup.check_dependency(Dependency("q", "Q", startup.KIND_EXECUTABLE, exe, version_args=("--version",)))
    assert not r["ok"]
    assert "Library not loaded" in r["detail"]


def test_executavel_ausente():
    r = startup.check_dependency(Dependency("x", "X", startup.KIND_EXECUTABLE, "nao-existe-clientela-xyz"))
    assert not r["ok"] and r["path"] is None


def test_modulo_python():
    assert startup.check_dependency(Dependency("j", "json", startup.KIND_PYTHON, "json"))["ok"]
    r = startup.check_dependency(Dependency("n", "N", startup.KIND_PYTHON, "modulo_que_nao_existe_xyz"))
    assert not r["ok"] and "modulo_que_nao_existe_xyz" in r["detail"]


def test_checagem_propria_com_defeito_nao_derruba_a_verificacao():
    def explode():
        raise RuntimeError("bum")

    report = startup.run_checks(
        [Dependency("c", "C", startup.KIND_CUSTOM, check=explode, features=("render",)),
         Dependency("j", "json", startup.KIND_PYTHON, "json", features=("baixar",))],
        FEATS,
    )
    by_key = {d["key"]: d for d in report["dependencies"]}
    assert not by_key["c"]["ok"] and "bum" in by_key["c"]["detail"]
    assert by_key["j"]["ok"]


def test_obrigatoria_faltando_bloqueia_e_opcional_so_desativa():
    report = startup.run_checks(
        [Dependency("ff", "FF", startup.KIND_PYTHON, "nao_existe_a", required=True, features=("render",)),
         Dependency("yt", "YT", startup.KIND_PYTHON, "nao_existe_b", required=False, features=("baixar",)),
         Dependency("ok", "OK", startup.KIND_PYTHON, "json", required=True, features=("render", "baixar"))],
        FEATS,
    )
    assert report["missing_required"] == ["ff"]
    assert report["missing_optional"] == ["yt"]
    assert report["features"]["render"]["level"] == startup.LEVEL_BLOCKED
    assert report["features"]["render"]["missing"] == ["ff"]
    assert report["features"]["render"]["actions"] == ["render-go"]
    assert report["features"]["baixar"]["level"] == startup.LEVEL_DISABLED
    assert not report["features"]["baixar"]["available"]


def test_tudo_presente_libera_os_recursos():
    report = startup.run_checks(
        [Dependency("ok", "OK", startup.KIND_PYTHON, "json", required=True, features=("render",))], FEATS
    )
    assert report["missing_required"] == [] and report["features"]["render"]["level"] == startup.LEVEL_OK


def test_dependencies_json_ajusta_padrao_e_acrescenta_nova(tmp_path):
    paths.DEPENDENCIES_JSON.write_text(json.dumps({
        "features": {"legendas_asr": {"label": "Legendas com ASR", "actions": ["ident-x"]}},
        "dependencies": [
            {"key": "node", "required": False},
            {"key": "whisper", "label": "Whisper", "kind": "executable", "target": "whisper",
             "features": ["legendas_asr"], "hint": "pip install openai-whisper"},
            {"key": "sem_tipo", "target": "x"},
            {"key": "func", "kind": "custom", "target": "x"},
        ],
    }))
    deps, features = startup.load_registry()
    by_key = {d.key: d for d in deps}
    assert by_key["node"].required is False
    assert by_key["node"].target == "node"          # o resto da entrada padrão fica
    assert by_key["whisper"].kind == startup.KIND_EXECUTABLE
    assert by_key["whisper"].features == ("legendas_asr",)
    assert "sem_tipo" not in by_key and "func" not in by_key
    assert features["legendas_asr"].actions == ("ident-x",)
    # o registro do módulo não foi tocado
    assert next(d for d in startup.DEPENDENCIES if d.key == "node").required is True


def test_registro_padrao_cobre_os_recursos_que_cita():
    keys = {k for d in startup.DEPENDENCIES for k in d.features}
    assert keys <= set(startup.FEATURES)


def test_feature_problem_nao_bloqueia_antes_de_terminar():
    assert startup.status()["status"] == "pending"
    assert startup.feature_problem("editor") is None


def test_rota_bloqueada_responde_503(monkeypatch):
    from app import create_app

    report = startup.run_checks(
        [Dependency("ffmpeg", "FFmpeg", startup.KIND_PYTHON, "nao_existe_ff", required=True,
                    features=("editor",), hint="brew install ffmpeg")],
        startup.FEATURES,
    )
    startup._store(report)
    c = create_app().test_client()
    r = c.post("/api/editor/jobs", json={"project_id": "x:001"})
    assert r.status_code == 503
    body = r.get_json()
    assert body["feature"] == "editor" and body["missing"][0]["hint"] == "brew install ffmpeg"
    assert c.get("/api/startup/status").get_json()["missing_required"] == ["ffmpeg"]


def test_verificacao_em_segundo_plano(monkeypatch):
    monkeypatch.setattr(startup, "DEPENDENCIES", [Dependency("j", "json", startup.KIND_PYTHON, "json")])
    _REAL_START().join(timeout=10)
    st = startup.status()
    assert st["status"] == "done" and st["dependencies"][0]["ok"]


def test_recheck_atualiza_o_relatorio(monkeypatch, tmp_path):
    alvo = tmp_path / "bin"
    alvo.mkdir()
    monkeypatch.setattr(startup, "DEPENDENCIES", [
        Dependency("t", "T", startup.KIND_EXECUTABLE, str(alvo / "tool"), required=True, features=("editor",))
    ])
    assert startup.recheck()["missing_required"] == ["t"]
    assert startup.feature_problem("editor")["level"] == startup.LEVEL_BLOCKED
    _script(alvo, "tool", "true")
    assert startup.recheck()["missing_required"] == []
    assert startup.feature_problem("editor") is None
    assert os.access(alvo / "tool", os.X_OK)


def test_instrucao_de_instalar_segue_o_sistema(monkeypatch):
    from clientela import machine
    dep = startup.Dependency("x", "X", startup.KIND_EXECUTABLE, "nao-existe-xyz",
                             hint="brew install x", hint_windows="winget install x", url="https://x")
    monkeypatch.setattr(machine, "current_os", lambda: machine.WINDOWS)
    assert startup.check_dependency(dep)["hint"] == "winget install x"
    monkeypatch.setattr(machine, "current_os", lambda: machine.MAC)
    r = startup.check_dependency(dep)
    assert r["hint"] == "brew install x" and r["url"] == "https://x" and not r["ok"]
    # sem hint de Windows, cai no padrão
    monkeypatch.setattr(machine, "current_os", lambda: machine.WINDOWS)
    assert startup.check_dependency(startup.Dependency("y", "Y", startup.KIND_PYTHON, "nao_existe_xyz", hint="pip y"))["hint"] == "pip y"
