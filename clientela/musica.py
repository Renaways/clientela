"""Música como o spotDL: metadados de catálogo, link do Spotify vira o áudio do YouTube.

Tudo sem chave de API: o Spotify pelo player embutido público
(open.spotify.com/embed/…), o catálogo pela busca do iTunes (álbum original,
nº da faixa e do disco, gênero, data, capa 1000x1000), a letra pelo LRCLIB.
As tags vão para o arquivo com o mutagen (dependência do yt-dlp[default]);
sem ele, fica o que o yt-dlp gravou sozinho.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TIMEOUT = 15
ITUNES_PAIS = "BR"
SPOTIFY_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]+)")
# Versões que não são a faixa pedida, a menos que o nome dela as tenha.
_OUTRAS_VERSOES = ("live", "ao vivo", "cover", "karaoke", "instrumental", "remix", "letra", "lyrics",
                   "acustico", "acoustic", "sped up", "slowed", "8d", "reaction", "tutorial", "presents", "remaster",
                   "remastered", "deluxe")


# Coletânea perde para o álbum de origem (no iTunes ela costuma ter a mesma data "1987-01-01").
_COLETANEA = re.compile(r"\b(best of|greatest|hits|originals|anthems|party|collection|essentials|"
                        r"melhores|sucessos|coletanea|so as melhores)\b")
_VARIOS = ("various artists", "varios interpretes", "varios artistas")


class MusicaError(Exception):
    pass


def _get(url: str, *, json_resp: bool = True) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Clientela)"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if json_resp else body


def normalizar(texto: str | None) -> str:
    """Para comparar nomes: sem acento, sem (…)/[…], sem "feat.", só letras e números."""
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[(\[][^()\[\]]*[)\]]", " ", t)
    t = re.sub(r"\b(feat|ft|featuring)\b.*", " ", t)
    return " ".join(re.findall(r"[a-z0-9]+", t))


def _versao_errada(candidato: str, pedido: str) -> bool:
    c, p = normalizar_bruto(candidato), normalizar_bruto(pedido)
    return any(re.search(rf"\b{v}\b", c) and not re.search(rf"\b{v}\b", p) for v in _OUTRAS_VERSOES)


def normalizar_bruto(texto: str | None) -> str:
    """Como normalizar, mas mantendo o que está entre parênteses (é lá que mora o "Live")."""
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", t))


# ------------------------------------------------------------------ Spotify
def is_spotify(url: str | None) -> bool:
    return bool(SPOTIFY_RE.search(url or ""))


def _capa_spotify(imagens: list[dict[str, Any]] | None) -> str | None:
    imgs = [i for i in imagens or [] if i.get("url")]
    return max(imgs, key=lambda i: i.get("maxWidth") or i.get("width") or 0)["url"] if imgs else None


def spotify(url: str) -> dict[str, Any]:
    """Faixa, álbum ou playlist do Spotify: {tipo, titulo, capa, itens: [{artista,
    artistas, faixa, duracao, capa, data, album}]}. Playlist/álbum: o embed
    traz até ~100 faixas."""
    m = SPOTIFY_RE.search(url or "")
    if not m:
        raise MusicaError("não é um link do Spotify")
    tipo, sid = m.groups()
    try:
        html = _get(f"https://open.spotify.com/embed/{tipo}/{sid}", json_resp=False)
        dado = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        ent = json.loads(dado.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except Exception as exc:
        raise MusicaError(f"não consegui ler o link do Spotify ({exc})")
    capa = _capa_spotify((ent.get("coverArt") or {}).get("sources") or (ent.get("visualIdentity") or {}).get("image"))
    data = ((ent.get("releaseDate") or {}).get("isoString") or "")[:10] or None
    if tipo == "track":
        artistas = [a["name"] for a in ent.get("artists") or [] if a.get("name")]
        itens = [{"artista": ", ".join(artistas), "artistas": artistas, "faixa": ent.get("name") or ent.get("title"),
                  "duracao": (ent.get("duration") or 0) / 1000 or None, "capa": capa, "data": data, "album": None}]
    else:
        itens = []
        for t in ent.get("trackList") or []:
            artistas = [a.strip() for a in (t.get("subtitle") or "").split(",") if a.strip()]
            itens.append({"artista": ", ".join(artistas), "artistas": artistas, "faixa": t.get("title"),
                          "duracao": (t.get("duration") or 0) / 1000 or None,
                          "capa": capa if tipo == "album" else None, "data": None,
                          "album": ent.get("name") if tipo == "album" else None})
    if not itens:
        raise MusicaError("o Spotify não devolveu nenhuma faixa deste link")
    return {"tipo": tipo, "titulo": ent.get("name") or ent.get("title"), "capa": capa,
            "dono": ent.get("subtitle"), "itens": itens}


# ------------------------------------------------------------------ catálogo
def catalogo(artista: str | None, faixa: str | None, duracao: float | None = None,
             album: str | None = None) -> dict[str, Any] | None:
    """A faixa no iTunes: a de mesmo nome e artista, duração mais perto, versão
    original antes de ao vivo/remix, e o lançamento mais antigo no empate (o
    álbum original antes da coletânea e do "Deluxe Remaster")."""
    if not faixa:
        return None
    termo = f"{artista or ''} {faixa}".strip()
    try:
        res = _get("https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": termo, "entity": "song", "limit": 15, "country": ITUNES_PAIS}))["results"]
    except Exception as exc:  # shortcut: o iTunes limita ~20 buscas/min; playlist grande perde as tags de catálogo do excesso
        logger.info("catálogo: busca falhou para %r: %s", termo, exc)
        return None
    escolhido = escolher_no_catalogo(res, artista, faixa, duracao, album)
    if not escolhido:
        return None
    t = escolhido
    return {
        "artista": t.get("artistName"), "faixa": t.get("trackName"), "album": t.get("collectionName"),
        "album_artista": t.get("collectionArtistName") or t.get("artistName"),
        "faixa_n": t.get("trackNumber"), "faixas": t.get("trackCount"),
        "disco": t.get("discNumber"), "discos": t.get("discCount"),
        "genero": t.get("primaryGenreName"), "data": (t.get("releaseDate") or "")[:10] or None,
        "ano": int(t["releaseDate"][:4]) if t.get("releaseDate") else None,
        "capa": re.sub(r"/\d+x\d+bb\.", "/1000x1000bb.", t.get("artworkUrl100") or "") or None,
        "explicita": t.get("trackExplicitness") == "explicit",
        "duracao": (t.get("trackTimeMillis") or 0) / 1000 or None,
    }


def escolher_no_catalogo(res: list[dict[str, Any]], artista: str | None, faixa: str,
                         duracao: float | None = None, album: str | None = None) -> dict[str, Any] | None:
    nf, na = normalizar(faixa), normalizar((artista or "").split(",")[0])
    melhores = []
    for t in res:
        if t.get("kind") != "song":
            continue
        tn, ta = normalizar(t.get("trackName")), normalizar(t.get("artistName"))
        if tn != nf and not (nf and (tn.startswith(nf) or nf.startswith(tn))):
            continue
        if na and na not in ta and ta not in na:
            continue
        nota = 4 if tn == nf else 2
        if duracao and t.get("trackTimeMillis"):
            dif = abs(t["trackTimeMillis"] / 1000 - duracao)
            if dif > 15:
                continue
            nota += 5 if dif <= 0.5 else (3 if dif <= 3 else 1)  # o Spotify dá a duração ao milissegundo
        colecao = normalizar_bruto(t.get("collectionName"))
        if _COLETANEA.search(colecao) or normalizar_bruto(t.get("collectionArtistName")) in _VARIOS:
            nota -= 3
        if album and normalizar(album) == normalizar(t.get("collectionName")):
            nota += 3
        if _versao_errada(f"{t.get('trackName')} {t.get('collectionName')}", f"{faixa} {album or ''}"):
            nota -= 5
        melhores.append((-nota, t.get("releaseDate") or "9999", t))
    return min(melhores, key=lambda x: x[:2])[2] if melhores else None


def _letra_valida(d: dict[str, Any] | None, duracao: float | None = None) -> str | None:
    """O LRCLIB é aberto a envios: há letras de uma palavra ("probe") e de outra versão."""
    if not d or d.get("instrumental"):
        return None
    texto = (d.get("plainLyrics") or "").strip()
    if len(texto) < 60 or texto.count("\n") < 3:
        return None
    if duracao and d.get("duration") and abs(d["duration"] - duracao) > 8:
        return None
    return texto


def letra(artista: str | None, faixa: str | None, album: str | None = None,
          duracao: float | None = None) -> str | None:
    """Letra (texto puro) do LRCLIB, ou None."""
    if not (artista and faixa):
        return None
    principal = artista.split(",")[0].strip()
    q = {"artist_name": principal, "track_name": faixa}
    if album:
        q["album_name"] = album
    if duracao:
        q["duration"] = round(duracao)
    try:
        texto = _letra_valida(_get("https://lrclib.net/api/get?" + urllib.parse.urlencode(q)), duracao)
        if texto:
            return texto
    except Exception:
        pass
    try:  # sem álbum/duração exatos, a busca larga, pelo nome limpo ("Faixa (2022 - Remaster)" lá, "(2022 Remaster)" aqui)
        limpo = re.sub(r"\s*[(\[][^()\[\]]*[)\]]", "", faixa).strip() or faixa
        busca = _get("https://lrclib.net/api/search?" + urllib.parse.urlencode({"artist_name": principal, "track_name": limpo}))
    except Exception:
        return None
    return next((t for t in (_letra_valida(b, duracao) for b in busca or []) if t), None)


# ------------------------------------------------------------------ YouTube
def escolher_no_youtube(entradas: list[dict[str, Any]], artista: str, faixa: str,
                        duracao: float | None) -> dict[str, Any] | None:
    """O resultado da busca que é a faixa: duração perto, título com o nome dela,
    canal do artista ("Artista - Topic" = o áudio oficial), sem ao vivo/cover/letra."""
    nf, na = normalizar(faixa), normalizar(artista.split(",")[0])
    melhores = []
    for i, e in enumerate(entradas):
        titulo, canal = e.get("title") or "", e.get("channel") or e.get("uploader") or ""
        nt, nc = normalizar(titulo), normalizar(canal)
        if nf not in nt:
            continue
        nota = 0.0
        if duracao and e.get("duration"):
            dif = abs(e["duration"] - duracao)
            if dif > 20:
                continue
            nota += 4 if dif <= 3 else (2 if dif <= 8 else 0)
        if canal.endswith(" - Topic"):
            nota += 3
        if na and (na in nc or na in nt):
            nota += 2
        if _versao_errada(titulo, faixa):
            nota -= 6
        nota -= i * 0.3  # a ordem do YouTube desempata
        melhores.append((nota, e))
    return max(melhores, key=lambda x: x[0])[1] if melhores else None


# ------------------------------------------------------------------ tags
def gravar_tags(arquivo: Path, meta: dict[str, Any], capa: bytes | None = None,
                texto_letra: str | None = None) -> bool:
    """Grava as tags de música no arquivo (MP3, WAV, M4A/ALAC, FLAC, Opus/Ogg).
    False = formato sem tags (ADTS .aac) ou mutagen ausente."""
    try:
        import mutagen  # noqa: F401
    except ImportError:
        return False
    ext = arquivo.suffix.lower()
    n, total = meta.get("faixa_n"), meta.get("faixas")
    d, discos = meta.get("disco"), meta.get("discos")
    data = meta.get("data") or (str(meta["ano"]) if meta.get("ano") else None)
    if ext in (".mp3", ".wav", ".aiff", ".aif"):
        from mutagen import id3
        if ext == ".mp3":
            try:
                tags = id3.ID3(arquivo)
            except id3.ID3NoHeaderError:
                tags = id3.ID3()
            alvo = None
        else:
            from mutagen.wave import WAVE
            from mutagen.aiff import AIFF
            alvo = (WAVE if ext == ".wav" else AIFF)(arquivo)
            if alvo.tags is None:
                alvo.add_tags()
            tags = alvo.tags
        for chave in ("TCON", "COMM", "TDES", "USLT"):
            tags.delall(chave)  # o que o yt-dlp pôs (tags do vídeo, descrição) sai; a capa só sai se vier outra
        quadros = [("TIT2", meta.get("faixa")), ("TPE1", meta.get("artista")), ("TALB", meta.get("album")),
                   ("TPE2", meta.get("album_artista")), ("TCON", meta.get("genero")), ("TDRC", data),
                   ("TRCK", f"{n}/{total}" if n and total else (str(n) if n else None)),
                   ("TPOS", f"{d}/{discos}" if d and discos else (str(d) if d else None))]
        for chave, valor in quadros:
            if valor:
                tags.setall(chave, [getattr(id3, chave)(encoding=3, text=str(valor))])
        if texto_letra:
            tags.setall("USLT", [id3.USLT(encoding=3, lang="und", desc="", text=texto_letra)])
        if capa:
            tags.setall("APIC", [id3.APIC(encoding=3, mime="image/jpeg", type=3, desc="Capa", data=capa)])
        if alvo is None:
            tags.save(arquivo, v2_version=3)
        else:
            alvo.save()
        return True
    if ext in (".m4a", ".mp4", ".alac"):
        from mutagen.mp4 import MP4, MP4Cover
        f = MP4(arquivo)
        if f.tags is None:
            f.add_tags()
        for chave in ("©gen", "desc", "ldes", "©cmt", "©lyr"):
            f.tags.pop(chave, None)
        valores = {"©nam": meta.get("faixa"), "©ART": meta.get("artista"), "©alb": meta.get("album"),
                   "aART": meta.get("album_artista"), "©gen": meta.get("genero"), "©day": data, "©lyr": texto_letra}
        for chave, valor in valores.items():
            if valor:
                f.tags[chave] = [str(valor)]
        if n:
            f.tags["trkn"] = [(int(n), int(total or 0))]
        if d:
            f.tags["disk"] = [(int(d), int(discos or 0))]
        if capa:
            f.tags["covr"] = [MP4Cover(capa, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()
        return True
    if ext in (".flac", ".opus", ".ogg"):
        from mutagen.flac import FLAC, Picture
        if ext == ".flac":
            f = FLAC(arquivo)
        else:
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis
            try:
                f = OggOpus(arquivo)
            except Exception:
                f = OggVorbis(arquivo)
        for chave in ("genre", "description", "synopsis", "comment", "lyrics"):
            f.pop(chave, None)
        valores = {"title": meta.get("faixa"), "artist": meta.get("artista"), "album": meta.get("album"),
                   "albumartist": meta.get("album_artista"), "genre": meta.get("genero"), "date": data,
                   "tracknumber": n, "tracktotal": total, "discnumber": d, "disctotal": discos, "lyrics": texto_letra}
        for chave, valor in valores.items():
            if valor:
                f[chave] = [str(valor)]
        if capa:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Capa", capa
            if ext == ".flac":
                f.clear_pictures()
                f.add_picture(pic)
            else:
                f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]
        f.save()
        return True
    return False


def baixar_capa(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Clientela)"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            dado = r.read()
        return dado if dado[:3] == b"\xff\xd8\xff" else None  # só JPEG (o que as tags declaram)
    except Exception:
        return None
