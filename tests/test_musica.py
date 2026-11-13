"""Música como o spotDL (clientela/musica.py + o pós-processador do download).
Sem rede: Spotify, iTunes, LRCLIB e a busca do YouTube são trocados por dados fixos."""

import json
import subprocess
import time

import pytest

from clientela import downloads, musica

ITUNES = [
    {"kind": "song", "artistName": "Banda", "trackName": "Faixa", "collectionName": "Coletânea Verão",
     "trackTimeMillis": 200_000, "releaseDate": "2015-01-01T00:00:00Z", "trackNumber": 9, "trackCount": 30},
    {"kind": "song", "artistName": "Banda", "trackName": "Faixa (Ao Vivo)", "collectionName": "Ao Vivo",
     "trackTimeMillis": 230_000, "releaseDate": "2010-01-01T00:00:00Z"},
    {"kind": "song", "artistName": "Banda", "trackName": "Faixa", "collectionName": "Disco",
     "trackTimeMillis": 201_000, "releaseDate": "2009-05-01T00:00:00Z", "trackNumber": 3, "trackCount": 11,
     "discNumber": 1, "discCount": 1, "primaryGenreName": "MPB", "artworkUrl100": "https://x/a/100x100bb.jpg"},
    {"kind": "song", "artistName": "Outra", "trackName": "Faixa", "collectionName": "Outro",
     "trackTimeMillis": 200_000, "releaseDate": "2000-01-01T00:00:00Z"},
]


def test_catalogo_prefere_o_album_original(monkeypatch):
    assert musica.escolher_no_catalogo(ITUNES, "Banda", "Faixa", 200)["collectionName"] == "Disco"
    # o álbum pedido (do Spotify) ganha
    assert musica.escolher_no_catalogo(ITUNES, "Banda", "Faixa", 200, "Coletânea Verão")["collectionName"] == "Coletânea Verão"
    assert musica.escolher_no_catalogo(ITUNES, "Ninguém", "Faixa", 200) is None
    assert musica.escolher_no_catalogo(ITUNES, "Banda", "Faixa", 400) is None  # duração longe demais
    # coletânea com a mesma data e duração quase igual perde para o álbum de origem
    coletanea = [dict(ITUNES[2], collectionName="The Best of Banda", releaseDate="2001-01-01T00:00:00Z", trackTimeMillis=200_400),
                 dict(ITUNES[2], releaseDate="2009-05-01T00:00:00Z", trackTimeMillis=200_000)]
    assert musica.escolher_no_catalogo(coletanea, "Banda", "Faixa", 200.0)["collectionName"] == "Disco"
    monkeypatch.setattr(musica, "_get", lambda url, **k: {"results": ITUNES})
    c = musica.catalogo("Banda", "Faixa", 200)
    assert c["album"] == "Disco" and c["faixa_n"] == 3 and c["genero"] == "MPB" and c["ano"] == 2009
    assert c["capa"] == "https://x/a/1000x1000bb.jpg"


def test_busca_no_youtube_acha_o_audio_oficial():
    entradas = [
        {"id": "ao", "title": "Banda - Faixa (Ao Vivo)", "duration": 201, "channel": "Banda"},
        {"id": "clipe", "title": "Banda - Faixa (Official Video)", "duration": 240, "channel": "BandaVEVO"},
        {"id": "topic", "title": "Faixa", "duration": 202, "channel": "Banda - Topic"},
        {"id": "letra", "title": "Faixa - letra", "duration": 200, "channel": "Fã"},
        {"id": "longe", "title": "Faixa", "duration": 500, "channel": "Banda - Topic"},
    ]
    assert musica.escolher_no_youtube(entradas, "Banda", "Faixa", 200)["id"] == "topic"
    assert musica.escolher_no_youtube(entradas, "Banda", "Outra Coisa", 200) is None


def test_normalizar_e_letra_lixo():
    assert musica.normalizar("ANAVITÓRIA - Trevo (Tu) [feat. Tiago]") == "anavitoria trevo"
    assert musica._letra_valida({"plainLyrics": "probe"}) is None
    boa = {"plainLyrics": "linha um\nlinha dois\nlinha três\nlinha quatro com mais texto aqui", "duration": 200}
    assert musica._letra_valida(boa, 201).startswith("linha um")
    assert musica._letra_valida(boa, 260) is None  # letra de outra versão
    assert musica._letra_valida({**boa, "instrumental": True}) is None


