"""Perfis de identidade visual por cliente (<pasta do cliente>/_Estagiário/):
estrutura, textos, pessoas, assets, status e o caminho pela API."""

import io
from pathlib import Path

import pytest

from clientela import identidades
from clientela import clients as clients_module
from clientela import projects as projects_module


class _Upload:
    """Imita o FileStorage do werkzeug: só o save() é usado."""

    def __init__(self, data: bytes):
        self.data = data

    def save(self, dest: str) -> None:
        Path(dest).write_bytes(self.data)


def _client(tmp_path):
    d = tmp_path / "Aurora"
    d.mkdir()
    return clients_module.create_client(name="Aurora", path=str(d))


def _pdir(client):
    return Path(client["path"]) / "_Estagiário"


def test_perfil_mora_na_pasta_do_cliente(tmp_path):
    client = _client(tmp_path)
    d = identidades.ensure_profile(client["id"])
    assert d == _pdir(client)
    for rel in ("assets/fonts", "assets/logos", "assets/texturas", "referencias"):
        assert (d / rel).is_dir()
    for name in ("marca.md", "frame.md", "regras.md", "pessoas.csv"):
        assert (d / name).is_file()


def test_abrir_a_aba_nao_cria_pasta_nenhuma(tmp_path):
    client = _client(tmp_path)
    profile = identidades.get_profile(client["id"])
    assert profile["status"] == "sem_perfil" and profile["disponivel"] is True
    assert list(Path(client["path"]).iterdir()) == []
    # "Abrir no Finder" cria as pastas, não um perfil com textos
    d = identidades.ensure_profile(client["id"], textos=False)
    assert (d / "assets" / "fonts").is_dir() and not (d / "marca.md").exists()


def test_pasta_do_estagiario_nao_vira_projeto(tmp_path):
    client = _client(tmp_path)
    projects_module.create_project(client["id"], "Primeiro")
    identidades.save_texts(client["id"], {"marca": "# M"})
    nomes = [p["folder_name"] for p in projects_module.list_projects(client["id"])]
    assert nomes == ["001 - Primeiro"]


def test_cliente_sem_pasta_no_disco(tmp_path):
    client = _client(tmp_path)
    Path(client["path"]).rmdir()
    profile = identidades.get_profile(client["id"])
    assert profile["disponivel"] is False and profile["status"] == "sem_perfil"
    assert identidades.save_texts(client["id"], {"marca": "x"}) is None
    assert identidades.get_profile("naoexiste") is None


def test_sem_perfil_ate_alguem_escrever_algo(tmp_path):
    client = _client(tmp_path)
    assert identidades.get_status(client["id"]) == identidades.STATUS_SEM_PERFIL
    identidades.ensure_profile(client["id"])
    # o esqueleto de seções não conta como conteúdo
    assert identidades.get_status(client["id"]) == identidades.STATUS_SEM_PERFIL
    identidades.save_texts(client["id"], {"marca": "# Aurora\nCamaleão amarelo."})
    assert identidades.get_status(client["id"]) == identidades.STATUS_EM_REVISAO


def test_textos_persistem_e_chave_desconhecida_e_ignorada(tmp_path):
    client = _client(tmp_path)
    identidades.save_texts(client["id"], {
        "frame": "cores:\n  amarelo: '#FFC126'\n",
        "pessoas": "nome,cargo\nAna,Cientista política\n",
        "../../fuga": "x",
    })
    profile = identidades.get_profile(client["id"])
    assert profile["textos"]["frame"].startswith("cores:")
    assert "Ana" in profile["textos"]["pessoas"]
    assert not (tmp_path / "fuga").exists()
    # a escrita é atômica e guarda a versão anterior
    identidades.save_texts(client["id"], {"frame": "v2"})
    assert (_pdir(client) / "frame.md.bak").read_text().startswith("cores:")


def test_pessoas_como_lista_nome_cargo(tmp_path):
    client = _client(tmp_path)
    identidades.save_texts(client["id"], {"pessoas_lista": [
        {"nome": "Ana Souza", "cargo": "Cientista política, UFPE"},
        {"nome": "João", "cargo": "Diretor"},
        {"nome": " ", "cargo": ""},  # linha vazia da tela é descartada
    ]})
    csv_text = (_pdir(client) / "pessoas.csv").read_text()
    assert csv_text.splitlines()[0] == "nome,cargo"
    assert '"Cientista política, UFPE"' in csv_text  # vírgula vai entre aspas
    pessoas = identidades.get_profile(client["id"])["pessoas"]
    assert pessoas == [
        {"nome": "Ana Souza", "cargo": "Cientista política, UFPE"},
        {"nome": "João", "cargo": "Diretor"},
    ]


