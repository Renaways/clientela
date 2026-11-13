from pathlib import Path

from clientela import downloads


def test_normalize_video_quality_defaults_to_max_for_invalid_input():
    assert downloads._normalize_video_quality("garbage") == "max"
    assert downloads._normalize_video_quality(None) == "max"
    assert downloads._normalize_video_quality("720") == "720"


def test_video_format_selector_caps_height_and_avoids_vp9():
    selector = downloads._video_format_selector("720")
    assert "height<=720" in selector
    assert "vcodec!=vp9" in selector


def test_video_format_selector_prefers_h264_first():
    selector = downloads._video_format_selector("max")
    assert selector.startswith("bestvideo[vcodec^=avc1]")
    assert "height<=" not in selector


def test_extract_estimated_bytes_from_requested_downloads():
    info = {"duration": 100, "requested_downloads": [{"filesize": 1000}, {"filesize": 500}]}
    assert downloads._extract_estimated_bytes(info) == 1500


def test_extract_estimated_bytes_falls_back_to_bitrate_and_duration():
    info = {"duration": 100, "requested_formats": [{"tbr": 800}]}
    # (100s * 800kbps * 1000) / 8 = 10_000_000 bytes
    assert downloads._extract_estimated_bytes(info) == 10_000_000


def test_extract_estimated_bytes_returns_none_when_no_data():
    assert downloads._extract_estimated_bytes({"duration": 100}) is None


def test_start_download_returns_none_when_yt_dlp_unavailable(monkeypatch):
    monkeypatch.setattr(downloads, "YT_DLP_AVAILABLE", False)
    job_id = downloads.start_download("client:001", ["https://youtube.com/watch?v=x"])
    assert job_id is None


def test_start_download_returns_none_for_unknown_project(monkeypatch):
    monkeypatch.setattr(downloads, "YT_DLP_AVAILABLE", True)
    job_id = downloads.start_download("nope:001", ["https://youtube.com/watch?v=x"])
    assert job_id is None


def test_estimate_size_returns_none_bytes_when_yt_dlp_unavailable(monkeypatch):
    monkeypatch.setattr(downloads, "YT_DLP_AVAILABLE", False)
    result = downloads.estimate_size("https://youtube.com/watch?v=x")
    assert result == {"bytes": None, "duration": None}


def test_video_format_selector_ends_with_unfiltered_fallback():
    # YouTube rarely serves muxed files; if nothing matches the codec/height
    # filters the selector must still take any stream instead of failing.
    for quality in ("max", "720"):
        assert downloads._video_format_selector(quality).endswith("bestvideo+bestaudio/best")


def test_video_format_selector_is_valid_yt_dlp_syntax():
    import pytest

    yt_dlp = pytest.importorskip("yt_dlp")
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        for quality in ("max", "144", "720", "4k"):
            ydl.build_format_selector(downloads._video_format_selector(quality))


def test_friendly_error_explains_missing_formats():
    msg = downloads._friendly_error(Exception("ERROR: [youtube] x: Requested format is not available."))
    assert "yt-dlp-ejs" in msg
    assert downloads._friendly_error(Exception("outro erro")) == "outro erro"
    msg = downloads._friendly_error(Exception('ERROR: could not find edge cookies database in "/x"'))
    assert "Acesso Total ao Disco" in msg


def test_destino_downloads_ou_brutos_do_projeto(tmp_path, monkeypatch):
    from clientela import clients as clients_module, paths
    from clientela import projects as projects_module

    d = tmp_path / "Cliente"
    d.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(d))
    project = projects_module.create_project(client["id"], "Reel")
    sem = projects_module.create_project(client["id"], "Sem pasta", criar_pasta=False)
    assert downloads.resolve_destination(None, "downloads") == paths.USER_DOWNLOADS_DIR
    assert downloads.resolve_destination(project["id"]) == Path(project["path"]) / "Brutos"
    # sem pasta não há destino: criar Brutos/ ali recriaria a pasta do projeto vazia
    assert downloads.resolve_destination(sem["id"]) is None
    assert downloads.resolve_destination(None) is None

    monkeypatch.setattr(downloads, "YT_DLP_AVAILABLE", True)
    rodou = {}
    monkeypatch.setattr(downloads, "_run_job", lambda job_id, dest_dir, urls, **kw: rodou.update(dest=dest_dir))
    job_id = downloads.start_download(None, ["https://youtu.be/x"], destino="downloads")
    status = downloads.get_status(job_id)
    assert status["pasta"] == str(paths.USER_DOWNLOADS_DIR) and status["project_id"] is None
    assert downloads.start_download(sem["id"], ["https://youtu.be/x"]) is None


