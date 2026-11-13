"""Detector de metadados de IA: BMFF sintético, sem vídeo de verdade."""

import os
import struct
from pathlib import Path

import pytest

from app import create_app
from clientela import clients as clients_module
from clientela import metadados_ia
from clientela import projects as projects_module
from clientela.metadados_ia import analisar_arquivo, analisar_pasta

FTYP = b"isom\0\0\0\0isom"
C2PA = bytes.fromhex("d8fec3d61b0e483c92975828877ec481")
XMP = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")


def atomo(tipo: bytes, corpo: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(corpo), tipo) + corpo


def atomo_64(tipo: bytes, corpo: bytes = b"") -> bytes:
    return struct.pack(">I4sQ", 1, tipo, 16 + len(corpo)) + corpo


def video(tmp_path, *atomos, nome="v.mp4"):
    p = tmp_path / nome
    p.write_bytes(b"".join(atomos))
    return p


def com_xmp(tmp_path, marcador: bytes):
    return analisar_arquivo(video(tmp_path, atomo(b"ftyp", FTYP), atomo(b"uuid", XMP + marcador), atomo(b"moov")))


def test_composite_com_caixa_mista_e_detectado(tmp_path):
    # Regressão: busca sensível à caixa por "trainedAlgorithmicMedia" não acha este.
    r = com_xmp(tmp_path, b"digitalSourceType/compositeWithTrainedAlgorithmicMedia")
    assert r == {"nome": "v.mp4", "status": "ia", "origens": ["Origem não identificada"]}


@pytest.mark.parametrize(
    "marcador, origem",
    [
        (b"action Generate_Music", "Música do Firefly"),
        (b"softwareAgent Generate_Fill", "Preenchimento Generativo"),
        (b"Generative Expand", "Expansão Generativa"),
        (b"Created by Google Generative AI", "Mídia gerada pelo Google"),
        (b"gemini_generated_video_abc.mp4", "Mídia gerada pelo Google"),
        (b"SynthID", "SynthID"),
    ],
)
def test_cada_origem_reconhecida(tmp_path, marcador, origem):
    assert com_xmp(tmp_path, marcador) == {"nome": "v.mp4", "status": "ia", "origens": [origem]}


def test_gen_ai_id_sem_origem_e_mais_de_uma_origem(tmp_path):
    assert com_xmp(tmp_path, b"com.adobe.genAiId")["origens"] == ["Origem não identificada"]
    r = com_xmp(tmp_path, b"generate_music generative expand")
    assert r["origens"] == ["Música do Firefly", "Expansão Generativa"]


def test_marcador_dentro_do_mdat_e_ignorado(tmp_path):
    r = analisar_arquivo(
        video(tmp_path, atomo(b"ftyp", FTYP), atomo(b"moov"), atomo(b"mdat", b"trainedAlgorithmicMedia c2pa.claim"))
    )
    assert r["status"] == "sem_marcador"


def test_marcador_em_uuid_desconhecido_e_ignorado(tmp_path):
    r = analisar_arquivo(
        video(tmp_path, atomo(b"ftyp", FTYP), atomo(b"uuid", b"\x11" * 16 + b"SynthID"), atomo(b"moov"))
    )
    assert r["status"] == "sem_marcador"


def test_moov_no_fim_depois_de_mdat_64_bits(tmp_path):
    r = analisar_arquivo(
        video(tmp_path, atomo(b"ftyp", FTYP), atomo_64(b"mdat", b"x" * 1000), atomo(b"moov", b"\xa9too SynthID"))
    )
    assert r["origens"] == ["SynthID"]


def test_atomo_ate_o_fim_do_arquivo(tmp_path):
    corpo = b"mdat trainedAlgorithmicMedia"
    mdat = struct.pack(">I4s", 0, b"mdat") + corpo
    assert analisar_arquivo(video(tmp_path, atomo(b"ftyp", FTYP), atomo(b"moov"), mdat))["status"] == "sem_marcador"
    moov = struct.pack(">I4s", 0, b"moov") + b"SynthID"
    assert analisar_arquivo(video(tmp_path, atomo(b"ftyp", FTYP), moov))["status"] == "ia"


def test_c2pa_sem_ia_e_credencial(tmp_path):
    r = analisar_arquivo(video(tmp_path, atomo(b"ftyp", FTYP), atomo(b"uuid", C2PA + b"c2pa.claim"), atomo(b"moov")))
    assert r == {"nome": "v.mp4", "status": "credencial", "origens": []}


def test_c2pa_com_ia_e_ia(tmp_path):
    r = analisar_arquivo(
        video(tmp_path, atomo(b"ftyp", FTYP), atomo(b"uuid", C2PA + b"c2pa.claim genAiId"), atomo(b"moov"))
    )
    assert r["status"] == "ia"


def test_vazio_e_erro(tmp_path):
    assert analisar_arquivo(video(tmp_path))["status"] == "erro"


