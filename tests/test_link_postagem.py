"""Link de postagem: a URL do post publicado fica no registro do projeto."""

from clientela import clients as clients_module
from clientela import projects as projects_module


def test_link_postagem_salva_e_so_aceita_http(tmp_path):
    d = tmp_path / "Cliente"
    d.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(d))
    p = projects_module.create_project(client["id"], "Reel")
    assert p["link_postagem"] == ""

    p = projects_module.set_link_postagem(p["id"], " https://instagram.com/p/abc ")
    assert projects_module.get_project(p["id"])["link_postagem"] == "https://instagram.com/p/abc"

    p = projects_module.set_link_postagem(p["id"], "javascript:alert(1)")
    assert p["link_postagem"].startswith("https://")

    assert projects_module.set_link_postagem(p["id"], "")["link_postagem"] == ""