# ------------------------------------------------------------- opções → yt-dlp
import shutil
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest


def _opts(opcoes, tmp_path):
    pytest.importorskip("yt_dlp")
    args, ydl = downloads.ydl_options(opcoes, tmp_path)
    return args, ydl, [pp["key"] for pp in ydl.get("postprocessors", [])]


def test_padrao_e_mp4_para_o_premiere(tmp_path):
    args, ydl, pps = _opts({}, tmp_path)
    assert ydl["format"].startswith("bestvideo[vcodec^=avc1]")
    assert ydl["merge_output_format"] == "mp4" and ydl["noplaylist"] is True
    assert ydl["paths"]["home"] == str(tmp_path)
    assert ydl["outtmpl"]["default"] == "%(title)s.%(ext)s"
    assert ydl["postprocessor_args"]["merger"] == ["-c:a", "aac", "-b:a", "192k"]
    # arquivo único fora de MP4 (WebM em resolução baixa) é convertido
    assert next(pp for pp in ydl["postprocessors"] if pp["key"] == "FFmpegVideoConvertor")["preferedformat"] == "mp4"


def test_prores_converte_pelo_proprio_yt_dlp(tmp_path):
    args, ydl, pps = _opts({"perfil": "prores", "qualidade": "1080"}, tmp_path)
    assert "FFmpegVideoConvertor" in pps and ydl["final_ext"] == "mov"
    assert "prores_ks" in ydl["postprocessor_args"]["videoconvertor"]
    assert "height<=1080" in ydl["format"]


def test_audio_com_formato_e_qualidade(tmp_path):
    args, ydl, pps = _opts({"modo": "audio", "audio_formato": "mp3", "audio_qualidade": "320K",
                            "capa": {"embutir": True}, "metadados": True}, tmp_path)
    extract = next(pp for pp in ydl["postprocessors"] if pp["key"] == "FFmpegExtractAudio")
    assert extract["preferredcodec"] == "mp3" and extract["preferredquality"] == "320"
    assert "EmbedThumbnail" in pps and "FFmpegMetadata" in pps
    # wav não leva capa embutida: o yt-dlp falharia no fim do job
    _, _, pps_wav = _opts({"modo": "audio", "audio_formato": "wav", "capa": {"embutir": True}}, tmp_path)
    assert "EmbedThumbnail" not in pps_wav


def test_legendas_embutidas_e_arquivo(tmp_path):
    _, ydl, pps = _opts({"legendas": {"idiomas": ["pt", "en-orig"], "auto": True, "embutir": True, "arquivo": True}}, tmp_path)
    assert ydl["subtitleslangs"] == ["pt", "en-orig"] and ydl["writeautomaticsub"] is True
    embed = next(pp for pp in ydl["postprocessors"] if pp["key"] == "FFmpegEmbedSubtitle")
    assert embed["already_have_subtitle"] is True  # mantém os .srt
    _, ydl2, _ = _opts({"legendas": {"idiomas": ["pt"], "embutir": True}}, tmp_path)
    embed2 = next(pp for pp in ydl2["postprocessors"] if pp["key"] == "FFmpegEmbedSubtitle")
    assert embed2["already_have_subtitle"] is False  # só embutir: não sobra .srt


def test_trecho_capitulos_playlist_e_sponsorblock(tmp_path):
    _, ydl, _ = _opts({"trecho": {"inicio": "1:05", "fim": "1:30"}}, tmp_path)
    assert ydl["download_ranges"].ranges == [[65.0, 90.0]] and ydl["force_keyframes_at_cuts"]
    assert "%(section_start)d" in ydl["outtmpl"]["default"]
    _, ydl, pps = _opts({"capitulos": ["Intro (1/2)"], "dividir_capitulos": True}, tmp_path)
    assert ydl["download_ranges"].chapters[0].pattern == r"^Intro\ \(1/2\)$"
    assert "FFmpegSplitChapters" in pps and "chapter" in ydl["outtmpl"]
    _, ydl, _ = _opts({"playlist": True, "itens": "1,3, 5-7"}, tmp_path)
    assert ydl["noplaylist"] is False and ydl["playlist_items"] == "1,3,5-7"
    assert ydl["outtmpl"]["default"].startswith("%(playlist_title|Playlist)s/%(playlist_index)03d - ")
    _, ydl, pps = _opts({"sponsorblock": {"acao": "remover", "categorias": ["sponsor", "inventada"]}}, tmp_path)
    assert "SponsorBlock" in pps
    assert next(pp for pp in ydl["postprocessors"] if pp["key"] == "ModifyChapters")["remove_sponsor_segments"] == {"sponsor"}