def test_spotify_pelo_embed(monkeypatch):
    def pagina(entity):
        return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
            {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}) + "</script>"
    faixa = {"name": "Faixa", "artists": [{"name": "Banda"}, {"name": "Convidada"}], "duration": 200_500,
             "releaseDate": {"isoString": "2009-05-01T00:00:00Z"},
             "visualIdentity": {"image": [{"url": "p", "maxWidth": 64}, {"url": "g", "maxWidth": 640}]}}
    monkeypatch.setattr(musica, "_get", lambda url, **k: pagina(faixa))
    sp = musica.spotify("https://open.spotify.com/intl-pt/track/ABC123?si=x")
    assert sp["tipo"] == "track" and sp["capa"] == "g"
    assert sp["itens"] == [{"artista": "Banda, Convidada", "artistas": ["Banda", "Convidada"], "faixa": "Faixa",
                            "duracao": 200.5, "capa": "g", "data": "2009-05-01", "album": None}]
    album = {"name": "Disco", "trackList": [{"title": "A", "subtitle": "Banda", "duration": 1000},
                                            {"title": "B", "subtitle": "Banda, X", "duration": 2000}]}
    monkeypatch.setattr(musica, "_get", lambda url, **k: pagina(album))
    sp = musica.spotify("spotify:album:XYZ")
    assert [(i["faixa"], i["artista"], i["album"]) for i in sp["itens"]] == [("A", "Banda", "Disco"), ("B", "Banda, X", "Disco")]
    assert not musica.is_spotify("https://youtu.be/x")
    with pytest.raises(musica.MusicaError):
        musica.spotify("https://youtu.be/x")


def test_indices_da_selecao():
    assert downloads._indices("1,3-5,9", 6) == [1, 3, 4, 5]
    assert downloads._indices("", 3) == [1, 2, 3]


META = {"artista": "Banda", "faixa": "Faixa", "album": "Disco", "album_artista": "Banda", "faixa_n": 3, "faixas": 11,
        "disco": 1, "discos": 1, "genero": "MPB", "data": "2009-05-01"}


def _jpeg(tmp_path):
    ff = downloads.find_tool("ffmpeg")
    out = tmp_path / "capa.jpg"
    subprocess.run([ff, "-loglevel", "error", "-f", "lavfi", "-i", "color=red:size=32x32", "-frames:v", "1", str(out)], check=True)
    return out.read_bytes()


@pytest.mark.parametrize("ext", ["mp3", "wav", "m4a", "flac", "opus"])
def test_grava_tags_capa_e_letra_em_cada_formato(tmp_path, ext):
    mutagen = pytest.importorskip("mutagen")
    ff = downloads.find_tool("ffmpeg")
    if not ff:
        pytest.skip("sem ffmpeg")
    arq = tmp_path / f"a.{ext}"
    subprocess.run([ff, "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1",
                    "-metadata", "genre=tag do vídeo", str(arq)], check=True)
    assert musica.gravar_tags(arq, META, _jpeg(tmp_path), "letra\nda música")
    out = subprocess.run([downloads.find_tool("ffprobe"), "-v", "error", "-show_entries", "format_tags:stream=codec_type:stream_tags",
                          "-of", "json", str(arq)], capture_output=True, text=True, check=True)
    tags = {}
    d = json.loads(out.stdout)
    # a capa é um stream de vídeo com título "Capa"; as tags da música ficam no formato/no áudio
    for bloco in [d.get("format", {})] + [st for st in d.get("streams", []) if st.get("codec_type") == "audio"]:
        tags.update({k.lower(): v for k, v in (bloco.get("tags") or {}).items()})
    assert tags.get("title") == "Faixa" and tags.get("artist") == "Banda" and tags.get("album") == "Disco"
    assert tags.get("genre") == "MPB" and str(tags.get("track", "")).startswith("3")
    f = mutagen.File(arq)
    texto = repr(f.tags) + repr(getattr(f, "pictures", ""))
    assert "letra" in texto
    capa = (f.pictures if hasattr(f, "pictures") and f.pictures else None) or \
        [v for k, v in f.tags.items() if k.startswith(("APIC", "covr", "metadata_block_picture"))]
    assert capa, f.tags.keys()


