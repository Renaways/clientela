import contextlib
import copy
import io
import logging
import optparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import musica as musica_module
from . import paths
from . import projects as projects_module

logger = logging.getLogger("clientela.downloads")

try:
    import yt_dlp

    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False
    logger.warning("yt-dlp not installed — media download disabled")

_jobs: dict[str, dict[str, Any]] = {}
_cancel_flags: dict[str, bool] = {}
_lock = threading.Lock()

_ALLOWED_QUALITIES = {"max", "144", "240", "360", "480", "720", "1080", "1440", "2160", "4k"}
_MAX_HEIGHT_BY_QUALITY = {
    "144": 144,
    "240": 240,
    "360": 360,
    "480": 480,
    "720": 720,
    "1080": 1080,
    "1440": 1440,
    "2160": 2160,
    "4k": 2160,
}


class _YtdlpLogger:
    """Route yt-dlp's own messages into app.log. Without this its warnings
    (e.g. "n challenge solving failed", which is what actually explains a
    "Requested format is not available") only reach a stderr nobody sees."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        logger.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.warning("yt-dlp: %s", msg)


_NO_FORMATS_HINT = (
    "o YouTube não entregou nenhum stream de vídeo. Normalmente é o yt-dlp "
    "desatualizado ou sem o solucionador de desafios (yt-dlp-ejs) — rode "
    "`.venv/bin/python -m pip install -U \"yt-dlp[default]\"` e reinicie o app."
)


_COOKIES_HINT = (
    "escolha em Rede e acesso › Cookies um navegador em que você esteja logado no site "
    "(o yt-dlp lê os cookies dele, nada é enviado para outro lugar)."
)
# (trecho da mensagem do yt-dlp, explicação em português)
_ERROR_HINTS = (
    ("Requested format is not available", _NO_FORMATS_HINT),
    ("not a bot", "o YouTube pediu login para liberar este vídeo: " + _COOKIES_HINT),
    ("Sign in to confirm your age", "vídeo com restrição de idade: " + _COOKIES_HINT),
    ("Private video", "vídeo privado: " + _COOKIES_HINT),
    ("members-only", "vídeo só para membros: " + _COOKIES_HINT),
    ("HTTP Error 403", "o site recusou (403): " + _COOKIES_HINT),
    ("logged-in", "o site só libera para quem está logado: " + _COOKIES_HINT),
    ("account credentials", "o site pede uma conta: " + _COOKIES_HINT),
    # no macOS o yt-dlp diz "could not find" quando a privacidade do sistema bloqueia a pasta do navegador
    ("cookies database in", "no Mac isso costuma ser o macOS bloqueando a pasta do navegador: em Ajustes do Sistema › "
     "Privacidade e Segurança › Acesso Total ao Disco, ligue o Clientela e reinicie o app. "
     "Se já estiver ligado, confira se você usa esse navegador logado no site."),
    ("Unsupported URL", "o yt-dlp não reconhece este link. Confira se é a página do vídeo, não um link encurtado."),
    ("Unable to download webpage", "não consegui abrir a página: confira o link e a conexão."),
    ("ffmpeg not found", "falta o ffmpeg (brew install ffmpeg): sem ele não dá para juntar vídeo e áudio nem converter."),
)


def _friendly_error(exc: Exception) -> str:
    msg = _strip_prefix(str(exc))
    for needle, hint in _ERROR_HINTS:
        if needle.lower() in msg.lower():
            return f"{msg} — {hint}"
    return msg


def _strip_prefix(msg: str) -> str:
    return re.sub(r"^(\x1b\[[0-9;]*m)*ERROR:(\x1b\[0m)?\s*", "", msg.strip())


class _CancelRequested(Exception):
    """Raised from inside a yt-dlp progress hook to abort a running download."""


def _normalize_video_quality(quality: str | None) -> str:
    q = str(quality or "max").lower()
    return q if q in _ALLOWED_QUALITIES else "max"


def _normalize_audio_format(audio_format: str | None) -> str:
    return "mp3" if str(audio_format or "wav").lower() == "mp3" else "wav"


def _video_format_selector(video_quality: str | None) -> str:
    """Build a yt-dlp format selector that prefers H.264/AVC and avoids VP9,
    so downloads come in a codec Premiere Pro can read without re-encoding."""
    max_height = _MAX_HEIGHT_BY_QUALITY.get(_normalize_video_quality(video_quality))
    if not max_height:
        return (
            "bestvideo[vcodec^=avc1]+bestaudio/"
            "bestvideo[vcodec^=avc]+bestaudio/"
            "bestvideo[vcodec!=vp9]+bestaudio/"
            "bestvideo+bestaudio/best"
        )
    return (
        f"bestvideo[vcodec^=avc1][height<={max_height}]+bestaudio/"
        f"bestvideo[vcodec^=avc][height<={max_height}]+bestaudio/"
        f"bestvideo[vcodec!=vp9][height<={max_height}]+bestaudio/"
        f"best[height<={max_height}]/"
        # Last resort: YouTube hardly serves muxed files any more, so if
        # nothing fits the codec/height filters take any stream rather than
        # failing with "Requested format is not available".
        f"bestvideo[height<={max_height}]+bestaudio/bestvideo+bestaudio/best"
    )


def _extract_estimated_bytes(info: dict[str, Any]) -> int | None:
    duration = info.get("duration")
    duration = float(duration) if isinstance(duration, (int, float)) else 0.0

    def item_size(item: Any) -> float:
        if not isinstance(item, dict):
            return 0.0
        size = item.get("filesize") or item.get("filesize_approx") or 0
        try:
            size = float(size)
        except (TypeError, ValueError):
            size = 0.0
        if size > 0:
            return size
        try:
            bitrate = float(item.get("tbr") or 0)
        except (TypeError, ValueError):
            bitrate = 0.0
        if duration > 0 and bitrate > 0:
            return (duration * bitrate * 1000) / 8
        return 0.0

    def sum_sizes(items: Any) -> float:
        if not isinstance(items, list) or not items:
            return 0.0
        return sum(item_size(i) for i in items)

    total = sum_sizes(info.get("requested_downloads"))
    if total > 0:
        return round(total)
    total = sum_sizes(info.get("requested_formats"))
    if total > 0:
        return round(total)
    total = item_size(info)
    return round(total) if total > 0 else None


DESTINO_DOWNLOADS = "downloads"


def resolve_destination(project_id: str | None, destino: str | None = None) -> Path | None:
    """Onde o arquivo cai: `Brutos/` do projeto (o padrão, e o que o painel do
    projeto sempre usou) ou, com destino "downloads", a ~/Downloads do usuário.
    Projeto sem pasta no disco não tem destino: criar Brutos/ ali recriaria a
    pasta do projeto vazia, só com o download dentro."""
    if destino == DESTINO_DOWNLOADS:
        return paths.USER_DOWNLOADS_DIR
    project = projects_module.get_project(project_id) if project_id else None
    if not project or not project.get("folder_available"):
        return None
    return Path(project["path"]) / "Brutos"


# Categorias que o SponsorBlock sabe remover/marcar (o resto é só para capítulos).
try:
    from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP as _SB

    SPONSORBLOCK_CATEGORIES = {k: v for k, v in _SB.CATEGORIES.items() if k not in ("poi_highlight", "chapter")}
except Exception:  # sem yt-dlp, ou versão que mudou a classe
    SPONSORBLOCK_CATEGORIES = {}


# ------------------------------------------------------------------ opções
# A tela monta um dicionário de opções; build_args o traduz em flags do
# yt-dlp de verdade e yt_dlp.parse_options() faz o resto (ordem dos
# pós-processadores, validação, nomes internos). Assim qualquer flag da
# documentação do yt-dlp funciona, inclusive pelo campo "Argumentos extras",
# e o job guarda o comando equivalente para copiar e rodar no Terminal.
MODOS = ("video", "video_mudo", "audio")
PERFIS_VIDEO = ("premiere", "prores", "original")
AUDIO_FORMATS = ("wav", "mp3", "m4a", "flac", "opus", "aac", "alac", "vorbis", "best")
AUDIO_QUALITIES = ("320K", "256K", "192K", "128K", "96K")
NOME_PADRAO = "%(title)s"
# Premiere não lê Opus/Vorbis: o merge do perfil Premiere sai com AAC.
_PREMIERE_MERGE_ARGS = "Merger:-c:a aac -b:a 192k"
_PRORES_ARGS = "VideoConvertor:-c:v prores_ks -profile:v 3 -vendor apl0 -pix_fmt yuv422p10le -c:a pcm_s16le"
# Clientela.app nasce com PATH mínimo: sem isto o yt-dlp não acha o ffmpeg
# (merge, áudio, legendas) nem o deno (desafios JS do YouTube).
_EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".deno" / "bin"), str(Path.home() / ".local" / "bin"))


class OptionError(ValueError):
    """Opção inválida: a mensagem vai direto para a tela."""


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    return shutil.which(name, path=os.pathsep.join(_EXTRA_PATH))


def _seconds(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).strip().replace(",", ".").split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise OptionError(f"tempo inválido: {value} (use h:mm:ss, m:ss ou segundos)")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    v = str(value or "").lower()
    return v if v in allowed else default


def _height(qualidade: Any) -> int | None:
    q = str(qualidade or "max").lower()
    if q in _MAX_HEIGHT_BY_QUALITY:
        return _MAX_HEIGHT_BY_QUALITY[q]
    try:
        h = int(q.rstrip("p"))
    except ValueError:
        return None
    return h if 0 < h <= 8640 else None


def _video_only_selector(perfil: str, h: int | None) -> str:
    cap = f"[height<={h}]" if h else ""
    if perfil == "original":
        return f"bv*{cap}/bv*"
    return f"bv*[vcodec^=avc]{cap}/bv*[vcodec!=vp9]{cap}/bv*{cap}/bv*"


def _original_selector(h: int | None) -> str:
    cap = f"[height<={h}]" if h else ""
    return f"bv*{cap}+ba/b{cap}/bv*+ba/b"


def _check_template(nome: str) -> str:
    nome = (nome or "").strip() or NOME_PADRAO
    if nome.startswith(("/", "~")) or ".." in Path(nome).parts:
        raise OptionError("o nome do arquivo não pode sair da pasta de destino")
    if nome.endswith(".%(ext)s"):
        nome = nome[: -len(".%(ext)s")]
    return nome


def legacy_options(
    media_format: str = "both",
    video_quality: str = "max",
    codec: str = "h264",
    audio_format: str = "wav",
    cookies_from_browser: str | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
) -> dict[str, Any]:
    """Os parâmetros do painel antigo (rail do projeto) no formato novo."""
    modo = {"audio": "audio", "video": "video_mudo"}.get(media_format, "video")
    trecho = None
    if start_time is not None and end_time is not None and end_time > start_time:
        trecho = {"inicio": start_time, "fim": end_time}
    return {
        "modo": modo,
        "qualidade": _normalize_video_quality(video_quality),
        "perfil": "prores" if codec == "prores" else "premiere",
        "audio_formato": _normalize_audio_format(audio_format),
        "cookies": cookies_from_browser or "",
        "trecho": trecho,
    }


def build_args(opcoes: dict[str, Any], dest_dir: Path) -> list[str]:
    """Flags do yt-dlp para as opções da tela, sem os links."""
    o = opcoes or {}
    modo = "audio" if o.get("spotify") else _choice(o.get("modo"), MODOS, "video")
    perfil = _choice(o.get("perfil"), PERFIS_VIDEO, "premiere")
    h = _height(o.get("qualidade"))
    musica = o.get("musica") if modo == "audio" and isinstance(o.get("musica"), dict) else None
    args = ["-P", str(dest_dir), "--windows-filenames", "--no-mtime"]

    ffmpeg = find_tool("ffmpeg")
    if ffmpeg:
        args += ["--ffmpeg-location", ffmpeg]
    deno = find_tool("deno")
    if deno:
        args += ["--js-runtimes", f"deno:{deno}"]

    # --- o que baixar
    if modo == "audio":
        fmt = _choice(o.get("audio_formato"), AUDIO_FORMATS, "wav")
        args += ["-f", "ba/b", "-x", "--audio-format", fmt]
        q = str(o.get("audio_qualidade") or "").upper()
        if q in AUDIO_QUALITIES and fmt in ("mp3", "m4a", "aac", "opus", "vorbis"):
            args += ["--audio-quality", q]
    elif modo == "video_mudo":
        args += ["-f", _video_only_selector(perfil, h)]
        if perfil == "prores":
            args += ["--recode-video", "mov", "--ppa", _PRORES_ARGS]
        elif perfil == "premiere":
            args += ["--recode-video", "mp4"]
    else:
        if perfil == "original":
            args += ["-f", _original_selector(h), "--merge-output-format", "mkv"]
        else:
            args += ["-f", _video_format_selector(str(h) if h else "max"),
                     "--merge-output-format", "mp4", "--ppa", _PREMIERE_MERGE_ARGS]
            if perfil == "prores":
                args += ["--recode-video", "mov", "--ppa", _PRORES_ARGS]
            else:
                # Quando a altura só existe num arquivo único (um WebM VP9, comum
                # em resolução baixa), não há merge e o --merge-output-format não
                # vale: sem isto sairia .webm, que o Premiere não abre. O yt-dlp
                # pula a conversão se o arquivo já é MP4.
                args += ["--recode-video", "mp4"]

    # --- playlist
    if o.get("playlist"):
        args.append("--yes-playlist")
        itens = str(o.get("itens") or "").replace(" ", "")
        if itens:
            if not re.fullmatch(r"[\d:,\-]+", itens):
                raise OptionError("itens da playlist: use números e intervalos, ex. 1,3,5-8")
            args += ["-I", itens]
    else:
        args.append("--no-playlist")

    # --- nome, trecho e capítulos
    nome = _check_template(o.get("nome"))
    if musica is not None and nome == "%(title)s":
        nome = "%(meta_artist)s - %(track,title)s"
    if o.get("playlist"):
        nome = f"%(playlist_title|Playlist)s/%(playlist_index)03d - {nome}"
    trecho = o.get("trecho") or None
    capitulos = [str(c) for c in (o.get("capitulos") or []) if str(c).strip()]
    if trecho:
        ini, fim = _seconds(trecho.get("inicio")), _seconds(trecho.get("fim"))
        if ini is None or fim is None or fim <= ini:
            raise OptionError("trecho inválido: o fim tem de vir depois do início")
        args += ["--download-sections", f"*{ini:g}-{fim:g}", "--force-keyframes-at-cuts"]
        nome += " (trecho %(section_start)d-%(section_end)ds)"
    elif capitulos:
        for titulo in capitulos:
            args += ["--download-sections", f"^{re.escape(titulo)}$"]
        nome += " - %(section_title)s"
    args += ["-o", nome + ".%(ext)s"]
    if o.get("dividir_capitulos"):
        args += ["--split-chapters", "-o", f"chapter:{nome} - %(section_number)02d %(section_title)s.%(ext)s"]

    # --- legendas
    # --write-subs também é o que mantém os .srt depois de --embed-subs; sem
    # ele o yt-dlp embute e apaga. As automáticas (YouTube) vêm de --write-auto-subs.
    leg = o.get("legendas") or {}
    idiomas = [str(i) for i in (leg.get("idiomas") or []) if re.fullmatch(r"[\w.\-*]+", str(i))]
    if idiomas:
        embutir = bool(leg.get("embutir")) and modo != "audio" and perfil != "prores"
        arquivo = bool(leg.get("arquivo")) or not embutir
        args += ["--sub-langs", ",".join(idiomas), "--sub-format", "srt/vtt/best", "--convert-subs", "srt"]
        if arquivo:
            args.append("--write-subs")
        if leg.get("auto"):
            args.append("--write-auto-subs")
        if embutir:
            args.append("--embed-subs")

    # --- capa, metadados, extras de texto
    capa = o.get("capa") or {}
    pode_embutir_capa = modo != "audio" or _choice(o.get("audio_formato"), AUDIO_FORMATS, "wav") in ("mp3", "m4a", "flac", "opus", "aac", "alac", "vorbis")
    if capa.get("arquivo"):
        args += ["--write-thumbnail", "--convert-thumbnails", "jpg"]
    if capa.get("embutir") and pode_embutir_capa and perfil != "prores":
        args.append("--embed-thumbnail")
        if not capa.get("arquivo"):
            args += ["--convert-thumbnails", "jpg"]
    if o.get("metadados") or musica is not None:
        args += ["--embed-metadata", "--embed-chapters"]
    if musica is not None:
        args += _music_args(musica, pode_embutir_capa and not capa.get("embutir"))
    if o.get("descricao"):
        args.append("--write-description")
    if o.get("info_json"):
        args.append("--write-info-json")
    if o.get("comentarios"):
        args += ["--write-comments"]

    # --- SponsorBlock (YouTube)
    sb = o.get("sponsorblock") or {}
    cats = [c for c in (sb.get("categorias") or []) if c in SPONSORBLOCK_CATEGORIES]
    if cats and sb.get("acao") in ("remover", "marcar"):
        args += ["--sponsorblock-remove" if sb["acao"] == "remover" else "--sponsorblock-mark", ",".join(cats)]

    # --- rede e acesso
    if o.get("cookies"):
        args += ["--cookies-from-browser", str(o["cookies"])]
    limite = str(o.get("limite") or "").strip()
    if limite:
        args += ["-r", limite]
    try:
        fragmentos = int(o.get("fragmentos") or 0)
    except (TypeError, ValueError):
        fragmentos = 0
    if fragmentos > 1:
        args += ["-N", str(min(fragmentos, 32))]
    if o.get("arquivo_baixados"):
        # --download-archive: não baixa de novo o que já veio para esta pasta
        args += ["--download-archive", str(dest_dir / ".yt-dlp-baixados.txt")]

    # --- tudo o mais que o yt-dlp oferece
    extras = str(o.get("extras") or "").strip()
    if extras:
        try:
            args += shlex.split(extras)
        except ValueError as exc:
            raise OptionError(f"argumentos extras: {exc}")
    return args


# Título de clipe "Artista - Faixa (Official Video)": os parênteses/colchetes
# do fim ficam de fora da faixa.
_TITULO_MUSICA = r"^(?P<artist>.+?) - (?P<track>.+?)(?:\s*[(\[][^()\[\]]*[)\]])*$"


def _music_args(musica: dict[str, Any], embutir_capa: bool) -> list[str]:
    """Tags de música no lugar das de vídeo: o yt-dlp, sozinho, grava as tags
    do vídeo como gênero, a descrição inteira e o canal "X - Topic" como artista."""
    args = []
    if embutir_capa:
        args += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    args += [
        # só quando não há track ("NA" é o que o yt-dlp escreve no campo vazio):
        # uma faixa "Song - Remastered" não pode virar artista "Song"
        "--parse-metadata", "%(track)s|%(title)s:^NA\\|" + _TITULO_MUSICA[1:],
        "--parse-metadata", "%(artist,uploader)s:^(?P<meta_artist>.+?)(?: - Topic)?$",
        "--parse-metadata", "%(release_year,upload_date>%Y)s:(?P<meta_date>.+)",
        # vazio = o yt-dlp não cai no fallback (tags do vídeo, descrição)
        "--parse-metadata", ":(?P<meta_genre>)",
        "--parse-metadata", ":(?P<meta_description>)",
        "--parse-metadata", ":(?P<meta_synopsis>)",
    ]
    return args


def _music_info(info: dict[str, Any], url: str = "") -> dict[str, Any] | None:
    """Artista/faixa/álbum quando o link é música: YouTube Music, um vídeo
    "auto-generated" (track/artist preenchidos) ou categoria Music com título
    "Artista - Faixa". None para o resto."""
    artista = info.get("artist") or ", ".join(info.get("artists") or []) or None
    faixa = info.get("track")
    yt_music = "music.youtube.com" in (url or "") or "music.youtube.com" in (info.get("webpage_url") or "")
    if not (faixa or artista or yt_music or "Music" in (info.get("categories") or [])):
        return None
    if not faixa:
        # título de playlist ("Album - Disco") não é "Artista - Faixa"
        m = info.get("_type") not in ("playlist", "multi_video") and re.match(_TITULO_MUSICA, info.get("title") or "")
        if m:
            artista, faixa = artista or m["artist"], m["track"]
        elif not yt_music:
            return None  # categoria Music sozinha não basta (vlog com trilha cai nela)
    if not artista:
        artista = re.sub(r" - Topic$", "", info.get("uploader") or info.get("channel") or "") or None
    return {"artista": artista, "faixa": faixa, "album": info.get("album"),
            "ano": info.get("release_year")}


def _parser_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    if "error:" in msg:
        msg = msg.rsplit("error:", 1)[1].strip()
    return msg or "opção inválida"


def ydl_options(opcoes: dict[str, Any], dest_dir: Path) -> tuple[list[str], dict[str, Any]]:
    args = build_args(opcoes, dest_dir)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            parsed = yt_dlp.parse_options(args)
    except SystemExit as exc:
        raise OptionError(_parser_error(Exception(err.getvalue() or exc)))
    except (optparse.OptParseError, ValueError) as exc:
        raise OptionError(_parser_error(exc))
    if parsed.urls:
        raise OptionError("os argumentos extras não podem trazer links: cole o link no campo de cima")
    return args, dict(parsed.ydl_opts)


def _opcoes_spotify(opcoes: dict[str, Any], urls: list[str]) -> dict[str, Any]:
    """Link do Spotify: o yt-dlp não baixa dele; cada faixa vira uma busca no YouTube, sempre só áudio."""
    if not any(musica_module.is_spotify(u) for u in urls):
        return opcoes
    if len(urls) > 1:
        raise OptionError("link do Spotify vai sozinho, um por download")
    return {**opcoes, "spotify": True, "musica": opcoes.get("musica") or {}}


def _comando(args: list[str], urls: list[str]) -> str:
    alvo = ["<busca no YouTube por faixa>"] if any(musica_module.is_spotify(u) for u in urls) else urls
    return shlex.join(["yt-dlp", *args, *(alvo or ["<link>"])])


def preview_command(project_id: str | None, destino: str | None, opcoes: dict[str, Any], urls: list[str]) -> str:
    """O comando yt-dlp equivalente, validado pelo próprio parser: a tela
    mostra (e deixa copiar) antes de baixar."""
    dest_dir = resolve_destination(project_id, destino) or Path("<destino>")
    urls = [u for u in urls if u]
    args, _ = ydl_options(_opcoes_spotify(opcoes, urls), dest_dir)
    return _comando(args, urls)


# ------------------------------------------------------------------ jobs
_PP_LABELS = {
    "Merger": "Juntando vídeo e áudio",
    "ExtractAudio": "Extraindo o áudio",
    "VideoConvertor": "Convertendo o vídeo",
    "VideoRemuxer": "Trocando o contêiner",
    "EmbedSubtitle": "Embutindo legendas",
    "SubtitlesConvertor": "Convertendo legendas",
    "EmbedThumbnail": "Embutindo a capa",
    "ThumbnailsConvertor": "Convertendo a capa",
    "Metadata": "Gravando metadados e capítulos",
    "SponsorBlock": "Consultando o SponsorBlock",
    "ModifyChapters": "Cortando trechos marcados",
    "SplitChapters": "Dividindo por capítulos",
    "MoveFiles": "Movendo para o destino",
    "Exec": "Rodando comando",
}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MAX_JOBS = 50
FINISHED = ("done", "error", "cancelled")


class _JobLogger(_YtdlpLogger):
    """Além do app.log, guarda os últimos avisos no job para a tela."""

    def __init__(self, job_id: str):
        self.job_id = job_id

    def _keep(self, msg: str) -> None:
        with _lock:
            job = _jobs.get(self.job_id)
            if job is not None:
                avisos = job.setdefault("avisos", [])
                clean = _ANSI.sub("", msg).replace("WARNING: ", "").strip()
                if clean and clean not in avisos:
                    avisos.append(clean)
                    del avisos[:-5]

    def warning(self, msg: str) -> None:
        super().warning(msg)
        self._keep(msg)

    def error(self, msg: str) -> None:
        super().error(msg)
        self._keep(msg)


def _prune_jobs() -> None:
    finished = sorted((j for j in _jobs.values() if j["status"] in FINISHED), key=lambda j: j["criado_em"])
    for job in finished[: max(0, len(_jobs) - _MAX_JOBS)]:
        _jobs.pop(job["id"], None)


def start_download(
    project_id: str | None,
    urls: list[str],
    *,
    destino: str | None = None,
    opcoes: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    media_format: str = "both",
    video_quality: str = "max",
    codec: str = "h264",
    audio_format: str = "wav",
    cookies_from_browser: str | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
) -> str | None:
    """Começa o download numa thread. None = yt-dlp ausente, sem link ou sem
    destino (projeto sem pasta); OptionError = opção que o yt-dlp recusou."""
    urls = [u.strip() for u in urls or [] if u and u.strip()]
    if not YT_DLP_AVAILABLE or not urls:
        return None
    dest_dir = resolve_destination(project_id, destino)
    if not dest_dir:
        return None
    if opcoes is None:
        opcoes = legacy_options(media_format, video_quality, codec, audio_format,
                                cookies_from_browser, start_time, end_time)
    opcoes = _opcoes_spotify(opcoes, urls)
    args, ydl_opts = ydl_options(opcoes, dest_dir)
    meta = meta or {}

    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "pending",
            "pct": 0.0,
            "speed": None,
            "eta": None,
            "baixado": None,
            "total": None,
            "urls": urls,
            "project_id": None if destino == DESTINO_DOWNLOADS else project_id,
            "destino": DESTINO_DOWNLOADS if destino == DESTINO_DOWNLOADS else "projeto",
            "destino_nome": meta.get("destino_nome") or "",
            "pasta": str(dest_dir),
            "error": None,
            "file": None,
            "arquivos": [],
            "avisos": [],
            "prores_warning": None,
            "titulo": meta.get("titulo") or urls[0],
            "thumbnail": meta.get("thumbnail"),
            "site": meta.get("site"),
            "item_titulo": None,
            "etapa": "Preparando",
            "item": None,
            "itens": None,
            "resumo": meta.get("resumo") or "",
            "criado_em": time.time(),
            "terminado_em": None,
            "comando": _comando(args, urls),
            "pedido": {"project_id": project_id, "destino": destino, "urls": urls, "opcoes": opcoes, "meta": meta},
        }
        _cancel_flags[job_id] = False
        _prune_jobs()
    threading.Thread(
        target=_run_job,
        args=(job_id, dest_dir, urls),
        kwargs={"ydl_opts": ydl_opts, "opcoes": opcoes},
        daemon=True,
        name=f"download-{job_id[:8]}",
    ).start()
    return job_id


def retry_download(job_id: str) -> str | None:
    with _lock:
        job = _jobs.get(job_id)
        pedido = copy.deepcopy(job.get("pedido")) if job else None
    if not pedido:
        return None
    return start_download(pedido["project_id"], pedido["urls"], destino=pedido["destino"],
                          opcoes=pedido["opcoes"], meta=pedido["meta"])


def cancel_download(job_id: str) -> bool:
    with _lock:
        if job_id not in _jobs:
            return False
        _cancel_flags[job_id] = True
    return True


def _check_cancel(job_id: str) -> None:
    if _cancel_flags.get(job_id):
        raise _CancelRequested("cancelado pelo usuário")


def _progress_hook(job_id: str, d: dict[str, Any]) -> None:
    with _lock:
        _check_cancel(job_id)
        job = _jobs.get(job_id)
        if not job:
            return
        info = d.get("info_dict") or {}
        idx, n = info.get("playlist_index"), info.get("n_entries") or info.get("playlist_count")
        if idx and n:
            job["item"], job["itens"] = idx, n
            job["item_titulo"] = info.get("title")
        if d.get("status") == "downloading":
            done = d.get("downloaded_bytes")
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if done and total:
                job["pct"] = max(0.0, min(100.0, done * 100.0 / total))
            else:
                try:
                    job["pct"] = float(_ANSI.sub("", d.get("_percent_str") or "0").strip().rstrip("%"))
                except ValueError:
                    pass
            job["baixado"], job["total"] = done, total
            job["speed"] = _ANSI.sub("", d.get("_speed_str") or "").strip() or None
            job["eta"] = _ANSI.sub("", d.get("_eta_str") or "").strip() or None
            so_audio = info.get("vcodec") == "none" and info.get("acodec") not in (None, "none")
            job["etapa"] = "Baixando o áudio" if so_audio else "Baixando"
            job["status"] = "running"
        elif d.get("status") == "finished":
            job["pct"] = 100.0
            job["etapa"] = "Processando"
            if d.get("filename"):
                job["file"] = Path(d["filename"]).name


def _pp_hook(job_id: str, d: dict[str, Any]) -> None:
    with _lock:
        _check_cancel(job_id)
        job = _jobs.get(job_id)
        if job and d.get("status") == "started":
            name = d.get("postprocessor") or ""
            label = _PP_LABELS.get(name)
            if not label and name.startswith("Fixup"):
                label = "Ajustando o arquivo"
            if name == "VideoConvertor" and "prores" in job.get("comando", ""):
                label = "Convertendo para ProRes 422 HQ"
            if label:
                job["etapa"] = label
                job["status"] = "processing"


def _post_hook(job_id: str, filepath: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            name = Path(filepath).name
            if name not in job["arquivos"]:
                job["arquivos"].append(name)
            job["file"] = name


def _job_update(job_id: str, **campos: Any) -> None:
    with _lock:
        _check_cancel(job_id)
        if job_id in _jobs:
            _jobs[job_id].update(campos)


def _aviso(job_id: str, msg: str) -> None:
    with _lock:
        avisos = _jobs.get(job_id, {}).setdefault("avisos", [])
        if msg not in avisos:
            avisos.append(msg)
            del avisos[:-5]


def _meta_do_youtube(info: dict[str, Any]) -> dict[str, Any]:
    """Artista/faixa de um vídeo baixado: os campos de música do YouTube, ou o
    que o --parse-metadata tirou do título "Artista - Faixa"."""
    artista = info.get("meta_artist") or info.get("artist") or ", ".join(info.get("artists") or []) \
        or re.sub(r" - Topic$", "", info.get("uploader") or "")
    return {"artista": artista or None, "faixa": info.get("track") or info.get("title"),
            "album": info.get("album"), "duracao": info.get("duration"),
            "ano": info.get("release_year"), "capa": None}


def _tags_de_musica(job_id: str, info: dict[str, Any], extra: dict[str, Any] | None, renomear: bool = False) -> None:
    """Depois do arquivo pronto: catálogo (iTunes) → tags completas, capa do
    álbum e letra. Sem catálogo, ficam as tags do yt-dlp (com o que o Spotify
    deu por cima, quando veio dele)."""
    arquivo = Path(info.get("filepath") or "")
    if not arquivo.is_file():
        return
    _job_update(job_id, etapa="Buscando os dados da música", status="processing")
    base = extra or _meta_do_youtube(info)
    cat = musica_module.catalogo(base.get("artista"), base.get("faixa"), base.get("duracao"), base.get("album"))
    meta = {**{k: v for k, v in base.items() if v}, **{k: v for k, v in (cat or {}).items() if v}}
    if extra and extra.get("artista"):
        meta["artista"] = extra["artista"]  # o Spotify lista todos os artistas; o iTunes, às vezes só o principal
    if not cat:
        _aviso(job_id, f"sem dados de catálogo para \"{base.get('artista') or ''} - {base.get('faixa') or arquivo.stem}\": ficaram as tags do YouTube")
    capa = musica_module.baixar_capa((cat or {}).get("capa") or base.get("capa"))
    texto = musica_module.letra(meta.get("artista"), meta.get("faixa"), meta.get("album"), meta.get("duracao"))
    _job_update(job_id, etapa="Gravando as tags")
    try:
        if not musica_module.gravar_tags(arquivo, meta, capa, texto):
            _aviso(job_id, f"{arquivo.suffix} não guarda tags de música")
    except Exception as exc:  # um arquivo que o mutagen não entende não derruba o download
        logger.warning("tags de música em %s: %s", arquivo, exc)
        _aviso(job_id, f"não consegui gravar as tags em {arquivo.name}: {exc}")
    # Nome do catálogo, como o spotDL: "Artista - Faixa (Audio) ft. X" do título do vídeo vira o nome real.
    # Do Spotify o nome já veio dele; com nome escolhido pelo usuário, fica o dele.
    if renomear and cat and not extra:
        prefixo = re.match(r"^\d{3} - ", arquivo.stem)
        novo = arquivo.with_name((prefixo.group(0) if prefixo else "")
                                 + _nome_de_arquivo(f"{meta['artista']} - {meta['faixa']}").replace("%%", "%") + arquivo.suffix)
        if novo != arquivo and not novo.exists():
            arquivo.rename(novo)
            info["filepath"] = str(novo)


if YT_DLP_AVAILABLE:
    class _MusicaPP(yt_dlp.postprocessor.PostProcessor):
        def __init__(self, job_id: str, extras: dict[str, dict[str, Any]], renomear: bool):
            super().__init__()
            self.job_id, self.extras, self.renomear = job_id, extras, renomear

        def run(self, info):
            _tags_de_musica(self.job_id, info, self.extras.get(info.get("original_url")), self.renomear)
            return [], info


def _indices(itens: str | None, total: int) -> list[int]:
    """"1,3,5-8" (o -I do yt-dlp) → índices 1-based; vazio = todos."""
    if not itens:
        return list(range(1, total + 1))
    out: list[int] = []
    for parte in str(itens).split(","):
        a, _, b = parte.partition("-")
        if a.isdigit():
            fim = int(b) if b.isdigit() else int(a)
            out += [i for i in range(int(a), fim + 1) if 1 <= i <= total and i not in out]
    return out


def _nome_de_arquivo(texto: str) -> str:
    return paths.sanitize_folder_component(texto).replace("%", "%%") or "faixa"


def _buscar_youtube(ydl: Any, termo: str) -> list[dict[str, Any]]:
    busca = ydl.extract_info(f"ytsearch10:{termo}", download=False, process=False)
    return list(busca.get("entries") or [])


def _baixar_spotify(ydl: Any, job_id: str, url: str, opcoes: dict[str, Any], extras: dict[str, dict[str, Any]]) -> int:
    """Cada faixa do Spotify: busca no YouTube, o resultado que bate (duração,
    canal do artista, sem ao vivo/cover), baixa com o nome "Artista - Faixa".
    Devolve quantas não foram achadas."""
    _job_update(job_id, etapa="Lendo o Spotify")
    sp = musica_module.spotify(url)
    itens = sp["itens"]
    escolhidos = _indices(opcoes.get("itens"), len(itens)) if sp["tipo"] != "track" else [1]
    pasta = f"{_nome_de_arquivo(sp['titulo'] or 'Spotify')}/" if sp["tipo"] != "track" else ""
    ext = ydl.params["outtmpl"]["default"].rsplit(".", 1)[-1]
    faltaram = 0
    for k, i in enumerate(escolhidos, 1):
        it = itens[i - 1]
        nome = f"{it['artista']} - {it['faixa']}"
        _job_update(job_id, item=k, itens=len(escolhidos), item_titulo=nome, etapa="Procurando no YouTube", pct=0.0)
        try:
            e = musica_module.escolher_no_youtube(_buscar_youtube(ydl, f"{it['artista']} {it['faixa']}"),
                                                  it["artista"], it["faixa"], it["duracao"])
        except _CancelRequested:
            raise
        except Exception as exc:
            logger.info("busca no YouTube falhou para %s: %s", nome, exc)
            e = None
        if not e:
            faltaram += 1
            _aviso(job_id, f"não achei no YouTube: {nome}")
            continue
        alvo = e.get("url") or f"https://www.youtube.com/watch?v={e['id']}"
        extras[alvo] = it
        prefixo = f"{pasta}{i:03d} - " if pasta else ""
        ydl.params["outtmpl"]["default"] = f"{prefixo}{_nome_de_arquivo(nome)}.{ext}"
        ydl.download([alvo])
    return faltaram


def _run_job(job_id: str, dest_dir: Path, urls: list[str], ydl_opts: dict[str, Any] | None = None,
             opcoes: dict[str, Any] | None = None, **_: Any) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = dict(ydl_opts or {})
    opts.update({
        "progress_hooks": [lambda d: _progress_hook(job_id, d)],
        "postprocessor_hooks": [lambda d: _pp_hook(job_id, d)],
        "post_hooks": [lambda f: _post_hook(job_id, f)],
        "logger": _JobLogger(job_id),
        "quiet": True,
        "noprogress": True,
        "color": {"stdout": "never", "stderr": "never"},
    })
    with _lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["etapa"] = "Lendo o link"
    status, error = "done", None
    try:
        o = opcoes or {}
        musica = o.get("musica") is not None and (o.get("spotify") or o.get("modo") == "audio")
        extras: dict[str, dict[str, Any]] = {}
        with yt_dlp.YoutubeDL(opts) as ydl:
            if musica:
                ydl.add_post_processor(_MusicaPP(job_id, extras, (o.get("nome") or "%(title)s") == "%(title)s"), when="after_move")
            if o.get("spotify"):
                retcode = _baixar_spotify(ydl, job_id, urls[0], o, extras)
            else:
                retcode = ydl.download(urls)
        with _lock:
            sem_arquivo = not _jobs[job_id]["arquivos"]
        if retcode and sem_arquivo:
            status, error = "error", "o yt-dlp terminou com erro (veja os avisos)"
    except _CancelRequested:
        status = "cancelled"
    except Exception as exc:  # yt-dlp levanta muitos tipos diferentes
        if isinstance(getattr(exc, "exc_info", (None, None))[1], _CancelRequested) or "cancelado pelo usuário" in str(exc):
            status = "cancelled"
        else:
            logger.warning("download job %s failed: %s", job_id, exc)
            status, error = "error", _friendly_error(exc)
    finally:
        with _lock:
            job = _jobs[job_id]
            job["status"] = status
            job["error"] = error
            job["terminado_em"] = time.time()
            job["etapa"] = {"done": "Concluído", "error": "Erro", "cancelled": "Cancelado"}[status]
            if status == "done":
                job["pct"] = 100.0
            _cancel_flags.pop(job_id, None)


def get_status(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return copy.deepcopy(job) if job else None


def list_jobs(limit: int = 30) -> list[dict[str, Any]]:
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["criado_em"], reverse=True)[:limit]
        return [{k: v for k, v in copy.deepcopy(j).items() if k != "pedido"} for j in jobs]


def reveal_target(job_id: str) -> Path | None:
    """O arquivo final (se existe) ou a pasta de destino do job."""
    job = get_status(job_id)
    if not job or not job.get("pasta"):
        return None
    pasta = Path(job["pasta"])
    for name in reversed(job.get("arquivos") or ([job["file"]] if job.get("file") else [])):
        for candidate in [pasta / name, *pasta.glob(f"*/{glob_escape(name)}")]:
            if candidate.exists():
                return candidate
    return pasta


def glob_escape(name: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", name)


# ------------------------------------------------------------------ prévia do link
class ProbeError(Exception):
    pass


def _fmt_size(f: dict[str, Any], duration: float) -> float:
    size = f.get("filesize") or f.get("filesize_approx")
    if size:
        return float(size)
    tbr = f.get("tbr")
    if tbr and duration:
        return duration * float(tbr) * 125
    return 0.0


def _codec_family(vcodec: str) -> str:
    v = (vcodec or "").lower()
    if v.startswith("avc") or v.startswith("h264"):
        return "H.264"
    if v.startswith(("vp9", "vp09")):
        return "VP9"
    if v.startswith("av01"):
        return "AV1"
    if v.startswith(("hev", "hvc", "h265")):
        return "HEVC"
    return v.split(".")[0].upper() if v else "?"


def _best_thumbnail(info: dict[str, Any]) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    if not thumbs:
        return None
    return max(thumbs, key=lambda t: (t.get("preference") or 0, t.get("width") or 0))["url"]


def _sub_list(subs: dict[str, Any] | None) -> list[dict[str, str]]:
    out = []
    for lang, tracks in (subs or {}).items():
        if lang == "live_chat":
            continue
        nome = next((t.get("name") for t in tracks or [] if t.get("name")), None) or lang
        out.append({"lang": lang, "nome": nome})
    return out


def summarize_info(info: dict[str, Any], url: str = "", playlist_requested: bool = False) -> dict[str, Any]:
    """O que a tela precisa de um extract_info: capa, título, alturas com
    tamanho estimado, legendas, capítulos ou os itens da playlist."""
    is_playlist = info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None
    base = {
        "tipo": "playlist" if is_playlist else "video",
        "id": info.get("id"),
        "titulo": info.get("title") or info.get("id") or url,
        "canal": info.get("uploader") or info.get("channel") or info.get("playlist_uploader"),
        "site": info.get("extractor_key") or info.get("ie_key") or info.get("extractor"),
        "url": info.get("webpage_url") or info.get("original_url") or url,
        "thumbnail": _best_thumbnail(info),
        "duracao": info.get("duration"),
        "data": info.get("upload_date"),
        "visualizacoes": info.get("view_count"),
        "ao_vivo": bool(info.get("is_live")),
        "idioma": info.get("language"),
        "musica": _music_info(info, url),
    }
    if is_playlist:
        itens = []
        for i, e in enumerate(info.get("entries") or []):
            if not e:
                continue
            itens.append({
                "indice": e.get("playlist_index") or i + 1,
                "id": e.get("id"),
                "titulo": e.get("title") or e.get("id") or f"item {i + 1}",
                "duracao": e.get("duration"),
                "thumbnail": _best_thumbnail(e),
            })
        if not base["thumbnail"] and itens:
            base["thumbnail"] = next((i["thumbnail"] for i in itens if i["thumbnail"]), None)
        base.update(itens=itens, total=info.get("playlist_count") or len(itens))
        return base

    duration = float(info.get("duration") or 0)
    fmts = info.get("formats") or []
    audios = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    audio_bytes = _fmt_size(best_audio, duration) if best_audio else 0.0
    by_height: dict[int, list[dict[str, Any]]] = {}
    for f in fmts:
        if f.get("vcodec") in (None, "none") or not f.get("height"):
            continue
        by_height.setdefault(int(f["height"]), []).append(f)

    def total(f: dict[str, Any] | None) -> int | None:
        if not f:
            return None
        size = _fmt_size(f, duration)
        if f.get("acodec") in (None, "none"):
            size += audio_bytes
        return round(size) if size else None

    alturas = []
    for h in sorted(by_height, reverse=True):
        group = by_height[h]
        best = max(group, key=lambda f: f.get("tbr") or 0)
        avc = [f for f in group if _codec_family(f.get("vcodec")) == "H.264"]
        alturas.append({
            "altura": h,
            "fps": max((f.get("fps") or 0) for f in group) or None,
            "codecs": sorted({_codec_family(f.get("vcodec")) for f in group}),
            "hdr": any((f.get("dynamic_range") or "SDR") != "SDR" for f in group),
            "bytes": total(best),
            "bytes_h264": total(max(avc, key=lambda f: f.get("tbr") or 0)) if avc else None,
        })
    auto = _sub_list(info.get("automatic_captions"))
    if len(auto) > 20:
        lingua = (info.get("language") or "").split("-")[0]
        keep = {"pt", "pt-BR", "en", lingua}
        auto = [a for a in auto if a["lang"].endswith("-orig") or a["lang"] in keep]
    auto.sort(key=lambda a: (not a["lang"].endswith("-orig"), a["lang"]))
    base.update(
        alturas=alturas,
        audio={"abr": best_audio.get("abr") if best_audio else None,
               "codec": best_audio.get("acodec") if best_audio else None,
               "bytes": round(audio_bytes) if audio_bytes else None},
        legendas=_sub_list(info.get("subtitles")),
        legendas_auto=auto,
        capitulos=[{"titulo": c.get("title") or f"Capítulo {i + 1}", "inicio": c.get("start_time"), "fim": c.get("end_time")}
                   for i, c in enumerate(info.get("chapters") or [])],
        tem_playlist=not playlist_requested and "list=" in (url or ""),
    )
    return base


def _probe_spotify(url: str) -> dict[str, Any]:
    try:
        sp = musica_module.spotify(url)
    except musica_module.MusicaError as exc:
        raise ProbeError(str(exc))
    itens = sp["itens"]
    if sp["tipo"] == "track":
        it = itens[0]
        return {"tipo": "video", "id": None, "titulo": it["faixa"], "canal": it["artista"], "site": "Spotify",
                "url": url, "thumbnail": sp["capa"], "duracao": it["duracao"], "data": (it["data"] or "").replace("-", "") or None,
                "visualizacoes": None, "ao_vivo": False, "idioma": None, "alturas": [], "audio": {}, "legendas": [],
                "legendas_auto": [], "capitulos": [], "tem_playlist": False, "so_audio": True,
                # nome e artistas ficam os do Spotify (são os do arquivo e das tags); o resto vem do catálogo
                "musica": {**(_com_catalogo({"artista": it["artista"], "faixa": it["faixa"], "album": None, "ano": None}, it["duracao"]) or {}),
                           "artista": it["artista"], "faixa": it["faixa"]}}
    return {"tipo": "playlist", "id": None, "titulo": sp["titulo"], "canal": sp.get("dono"), "site": "Spotify",
            "rotulo": "álbum" if sp["tipo"] == "album" else "playlist",
            "url": url, "thumbnail": sp["capa"], "so_audio": True, "total": len(itens),
            "musica": {"artista": sp.get("dono"), "faixa": None, "album": sp["titulo"] if sp["tipo"] == "album" else None, "ano": None},
            "itens": [{"indice": i, "id": None, "titulo": f"{it['artista']} - {it['faixa']}", "duracao": it["duracao"],
                       "thumbnail": it["capa"]} for i, it in enumerate(itens, 1)]}


def _com_catalogo(m: dict[str, Any] | None, duracao: float | None) -> dict[str, Any] | None:
    """O que a prévia mostra de uma faixa: o catálogo por cima do que o link deu."""
    if not m or not m.get("faixa"):
        return m
    cat = musica_module.catalogo(m.get("artista"), m["faixa"], duracao, m.get("album")) or {}
    return {**m, **{k: cat[k] for k in ("artista", "faixa", "album", "ano", "genero", "faixa_n", "faixas", "capa") if cat.get(k)},
            "catalogo": bool(cat)}


def probe(url: str, *, cookies_from_browser: str | None = None, playlist: bool = False) -> dict[str, Any]:
    """Lê o link sem baixar (o -F / --list-subs do yt-dlp, num resumo só)."""
    if musica_module.is_spotify(url):
        return _probe_spotify(url)
    if not YT_DLP_AVAILABLE:
        raise ProbeError("o yt-dlp não está instalado")
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ProbeError("cole um link que comece com http:// ou https://")
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": not playlist,
        "extract_flat": "in_playlist",
        "playlistend": 500,
        "logger": _YtdlpLogger(),
    }
    ffmpeg, deno = find_tool("ffmpeg"), find_tool("deno")
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.sanitize_info(ydl.extract_info(url, download=False))
    except Exception as exc:
        logger.info("probe failed for %s: %s", url, exc)
        raise ProbeError(_friendly_error(exc))
    if not isinstance(info, dict):
        raise ProbeError("o yt-dlp não devolveu informação deste link")
    resumo = summarize_info(info, url, playlist)
    if resumo["tipo"] == "video":
        resumo["musica"] = _com_catalogo(resumo["musica"], resumo.get("duracao"))
    return resumo


# ------------------------------------------------------------------ o próprio yt-dlp
def tool_info() -> dict[str, Any]:
    versao = None
    if YT_DLP_AVAILABLE:
        versao = getattr(getattr(yt_dlp, "version", None), "__version__", None)
    try:
        import yt_dlp_ejs  # noqa: F401  (desafios JS do YouTube)
        ejs = True
    except ImportError:
        ejs = False
    try:
        import curl_cffi  # noqa: F401  (--impersonate)
        impersonate = True
    except ImportError:
        impersonate = False
    return {
        "disponivel": YT_DLP_AVAILABLE,
        "versao": versao,
        "ffmpeg": find_tool("ffmpeg"),
        "deno": find_tool("deno"),
        "ejs": ejs,
        "impersonate": impersonate,
        "sponsorblock": SPONSORBLOCK_CATEGORIES,
        "audio_formatos": list(AUDIO_FORMATS),
    }


_update_lock = threading.Lock()


def update_ytdlp() -> dict[str, Any]:
    """pip install -U "yt-dlp[default]" no Python do app. A versão nova só vale
    depois de reiniciar o Clientela (o módulo já está carregado)."""
    if not _update_lock.acquire(blocking=False):
        return {"ok": False, "mensagem": "já estou atualizando"}
    try:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
                capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "mensagem": f"não consegui rodar o pip: {exc}"}
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
            return {"ok": False, "mensagem": " ".join(tail) or "o pip falhou"}
        novo = subprocess.run(
            [sys.executable, "-c", "import yt_dlp.version as v; print(v.__version__)"],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        atual = tool_info()["versao"]
        return {"ok": True, "versao": novo or atual, "reiniciar": bool(novo and novo != atual),
                "mensagem": "já estava na versão mais nova" if novo == atual else f"atualizado para {novo}: reinicie o app"}
    finally:
        _update_lock.release()


def estimate_size(
    url: str,
    *,
    media_format: str = "both",
    video_quality: str = "max",
    audio_format: str = "wav",
    start_time: float | None = None,
    end_time: float | None = None,
    cookies_from_browser: str | None = None,
) -> dict[str, Any]:
    if not YT_DLP_AVAILABLE or not url:
        return {"bytes": None, "duration": None}

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "simulate": True,
        "skip_download": True,
        "noplaylist": True,
        "logger": _YtdlpLogger(),
    }
    if media_format == "audio":
        opts["format"] = "bestaudio/best"
    else:
        opts["format"] = _video_format_selector(video_quality)
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        logger.info("size estimate failed for %s: %s", url, exc)
        return {"bytes": None, "duration": None}

    if not isinstance(info, dict):
        return {"bytes": None, "duration": None}

    estimated_bytes = _extract_estimated_bytes(info)
    duration = info.get("duration")
    duration = float(duration) if isinstance(duration, (int, float)) else None

    if (
        estimated_bytes
        and duration
        and start_time is not None
        and end_time is not None
        and end_time > start_time
    ):
        clipped = max(0.0, min(duration, end_time) - max(0.0, start_time))
        if clipped > 0 and duration > 0:
            estimated_bytes = round(estimated_bytes * (clipped / duration))

    return {"bytes": estimated_bytes, "duration": duration}