def test_argumentos_extras_e_erros(tmp_path):
    _, ydl, _ = _opts({"extras": "--limit-rate 3M --retries 7", "nome": "%(uploader)s - %(title)s"}, tmp_path)
    assert ydl["ratelimit"] == 3 * 1024 * 1024 and ydl["retries"] == 7
    assert ydl["outtmpl"]["default"] == "%(uploader)s - %(title)s.%(ext)s"
    with pytest.raises(downloads.OptionError, match="bogus"):
        downloads.ydl_options({"extras": "--bogus"}, tmp_path)
    with pytest.raises(downloads.OptionError, match="links"):
        downloads.ydl_options({"extras": "https://exemplo.com/v"}, tmp_path)
    with pytest.raises(downloads.OptionError, match="pasta de destino"):
        downloads.ydl_options({"nome": "../fora"}, tmp_path)
    with pytest.raises(downloads.OptionError, match="trecho"):
        downloads.ydl_options({"trecho": {"inicio": "10", "fim": "5"}}, tmp_path)
    with pytest.raises(downloads.OptionError, match="playlist"):
        downloads.ydl_options({"playlist": True, "itens": "1;rm"}, tmp_path)


def test_parametros_antigos_viram_opcoes():
    o = downloads.legacy_options("audio", "720", "h264", "mp3", "chrome", 10, 20)
    assert o["modo"] == "audio" and o["audio_formato"] == "mp3" and o["cookies"] == "chrome"
    assert o["trecho"] == {"inicio": 10, "fim": 20}
    assert downloads.legacy_options(codec="prores")["perfil"] == "prores"


