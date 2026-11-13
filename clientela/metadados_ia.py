"""Detecta metadados que declaram IA nos vídeos finais de um projeto.

Só informa: nada aqui escreve no vídeo. Percorre os átomos de topo com seek e
lê apenas moov e os uuid de C2PA e XMP, nunca o mdat. Faixas sem credencial não
deixam marca, então "sem_marcador" não quer dizer "sem IA".
"""

import logging
import os
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
UUID_C2PA = bytes.fromhex("d8fec3d61b0e483c")
UUID_XMP = bytes.fromhex("be7acfcb97a942e8")
MAX_ATOMOS = 4096
MAX_BYTES = 64 * 1024 * 1024  # por arquivo; um moov maior que isso vira "erro", não "sem_marcador"

ORIGEM_DESCONHECIDA = "Origem não identificada"
# Tudo em minúsculas: a busca é feita sobre os bytes em minúsculas.
ORIGENS = (
    ("Música do Firefly", (b"generate_music", b"generate music")),
    ("Preenchimento Generativo", (b"generate_fill", b"generative fill")),
    ("Expansão Generativa", (b"generative expand", b"generative_expand")),
    ("Mídia gerada pelo Google", (b"created by google generative ai", b"gemini_generated_")),
    ("SynthID", (b"synthid",)),
)
MARCADORES_IA = (b"trainedalgorithmicmedia", b"genaiid")
MARCADOR_C2PA = b"c2pa.claim"


def _ler_metadados(f, tamanho: int) -> tuple[bytes, bool, bool]:
    """Devolve (metadados em minúsculas, percorreu o arquivo inteiro, tem uuid C2PA)."""
    blocos: list[bytes] = []
    restante = MAX_BYTES
    tem_c2pa = tem_moov = False
    completo = False
    pos = 0
    for _ in range(MAX_ATOMOS):
        if pos == tamanho:
            completo = tem_moov
            break
        f.seek(pos)
        cabecalho = f.read(8)
        if len(cabecalho) < 8:
            break
        size, tipo = struct.unpack(">I4s", cabecalho)
        if not all(0x20 <= b < 0x7F for b in tipo):
            break
        tam_cabecalho = 8
        if size == 1:
            estendido = f.read(8)
            if len(estendido) < 8:
                break
            size = struct.unpack(">Q", estendido)[0]
            tam_cabecalho = 16
        elif size == 0:
            size = tamanho - pos
        if size < tam_cabecalho:
            break

        fim = pos + size
        ler = tipo == b"moov"
        if tipo == b"moov":
            tem_moov = True
        elif tipo == b"uuid":
            uuid = f.read(16)
            e_c2pa = uuid.startswith(UUID_C2PA)
            tem_c2pa = tem_c2pa or e_c2pa
            ler = e_c2pa or uuid.startswith(UUID_XMP)
        if ler:
            # max: um uuid menor que o próprio id deixa f.tell() além do fim, e read(negativo) leria tudo.
            falta = max(min(fim, tamanho) - f.tell(), 0)
            blocos.append(f.read(min(falta, restante)))
            if falta > restante:
                break
            restante -= falta
        if fim > tamanho:
            break
        pos = fim
    # O separador impede que o fim de um átomo e o início do outro formem um marcador.
    return b"\0".join(blocos).lower(), completo, tem_c2pa


def analisar_arquivo(caminho: Path) -> dict:
    resultado = {"nome": caminho.name, "status": "erro", "origens": []}
    try:
        with open(caminho, "rb") as f:
            dados, completo, tem_c2pa = _ler_metadados(f, os.fstat(f.fileno()).st_size)
    except OSError as exc:
        logger.warning("Metadados de IA: não consegui ler %s: %s", caminho, exc)
        return resultado

    origens = [nome for nome, marcadores in ORIGENS if any(m in dados for m in marcadores)]
    if origens or any(m in dados for m in MARCADORES_IA):
        resultado.update(status="ia", origens=origens or [ORIGEM_DESCONHECIDA])
    elif completo:
        # Só um arquivo lido até o fim pode dizer que não achou nada.
        resultado["status"] = "credencial" if tem_c2pa or MARCADOR_C2PA in dados else "sem_marcador"
    return resultado


def analisar_pasta(pasta: Path) -> list[dict]:
    """Vídeos do primeiro nível da pasta, do mais recente para o mais antigo."""
    videos = []
    for p in pasta.iterdir():
        if p.name.startswith(".") or p.suffix.lower() not in VIDEO_EXTS or not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0
        videos.append((-mtime, p.name, p))
    videos.sort(key=lambda v: v[:2])
    return [analisar_arquivo(p) for _, _, p in videos]
