"""Cortar silêncio → XML do Premiere. Sem ffmpeg: o envelope e o ffprobe são trocados por funções falsas."""
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from app import create_app
from clientela import clients as clients_module, editor, silencio
from clientela import projects as projects_module

PASSO = 0.02
P = silencio.params_validos({"silencio_db": -38, "silencio_min": 0.5, "respiro": 0.1})


def env(*partes):
    """(segundos, alto?) → envelope em dBFS."""
    out = []
    for seg, alto in partes:
        out += [-12.0 if alto else -80.0] * round(seg / PASSO)
    return out


def test_trechos_com_silencio_no_meio_comeco_e_fim():
    t = silencio.trechos_de_fala(env((1, False), (2, True), (1, False), (2, True), (1, False)), PASSO, P)
    assert t == [(0.9, 3.1), (3.9, 6.1)]  # respiro de 0,1 s dos dois lados


def test_sem_silencio_e_so_silencio():
    assert silencio.trechos_de_fala(env((3, True)), PASSO, P) == [(0.0, 3.0)]
    assert silencio.trechos_de_fala(env((3, False)), PASSO, P) == []


def test_respiro_nao_sobrepoe_trechos():
    # pausa de 0,6 s com respiro máximo (1 s de cada lado): a pausa fica, nada se cruza
    p = silencio.params_validos({"silencio_db": -38, "silencio_min": 0.5, "respiro": 5})
    assert p["respiro"] == 1.0
    t = silencio.trechos_de_fala(env((2, True), (0.6, False), (2, True)), PASSO, p)
    assert all(a < b for a, b in t) and all(t[i][1] <= t[i + 1][0] for i in range(len(t) - 1))


def test_parametros_limitados_e_invalidos():
    p = silencio.params_validos({"silencio_db": -200, "silencio_min": 99, "respiro": -1})
    assert (p["silencio_db"], p["silencio_min"], p["respiro"]) == (-70.0, 5.0, 0.0)
    assert p["gaguejos"] is False
    try:
        silencio.params_validos({"silencio_db": "x"})
        assert False
    except ValueError:
        pass


def _xml(fps, trechos, canais=2):
    info = {"fps": fps, "largura": 1920, "altura": 1080, "duracao": 10.0, "audios": [{"canais": canais}]}
    return ET.fromstring(silencio.xml_sem_silencio(Path("/tmp/bruto.mp4"), info, trechos, "Sem silêncio - bruto"))


def test_xml_tem_um_clipitem_por_trecho_e_encostados():
    root = _xml(25.0, [(1.0, 2.0), (4.0, 6.5)])
    seq = root.find("sequence")
    v = seq.findall("media/video/track/clipitem")
    assert [(c.findtext("in"), c.findtext("out")) for c in v] == [("25", "50"), ("100", "162")]
    assert [(c.findtext("start"), c.findtext("end")) for c in v] == [("0", "25"), ("25", "87")]
    assert seq.findtext("duration") == "87"
    assert len(seq.findall("media/audio/track")) == 2       # estéreo → A1 e A2
    assert len(seq.findall("media/audio/track/clipitem")) == 4
    assert root.findtext(".//rate/timebase") == "25" and root.findtext(".//rate/ntsc") == "FALSE"


def test_2997_vira_30_ntsc_e_mono_so_a1():
    root = _xml(29.97, [(0.0, 1.0)], canais=1)
    assert root.findtext(".//rate/timebase") == "30" and root.findtext(".//rate/ntsc") == "TRUE"
    assert len(root.findall("sequence/media/audio/track")) == 1


def _projeto(tmp_path, monkeypatch, envelope):
    d = tmp_path / "Cliente"
    d.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(d))
    project = projects_module.create_project(client["id"], "Teste")
    folder = Path(project["path"])
    (folder / "bruto.mp4").write_bytes(b"video")
    monkeypatch.setattr(editor, "audio_envelope", lambda *a: {"passo": PASSO, "db": envelope,
                                                             "duracao": len(envelope) * PASSO})
    monkeypatch.setattr(editor, "media_info", lambda *a: {"fps": 25.0, "largura": 1920, "altura": 1080,
                                                          "duracao": len(envelope) * PASSO,
                                                          "audios": [{"canais": 2}]})
    return project, folder


def test_previa_nao_grava_e_gerar_nao_sobrescreve(tmp_path, monkeypatch):
    project, folder = _projeto(tmp_path, monkeypatch, env((1, False), (2, True), (1, False)))
    c = create_app().test_client()
    antes = sorted(p.name for p in folder.iterdir())
    r = c.post("/api/projects/cortar-silencio", json={"project_id": project["id"], "previa": True})
    assert r.status_code == 200, r.json
    assert r.json["cortes"] == 2 and r.json["duracao"] == 4.0 and r.json["final"] == 2.24  # respiro padrão 0,12 s
    assert sorted(p.name for p in folder.iterdir()) == antes
    um = c.post("/api/projects/cortar-silencio", json={"project_id": project["id"], "video": "bruto.mp4"}).json
    dois = c.post("/api/projects/cortar-silencio", json={"project_id": project["id"]}).json
    assert um["nome"] == "Sem silêncio - bruto.xml" and dois["nome"] == "Sem silêncio - bruto (2).xml"
    assert (folder / "bruto.mp4").read_bytes() == b"video"
    ET.parse(um["arquivo"])


def test_409_sem_pasta_e_400_so_silencio(tmp_path, monkeypatch):
    project, folder = _projeto(tmp_path, monkeypatch, env((3, False)))
    c = create_app().test_client()
    r = c.post("/api/projects/cortar-silencio", json={"project_id": project["id"]})
    assert r.status_code == 400 and not list(folder.glob("*.xml"))
    shutil.rmtree(folder)
    r = c.post("/api/projects/cortar-silencio", json={"project_id": project["id"], "previa": True})
    assert r.status_code == 409
