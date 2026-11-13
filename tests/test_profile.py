"""Perfil do usuário — nome e foto que ancoram o menu de configurações na
topbar. Mora no config.json e a foto vai pra covers/, que o app já serve."""

import io

from clientela import paths, profile


class _Upload:
    """Imita o file_obj do Flask: só precisa de .save(destino)."""

    def __init__(self, conteudo: bytes = b"fake-image"):
        self._buf = io.BytesIO(conteudo)

    def save(self, dest):
        dest.write_bytes(self._buf.getvalue())


def test_perfil_vazio_tem_nome_padrao_e_sem_foto():
    assert profile.get_profile() == {"name": profile.DEFAULT_NAME, "photo": None}


def test_define_nome():
    assert profile.set_profile(name="Bruno")["name"] == "Bruno"
    assert profile.get_profile()["name"] == "Bruno"


def test_nome_em_branco_volta_pro_padrao():
    profile.set_profile(name="Bruno")
    assert profile.set_profile(name="   ")["name"] == profile.DEFAULT_NAME


def test_foto_vai_pra_covers_e_entra_no_perfil():
    nome = profile.save_photo_upload("qualquer-coisa.JPG", _Upload())

    assert nome == "perfil.jpg"
    assert (paths.COVERS_DIR / "perfil.jpg").is_file()
    assert profile.get_profile()["photo"] == "perfil.jpg"


def test_arquivo_que_nao_e_imagem_e_recusado():
    assert profile.save_photo_upload("curriculo.pdf", _Upload()) is None
    assert profile.get_profile()["photo"] is None


def test_trocar_a_extensao_apaga_a_foto_anterior():
    """Sem isso sobraria um perfil.jpg órfão e o navegador poderia continuar
    servindo o antigo do cache."""
    profile.save_photo_upload("foto.jpg", _Upload())
    assert (paths.COVERS_DIR / "perfil.jpg").is_file()

    profile.save_photo_upload("foto.png", _Upload())

    assert (paths.COVERS_DIR / "perfil.png").is_file()
    assert not (paths.COVERS_DIR / "perfil.jpg").exists()
    assert profile.get_profile()["photo"] == "perfil.png"


def test_trocar_a_foto_nao_apaga_o_nome():
    profile.set_profile(name="Bruno")
    profile.save_photo_upload("foto.jpg", _Upload())
    assert profile.get_profile()["name"] == "Bruno"


def test_perfil_nao_atropela_o_resto_do_config():
    """download_prefs divide o config.json com o perfil."""
    from clientela.persistence import safe_read_json, safe_write_json

    safe_write_json(paths.CONFIG_JSON, {"download_prefs": {"codec": "prores"}})

    profile.set_profile(name="Bruno")

    config = safe_read_json(paths.CONFIG_JSON, {})
    assert config["download_prefs"] == {"codec": "prores"}
    assert config["profile"]["name"] == "Bruno"
