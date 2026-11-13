"""Animações universais de legenda (kit/legendas.py --animacao) e troca de fonte."""
import json
import re
import shutil
import subprocess
import sys

import pytest

from clientela import editor

MARCA = {
    "cores": {"texto": "#ffffff", "acento": "#ffb000"},
    "tipografia": {"fontes": [{"familia": "Hando", "arquivo": "Hando-Black.otf", "peso": 900}],
                   "papeis": {"legenda_destaque": {"cor": "acento"}}},
}


def _bancada(tmp_path):
    b = tmp_path / "legendas v1"
    shutil.copytree(editor.KIT_DIR, b / "kit")
    (b / "hf" / "assets" / "fonts").mkdir(parents=True)
    (b / "hf" / "assets" / "fonts" / "Outra.ttf").write_bytes(b"x")
    (b / "job.json").write_text(json.dumps({"video": {"largura": 1080, "altura": 1920, "fps": 30, "duracao": 10.0}}))
    (b / "marca.json").write_text(json.dumps(MARCA))
    (b / "palavras.json").write_text(json.dumps({"palavras": [
        {"texto": "São", "t": 0.5, "fim": 0.8}, {"texto": "63", "t": 0.8, "fim": 1.2},
        {"texto": "milhões", "t": 1.2, "fim": 1.8}, {"texto": "de", "t": 1.8, "fim": 1.9},
        {"texto": "eleitores.", "t": 1.9, "fim": 2.6}, {"texto": "Agora", "t": 4.0, "fim": 4.5}]}))
    subprocess.run([sys.executable, "kit/legendas.py", "blocos"], cwd=b, check=True, capture_output=True)
    return b


def _html(b, *args):
    r = subprocess.run([sys.executable, "kit/legendas.py", "html", *args], cwd=b, capture_output=True, text=True)
    return r, (b / "hf" / "legendas.html").read_text() if r.returncode == 0 else ""


@pytest.mark.parametrize("anim", editor.ANIMACOES_LEGENDA)
def test_cada_animacao_gera_a_composicao(tmp_path, anim):
    b = _bancada(tmp_path)
    r, html = _html(b, "--animacao", anim)
    assert r.returncode == 0, r.stderr
    assert 'id="b0w1"' in html and ("milhões" in html or anim == "maquina")  # cada palavra continua endereçável
    assert "var(--" not in "".join(re.findall(r"tl\.to\([^;]*color[^;]*;", html))  # o GSAP não anima var(--x)
    assert json.loads((b / "jobs.json").read_text())[-1]["comp"] == "legendas"
    if anim == "maquina":
        assert '<span class="c">m</span>' in html and '.c", { opacity: 1, stagger' in html
    if anim == "karaoke":
        assert 'color: "#ffb000"' in html  # o acento do papel, como valor literal
        assert "lw dest" not in html  # só a palavra falada acende; sem destaque fixo no meio do bloco
    if anim == "caixa":
        assert "caixa" in html


def test_fonte_troca_a_familia_e_declara_o_arquivo(tmp_path):
    b = _bancada(tmp_path)
    _, html = _html(b, "--animacao", "suave", "--fonte", "Outra")
    assert '@font-face { font-family: "Outra"; font-weight: 100 900; src: url("assets/fonts/Outra.ttf")' in html
    assert '#root, #root * { font-family: "Outra",' in html
    _, html = _html(b, "--fonte", "Hando")  # já declarada pelo marca.json: só a regra
    assert html.count('font-family: "Hando"; font-weight: 100 900') == 0 and '#root * { font-family: "Hando"' in html
    _, html = _html(b)  # sem fonte: a do perfil
    assert "!important" not in html


def test_animacao_ou_fonte_invalida_falha(tmp_path):
    b = _bancada(tmp_path)
    assert _html(b, "--animacao", "nao-existe")[0].returncode != 0
    assert _html(b, "--fonte", 'X"; } body {')[0].returncode != 0


def test_opcoes_validadas_no_app_e_espelho_do_kit():
    assert editor.legendas_opcoes(None) == {"animacao": "perfil", "fonte": ""}
    assert editor.legendas_opcoes({"animacao": "pop", "fonte": " Futura "}) == {"animacao": "pop", "fonte": "Futura"}
    for ruim in ({"animacao": "x"}, {"fonte": "a<b"}, {"fonte": "x" * 81}):
        with pytest.raises(editor.EditorError):
            editor.legendas_opcoes(ruim)
    kit = (editor.KIT_DIR / "legendas.py").read_text()
    bloco = kit.split("ANIMACOES = {")[1].split("}")[0]
    assert tuple(re.findall(r'^\s+"(\w+)":', bloco, re.M)) == editor.ANIMACOES_LEGENDA


def test_gerundio_nao_vira_destaque(tmp_path):
    b = _bancada(tmp_path)
    (b / "palavras.json").write_text(json.dumps({"palavras": [
        {"texto": "Ainda", "t": 0.1, "fim": 0.4}, {"texto": "estamos", "t": 0.4, "fim": 0.8},
        {"texto": "assistindo", "t": 0.8, "fim": 1.4}, {"texto": "a", "t": 1.4, "fim": 1.5},
        {"texto": "debates.", "t": 1.5, "fim": 2.0}]}))
    subprocess.run([sys.executable, "kit/legendas.py", "blocos"], cwd=b, check=True, capture_output=True)
    bloco = json.loads((b / "legendas.json").read_text())[0]
    assert bloco["palavras"][bloco["destaque"]]["texto"] == "debates."
