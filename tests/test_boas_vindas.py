"""Boas-vindas: aparecem só na primeira abertura (sem cliente e sem ter concluído)."""

from app import create_app
from clientela import clients as clients_module


def test_aparece_sem_cliente_e_some_ao_concluir():
    c = create_app().test_client()
    info = c.get("/api/boas-vindas").get_json()
    assert info["mostrar"] is True and info["pasta_dados"]
    assert c.post("/api/boas-vindas").get_json()["concluida"]
    assert c.get("/api/boas-vindas").get_json()["mostrar"] is False


def test_quem_ja_tem_cliente_nao_ve(tmp_path):
    (tmp_path / "Cli").mkdir()
    clients_module.create_client(name="Cli", path=str(tmp_path / "Cli"))
    assert create_app().test_client().get("/api/boas-vindas").get_json()["mostrar"] is False
