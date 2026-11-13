"""Acesso remoto pelo celular: do próprio computador passa direto; de outro endereço só com o acesso
ligado, o PIN certo e o cookie que a tela do PIN entrega."""

import json

import pytest

from app import create_app
from clientela import acesso, paths

FORA = {"REMOTE_ADDR": "10.147.17.5"}   # um IP de ZeroTier


def _client():
    return create_app().test_client()


def test_local_passa_sem_pin_e_de_fora_fica_trancado_com_o_acesso_desligado():
    c = _client()
    assert c.get("/").status_code == 200
    assert c.get("/api/clients").status_code == 200
    assert c.get("/", environ_base={"REMOTE_ADDR": "::1"}).status_code == 200
    assert c.get("/", environ_base={"REMOTE_ADDR": "::ffff:127.0.0.1"}).status_code == 200
    r = c.get("/", environ_base=FORA)
    assert r.status_code == 403 and b"desligado" in r.data
    assert c.get("/api/clients", environ_base=FORA).status_code == 403
    assert acesso.host_de_escuta() == "127.0.0.1"


def test_ligar_exige_pin_e_o_pin_fica_so_como_hash():
    c = _client()
    r = c.put("/api/acesso", json={"ativo": True})
    assert r.status_code == 400 and "senha" in r.get_json()["error"]
    for ruim in ("12", "abcd", "1234567890123"):
        assert c.put("/api/acesso", json={"pin": ruim}).status_code == 400
    r = c.put("/api/acesso", json={"pin": "4821", "ativo": True})
    assert r.status_code == 200
    st = r.get_json()
    assert st["ativo"] and st["pin_definido"] and st["precisa_reiniciar"]   # o servidor subiu em 127.0.0.1
    assert acesso.host_de_escuta() == "0.0.0.0"
    bruto = paths.CONFIG_JSON.read_text()
    assert "4821" not in bruto
    # a config genérica não entrega nem deixa sobrescrever o PIN
    assert "acesso_remoto" not in c.get("/api/config").get_json()
    c.put("/api/config", json={"acesso_remoto": {"ativo": False}, "outra": 1})
    assert json.loads(paths.CONFIG_JSON.read_text())["acesso_remoto"]["ativo"] is True


def test_de_fora_precisa_do_pin_e_o_cookie_libera():
    c = _client()
    c.put("/api/acesso", json={"pin": "4821", "ativo": True})
    r = c.get("/", environ_base=FORA)
    assert r.status_code == 401 and b'name="pin"' in r.data and b"Clientela" in r.data
    r = c.get("/api/clients", environ_base=FORA)
    assert r.status_code == 401 and r.get_json()["pin"] is True
    # configurar o acesso só no próprio computador, mesmo sem login
    assert c.put("/api/acesso", json={"ativo": False}, environ_base=FORA).status_code == 401
    r = c.post("/acesso/entrar", data={"pin": "0000"}, environ_base=FORA)
    assert r.status_code == 401 and "senha errada" in r.get_data(as_text=True)
    r = c.post("/acesso/entrar", data={"pin": "4821"}, environ_base=FORA)
    assert r.status_code == 302
    cookie = r.headers["Set-Cookie"]
    assert acesso.COOKIE in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert c.get("/", environ_base=FORA).status_code == 200
    assert c.get("/api/clients", environ_base=FORA).status_code == 200
    # mesmo logado, de fora não reconfigura
    assert c.put("/api/acesso", json={"ativo": False}, environ_base=FORA).status_code == 403
    assert c.get("/api/acesso", environ_base=FORA).get_json()["local"] is False
    # trocar o PIN no computador derruba os aparelhos já liberados
    c.put("/api/acesso", json={"pin": "999999"})
    assert c.get("/api/clients", environ_base=FORA).status_code == 401
    # desligar tranca de novo, mesmo com o servidor ainda escutando fora
    c.put("/api/acesso", json={"ativo": False})
    assert c.get("/", environ_base=FORA).status_code == 403


def test_pin_errado_demais_bloqueia_o_endereco():
    acesso.configurar(pin="4821", ativo=True)
    for _ in range(acesso.MAX_TENTATIVAS):
        with pytest.raises(acesso.AcessoError):
            acesso.conferir_pin("10.0.0.9", "1111", agora=1000)
    with pytest.raises(acesso.AcessoError) as exc:
        acesso.conferir_pin("10.0.0.9", "4821", agora=1001)   # nem o certo entra durante o bloqueio
    assert exc.value.status == 429
    assert acesso.conferir_pin("10.0.0.10", "4821", agora=1001)   # outro aparelho segue
    assert acesso.conferir_pin("10.0.0.9", "4821", agora=1000 + acesso.BLOQUEIO_S + 1)


def test_sair_apaga_o_cookie_e_reiniciar_nos_testes_e_recusado():
    c = _client()
    r = c.post("/api/acesso/sair")
    assert r.status_code == 200 and acesso.COOKIE in r.headers.get("Set-Cookie", "")
    r = c.post("/api/acesso/reiniciar")
    assert r.status_code == 409


def test_rotulos_dos_enderecos(monkeypatch):
    monkeypatch.setattr(acesso.machine, "current_os", lambda: acesso.machine.MAC)
    monkeypatch.setattr(acesso, "_ifconfig", lambda: [("lo0", "127.0.0.1"), ("en0", "192.168.0.12"),
                                                      ("feth1234", "10.147.17.20"), ("en5", "169.254.3.3")])
    es = acesso.enderecos()
    assert [(e["rotulo"], e["url"]) for e in es] == [("ZeroTier", "http://10.147.17.20:8000"),
                                                    ("Rede local", "http://192.168.0.12:8000")]


def test_usuario_e_senha_com_pedido_tambem_neste_computador():
    c = _client()
    assert c.put("/api/acesso", json={"exigir_local": True}).status_code == 400   # sem senha, não liga
    assert c.put("/api/acesso", json={"senha": "curta"}).status_code == 400
    r = c.put("/api/acesso", json={"usuario": "Ana", "senha": "segredo longo", "exigir_local": True})
    assert r.status_code == 200 and r.get_json()["exigir_local"] and r.get_json()["usuario"] == "Ana"
    assert "segredo longo" not in paths.CONFIG_JSON.read_text()
    # quem configurou recebeu o cookie e continua dentro
    assert c.get("/api/clients").status_code == 200
    # outro navegador neste mesmo computador precisa entrar
    outro = _client()
    assert outro.get("/").status_code == 401 and b'name="usuario"' in outro.get("/").data
    assert outro.get("/api/clients").status_code == 401
    r = outro.post("/acesso/entrar", data={"usuario": "<bia>", "pin": "segredo longo"})
    assert r.status_code == 401 and b'value="&lt;bia>"' in r.data   # o usuário volta escapado no campo
    assert outro.post("/acesso/entrar", data={"usuario": "ANA", "pin": "segredo longo"}).status_code == 302
    assert outro.get("/api/clients").status_code == 200
    # o acesso remoto continua desligado: de fora, nem com a senha
    assert outro.get("/api/clients", environ_base=FORA).status_code == 403


def test_remover_senha_pelo_terminal_destrava_o_computador():
    c = _client()
    c.put("/api/acesso", json={"senha": "segredo longo", "exigir_local": True, "ativo": True})
    acesso.remover_senha()
    st = acesso.status()
    assert not st["pin_definido"] and not st["exigir_local"] and not st["ativo"]
    assert _client().get("/api/clients").status_code == 200