def test_resumo_do_link():
    info = {
        "id": "abc", "title": "Debate", "uploader": "Canal", "extractor_key": "Youtube", "duration": 100,
        "webpage_url": "https://youtu.be/abc", "thumbnail": "https://img/x.jpg", "language": "pt",
        "formats": [
            {"format_id": "a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "filesize": 1_000},
            {"format_id": "v1", "vcodec": "avc1.64", "acodec": "none", "height": 1080, "fps": 30, "filesize": 10_000},
            {"format_id": "v2", "vcodec": "vp9", "acodec": "none", "height": 1080, "fps": 60, "tbr": 2000},
            {"format_id": "v3", "vcodec": "av01", "acodec": "none", "height": 2160, "fps": 60, "filesize": 90_000, "dynamic_range": "HDR10"},
        ],
        "subtitles": {"pt": [{"name": "Português"}], "live_chat": [{}]},
        "automatic_captions": {f"l{i}": [{"name": f"L{i}"}] for i in range(30)} | {"pt-orig": [{"name": "Português (original)"}]},
        "chapters": [{"title": "Abertura", "start_time": 0, "end_time": 12.5}],
    }
    r = downloads.summarize_info(info, "https://www.youtube.com/watch?v=abc&list=PL1")
    assert r["tipo"] == "video" and r["site"] == "Youtube" and r["tem_playlist"] is True
    assert [a["altura"] for a in r["alturas"]] == [2160, 1080]
    assert r["alturas"][0]["hdr"] is True and r["alturas"][0]["bytes"] == 91_000
    h1080 = r["alturas"][1]
    assert h1080["codecs"] == ["H.264", "VP9"] and h1080["fps"] == 60
    assert h1080["bytes"] == 2000 * 100 * 125 + 1000 and h1080["bytes_h264"] == 11_000
    assert r["legendas"] == [{"lang": "pt", "nome": "Português"}]
    assert r["legendas_auto"][0]["lang"] == "pt-orig" and len(r["legendas_auto"]) == 1
    assert r["capitulos"] == [{"titulo": "Abertura", "inicio": 0, "fim": 12.5}]
    pl = downloads.summarize_info({"_type": "playlist", "title": "Série", "entries": [
        {"id": "1", "title": "Ep 1", "duration": 60, "thumbnails": [{"url": "https://t/1.jpg"}]}, None,
        {"id": "2", "title": "Ep 2"}]})
    assert pl["tipo"] == "playlist" and pl["total"] == 2 and pl["thumbnail"] == "https://t/1.jpg"
    assert [i["indice"] for i in pl["itens"]] == [1, 3]


def test_probe_recusa_o_que_nao_e_link():
    pytest.importorskip("yt_dlp")
    with pytest.raises(downloads.ProbeError, match="http"):
        downloads.probe("não é link")


def test_erros_ganham_explicacao():
    assert "logado" in downloads._friendly_error(Exception("ERROR: [youtube] x: Sign in to confirm you’re not a bot."))
    assert downloads._friendly_error(Exception("ERROR: algo")) == "algo"


def _esperar(job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = downloads.get_status(job_id)
        if job["status"] in downloads.FINISHED:
            return job
        time.sleep(0.1)
    raise AssertionError(downloads.get_status(job_id))


def test_baixa_de_verdade_na_pasta_de_downloads(servidor_de_video):
    from clientela import paths

    job_id = downloads.start_download(None, [servidor_de_video], destino="downloads",
                                      opcoes={"modo": "audio", "audio_formato": "mp3", "nome": "%(title)s audio"},
                                      meta={"titulo": "Clipe de teste"})
    job = _esperar(job_id)
    assert job["status"] == "done", job
    assert job["arquivos"] == ["clipe audio.mp3"] and (paths.USER_DOWNLOADS_DIR / "clipe audio.mp3").is_file()
    assert job["titulo"] == "Clipe de teste" and job["comando"].startswith("yt-dlp -P ")
    assert downloads.reveal_target(job_id) == paths.USER_DOWNLOADS_DIR / "clipe audio.mp3"
    assert downloads.list_jobs()[0]["id"] == job_id and "pedido" not in downloads.list_jobs()[0]
    # tentar de novo repete o mesmo pedido
    again = _esperar(downloads.retry_download(job_id))
    assert again["status"] == "done" and again["urls"] == [servidor_de_video]

    info = downloads.probe(servidor_de_video)
    assert info["tipo"] == "video" and info["titulo"] == "clipe"


def test_api_da_fila_e_do_probe(servidor_de_video, monkeypatch):
    from app import create_app
    from clientela import files_native

    monkeypatch.setattr(files_native, "reveal_in_finder", lambda p: True)
    c = create_app().test_client()
    r = c.post("/api/projects/download/start", json={"destino": "downloads", "urls": [servidor_de_video],
                                                    "opcoes": {"modo": "video", "perfil": "original"}})
    assert r.status_code == 200, r.json
    job = _esperar(r.json["job_id"])
    # um arquivo só (já com áudio) não passa pelo merge: "original" mantém o contêiner do site
    assert job["status"] == "done" and job["arquivos"] == ["clipe.mp4"]
    assert c.get("/api/downloads").json[0]["id"] == r.json["job_id"]
    assert c.post("/api/projects/download/reveal", json={"job_id": r.json["job_id"]}).json == {"opened": True}
    bad = c.post("/api/projects/download/start", json={"destino": "downloads", "urls": [servidor_de_video],
                                                      "opcoes": {"extras": "--nao-existe"}})
    assert bad.status_code == 400 and "nao-existe" in bad.json["error"]
    assert c.post("/api/downloads/probe", json={"url": "ftp://x"}).status_code == 422
    assert c.get("/api/downloads/ytdlp").json["versao"]


def test_comando_equivalente_validado(tmp_path):
    pytest.importorskip("yt_dlp")
    cmd = downloads.preview_command(None, "downloads", {"modo": "audio", "audio_formato": "flac"}, ["https://x.y/v"])
    assert cmd.startswith("yt-dlp -P ") and "--audio-format flac" in cmd and cmd.endswith("https://x.y/v")
    with pytest.raises(downloads.OptionError):
        downloads.preview_command(None, "downloads", {"extras": "--nada"}, [])


def test_premiere_converte_webm_unico_para_mp4(tmp_path):
    """O caso real do Wikimedia: em 240p só existe um WebM (VP9) com áudio, não
    há merge, e o perfil Premiere tem de entregar MP4 mesmo assim."""
    pytest.importorskip("yt_dlp")
    ffmpeg = downloads.find_tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("sem ffmpeg")
    site = tmp_path / "site"
    site.mkdir()
    r = subprocess.run([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-c:v", "libvpx-vp9", "-c:a", "libopus",
                        "-shortest", str(site / "clipe.webm")])
    if r.returncode != 0:
        pytest.skip("ffmpeg sem libvpx/libopus")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(site)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/clipe.webm"
        job = _esperar(downloads.start_download(None, [url], destino="downloads", opcoes={"perfil": "premiere"}))
    finally:
        httpd.shutdown()
    assert job["status"] == "done", job
    assert job["arquivos"] == ["clipe.mp4"]


def test_detecta_musica_no_resumo():
    topic = {"title": "Faixa Um", "uploader": "Banda - Topic", "track": "Faixa Um", "artists": ["Banda"],
             "album": "Disco", "release_year": 2021}
    assert downloads.summarize_info(topic)["musica"] == {"artista": "Banda", "faixa": "Faixa Um", "album": "Disco", "ano": 2021}
    clipe = {"title": "Banda - Faixa Dois (Official Video) [4K]", "uploader": "BandaVEVO", "categories": ["Music"]}
    assert downloads.summarize_info(clipe)["musica"]["artista"] == "Banda"
    assert downloads.summarize_info(clipe)["musica"]["faixa"] == "Faixa Dois"
    # categoria Music sem "Artista - Faixa" no título é vlog com trilha, não música
    assert downloads.summarize_info({"title": "Meu dia", "categories": ["Music"]})["musica"] is None
    assert downloads.summarize_info({"title": "Debate", "uploader": "Canal"})["musica"] is None
    album = downloads.summarize_info({"_type": "playlist", "title": "Album - Disco", "uploader": "Banda - Topic", "entries": []},
                                     "https://music.youtube.com/playlist?list=OLAK")
    assert album["musica"]["artista"] == "Banda"


def test_musica_so_muda_o_modo_audio(tmp_path):
    musica = {"artista": "Banda", "faixa": "Faixa"}
    args, ydl, pps = _opts({"modo": "audio", "audio_formato": "m4a", "musica": musica}, tmp_path)
    assert "EmbedThumbnail" in pps and "FFmpegMetadata" in pps
    assert ydl["outtmpl"]["default"] == "%(meta_artist)s - %(track,title)s.%(ext)s"
    # WAV: a capa vai pelo mutagen depois, não pelo yt-dlp; nome escolhido pelo usuário fica
    args, ydl, pps = _opts({"modo": "audio", "musica": musica, "nome": "%(id)s"}, tmp_path)
    assert "EmbedThumbnail" not in pps and ydl["outtmpl"]["default"] == "%(id)s.%(ext)s"
    args, ydl, pps = _opts({"modo": "video", "musica": musica}, tmp_path)
    assert "FFmpegMetadata" not in pps and "--parse-metadata" not in args
    # Spotify é sempre áudio, mesmo com o modo vídeo salvo nas preferências
    args, ydl, pps = _opts({"modo": "video", "spotify": True, "musica": {}}, tmp_path)
    assert "FFmpegExtractAudio" in pps and "FFmpegMetadata" in pps


@pytest.mark.parametrize("info, esperado", [
    ({"title": "Faixa - Remastered", "track": "Faixa - Remastered", "uploader": "Banda - Topic", "release_year": 1999},
     ("Banda - Faixa - Remastered.m4a", {"artist": "Banda", "title": "Faixa - Remastered", "date": "1999"})),
    ({"title": "Banda - Faixa (Official Video)", "uploader": "BandaVEVO", "upload_date": "20200105",
      "tags": ["tag lixo"], "categories": ["Music"], "description": "Inscreva-se!"},
     ("Banda - Faixa.m4a", {"artist": "Banda", "title": "Faixa", "date": "2020"})),
])
def test_tags_de_musica_de_verdade(servidor_de_video, tmp_path, info, esperado):
    import json
    import yt_dlp

    destino = tmp_path / "out"
    _, ydl = downloads.ydl_options({"modo": "audio", "audio_formato": "m4a", "musica": {"faixa": info.get("track")}}, destino)
    ydl.update(quiet=True, noprogress=True)
    with yt_dlp.YoutubeDL(ydl) as y:
        y.process_ie_result({"id": "x", "url": servidor_de_video, "ext": "mp4", "extractor": "teste",
                             "webpage_url": servidor_de_video, **info})
    nome, tags_esperadas = esperado
    assert [p.name for p in destino.iterdir()] == [nome]
    out = subprocess.run([downloads.find_tool("ffprobe"), "-v", "error", "-show_entries", "format_tags", "-of", "json",
                          str(destino / nome)], capture_output=True, text=True, check=True)
    tags = {k.lower(): v for k, v in json.loads(out.stdout)["format"]["tags"].items()}
    assert {k: tags.get(k) for k in tags_esperadas} == tags_esperadas
    assert "genre" not in tags and "description" not in tags and "synopsis" not in tags