@pytest.mark.parametrize(
    "lixo",
    [
        b"isto nao e um video, so texto qualquer",
        b"\0" * 64,
        b"\xff" * 64,
        b"abc",
        struct.pack(">I4s", 4, b"moov") + b"x" * 32,  # tamanho menor que o cabeçalho
        struct.pack(">I4sQ", 1, b"moov", 8),  # 64 bits menor que o cabeçalho
    ],
)
def test_lixo_e_erro(tmp_path, lixo):
    assert analisar_arquivo(video(tmp_path, lixo))["status"] == "erro"


def test_sem_moov_e_erro(tmp_path):
    assert analisar_arquivo(video(tmp_path, atomo(b"ftyp", FTYP), atomo(b"mdat", b"x")))["status"] == "erro"


def test_truncado_sem_marcador_e_erro_mas_com_marcador_e_ia(tmp_path):
    inteiro = atomo(b"ftyp", FTYP) + atomo(b"moov") + atomo(b"mdat", b"x" * 100)
    assert analisar_arquivo(video(tmp_path, inteiro[:-10]))["status"] == "erro"
    meio_xmp = atomo(b"ftyp", FTYP) + atomo(b"uuid", XMP + b"SynthID" + b"x" * 100)
    r = analisar_arquivo(video(tmp_path, meio_xmp[:-50]))
    assert r["status"] == "ia"


def test_uuid_menor_que_o_proprio_id_nao_le_o_resto_do_arquivo(tmp_path):
    r = analisar_arquivo(
        video(tmp_path, atomo(b"ftyp", FTYP), atomo(b"uuid", XMP[:4]), atomo(b"moov"), atomo(b"mdat", b"SynthID"))
    )
    assert r["status"] == "sem_marcador"


def test_limite_de_atomos_vira_erro(tmp_path, monkeypatch):
    monkeypatch.setattr(metadados_ia, "MAX_ATOMOS", 3)
    r = analisar_arquivo(video(tmp_path, atomo(b"ftyp", FTYP), atomo(b"free"), atomo(b"free"), atomo(b"moov")))
    assert r["status"] == "erro"


def test_limite_de_bytes_nunca_vira_sem_marcador(tmp_path, monkeypatch):
    monkeypatch.setattr(metadados_ia, "MAX_BYTES", 10)
    r = analisar_arquivo(video(tmp_path, atomo(b"ftyp", FTYP), atomo(b"moov", b"x" * 20 + b"SynthID")))
    assert r["status"] == "erro"


def test_pasta_so_primeiro_nivel_do_mais_recente_ao_mais_antigo(tmp_path):
    base = atomo(b"ftyp", FTYP) + atomo(b"moov")
    for i, nome in enumerate(["a.mp4", "b.MOV", "c.m4v"]):
        os.utime(video(tmp_path, base, nome=nome), (1000 + i, 1000 + i))
    video(tmp_path, base, nome="d.avi")
    video(tmp_path, base, nome="._a.mp4")
    (tmp_path / "pasta.mp4").mkdir()
    (tmp_path / "Export").mkdir()
    video(tmp_path / "Export", base, nome="fundo.mp4")
    assert [v["nome"] for v in analisar_pasta(tmp_path)] == ["c.m4v", "b.MOV", "a.mp4"]


def test_erro_de_leitura_nao_derruba_os_outros(tmp_path, monkeypatch):
    base = atomo(b"ftyp", FTYP) + atomo(b"moov")
    ruim = video(tmp_path, base, nome="ruim.mp4")
    video(tmp_path, base, nome="bom.mp4")
    real = open

    def abre(caminho, *a, **k):
        if caminho == ruim:
            raise PermissionError("sem acesso")
        return real(caminho, *a, **k)

    monkeypatch.setattr("builtins.open", abre)
    r = {v["nome"]: v["status"] for v in analisar_pasta(tmp_path)}
    assert r == {"ruim.mp4": "erro", "bom.mp4": "sem_marcador"}


@pytest.fixture
def client():
    return create_app().test_client()


def _projeto(tmp_path, **kw):
    pasta = tmp_path / "Cliente"
    pasta.mkdir()
    cliente = clients_module.create_client(name="Cliente", path=str(pasta))
    return projects_module.create_project(cliente["id"], "Reels", **kw)


def test_rota_devolve_a_analise(tmp_path, client):
    projeto = _projeto(tmp_path)
    video(Path(projeto["path"]), atomo(b"ftyp", FTYP), atomo(b"uuid", XMP + b"SynthID"), atomo(b"moov"), nome="f.mp4")
    r = client.get("/api/projects/metadados-ia", query_string={"project_id": projeto["id"]})
    assert r.status_code == 200
    assert r.get_json() == [{"nome": "f.mp4", "status": "ia", "origens": ["SynthID"]}]


def test_rota_projeto_inexistente_e_404(client):
    assert client.get("/api/projects/metadados-ia", query_string={"project_id": "x:999"}).status_code == 404
    assert client.get("/api/projects/metadados-ia").status_code == 404


def test_rota_sem_pasta_e_409(tmp_path, client):
    projeto = _projeto(tmp_path, criar_pasta=False)
    r = client.get("/api/projects/metadados-ia", query_string={"project_id": projeto["id"]})
    assert r.status_code == 409