def test_pessoas_csv_escrito_a_mao_sem_aspas():
    assert identidades.parse_pessoas("nome,cargo\nAna, Vereadora, PT\n\n") == [
        {"nome": "Ana", "cargo": "Vereadora, PT"}]


def test_pronto_so_quando_o_usuario_marca(tmp_path):
    client = _client(tmp_path)
    identidades.save_texts(client["id"], {"regras": "Sem HUD."})
    identidades.set_status(client["id"], identidades.STATUS_PRONTO)
    assert identidades.get_status(client["id"]) == identidades.STATUS_PRONTO
    # editar depois de pronto não rebaixa
    identidades.save_texts(client["id"], {"regras": "Sem HUD. Sem encerramento."})
    assert identidades.get_status(client["id"]) == identidades.STATUS_PRONTO
    # "em_analise" é exclusivo do job
    assert identidades.set_status(client["id"], identidades.STATUS_EM_ANALISE) is None


def test_analise_vira_rascunho_nunca_pronto(tmp_path):
    client = _client(tmp_path)
    identidades.mark_analysis_started(client["id"], "a" * 32)
    assert identidades.get_status(client["id"]) == identidades.STATUS_EM_ANALISE
    identidades.mark_analysis_finished(client["id"], ok=True)
    assert identidades.get_status(client["id"]) == identidades.STATUS_EM_REVISAO


def test_analise_que_falha_volta_ao_status_anterior(tmp_path):
    client = _client(tmp_path)
    identidades.save_texts(client["id"], {"regras": "x"})
    identidades.set_status(client["id"], identidades.STATUS_PRONTO)
    identidades.mark_analysis_started(client["id"], "a" * 32)
    identidades.mark_analysis_finished(client["id"], ok=False)
    assert identidades.get_status(client["id"]) == identidades.STATUS_PRONTO


def test_assets_upload_nome_repetido_e_remocao(tmp_path):
    client = _client(tmp_path)
    r1 = identidades.add_asset(client["id"], "fonts", "Hando Black.otf", _Upload(b"1"))
    r2 = identidades.add_asset(client["id"], "fonts", "Hando Black.otf", _Upload(b"2"))
    assert r1["name"] == "Hando Black.otf"  # acento/espaço preservados
    assert r2["name"] == "Hando Black (2).otf"  # não sobrescreve
    names = [a["name"] for a in identidades.get_profile(client["id"])["assets"]["fonts"]]
    assert names == ["Hando Black (2).otf", "Hando Black.otf"]
    assert identidades.get_status(client["id"]) == identidades.STATUS_EM_REVISAO
    assert identidades.delete_asset(client["id"], "fonts", "Hando Black.otf")
    assert identidades.asset_path(client["id"], "fonts", "Hando Black.otf") is None


@pytest.mark.parametrize("name", ["../perfil.json", "../../clients.json", "/etc/passwd"])
def test_asset_path_nao_sai_da_categoria(tmp_path, name):
    client = _client(tmp_path)
    identidades.ensure_profile(client["id"])
    assert identidades.asset_path(client["id"], "fonts", name) is None


def test_categoria_e_id_invalidos(tmp_path):
    client = _client(tmp_path)
    assert identidades.add_asset(client["id"], "virus", "a.otf", _Upload(b"x")) is None
    assert identidades.profile_dir("../x") is None
    assert identidades.get_profile("../x") is None


def test_api_identidade_ciclo(tmp_path):
    from app import create_app

    client = _client(tmp_path)
    c = create_app().test_client()
    base = f"/api/clients/{client['id']}/identidade"
    r = c.get(base)
    assert r.status_code == 200 and r.json["status"] == "sem_perfil"
    r = c.put(base, json={"textos": {"marca": "# Aurora"}})
    assert r.json["status"] == "em_revisao"
    r = c.post(f"{base}/assets", data={"categoria": "logos", "file": (io.BytesIO(b"png"), "logo.png")},
               content_type="multipart/form-data")
    assert r.status_code == 200 and r.json["saved"][0]["name"] == "logo.png"
    r = c.get(f"{base}/assets/logos/logo.png")
    assert r.status_code == 200 and r.data == b"png"
    assert c.post(f"{base}/status", json={"status": "pronto"}).json["status"] == "pronto"
    assert c.delete(f"{base}/assets/logos/logo.png").status_code == 200
    assert c.get(f"{base}/assets/logos/logo.png").status_code == 404
    assert c.get("/api/clients/naoexiste/identidade").status_code == 404