def test_aac_puro_nao_tem_tags(tmp_path):
    (tmp_path / "a.aac").write_bytes(b"")
    assert musica.gravar_tags(tmp_path / "a.aac", META) is False


@pytest.fixture
def sem_rede(monkeypatch, tmp_path):
    capa = _jpeg(tmp_path) if downloads.find_tool("ffmpeg") else None
    monkeypatch.setattr(musica, "catalogo", lambda *a, **k: dict(META, capa="https://x/c.jpg"))
    monkeypatch.setattr(musica, "letra", lambda *a, **k: "uma letra\nde verdade")
    monkeypatch.setattr(musica, "baixar_capa", lambda url: capa)


def _esperar(job_id, timeout=60):
    fim = time.time() + timeout
    while time.time() < fim:
        job = downloads.get_status(job_id)
        if job["status"] in downloads.FINISHED:
            return job
        time.sleep(0.1)
    raise AssertionError(downloads.get_status(job_id))


def test_link_do_youtube_com_musica_sai_com_nome_e_tags_do_catalogo(servidor_de_video, sem_rede):
    from clientela import paths
    mutagen = pytest.importorskip("mutagen")
    job = _esperar(downloads.start_download(None, [servidor_de_video], destino="downloads",
                                            opcoes={"modo": "audio", "audio_formato": "m4a", "musica": {"faixa": "clipe"}}))
    assert job["status"] == "done", job
    assert job["arquivos"] == ["Banda - Faixa.m4a"]
    f = mutagen.File(paths.USER_DOWNLOADS_DIR / "Banda - Faixa.m4a")
    assert f.tags["©alb"] == ["Disco"] and f.tags["trkn"] == [(3, 11)] and f.tags["©lyr"] == ["uma letra\nde verdade"]
    assert f.tags["covr"]


def test_link_do_spotify_baixa_cada_faixa_do_youtube(servidor_de_video, sem_rede, monkeypatch):
    from clientela import paths
    mutagen = pytest.importorskip("mutagen")
    itens = [{"artista": "Banda, Convidada", "artistas": ["Banda", "Convidada"], "faixa": f"Faixa {n}", "duracao": 1.0,
              "capa": None, "data": None, "album": "Disco"} for n in (1, 2, 3)]
    monkeypatch.setattr(musica, "spotify", lambda url: {"tipo": "album", "titulo": "Disco", "capa": None, "itens": itens})
    vistos = []

    def busca(ydl, termo):
        vistos.append(termo)
        if termo.endswith("3"):
            return []  # não achou: vira aviso, as outras seguem
        return [{"id": termo[-1], "title": f"Faixa {termo[-1]}", "duration": 1, "channel": "Banda - Topic", "url": servidor_de_video}]
    monkeypatch.setattr(downloads, "_buscar_youtube", busca)

    job = _esperar(downloads.start_download(None, ["https://open.spotify.com/album/XYZ"], destino="downloads",
                                            opcoes={"modo": "video", "audio_formato": "mp3", "playlist": True, "itens": "1-3"}))
    assert job["status"] == "done", job
    assert job["arquivos"] == ["001 - Banda, Convidada - Faixa 1.mp3", "002 - Banda, Convidada - Faixa 2.mp3"]
    assert any("não achei no YouTube: Banda, Convidada - Faixa 3" in a for a in job["avisos"])
    f = mutagen.File(paths.USER_DOWNLOADS_DIR / "Disco" / "001 - Banda, Convidada - Faixa 1.mp3")
    # o artista é o do Spotify (todos), o resto do catálogo
    assert str(f.tags["TPE1"]) == "Banda, Convidada" and str(f.tags["TALB"]) == "Disco" and f.tags.getall("APIC")
    assert vistos == ["Banda, Convidada Faixa 1", "Banda, Convidada Faixa 2", "Banda, Convidada Faixa 3"]
    with pytest.raises(downloads.OptionError):
        downloads.start_download(None, ["https://open.spotify.com/track/A", "https://youtu.be/x"], destino="downloads", opcoes={})
