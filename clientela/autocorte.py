"""Corte vertical automático: clipe bruto → trechos escolhidos por um LLM →
vídeo 9:16 com legenda e áudio soltos para terminar a edição à mão.

O Clientela roda este arquivo como script, num job do editor (como o
``kit/auto.py`` das legendas), e ele fala o mesmo stream-json do Claude Code:
o painel acompanha, cancela e fecha o job sem saber a diferença. Por isso
este módulo só usa a biblioteca padrão e não importa nada do app — as funções
puras são importadas pelos testes.

  1. Transcrição: o áudio vai para o whisper (o whisper.cpp e o modelo do
     ``hyperframes transcribe``, o mesmo ASR do kit) em pedaços de 10 s sem
     contexto, com tempo por palavra — inteiro, com música por baixo, ele
     perdia a fala (ver ``transcrever_em_pedacos``).
  2. Locutores: pyannote, se instalado e com token do Hugging Face; senão,
     pelas faixas de áudio separadas do arquivo (OBS grava mic e convidado em
     faixas próprias), se configuradas; senão, um locutor só.
  3. Imagem e som: quadros do clipe inteiro (soltos e em folhas de contato),
     movimento e trocas de cena (``scdet``) e volume por janela. Sem isto o
     LLM só lia texto — e num gameplay sem narração o whisper inventa letra
     em cima da música (as palavras ``duvidosa``).
  4. LLM: quadros, linha do tempo e transcrição vão para o Claude Code CLI
     (``claude -p``, já logado na máquina, folhas anexadas pelo stream-json
     de entrada) ou para a OpenRouter (idem), que assiste, conta o
     que aconteceu e devolve trechos, gancho e o que descartou. Os tempos são
     encaixados nas palavras de verdade quando caem nelas ou perto (o LLM erra
     décimos e corta sílaba) e ganham um respiro; longe da fala, ficam.
  5. FFmpeg: os trechos são emendados e o quadro vira 1080x1920 a 30 fps,
     recortado (``corte``) ou sobre o próprio vídeo desfocado (``desfocado``).
  6. Entrega, solta em ``<projeto>/Corte 9x16 vN/``: o vídeo sem legenda
     impressa, ``.ass`` animado (karaokê, um estilo por locutor, e o gancho no alto), ``.srt``
     geral e por locutor, cada faixa de áudio do arquivo cortada igual ao
     vídeo, a voz de cada locutor isolada e o ``corte.json`` com os trechos.

uso: python3 clientela/autocorte.py --args <arquivo.json>   (cwd = bancada)
     python3 clientela/autocorte.py --transcrever <vídeo> <saída.json> [modelo] [idioma]
       (só a transcrição, para os cortes inteligentes do estúdio; cwd = pasta de cache)
"""

import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path

LARGURA, ALTURA, FPS = 1080, 1920, 30
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULTS = {
    "provedor_llm": "claude",        # "claude" (CLI local) | "openrouter"
    "modelo_llm": "",                # claude: vazio = padrão do CLI
    "openrouter_modelo": "",
    "openrouter_api_key": "",
    "duracao_min": 20,
    "duracao_max": 60,
    "formato": "corte",              # "corte" | "desfocado" (sem `layout`)
    "enquadramento": 0.5,            # posição horizontal do recorte: 0 = esquerda, 1 = direita (sem `layout`)
    # Enquadramento escolhido no editor visual, como o da Twitch (ver normalizar_layout):
    # {"modo": "inteiro" | "dividido" | "desfocado", "principal": caixa, "camera": caixa, "divisao": 0.35,
    #  "escala": 1.0, "posicao": 0.5,  (só no desfocado)
    #  "capa": {"tipo": "nenhuma" | "quadro" | "imagem", "t": s, "imagem": caminho}}
    "layout": None,
    "asr_modelo": "large-v3",
    "idioma": "pt",
    "legenda_maiusculas": True,
    "legenda_fonte": "Arial Black",
    "legenda_tamanho": 84,
    "palavras_por_bloco": 4,
    "hf_token": "",
    "pyannote_modelo": "pyannote/speaker-diarization-3.1",
    # [{"faixa": 2, "nome": "Bruno"}, …] — faixa de áudio do arquivo, contando de 1
    "faixas_locutores": [],
}

# Destaque da palavra falada, por locutor (o resto do bloco fica branco).
CORES_LOCUTOR = ["#FFD60A", "#4CC9F0", "#B2DC5B", "#FF6FAE", "#FF9F1C", "#C77DFF"]

_TRECHO = {
    "type": "object",
    "properties": {
        "inicio": {"type": "number"},
        "fim": {"type": "number"},
        "motivo": {"type": "string"},
    },
    "required": ["inicio", "fim"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "resumo": {"type": "string"},
        "gancho": {"type": "string"},
        "legenda": {"type": "boolean"},
        "segmentos": {"type": "array", "items": _TRECHO},
        "descartado": {"type": "array", "items": _TRECHO},
    },
    "required": ["segmentos"],
}

PROMPT = """Você é editor de cortes verticais (Reels, TikTok, Shorts) de lives e gravações. Assista a este clipe — \
imagem, som e fala —, entenda o que aconteceu e monte com ele o corte mais forte possível.

O CLIPE
- {duracao} s, {largura}x{altura}.
- {como_ver}
- Linha do tempo (uma linha a cada {janela} s): volume do áudio em dB (mais perto de 0 = mais alto), movimento da \
imagem (diferença média entre quadros: perto de 0 = tela parada, alto = ação ou câmera mexendo) e "cena" quando a \
imagem muda de vez.
- Transcrição do whisper, uma linha por frase: [início–fim em segundos] locutor: texto. Linha marcada (duvidosa) tem \
cara de música ou de alucinação do whisper (poucas palavras para o tempo, ou o mesmo texto repetido): confira com a \
imagem antes de tratar como fala. Música com letra também é transcrita, misturada à fala de quem está na live: \
separe pelo sentido (a conversa reage ao que aparece na imagem; a letra não).

COMO TRABALHAR
1. Veja todos os quadros antes de decidir. Entenda que tipo de vídeo é (gameplay, conversa, reação, tutorial, IRL…), \
onde está a ação, onde nada acontece (lobby, menu, carregamento, espera, tela parada, andar sem rumo) e onde está o \
pico (jogada, kill, susto, risada, revelação, frase de impacto).
2. Decida a história do corte: gancho → desenvolvimento → pico. Quem não viu a live precisa entender sozinho.
3. Tire tudo o que está fora de contexto ou não acrescenta: menu, lobby, carregamento, espera, trecho morno, fala que \
não se resolve, repetição, hesitação, respiro longo. Vários trechos curtos e dinâmicos valem mais que um longo morno.
4. Sem fala de verdade (só jogo, só música): escolha pela imagem, pelo volume e pelo movimento, e corte onde fizer \
sentido — de preferência numa troca de cena ou de ação.
5. Com fala: nunca corte no meio de uma palavra nem de uma ideia; mantenha a reação depois do pico (a risada é o \
momento); prefira cortar em pausas de 0,4 s ou mais.

REGRAS
- duração total entre {min} e {max} s (o clipe tem {duracao} s); se não houver material bom para tanto, entregue \
menos — nunca encha com trecho morto;
- o primeiro trecho prende nos 3 primeiros segundos: começa na ação ou na frase mais forte, nunca em menu ou espera;
- trechos em ordem cronológica, sem sobreposição, com tempos do clipe original (entre 0 e {duracao}), cada um com \
pelo menos 1 s;
- "gancho": texto curto (até 6 palavras, sem emoji) para a tela nos 3 primeiros segundos, que faça a pessoa querer ver o resto; \
vazio se o próprio começo já basta;
- "legenda": false quando não há fala que valha legendar (só jogo, só música, letra de música);
- "descartado": os trechos que você tirou e por quê, para o editor conferir.
{observacoes}
Responda só com JSON: {{"titulo": "título curto do corte", "resumo": "o que acontece no clipe, em 1–2 frases", \
"gancho": "", "legenda": true, "segmentos": [{{"inicio": 0.0, "fim": 0.0, "motivo": "o que acontece aqui e por que \
fica"}}], "descartado": [{{"inicio": 0.0, "fim": 0.0, "motivo": "por que saiu"}}]}}

Linha do tempo:
{linha_do_tempo}

Transcrição:
{linhas}
"""


class Falha(Exception):
    pass


# ------------------------------------------------------------------ stream-json (o que o painel lê)
def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def texto(msg):
    _emit({"type": "assistant", "message": {"content": [{"type": "text", "text": msg}]}})


def passo(desc):
    _emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"description": desc}}]}})


def fim(ok, msg, inicio, custo=0.0):
    _emit({"type": "result", "subtype": "success" if ok else "error", "is_error": not ok, "result": msg,
           "total_cost_usd": custo, "num_turns": 0, "duration_ms": int((time.time() - inicio) * 1000)})


def rodar(cmd, timeout=3600, **kw):
    # O stdin do job é o arquivo do prompt: um ffmpeg que o herda lê o "c" de
    # "(corte…" como a tecla de comando de filtro ("Enter command:") e gira a
    # 100% de CPU para sempre. Nenhum filho lê stdin.
    cmd = [str(c) for c in cmd]
    if Path(cmd[0]).name == "ffmpeg":
        cmd.insert(1, "-nostdin")
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
        linhas = [ln for ln in err.splitlines() if ln.strip()]
        raise Falha(f"{Path(str(cmd[0])).name} falhou:\n" + "\n".join(linhas[-8:]))
    return r


# ------------------------------------------------------------------ mídia
def probe(video):
    out = rodar(["ffprobe", "-v", "error", "-show_entries",
                 "stream=index,codec_type,width,height,r_frame_rate,channels:stream_tags=title,handler_name"
                 ":format=duration", "-of", "json", video], timeout=120, text=True).stdout
    d = json.loads(out)
    streams = d.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise Falha("o arquivo não tem vídeo")
    audios = []
    for s in (s for s in streams if s.get("codec_type") == "audio"):
        tags = s.get("tags") or {}
        titulo = (tags.get("title") or tags.get("handler_name") or "").strip()
        if titulo.lower() in ("soundhandler", "sound handler", "core media audio"):
            titulo = ""  # nome genérico do contêiner, não do OBS
        audios.append({"faixa": len(audios) + 1, "canais": int(s.get("channels") or 2), "titulo": titulo})
    return {"largura": int(v["width"]), "altura": int(v["height"]),
            "duracao": float((d.get("format") or {}).get("duration") or 0), "audios": audios}


def extrair_audio(video, faixa, destino, taxa=16000):
    rodar(["ffmpeg", "-v", "error", "-y", "-i", video, "-map", f"0:a:{faixa - 1}", "-vn", "-ac", "1",
           "-ar", str(taxa), destino])


def energia_faixa(video, faixa, janelas, taxa=8000):
    """RMS da faixa em cada janela (t0, t1): o locutor de uma palavra é a faixa mais alta nela."""
    r = rodar(["ffmpeg", "-v", "error", "-i", video, "-map", f"0:a:{faixa - 1}", "-ac", "1", "-ar", str(taxa),
               "-f", "s16le", "-"])
    amostras = array("h")
    amostras.frombytes(r.stdout[: len(r.stdout) - len(r.stdout) % 2])
    out = []
    for t0, t1 in janelas:
        i0, i1 = max(0, int(t0 * taxa)), min(len(amostras), int(t1 * taxa))
        if i1 <= i0:
            out.append(0.0)
            continue
        soma = sum(x * x for x in amostras[i0:i1])
        out.append(math.sqrt(soma / (i1 - i0)))
    return out


# ------------------------------------------------------------------ o que o LLM vê e ouve
QUADROS_MAX = 120          # quadros do clipe inteiro que o LLM recebe (o intervalo cresce com a duração)
FOLHA_COLUNAS, FOLHA_LINHAS = 3, 4
QUADRO_W, QUADRO_H = 960, 540        # quadro solto, para olhar um momento de perto
MINIATURA_W, MINIATURA_H = 640, 360  # cada quadro na folha de contato
LIMIAR_CENA = 12.0                   # lavfi.scd.score a partir do qual a imagem mudou de vez


def intervalo_quadros(duracao, maximo=QUADROS_MAX):
    return max(1, math.ceil(duracao / maximo))


def ler_metadata(txt):
    """Saída do filtro ``metadata=mode=print`` → [(t, {chave: valor})]."""
    out = []
    for linha in txt.splitlines():
        m = re.search(r"pts_time:([-\d.]+)", linha)
        if m:
            out.append((float(m.group(1)), {}))
        elif "=" in linha and out:
            k, v = linha.split("=", 1)
            try:
                out[-1][1][k.strip()] = float(v)
            except ValueError:
                pass
    return out


def linha_do_tempo(movimento, volume, duracao, janela):
    """Uma linha por janela: volume médio (dB, pela potência), movimento médio e se houve troca de cena."""
    out = []
    n = max(1, math.ceil(duracao / janela))
    for k in range(n):
        t0, t1 = k * janela, min(duracao, (k + 1) * janela)
        mov = [d.get("lavfi.scd.mafd", 0.0) for t, d in movimento if t0 <= t < t1]
        cena = any(d.get("lavfi.scd.score", 0.0) >= LIMIAR_CENA for t, d in movimento if t0 <= t < t1 and t > 0)
        vol = [d["lavfi.astats.Overall.RMS_level"] for t, d in volume
               if t0 <= t < t1 and math.isfinite(d.get("lavfi.astats.Overall.RMS_level", float("-inf")))]
        db = 10 * math.log10(sum(10 ** (v / 10) for v in vol) / len(vol)) if vol else None
        out.append({"t": round(t0, 2), "fim": round(t1, 2), "volume_db": None if db is None else round(db, 1),
                    "movimento": round(sum(mov) / len(mov), 1) if mov else None, "cena": cena})
    return out


def texto_linha_do_tempo(linhas):
    def fmt(x):
        vol = "silêncio" if x["volume_db"] is None else f"{x['volume_db']:.0f} dB"
        mov = "—" if x["movimento"] is None else f"{x['movimento']:.1f}"
        return f"[{x['t']:.0f}–{x['fim']:.0f}] volume {vol} · movimento {mov}{' · cena' if x['cena'] else ''}"
    return "\n".join(fmt(x) for x in linhas)


def folhas_de_contato(tempos, por_folha=FOLHA_COLUNAS * FOLHA_LINHAS):
    """[[tempos da folha 1], …] na ordem em que os quadros entram nas folhas (o filtro tile preenche linha a linha)."""
    return [tempos[i:i + por_folha] for i in range(0, len(tempos), por_folha)]


def analisar_midia(video, info, trab):
    """Um passe no vídeo tira os quadros (soltos e em folhas de contato) e mede o movimento; um passe no
    áudio mede o volume. Tudo relativo a ``trab`` (cwd dos ffmpeg): caminho de Windows dentro de um filtro
    precisaria de escape."""
    passo_q = intervalo_quadros(info["duracao"])
    qdir, fdir = trab / "quadros", trab / "folhas"
    for d in (qdir, fdir):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
    caixa = f"scale={QUADRO_W}:{QUADRO_H}:force_original_aspect_ratio=decrease"
    rodar(["ffmpeg", "-v", "error", "-y", "-i", video, "-an", "-filter_complex",
           f"[0:v:0]split[a][b];[a]fps=5,scale=320:-2,scdet=threshold={LIMIAR_CENA},"
           f"metadata=mode=print:file=movimento.txt[m];[b]fps=1/{passo_q}:round=down,{caixa}[q]",
           "-map", "[m]", "-f", "null", "-", "-map", "[q]", "-q:v", "4", "quadros/%05d.jpg"], cwd=trab)
    indices = sorted(qdir.glob("*.jpg"))
    tempos = [i * passo_q for i in range(len(indices))]
    if indices:
        rodar(["ffmpeg", "-v", "error", "-y", "-framerate", "1", "-i", "quadros/%05d.jpg", "-vf",
               f"scale={MINIATURA_W}:{MINIATURA_H}:force_original_aspect_ratio=decrease,"
               f"pad={MINIATURA_W}:{MINIATURA_H}:-1:-1,tile={FOLHA_COLUNAS}x{FOLHA_LINHAS}:padding=6",
               "-q:v", "4", "folhas/folha-%02d.jpg"], cwd=trab)
    quadros = []
    for arq, t in zip(indices, tempos):  # depois das folhas: o tile lê a sequência numerada
        nome = f"q-{t:05d}s.jpg"
        arq.rename(qdir / nome)
        quadros.append({"t": t, "arquivo": f"quadros/{nome}"})
    folhas = [{"arquivo": f"folhas/folha-{i:02d}.jpg", "tempos": ts}
              for i, ts in enumerate(folhas_de_contato(tempos), 1) if (fdir / f"folha-{i:02d}.jpg").exists()]
    movimento = ler_metadata((trab / "movimento.txt").read_text(encoding="utf-8", errors="replace"))
    volume = []
    if info["audios"]:
        rodar(["ffmpeg", "-v", "error", "-y", "-i", video, "-vn", "-map", "0:a:0", "-af",
               "aresample=16000,asetnsamples=n=8000,astats=metadata=1:reset=1:measure_perchannel=none"
               ":measure_overall=RMS_level,ametadata=mode=print:file=volume.txt", "-f", "null", "-"], cwd=trab)
        volume = ler_metadata((trab / "volume.txt").read_text(encoding="utf-8", errors="replace"))
    return {"passo": passo_q, "quadros": quadros, "folhas": folhas,
            "linha_do_tempo": linha_do_tempo(movimento, volume, info["duracao"], passo_q)}


# ------------------------------------------------------------------ transcrição
def palavras_de_json(d):
    """hyperframes transcribe ([{text, start, end}]) ou whisper/Premiere (segments[].words[])."""
    words = []

    def add(txt, t, f):
        txt = str(txt or "").strip()
        if txt:
            words.append({"texto": txt, "t": round(float(t), 3), "fim": round(float(max(f, t)), 3)})

    if isinstance(d, dict) and d.get("segments"):
        for seg in d["segments"]:
            for w in seg.get("words") or []:
                if w.get("type", "word") != "word":
                    continue
                start = float(w.get("start", 0))
                add(w.get("text", w.get("word")), start, w.get("end", start + float(w.get("duration", 0))))
    else:
        for w in d if isinstance(d, list) else (d or {}).get("words") or []:
            start = float(w.get("start", 0))
            add(w.get("text", w.get("word")), start, w.get("end", start))
    words.sort(key=lambda w: w["t"])
    return words


PEDACO_S, FOLGA_S = 10.0, 1.5
WHISPER_HOME = Path.home() / ".cache" / "hyperframes" / "whisper"   # onde o hyperframes guarda modelo e build


def achar_whisper_cli():
    """O mesmo whisper.cpp que o ``hyperframes transcribe`` usa, na mesma ordem de busca dele."""
    env = os.environ.get("HYPERFRAMES_WHISPER_PATH")
    if env and Path(env).is_file():
        return env
    achado = shutil.which("whisper-cli")
    if achado:
        return achado
    build = WHISPER_HOME / "whisper.cpp" / "build"
    for c in (Path("/opt/homebrew/bin/whisper-cli"), Path("/usr/local/bin/whisper-cli"), build / "bin" / "whisper-cli",
              build / "whisper-cli", build / "bin" / "whisper-cli.exe", build / "bin" / "Release" / "whisper-cli.exe"):
        if c.is_file():
            return str(c)
    return None


def modelo_whisper(modelo):
    return WHISPER_HOME / "models" / f"ggml-{modelo}.bin"


def pedacos(duracao, tamanho=PEDACO_S, folga=FOLGA_S):
    """[(início, fim, miolo_ini, miolo_fim)]: pedaços de `tamanho` s com `folga` dos dois lados."""
    out, k = [], 0
    while k * tamanho < duracao:
        lo, hi = k * tamanho, min(duracao, (k + 1) * tamanho)
        out.append((max(0.0, lo - folga), min(duracao, hi + folga), lo, hi))
        k += 1
    return out


def juntar_pedacos(resultados, perto=0.3):
    """resultados = [(início, miolo_ini, miolo_fim, [(t, fim, texto)] relativos ao pedaço)] → palavras do clipe.
    Cada palavra vale no pedaço em cujo miolo cai o meio dela. A folga de um vizinho só entra onde o dono do
    trecho não ouviu nada: a letra da música no começo de um pedaço às vezes "tampa" a fala logo depois."""
    cands = []
    for k, (a, lo, hi, ws) in enumerate(resultados):
        for t, f, tx in ws:
            t0, t1 = a + t, a + max(f, t)
            meio = (t0 + t1) / 2
            dist = 0.0 if lo <= meio < hi else min(abs(meio - lo), abs(meio - hi))
            cands.append((dist, t0, t1, tx, k))
    aceitas = [c for c in cands if c[0] == 0.0]
    for c in sorted((c for c in cands if c[0] > 0), key=lambda c: (c[0], c[1])):
        _, t0, t1, _, k = c
        if not any(o[4] != k and o[1] < t1 + perto and o[2] > t0 - perto for o in aceitas):
            aceitas.append(c)
    aceitas.sort(key=lambda c: c[1])
    return [{"texto": tx, "t": round(t0, 3), "fim": round(t1, 3)} for _, t0, t1, tx, _ in aceitas]


def palavras_whisper_cli(d, max_palavra=1.2):
    """JSON do ``whisper-cli -oj -ml 1 -sow``: um segmento por palavra. O fim da última palavra antes de um
    silêncio vem esticado até a próxima fala; a palavra fica com no máximo `max_palavra` s."""
    out = []
    for seg in (d or {}).get("transcription") or []:
        tx = str(seg.get("text") or "").strip()
        if not tx or re.fullmatch(r"\[.*\]|\(.*\)", tx):
            continue
        t, f = seg["offsets"]["from"] / 1000, seg["offsets"]["to"] / 1000
        out.append((t, min(max(f, t), t + max_palavra), tx))
    return out


def transcrever_em_pedacos(audio, trab, cli, modelo, idioma, duracao):
    """O whisper lê de 30 em 30 s e, com o contexto da frase anterior ligado (o padrão do ``hyperframes
    transcribe``), uma música com letra o prende: no clipe do BF6 ele escreveu a letra, pulou de 7,6 para 30 s
    e depois repetiu a letra em loop — e a conversa sobre pular do helicóptero sumiu. Em pedaços de 10 s,
    sem contexto (``-mc 0``), numa chamada só (o modelo carrega uma vez), a fala aparece."""
    pdir = trab / "pedacos"
    shutil.rmtree(pdir, ignore_errors=True)
    pdir.mkdir()
    partes, arquivos = pedacos(duracao), []
    for k, (a, b, _, _) in enumerate(partes):
        arq = pdir / f"p{k:04d}.wav"
        rodar(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", audio, arq])
        arquivos.append(arq)
    cmd = [cli, "-m", modelo, "-l", idioma or "auto", "-mc", "0", "-ml", "1", "-sow", "-oj", "-np"]
    for arq in arquivos:
        cmd += ["-f", arq]
    rodar(cmd, timeout=max(600, int(duracao * 20)))
    resultados = []
    for (a, _, lo, hi), arq in zip(partes, arquivos):
        js = arq.with_name(arq.name + ".json")
        d = json.loads(js.read_text(encoding="utf-8", errors="replace")) if js.exists() else {}
        resultados.append((a, lo, hi, palavras_whisper_cli(d)))
    return juntar_pedacos(resultados)


def transcrever(audio, trab, modelo, idioma, duracao=None):
    feito = trab / f"asr-{modelo}-pedacos.json"
    if feito.exists():
        texto(f"Reaproveitando a transcrição ({modelo}) desta bancada.")
        return palavras_de_json(json.loads(feito.read_text(encoding="utf-8")))
    cli, arq_modelo = achar_whisper_cli(), modelo_whisper(modelo)
    inteira = None
    if not (cli and arq_modelo.is_file()):
        # Na primeira vez o hyperframes baixa o modelo (e compila o whisper.cpp se precisar).
        inteira = _transcrever_hyperframes(audio, trab, modelo, idioma)
        cli, arq_modelo = achar_whisper_cli(), modelo_whisper(modelo)
    if cli and arq_modelo.is_file() and duracao:
        words = transcrever_em_pedacos(audio, trab, cli, arq_modelo, idioma, duracao)
    elif inteira is not None:
        texto("aviso: sem o whisper-cli à mão, a transcrição foi do arquivo inteiro — com música por baixo, "
              "ela pode perder fala.")
        words = inteira
    else:
        words = _transcrever_hyperframes(audio, trab, modelo, idioma)
    feito.write_text(json.dumps([{"text": w["texto"], "start": w["t"], "end": w["fim"]} for w in words],
                                ensure_ascii=False), encoding="utf-8")
    return words


def _transcrever_hyperframes(audio, trab, modelo, idioma):
    cmd = ["npx", "hyperframes", "transcribe", str(audio), "-e", "whisper", "-m", modelo, "--json"]
    if idioma:
        cmd += ["-l", idioma]
    r = rodar(cmd, cwd=trab, text=True)
    path = trab / "transcript.json"
    for line in r.stdout.splitlines():
        try:
            path = Path(json.loads(line).get("transcriptPath") or path)
        except (ValueError, AttributeError):
            pass
    if not path.exists():
        raise Falha("o whisper não gerou a transcrição")
    return palavras_de_json(json.loads(path.read_text(encoding="utf-8")))


# ------------------------------------------------------------------ locutores
def atribuir_por_turnos(words, turnos):
    """turnos = [(t0, t1, rótulo)]: cada palavra fica com o rótulo de maior sobreposição."""
    for w in words:
        melhor, sobra = None, 0.0
        for t0, t1, rot in turnos:
            ov = min(w["fim"], t1) - max(w["t"], t0)
            if ov > sobra:
                melhor, sobra = rot, ov
        if melhor is None and turnos:  # palavra num vão entre turnos: o mais próximo
            melhor = min(turnos, key=lambda tr: min(abs(w["t"] - tr[1]), abs(w["fim"] - tr[0])))[2]
        w["locutor"] = melhor
    return words


def nomear_locutores(words, nomes=None):
    """Rótulos crus (SPEAKER_00, faixa 2…) → "Locutor 1", "Locutor 2" pela ordem em que falam."""
    nomes = nomes or {}
    mapa = {}
    for w in words:
        rot = w.get("locutor")
        if rot not in mapa:
            mapa[rot] = nomes.get(rot) or f"Locutor {len(mapa) + 1}"
        w["locutor"] = mapa[rot]
    return words


def por_energia(words, energias):
    """energias = {rótulo: [rms por palavra]}: a faixa mais alta leva a palavra."""
    rotulos = list(energias)
    for i, w in enumerate(words):
        w["locutor"] = max(rotulos, key=lambda r: energias[r][i]) if rotulos else None
    return words


def diarizar_pyannote(audio, token, modelo):
    from pyannote.audio import Pipeline  # opcional: só existe se o usuário instalou

    try:
        pipe = Pipeline.from_pretrained(modelo, token=token)
    except TypeError:  # pyannote 3.x
        pipe = Pipeline.from_pretrained(modelo, use_auth_token=token)
    if pipe is None:
        raise RuntimeError("o pyannote não carregou o modelo (aceite os termos dele no Hugging Face)")
    out = pipe(str(audio))
    anot = getattr(out, "speaker_diarization", out)  # pyannote 4 devolve um objeto com a anotação dentro
    return [(turn.start, turn.end, rot) for turn, _, rot in anot.itertracks(yield_label=True)]


def locutores(words, video, audio, info, cfg):
    """Devolve (palavras com locutor, origem, faixas_isoladas). Nunca derruba o job: sem como separar, um locutor."""
    faixas = [f for f in cfg.get("faixas_locutores") or [] if 1 <= int(f.get("faixa") or 0) <= len(info["audios"])]
    if len(faixas) >= 2:
        janelas = [(w["t"], max(w["fim"], w["t"] + 0.08)) for w in words]
        energias = {}
        for f in faixas:
            energias[f"faixa{f['faixa']}"] = energia_faixa(video, int(f["faixa"]), janelas)
        por_energia(words, energias)
        nomes = {f"faixa{f['faixa']}": (f.get("nome") or f"Faixa {f['faixa']}").strip() for f in faixas}
        return nomear_locutores(words, nomes), "faixas de áudio", {nomes[k]: int(k[5:]) for k in nomes}
    # O app manda o token pelo ambiente (HF_TOKEN), nunca pelo auto-args.json.
    token = os.environ.get("HF_TOKEN") or cfg.get("hf_token")
    if token:
        try:
            turnos = diarizar_pyannote(audio, token, cfg.get("pyannote_modelo") or DEFAULTS["pyannote_modelo"])
            if turnos:
                return nomear_locutores(atribuir_por_turnos(words, turnos)), "pyannote", {}
        except ImportError:
            texto("aviso: pyannote não está instalado neste Python; seguindo com um locutor só.")
        except Exception as exc:
            texto(f"aviso: a separação de locutores falhou ({exc}); seguindo com um locutor só.")
    for w in words:
        w["locutor"] = "Locutor 1"
    return words, "um locutor", {}


# ------------------------------------------------------------------ frases e prompt
def frases(words, pausa=0.6, maximo=24):
    """Uma frase por pausa, pontuação final, troca de locutor ou `maximo` palavras."""
    out, cur = [], []
    for i, w in enumerate(words):
        if cur and (w["t"] - words[i - 1]["fim"] > pausa or w.get("locutor") != cur[-1].get("locutor")):
            out.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.?!…]$", w["texto"]) or len(cur) >= maximo:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [{"t": f[0]["t"], "fim": f[-1]["fim"], "locutor": f[0].get("locutor"),
             "texto": " ".join(w["texto"] for w in f), "duvidosa": any(w.get("duvidosa") for w in f)} for f in out]


def _norm(t):
    return re.sub(r"[^\w]+", "", t.lower())


# O que o whisper escreve quando um pedaço só tem música (veio das legendas com que ele foi treinado).
ALUCINACOES = re.compile(r"amara\.org|legendas? pela comunidade|obrigad[oa] por assistir|inscreva-se|"
                         r"legendado por|legendas? por|(tradução|transcrição) e legendas?|legendagem|"
                         r"subtitles by|transcript(ion)? by", re.I)   # "Transcrição e Legendas Pedro Negri" num tom puro


def marcar_duvidosas(words, max_palavra=1.5, ritmo_min=0.8, repeticoes=3, n=4):
    """Whisper em cima de música (ou de jogo sem fala) inventa letra: palavras de vários segundos, frases
    com quase nada para o tempo e o mesmo trecho repetido. Marca essas palavras com ``duvidosa`` — o LLM é
    avisado e o encaixe dos cortes não se prende a elas."""
    for w in words:
        w["duvidosa"] = w["fim"] - w["t"] > max_palavra
    for f in _grupos_por_pausa(words):
        dur = f[-1]["fim"] - f[0]["t"]
        if dur > 2.5 and len(f) / dur < ritmo_min:
            for w in f:
                w["duvidosa"] = True
    chaves = [_norm(w["texto"]) for w in words]
    contagem = {}
    for i in range(len(words) - n + 1):
        k = tuple(chaves[i:i + n])
        contagem[k] = contagem.get(k, 0) + 1
    for i in range(len(words) - n + 1):
        if contagem[tuple(chaves[i:i + n])] >= repeticoes:
            for w in words[i:i + n]:
                w["duvidosa"] = True
    for f in _grupos_por_pausa(words):
        if ALUCINACOES.search(" ".join(w["texto"] for w in f)):
            for w in f:
                w["duvidosa"] = True
    return words


def _grupos_por_pausa(words, pausa=0.6):
    out, cur = [], []
    for i, w in enumerate(words):
        if cur and w["t"] - words[i - 1]["fim"] > pausa:
            out.append(cur)
            cur = []
        cur.append(w)
    return out + ([cur] if cur else [])


def _como_ver(midia):
    if not midia or not midia.get("folhas"):
        return "Sem quadros: decida pela linha do tempo e pela transcrição."
    cab = (f"Quadros: um a cada {midia['passo']} s, em folhas de contato de {FOLHA_COLUNAS} colunas x {FOLHA_LINHAS} "
           "linhas, lidas da esquerda para a direita e de cima para baixo. As folhas vão anexadas a esta mensagem, "
           "nesta ordem:")
    linhas = [f"  {f['arquivo']}: quadros em {', '.join(str(t) for t in f['tempos'])} s" for f in midia["folhas"]]
    return cab + "\n" + "\n".join(linhas)


def montar_prompt(words, duracao, cfg, observacoes="", midia=None, info=None):
    linhas = "\n".join(f"[{f['t']:.2f}–{f['fim']:.2f}] {f['locutor'] or 'Locutor 1'}{' (duvidosa)' if f['duvidosa'] else ''}: "
                       f"{f['texto']}" for f in frases(words)) or "(sem fala)"
    obs = (observacoes or "").strip()
    info = info or {}
    tempo = (midia or {}).get("linha_do_tempo")
    return PROMPT.format(
        min=cfg.get("duracao_min") or DEFAULTS["duracao_min"], max=cfg.get("duracao_max") or DEFAULTS["duracao_max"],
        duracao=round(duracao, 1), linhas=linhas, largura=info.get("largura", "?"), altura=info.get("altura", "?"),
        como_ver=_como_ver(midia), janela=(midia or {}).get("passo", 1),
        linha_do_tempo=texto_linha_do_tempo(tempo) if tempo else "(sem linha do tempo)",
        observacoes=f"- pedido do editor (vale mais que as regras acima): {obs}\n" if obs else "",
    )


# ------------------------------------------------------------------ LLM
def extrair_json(txt):
    txt = str(txt or "").strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", txt, re.S)
    if m:
        txt = m.group(1)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j <= i:
        raise Falha("o LLM não devolveu JSON")
    try:
        return json.loads(txt[i:j + 1])
    except ValueError as exc:
        raise Falha(f"o LLM devolveu um JSON inválido: {exc}") from None


def _imagem_b64(caminho):
    return base64.b64encode(Path(caminho).read_bytes()).decode()


def llm_claude(prompt, cfg, binario, timeout=900, imagens=(), cwd=None, schema=None, env=None):
    """Sem ferramentas: com `imagens` (as folhas de contato), elas vão dentro da própria mensagem, pelo
    stream-json de entrada. A primeira versão deixava o claude abri-las com Read: 8 voltas, cada uma
    reenviando a conversa, ~US$ 1,40 num clipe de 1 min no Opus; anexadas, é uma volta só."""
    if not binario:
        raise Falha("Claude Code CLI não encontrado (Configurar Editor → caminho do claude)")
    if imagens:
        conteudo = [{"type": "text", "text": prompt}] + [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _imagem_b64(im)}}
            for im in imagens]
        entrada = json.dumps({"type": "user", "message": {"role": "user", "content": conteudo}}, ensure_ascii=False) + "\n"
        formato = ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    else:
        entrada, formato = prompt, ["--output-format", "json"]
    cmd = [binario, "-p", *formato, "--tools", "", "--no-session-persistence", "--json-schema", json.dumps(schema or SCHEMA)]
    if cfg.get("modelo_llm"):
        cmd += ["--model", cfg["modelo_llm"]]
    r = subprocess.run(cmd, input=entrada, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    d = None
    for linha in reversed((r.stdout or "").strip().splitlines()):  # stream-json: a última linha "result"
        try:
            item = json.loads(linha)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("type", "result") == "result":
            d = item
            break
    if d is None:
        raise Falha("o claude não respondeu: " + ((r.stderr or r.stdout or "").strip()[-400:] or f"código {r.returncode}"))
    if d.get("is_error"):
        msg = str(d.get("result") or "erro")
        if "authenticate" in msg.lower() or "/login" in msg:
            msg = "o Claude Code CLI não está logado: rode `claude` no Terminal e faça /login"
        raise Falha(f"o claude falhou: {msg[:400]}")
    if cwd:  # quanto custou e quantas voltas deu, para conferir na bancada
        uso = {k: d.get(k) for k in ("num_turns", "total_cost_usd", "duration_ms", "usage", "modelUsage")}
        (Path(cwd) / "llm-uso.json").write_text(json.dumps(uso, ensure_ascii=False, indent=1), encoding="utf-8")
    resposta = d.get("structured_output") or extrair_json(d.get("result"))
    return resposta, float(d.get("total_cost_usd") or 0)


def llm_openrouter(prompt, cfg, timeout=300, imagens=()):
    """`imagens`: caminhos das folhas de contato, anexadas à mensagem (o modelo precisa aceitar imagem)."""
    chave = os.environ.get("OPENROUTER_API_KEY") or cfg.get("openrouter_api_key")
    if not chave:
        raise Falha("sem chave da OpenRouter (Configurar Editor → Corte vertical, ou a variável OPENROUTER_API_KEY)")
    if not cfg.get("openrouter_modelo"):
        raise Falha("escolha o modelo da OpenRouter (ex.: anthropic/claude-sonnet-4.5) em Configurar Editor")
    corpo = json.dumps({
        "model": cfg["openrouter_modelo"],
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, *(
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _imagem_b64(im)}}
            for im in imagens)] if imagens else prompt}],
        "response_format": {"type": "json_object"},
        "temperature": cfg.get("temperatura", 0.3),
        "usage": {"include": True},  # sem isto a resposta não traz o custo
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=corpo, headers={
        "Authorization": f"Bearer {chave}", "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:8000", "X-Title": "Clientela",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode("utf-8", "replace")[:300]
        raise Falha(f"a OpenRouter recusou ({exc.code}): {detalhe}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise Falha(f"não consegui falar com a OpenRouter: {exc}") from None
    try:
        conteudo = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise Falha(f"resposta inesperada da OpenRouter: {str(d)[:300]}") from None
    custo = float(((d.get("usage") or {}).get("cost")) or 0)
    return extrair_json(conteudo), custo


# ------------------------------------------------------------------ trechos
def _encaixar(x, words, borda, tolerancia):
    """Nunca no meio de uma palavra: dentro dela, vai para a borda (`"t"` = começo, `"fim"`); perto de uma
    borda (até `tolerancia`), encaixa nela; longe de qualquer fala (jogo, música), fica onde o LLM pediu."""
    for w in words:
        if w["t"] < x < w["fim"]:
            return w[borda]
    perto = [w[borda] for w in words if abs(w[borda] - x) <= tolerancia]
    return min(perto, key=lambda v: abs(v - x)) if perto else x


def normalizar_segmentos(raw, words, duracao, respiro_ini=0.12, respiro_fim=0.25, minimo=0.6, tolerancia=0.4):
    """Trechos do LLM → trechos que dá para cortar: dentro do clipe, encaixados no começo e no fim das
    palavras (o LLM erra décimos e cortaria sílaba), com respiro que não invade a palavra vizinha, em ordem
    e sem sobreposição. Palavra ``duvidosa`` (letra de música que o whisper inventou) não segura corte: uma
    "palavra" de 11 s arrastaria o trecho para longe do que o LLM viu na imagem."""
    words = [w for w in words if not w.get("duvidosa")]
    segs = []
    for item in (raw or {}).get("segmentos") or []:
        try:
            a, b = float(item.get("inicio")), float(item.get("fim"))
        except (TypeError, ValueError, AttributeError):
            continue
        a, b = max(0.0, a), min(duracao, b)
        if b - a < minimo / 2:
            continue
        a, b = _encaixar(a, words, "t", tolerancia), _encaixar(b, words, "fim", tolerancia)
        if b - a >= minimo:
            segs.append({"inicio": a, "fim": b, "motivo": str(item.get("motivo") or "").strip()})
    segs.sort(key=lambda s: s["inicio"])
    for s in segs:
        antes = [w["fim"] for w in words if w["fim"] <= s["inicio"] + 1e-6 and w["t"] < s["inicio"] - 1e-6]
        depois = [w["t"] for w in words if w["t"] >= s["fim"] - 1e-6 and w["fim"] > s["fim"] + 1e-6]
        s["inicio"] = max(0.0, s["inicio"] - respiro_ini, max(antes) if antes else 0.0)
        s["fim"] = min(duracao, s["fim"] + respiro_fim, min(depois) if depois else duracao)
    juntos = []
    for s in segs:
        if juntos and s["inicio"] <= juntos[-1]["fim"] + 0.15:
            juntos[-1]["fim"] = max(juntos[-1]["fim"], s["fim"])
            if s["motivo"] and s["motivo"] not in juntos[-1]["motivo"]:
                juntos[-1]["motivo"] = "; ".join(x for x in (juntos[-1]["motivo"], s["motivo"]) if x)
        else:
            juntos.append(dict(s))
    for s in juntos:
        s["inicio"], s["fim"] = round(s["inicio"], 3), round(s["fim"], 3)
    return juntos


def remapear(words, segs):
    """Palavras do bruto → tempo do corte. Entra a palavra cujo meio cai num trecho."""
    out, offset = [], 0.0
    for s in segs:
        a, b = s["inicio"], s["fim"]
        for w in words:
            meio = (w["t"] + w["fim"]) / 2
            if a <= meio <= b:
                out.append({**w, "t": round(offset + max(w["t"], a) - a, 3), "fim": round(offset + min(w["fim"], b) - a, 3)})
        offset += b - a
    return out


def duracao_total(segs):
    return round(sum(s["fim"] - s["inicio"] for s in segs), 3)


# ------------------------------------------------------------------ cortes inteligentes (sem LLM)
# O estúdio pede, ajusta os parâmetros e vê as propostas na trilha IA antes de aplicar. A onda decide o
# silêncio; as palavras do whisper decidem gaguejo, repetição e hesitação, e protegem a fala do corte.
CORTES_PADRAO = {
    "silencio_db": -38.0,    # abaixo disto (pico da janela, dBFS) é silêncio
    "silencio_min": 0.5,     # silêncio mais curto que isto fica
    "respiro": 0.12,         # o que sobra de silêncio encostado na fala, dos dois lados
    "pausa_fala": 0.0,       # > 0: corta também vão entre palavras maior que isto, mesmo com som (jogo, música)
    "gaguejos": True,        # "pro- problema", "ca casa"
    "repeticoes": True,      # "eu acho eu acho que": fica a última vez
    "hesitacoes": True,      # "hã", "hum", "ééé"
    "minimo": 0.2,           # pedaço que sobra entre dois cortes menor que isto vai junto (vira um piscar)
}
HESITACOES = frozenset("ã hã ah ahn an hum hm hmm hmmm uhm uh eh ehh éé ééé aham".split())
# Curtas demais para dizer que são começo de outra palavra ("a aula", "de depois", "pra prato").
_NAO_GAGUEJO = frozenset("se me te eu ele ela mas mais já só não tá é há lá cá vai vou".split())


def cortes_padrao(params=None):
    p = dict(CORTES_PADRAO)
    for k, v in (params or {}).items():
        if k in CORTES_PADRAO:
            p[k] = bool(v) if isinstance(CORTES_PADRAO[k], bool) else float(v)
    return p


def silencios(envelope, passo, limiar_db, minimo):
    """Envelope (dBFS por janela de `passo` s) → [(início, fim)] dos trechos abaixo do limiar por ≥ `minimo`."""
    out, ini = [], None
    for i, db in enumerate(list(envelope) + [0.0]):
        if db < limiar_db:
            if ini is None:
                ini = i
        elif ini is not None:
            if (i - ini) * passo >= minimo:
                out.append((round(ini * passo, 3), round(min(i, len(envelope)) * passo, 3)))
            ini = None
    return out


def _subtrai(intervalos, buracos):
    """Cada (a, b) de `intervalos` sem as partes que caem em `buracos` (ordenados ou não)."""
    buracos = sorted(buracos)
    out = []
    for a, b in intervalos:
        cur = a
        for x, y in buracos:
            if y <= cur or x >= b:
                continue
            if x > cur:
                out.append((cur, x))
            cur = max(cur, y)
        if cur < b:
            out.append((cur, b))
    return out


def disfluencias(words, gaguejos=True, repeticoes=True, hesitacoes=True, vao=1.0):
    """Gaguejo, repetição e hesitação nas palavras (já sem as `duvidosa`). Cada uma vira um corte do começo
    da palavra a tirar até o começo da que fica, para a emenda cair na fala e não num silêncio novo."""
    out = []
    n = [_norm(w["texto"]) for w in words]

    def ate(k):   # fim do corte que tira a palavra k: o começo da próxima, se ela vem logo
        if k + 1 < len(words) and words[k + 1]["t"] - words[k]["fim"] <= vao:
            return words[k + 1]["t"]
        return words[k]["fim"]

    i = 0
    while i < len(words):
        w = words[i]
        if hesitacoes and n[i] and (n[i] in HESITACOES or re.fullmatch(r"(\w)\1{2,}", n[i])
                                    or (n[i] in ("é", "e", "ah") and w["fim"] - w["t"] > 0.7)):
            out.append({"inicio": w["t"], "fim": ate(i), "tipo": "hesitação", "texto": w["texto"]})
            i += 1
            continue
        if repeticoes:
            achou = False
            for k in (4, 3, 2, 1):
                if i + 2 * k > len(words) or not all(n[i:i + 2 * k]):
                    continue
                if n[i:i + k] == n[i + k:i + 2 * k] and words[i + k]["t"] - words[i + k - 1]["fim"] <= vao:
                    out.append({"inicio": w["t"], "fim": words[i + k]["t"], "tipo": "repetição",
                                "texto": " ".join(x["texto"] for x in words[i:i + k])})
                    i += k
                    achou = True
                    break
            if achou:
                continue
        if gaguejos and i + 1 < len(words) and n[i] and n[i + 1] != n[i]:
            prox = words[i + 1]
            fragmento = w["texto"].rstrip().endswith("-")
            prefixo = (len(n[i]) <= 4 and n[i] not in _NAO_GAGUEJO and n[i] not in LIGACOES and len(n[i]) < len(n[i + 1])
                       and n[i + 1].startswith(n[i]) and w["fim"] - w["t"] < 0.6)
            if (fragmento or prefixo) and prox["t"] - w["fim"] <= 0.6:
                out.append({"inicio": w["t"], "fim": prox["t"], "tipo": "gaguejo", "texto": w["texto"]})
        i += 1
    return out


def _fala_com_som(fala, envelope, passo, piso_db, respiro):
    """O que cada palavra protege do corte de silêncio: só a parte do tempo dela que tem som (acima de
    `piso_db`, 10 dB abaixo do limiar — fala baixinha ainda conta). O whisper estica a palavra para dentro
    da pausa ("Hoje" começando 1,6 s antes da voz voltar); protegê-la inteira seguraria o silêncio todo."""
    out = []
    for w in fala:
        i0, i1 = max(0, int(w["t"] / passo)), min(len(envelope), int(math.ceil(w["fim"] / passo)))
        ini = None
        for i in range(i0, i1 + 1):
            alto = i < i1 and envelope[i] >= piso_db
            if alto and ini is None:
                ini = i
            elif not alto and ini is not None:
                out.append((max(0.0, ini * passo - respiro), i * passo + respiro))
                ini = None
    return out


def cortes_inteligentes(duracao, envelope, passo, palavras=None, segmentos=None, params=None, ignorar=None):
    """Propostas de corte para os trechos da timeline (ou o vídeo inteiro): silêncio pela onda, pausa entre
    palavras, gaguejo, repetição e hesitação. Devolve as remoções (para desenhar e deixar a pessoa ignorar
    uma a uma), os trechos que sobram (cada um com `origem` = índice do trecho de onde veio, para herdar o
    enquadramento) e a duração antes e depois. Tudo em tempo do bruto."""
    p = cortes_padrao(params)
    resp = max(0.0, p["respiro"])
    fala = sorted((w for w in palavras or [] if not w.get("duvidosa") and str(w.get("texto") or "").strip()),
                  key=lambda w: w["t"])
    base = [(float(s["inicio"]), float(s["fim"])) for s in segmentos or [] if float(s["fim"]) > float(s["inicio"])]
    base = sorted(base) or [(0.0, float(duracao))]

    remocoes = []
    protegido = _fala_com_som(fala, envelope, passo, p["silencio_db"] - 10, resp)
    for a, b in silencios(envelope, passo, p["silencio_db"], p["silencio_min"]):
        a2 = a if a <= 0 else a + resp
        b2 = b if b >= duracao - passo else b - resp
        for x, y in _subtrai([(a2, b2)], protegido):
            if y - x >= 0.15:
                remocoes.append({"inicio": x, "fim": y, "tipo": "silêncio", "texto": ""})
    if p["pausa_fala"] > 0:
        for a, b in zip(fala, fala[1:]):
            if b["t"] - a["fim"] > p["pausa_fala"]:
                x, y = a["fim"] + resp, b["t"] - resp
                if y - x >= 0.15:
                    remocoes.append({"inicio": x, "fim": y, "tipo": "pausa", "texto": ""})
    remocoes += disfluencias(fala, p["gaguejos"], p["repeticoes"], p["hesitacoes"])

    # Só o que cai nos trechos da timeline, recortado neles; silêncio dentro de uma remoção de fala some.
    ign = [(float(x["inicio"]), float(x["fim"])) for x in ignorar or []]
    finais = []
    for r in sorted(remocoes, key=lambda r: (r["inicio"], r["tipo"] == "silêncio")):
        for a, b in base:
            x, y = max(a, r["inicio"]), min(b, r["fim"])
            # o estúdio manda de volta o pedaço como desenhou: já recortado no trecho
            if y - x < 0.05 or any(abs(round(x, 3) - i) < 0.02 and abs(round(y, 3) - f) < 0.02 for i, f in ign):
                continue
            if finais and x < finais[-1]["fim"]:
                if y <= finais[-1]["fim"]:
                    continue
                x = finais[-1]["fim"]
            finais.append({**r, "inicio": round(x, 3), "fim": round(y, 3)})
    trechos = []
    for i, (a, b) in enumerate(base):
        for x, y in _subtrai([(a, b)], [(r["inicio"], r["fim"]) for r in finais]):
            if y - x >= p["minimo"]:
                trechos.append({"inicio": round(x, 3), "fim": round(y, 3), "origem": i})
    antes = sum(b - a for a, b in base)
    depois = sum(t["fim"] - t["inicio"] for t in trechos)
    contagem = {}
    for r in finais:
        contagem[r["tipo"]] = contagem.get(r["tipo"], 0) + 1
    return {"removidos": finais, "segmentos": trechos, "antes": round(antes, 3), "depois": round(depois, 3),
            "contagem": contagem, "params": p}


# ------------------------------------------------------------------ legendas
# Palavras que não ficam penduradas no fim de uma tela: a preposição/artigo vai com o que vem depois
# ("de | São Paulo" vira "| de São Paulo"). Espelho em LIGACOES do index.html.
LIGACOES = frozenset("a o as os um uma uns umas de da do das dos em no na nos nas e ou que pra pro pras pros "
                     "para por pelo pela pelos pelas com sem ao à aos às num numa seu sua meu minha teu tua "
                     "nosso nossa the of".split())
# Depois de um número, ficam com ele ("10 mil", "50 reais", "3 horas"). Espelho em UNIDADES do index.html.
UNIDADES = frozenset("mil milhão milhões bilhão bilhões reais real dólares dólar por cento % anos ano dias dia "
                     "horas hora minutos minuto segundos segundo vezes pessoas quilos km metros mês meses "
                     "semanas semana k".split())
_ELO_NOME = frozenset("de da do das dos e".split())   # "Rio de Janeiro", "Maria e José"


def _limpa(t):
    return re.sub(r"^[^\w$%]+|[^\w$%]+$", "", t or "")


def _maiuscula(t):
    t = _limpa(t)
    return bool(t) and t[0].isupper()


def _numero(t):
    t = _limpa(t).lower()
    return bool(re.fullmatch(r"(r\$|us\$)?\d[\d.,]*%?", t))


def termos(words, max_termo=4, vao=0.5):
    """Grupo de cada palavra: nomes compostos ("São Paulo", "Rio de Janeiro") e número + unidade ("10 mil",
    "R$ 50", "50 por cento") são um termo só — não quebram de tela e acendem juntos na legenda. Devolve
    uma lista paralela a `words`: o índice da primeira palavra do termo, ou None para palavra solta."""
    out = [None] * len(words)
    i = 0
    while i < len(words):
        j = i
        while j + 1 < len(words) and j + 1 - i < max_termo:
            a, b = words[j], words[j + 1]
            if re.search(r"[.?!…,;:]$", a["texto"]) or b["t"] - a["fim"] > vao or a.get("locutor") != b.get("locutor"):
                break
            na, nb = _limpa(a["texto"]).lower(), _limpa(b["texto"]).lower()
            if _maiuscula(a["texto"]) and _maiuscula(b["texto"]) and na not in LIGACOES:
                j += 1
            elif (_maiuscula(a["texto"]) and na not in LIGACOES and nb in _ELO_NOME and j + 2 < len(words)
                  and j + 2 - i < max_termo and _maiuscula(words[j + 2]["texto"])
                  and not re.search(r"[.?!…,;:]$", b["texto"]) and words[j + 2]["t"] - b["fim"] <= vao):
                j += 2
            elif (_numero(a["texto"]) or na in ("r$", "us$")) and (nb in UNIDADES or _numero(b["texto"])):
                j += 1
            elif nb == "cento" and na == "por" and j > i:
                j += 1
            else:
                break
        if j > i:
            for k in range(i, j + 1):
                out[k] = i
        i = j + 1
    return out


def blocos(words, max_palavras=4, max_chars=22, max_dur=2.2, pausa=0.45):
    """Palavras → telas de legenda. Quebra por locutor, pausa, pontuação, número de palavras, caracteres e
    duração; mas um termo (ver `termos`) nunca se divide — passa um pouco do limite ou desce inteiro para a
    próxima tela — e uma preposição/artigo não fica sozinha no fim. Cada palavra sai com `grupo`."""
    out, cur = [], []
    grupos = termos(words)
    for w, g in zip(words, grupos):
        w["grupo"] = g

    def fecha():
        if cur:
            out.append({"t": cur[0]["t"], "fim": cur[-1]["fim"], "locutor": cur[0].get("locutor"), "palavras": list(cur)})
            cur.clear()

    def junto(a, b):
        # `junto` = marca da passada semântica (a palavra fica com a próxima), além dos termos detectados
        return bool(a.get("junto")) or (a.get("grupo") is not None and a.get("grupo") == b.get("grupo"))

    for w in words:
        if cur:
            chars = len(" ".join(x["texto"] for x in cur + [w]))
            if (w.get("locutor") != cur[-1].get("locutor") or w["t"] - cur[-1]["fim"] > pausa
                    or (w.get("quebra") and not junto(cur[-1], w))):   # `quebra` = começa tela nova aqui
                fecha()
            elif len(cur) >= max_palavras or chars > max_chars or w["fim"] - cur[0]["t"] > max_dur:
                if not (junto(cur[-1], w) and len(cur) < max_palavras + 3 and w["fim"] - cur[0]["t"] <= max_dur + 1.2):
                    leva = []
                    while len(cur) > 1 and (junto(cur[-1], (leva or [w])[0])
                                            or _limpa(cur[-1]["texto"]).lower() in LIGACOES):
                        leva.insert(0, cur.pop())
                    fecha()
                    cur.extend(leva)
        cur.append(w)
        if re.search(r"[.?!…,;:]$", w["texto"]):
            fecha()
    fecha()
    # Sem piscar: o bloco fica até o próximo entrar se o vão for curto (até +0,3 s, ou até 0,5 s na tela).
    for i, b in enumerate(out):
        prox = out[i + 1]["t"] if i + 1 < len(out) else b["fim"] + 0.3
        b["fim"] = round(max(b["fim"], min(prox, max(b["fim"] + 0.3, b["t"] + 0.5))), 3)
    return out


def _tc_ass(s):
    cs = int(round(max(0.0, s) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _tc_srt(s):
    ms = int(round(max(0.0, s) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _cor_ass(hexcor, alpha=0):
    h = hexcor.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _esc_ass(t):
    return t.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def _txt(w, maiusculas):
    return w["texto"].upper() if maiusculas else w["texto"]


GANCHO_DURACAO = 3.0
LEGENDA_MARGEM = 560   # MarginV da legenda no 1920 de altura (de baixo para cima)


def gerar_ass(cues, nomes, cfg, gancho="", largura=LARGURA, altura=ALTURA):
    """Karaokê: a palavra acende na cor do locutor quando é falada (\\k), o bloco entra com um pop curto.
    `gancho`: o texto do LLM para os 3 primeiros segundos, num estilo próprio no alto da tela."""
    fonte = cfg.get("legenda_fonte") or DEFAULTS["legenda_fonte"]
    tam = int(cfg.get("legenda_tamanho") or DEFAULTS["legenda_tamanho"])
    mai = cfg.get("legenda_maiusculas", True)
    # Ajustes do editor (fora dos DEFAULTS: valem só para a edição que os pediu).
    margem = int(cfg.get("legenda_margem") if cfg.get("legenda_margem") is not None else LEGENDA_MARGEM * altura / ALTURA)
    estatica = bool(cfg.get("legenda_estatica"))   # legenda fixa: o bloco inteiro de uma vez, sem karaokê
    cores = [c for c in cfg.get("legenda_cores") or [] if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(c))] or CORES_LOCUTOR
    estilos = []
    for i, nome in enumerate(nomes):
        cor = cores[i % len(cores)]
        estilos.append(
            f"Style: {_estilo(nome)},{fonte},{tam},{_cor_ass(cor)},{_cor_ass('#FFFFFF')},{_cor_ass('#000000')},"
            f"{_cor_ass('#000000', 0x80)},-1,0,0,0,100,100,0,0,1,7,3,2,80,80,{margem},1")
    # Emoji some na fonte da legenda (Arial Black): vira um quadrado ou nada.
    gancho = re.sub(r"\s+", " ", re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F]", "", gancho or "")).strip()
    if gancho:
        estilos.append(
            f"Style: Gancho,{fonte},{int(tam * 1.1)},{_cor_ass('#000000')},{_cor_ass('#000000')},{_cor_ass('#FFFFFF')},"
            f"{_cor_ass('#FFFFFF')},-1,0,0,0,100,100,0,0,3,18,0,8,90,90,300,1")
    linhas = [
        "[Script Info]", "; Clientela", "ScriptType: v4.00+", f"PlayResX: {largura}",
        f"PlayResY: {altura}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        *estilos, "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if gancho:
        anim = "{\\fad(80,160)\\fscx70\\fscy70\\t(0,180,\\fscx104\\fscy104)\\t(180,280,\\fscx100\\fscy100)}"
        dur = float(cfg.get("gancho_duracao") or GANCHO_DURACAO)
        linhas.append(f"Dialogue: 1,{_tc_ass(0)},{_tc_ass(dur)},Gancho,Gancho,0,0,0,,{anim}"
                      f"{_esc_ass(gancho.upper() if mai else gancho)}")
    for c in cues:
        # Um termo ("São Paulo", "10 mil") acende inteiro e não quebra de linha (\h = espaço que não quebra).
        partes, ps = [], c["palavras"]
        j = 0
        while j < len(ps):
            k = j
            while k + 1 < len(ps) and ps[j].get("grupo") is not None and ps[k + 1].get("grupo") == ps[j]["grupo"]:
                k += 1
            ate = ps[k + 1]["t"] if k + 1 < len(ps) else ps[k]["fim"]
            cs = max(1, int(round((ate - ps[j]["t"]) * 100)))
            texto = "\\h".join(_esc_ass(_txt(w, mai)) for w in ps[j:k + 1])
            partes.append(texto if estatica else f"{{\\k{cs}}}" + texto)
            j = k + 1
        anim = "{\\fad(60,40)\\fscx88\\fscy88\\t(0,140,\\fscx100\\fscy100)}"
        linhas.append(f"Dialogue: 0,{_tc_ass(c['t'])},{_tc_ass(c['fim'])},{_estilo(c['locutor'])},{c['locutor']},"
                      f"0,0,0,,{anim}{' '.join(partes)}")
    return "\n".join(linhas) + "\n"


def _estilo(nome):
    return re.sub(r"[^\w]+", "_", str(nome or "Locutor")).strip("_") or "Locutor"


def gerar_srt(cues, com_locutor=False, maiusculas=False):
    out = []
    for i, c in enumerate(cues, 1):
        frase = " ".join(_txt(w, maiusculas) for w in c["palavras"])
        if com_locutor:
            frase = f"[{c['locutor']}] {frase}"
        out.append(f"{i}\n{_tc_srt(c['t'])} --> {_tc_srt(c['fim'])}\n{frase}\n")
    return "\n".join(out)


def transcricao_corte(words):
    return "\n".join(f"[{f['t']:06.2f}] {f['locutor']}: {f['texto']}" for f in frases(words)) + "\n"


# ------------------------------------------------------------------ ffmpeg
def _par(n):
    return max(2, int(n) // 2 * 2)


LAYOUT_MODOS = ("inteiro", "dividido", "desfocado")
DIVISAO_MIN, DIVISAO_MAX = 0.2, 0.8


def _caixa(box, largura, altura, aspecto):
    """Caixa normalizada {x, y, w, h} (0–1 do quadro) → recorte em pixels com a proporção do espaço
    que ela ocupa no 9:16 (`aspecto` = largura/altura). Mantém o centro e a largura pedidos;
    o que não couber no quadro encolhe, e a caixa é empurrada para dentro."""
    box = box or {}
    w = float(box.get("w") or 1.0) * largura
    cx = (float(box.get("x") or 0.0) + float(box.get("w") or 1.0) / 2) * largura
    cy = (float(box.get("y") or 0.0) + float(box.get("h") or 1.0) / 2) * altura
    h = w / aspecto
    if h > altura:
        h, w = altura, altura * aspecto
    if w > largura:
        w, h = largura, largura / aspecto
    cw, ch = _par(w), _par(h)
    x = int(min(max(0, cx - cw / 2), largura - cw))
    y = int(min(max(0, cy - ch / 2), altura - ch))
    return {"w": cw, "h": ch, "x": x, "y": y}


def normalizar_layout(layout, largura, altura):
    """O layout do editor visual → recortes em pixels deste vídeo.

    - ``inteiro``: uma caixa (``principal``) 9:16 vira a tela toda;
    - ``dividido``: ``camera`` em cima (``divisao`` da altura, 20–80 %) e ``principal`` embaixo,
      cada uma com a proporção do seu espaço — a facecam e o jogo, como na Twitch;
    - ``desfocado``: o vídeo sobre ele mesmo desfocado. ``escala`` 1 = cabe na largura; até
      ``escala_maxima`` = preenche a altura (o que passa da largura é cortado, na ``posicao`` 0–1).
    Sem caixa, a principal fica centralizada e a câmera no canto de cima à direita."""
    layout = layout or {}
    modo = layout.get("modo") if layout.get("modo") in LAYOUT_MODOS else "inteiro"
    out = {"modo": modo}
    if modo == "inteiro":
        out["principal"] = _caixa(layout.get("principal"), largura, altura, LARGURA / ALTURA)
    elif modo == "dividido":
        d = min(DIVISAO_MAX, max(DIVISAO_MIN, float(layout.get("divisao") or 0.35)))
        topo = _par(ALTURA * d)
        out["divisao"] = round(d, 4)
        out["altura_camera"] = topo
        camera = layout.get("camera") or {"x": 0.72, "y": 0.05, "w": 0.26, "h": 0.3}
        out["camera"] = _caixa(camera, largura, altura, LARGURA / topo)
        out["principal"] = _caixa(layout.get("principal"), largura, altura, LARGURA / (ALTURA - topo))
    else:
        out["escala"] = round(min(escala_maxima(largura, altura), max(1.0, float(layout.get("escala") or 1.0))), 4)
        out["posicao"] = round(min(1.0, max(0.0, float(layout.get("posicao", 0.5)))), 4)
    return out


def escala_maxima(largura, altura):
    """Escala do vídeo no desfocado em que ele preenche a altura do 9:16 (1 = cabe na largura)."""
    return max(1.0, ALTURA * largura / (LARGURA * altura))


def _crop(c):
    return f"crop={c['w']}:{c['h']}:{c['x']}:{c['y']}"


def cadeia_video(largura, altura, formato="corte", enquadramento=0.5, layout=None):
    """Filtros do quadro emendado → 1080x1920 a 30 fps. Com `layout` (o editor visual), ele manda;
    sem, vale o `formato`/`enquadramento` da configuração."""
    final = f"setsar=1,fps={FPS},format=yuv420p"
    if layout:
        px = normalizar_layout(layout, largura, altura)
        if px["modo"] == "inteiro":
            return f"{_crop(px['principal'])},scale={LARGURA}:{ALTURA}:flags=lanczos,{final}"
        if px["modo"] == "dividido":
            topo = px["altura_camera"]
            return (f"split[cam][jogo];[cam]{_crop(px['camera'])},scale={LARGURA}:{topo}:flags=lanczos,setsar=1[camv];"
                    f"[jogo]{_crop(px['principal'])},scale={LARGURA}:{ALTURA - topo}:flags=lanczos,setsar=1[jogov];"
                    f"[camv][jogov]vstack=inputs=2,{final}")
        return _cadeia_desfocada(largura, altura, px["escala"], px["posicao"], final)
    if formato == "desfocado":
        return _cadeia_desfocada(largura, altura, 1.0, 0.5, final)
    enq = min(1.0, max(0.0, float(enquadramento)))
    if largura / altura > 9 / 16:
        cw, ch = _par(altura * 9 / 16), _par(altura)
        x, y = int((largura - cw) * enq), 0
    else:
        cw, ch = _par(largura), _par(largura * 16 / 9)
        x, y = 0, int((altura - ch) / 2)
    return f"crop={cw}:{ch}:{x}:{y},scale={LARGURA}:{ALTURA}:flags=lanczos,{final}"


def _cadeia_desfocada(largura, altura, escala, posicao, final):
    fw = _par(LARGURA * escala)
    # altura arredondada para cima: na escala máxima, para baixo deixaria 1 px de fundo em cima e embaixo
    fh = _par(math.ceil(fw * altura / largura / 2) * 2)
    cw, ch = min(fw, LARGURA), min(fh, ALTURA)
    x, y = int((fw - cw) * posicao), (fh - ch) // 2
    return (f"split[fundo][frente];[fundo]scale={LARGURA}:{ALTURA}:force_original_aspect_ratio=increase,"
            f"crop={LARGURA}:{ALTURA},boxblur=40:2,eq=brightness=-0.08[fundob];"
            f"[frente]scale={fw}:{fh}:flags=lanczos,crop={cw}:{ch}:{x}:{y}[frentes];"
            f"[fundob][frentes]overlay=(W-w)/2:(H-h)/2,{final}")


# ------------------------------------------------------------------ capa
CAPA_TIPOS = ("nenhuma", "quadro", "imagem")


def comando_capa_quadro(video, t, cadeia, saida):
    """Um quadro do bruto com o mesmo enquadramento do corte → a capa."""
    return ["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", video,
            "-filter_complex", f"[0:v]{cadeia}[o]", "-map", "[o]", "-frames:v", "1", "-q:v", "2", saida]


def comando_capa_imagem(imagem, saida):
    """Imagem do editor → 1080x1920 preenchendo o quadro (o que sobra é cortado, centralizado)."""
    return ["ffmpeg", "-v", "error", "-y", "-i", imagem, "-vf",
            f"scale={LARGURA}:{ALTURA}:force_original_aspect_ratio=increase,crop={LARGURA}:{ALTURA},setsar=1",
            "-frames:v", "1", "-q:v", "2", saida]


def _trecho_audio(entrada, i, s, rotulo):
    d = s["fim"] - s["inicio"]
    fade = min(0.012, d / 4)
    return (f"[{entrada}]atrim=start={s['inicio']}:end={s['fim']},asetpts=PTS-STARTPTS,"
            f"afade=t=in:d={fade},afade=t=out:st={d - fade:.3f}:d={fade}[{rotulo}{i}]")


TRANSICOES = ("corte", "fade", "flash")
FADE_S, FLASH_S = 0.15, 0.2


def _rotular(cadeia, i):
    """Rótulos internos de uma cadeia (``[cam]``, ``[fundo]``…) ganham o número do trecho: com um
    enquadramento por trecho, a mesma cadeia aparece várias vezes no grafo e os rótulos colidiriam."""
    return re.sub(r"\[([A-Za-z_]+)\]", lambda m: f"[{m.group(1)}_t{i}]", cadeia)


def _fades(segs, i):
    """Transição na entrada do trecho ``i`` (do editor): ``fade`` = mergulho no preto (o anterior sai e
    este entra), ``flash`` = este entra a partir do branco. Dentro do trecho, sem mudar a duração —
    legendas e áudios soltos continuam batendo."""
    s = segs[i]
    d = s["fim"] - s["inicio"]
    out = []
    if s.get("transicao") == "fade":
        out.append(f"fade=t=in:st=0:d={min(FADE_S, d / 3):.3f}")
    elif s.get("transicao") == "flash":
        out.append(f"fade=t=in:st=0:d={min(FLASH_S, d / 3):.3f}:color=white")
    if i + 1 < len(segs) and segs[i + 1].get("transicao") == "fade":
        f = min(FADE_S, d / 3)
        out.append(f"fade=t=out:st={d - f:.3f}:d={f:.3f}")
    return ",".join(out)


def cadeia_cor(cor):
    """Correção de cor do editor: ``brilho`` multiplica o RGB (igual ao brightness() do CSS da prévia),
    ``contraste`` e ``saturacao`` vão pelo ``eq``. Neutro (tudo 1) = nenhum filtro."""
    cor = cor or {}
    b, c, s = (1.0 if cor.get(k) is None else float(cor[k]) for k in ("brilho", "contraste", "saturacao"))
    partes = []
    if abs(b - 1) > 1e-3:
        partes.append(f"colorchannelmixer=rr={b:.3f}:gg={b:.3f}:bb={b:.3f}")
    if abs(c - 1) > 1e-3 or abs(s - 1) > 1e-3:
        partes.append(f"eq=contrast={c:.3f}:saturation={s:.3f}")
    return ",".join(partes + ["format=yuv420p"]) if partes else ""


def grafo_corte(segs, cadeia, tem_audio=True, capa=False, cadeias=None, cor=""):
    """`capa`: a entrada 1 (a imagem da capa) substitui só o primeiro quadro — no lugar de um quadro a
    mais no começo, que adiantaria o vídeo em relação às legendas e aos áudios soltos.
    `cadeias`: uma por trecho (o editor deixa cada trecho ter o próprio enquadramento, um zoom por
    exemplo); cada uma vira 1080x1920 antes da emenda. Sem ela, a `cadeia` vale para o corte todo."""
    partes = []
    for i, s in enumerate(segs):
        filtros = f"[0:v:0]trim=start={s['inicio']}:end={s['fim']},setpts=PTS-STARTPTS"
        if cadeias is not None:
            filtros += "," + _rotular(cadeias[i], i)
        fx = _fades(segs, i)
        if fx:
            filtros += "," + fx
        partes.append(f"{filtros}[v{i}]")
        if tem_audio:
            partes.append(_trecho_audio("0:a:0", i, s, "a"))
    n = len(segs)
    if tem_audio:
        partes.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][aout]")
    else:
        partes.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
    depois = ",".join(x for x in ("" if cadeias is not None else cadeia, cor) if x) or "null"
    if capa:
        partes.append(f"[vc]{depois}[vbase]")
        partes.append(f"[1:v]scale={LARGURA}:{ALTURA},setsar=1,format=yuv420p[capa]")
        partes.append("[vbase][capa]overlay=0:0:enable='lt(t,0.02)',format=yuv420p[vout]")
    else:
        partes.append(f"[vc]{depois}[vout]")
    return ";".join(partes)


def grafo_mix(segs, faixas, fade_entrada=0.0, fade_saida=0.0, normalizar=False):
    """O áudio do MP4 quando o editor mexeu nele: as `faixas` [(faixa, ganho_db)] cortadas como o vídeo,
    somadas (``amix`` sem normalizar, para o ganho ser o que se pediu), com fade no começo/fim e,
    opcional, loudness de rede social (-14 LUFS). Nenhuma faixa = silêncio do tamanho do corte."""
    total = duracao_total(segs)
    if not faixas:
        return f"anullsrc=r=48000:cl=stereo,atrim=0:{total:.3f}[aout]"
    partes, saidas, n = [], [], len(segs)
    for f, ganho in faixas:
        partes += [_trecho_audio(f"0:a:{f - 1}", i, s, f"m{f}_") for i, s in enumerate(segs)]
        partes.append("".join(f"[m{f}_{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1,"
                      f"aformat=channel_layouts=stereo,volume={float(ganho):.2f}dB[mv{f}]")
        saidas.append(f"mv{f}")
    atual = saidas[0]
    if len(saidas) > 1:
        partes.append("".join(f"[{x}]" for x in saidas) + f"amix=inputs={len(saidas)}:normalize=0:duration=longest[mx]")
        atual = "mx"
    master = []
    if fade_entrada > 0:
        master.append(f"afade=t=in:st=0:d={min(fade_entrada, total / 2):.3f}")
    if fade_saida > 0:
        d = min(fade_saida, total / 2)
        master.append(f"afade=t=out:st={max(0.0, total - d):.3f}:d={d:.3f}")
    if normalizar:
        master.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    partes.append(f"[{atual}]{','.join(master) or 'anull'}[aout]")
    return ";".join(partes)


def grafo_audio(segs, faixa, regioes=None):
    """Uma faixa do arquivo cortada igual ao vídeo; com `regioes` (tempo do corte), só elas soam."""
    partes = [_trecho_audio(f"0:a:{faixa - 1}", i, s, "a") for i, s in enumerate(segs)]
    n = len(segs)
    final = "aout"
    partes.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[{'ac' if regioes is not None else final}]")
    if regioes is not None:
        expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in regioes) or "0"
        partes.append(f"[ac]volume=volume='if({expr},1,0)':eval=frame[{final}]")
    return ";".join(partes)


def regioes_locutor(cues, nome, folga=0.12):
    out = []
    for c in cues:
        if c["locutor"] != nome:
            continue
        a, b = max(0.0, c["t"] - folga), c["fim"] + folga
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _num(x):
    try:
        return f"{float(x):.1f}"
    except (TypeError, ValueError):
        return "?"


def _nome_arquivo(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(s)).strip().rstrip(". ")
    return s[:80] or "faixa"


# ------------------------------------------------------------------ pipeline
def _montar_capa(layout, video, cadeia, info, entrega):
    """Capa.jpg na entrega (também solta, para subir como miniatura). None = sem capa."""
    capa = (layout or {}).get("capa") or {}
    saida = entrega / "Capa.jpg"
    if capa.get("tipo") == "quadro":
        t = min(max(0.0, float(capa.get("t") or 0)), max(0.0, info["duracao"] - 0.1))
        passo(f"Montando a capa: quadro em {t:.1f} s com o enquadramento do corte")
        rodar(comando_capa_quadro(video, t, cadeia, saida))
        return saida
    if capa.get("tipo") == "imagem":
        imagem = Path(str(capa.get("imagem") or ""))
        if not imagem.is_file():
            texto(f"aviso: a imagem da capa não existe neste computador ({imagem}); o corte sai sem capa.")
            return None
        passo("Montando a capa: imagem escolhida em 1080x1920")
        rodar(comando_capa_imagem(imagem, saida))
        return saida
    return None


def _transcrever_e_separar(a, cfg, video, info, trab):
    audio = trab / "audio.wav"
    passo("Extraindo o áudio para a transcrição")
    extrair_audio(video, 1, str(audio))
    if a.get("transcricao") and Path(a["transcricao"]).suffix.lower() == ".json":
        passo("Lendo a transcrição escolhida")
        words = palavras_de_json(json.loads(Path(a["transcricao"]).read_text(encoding="utf-8")))
    else:
        passo(f"Transcrevendo com o whisper ({cfg['asr_modelo']})")
        words = transcrever(audio, trab, cfg["asr_modelo"], cfg.get("idioma"), info["duracao"])
    # Sem fala (gameplay, música) o corte sai pela imagem e pelo som; uma transcrição vazia não para o job.
    marcar_duvidosas(words)
    duvidosas = sum(1 for w in words if w["duvidosa"])
    texto(f"Transcrição: {len(words)} palavras" + (f", {duvidosas} com cara de música ou alucinação do whisper"
                                                   if duvidosas else "") + "." if words else "Transcrição: sem fala.")
    passo("Separando os locutores")
    return locutores(words, video, audio, info, cfg)


def _escolher_trechos(a, cfg, video, info, trab, words):
    """Imagem, som e transcrição → o LLM → trechos encaixados nas palavras. Devolve (trechos, resposta, custo, provedor)."""
    passo("Olhando o vídeo: quadros, movimento, trocas de cena e volume")
    midia = analisar_midia(video, info, trab)
    (trab / "linha-do-tempo.json").write_text(json.dumps(midia, ensure_ascii=False, indent=1), encoding="utf-8")
    texto(f"{len(midia['quadros'])} quadros (um a cada {midia['passo']} s) em {len(midia['folhas'])} folha(s) de contato.")

    provedor = cfg.get("provedor_llm") or "claude"
    prompt = montar_prompt(words, info["duracao"], cfg, a.get("observacoes"), midia, info)
    (trab / "prompt-corte.txt").write_text(prompt, encoding="utf-8")
    passo(f"O LLM assiste ao clipe e escolhe os trechos ({'OpenRouter · ' + cfg['openrouter_modelo'] if provedor == 'openrouter' else 'Claude Code'})")
    folhas = [trab / f["arquivo"] for f in midia["folhas"]]
    if provedor == "openrouter":
        resposta, custo = llm_openrouter(prompt, cfg, imagens=folhas)
    else:
        resposta, custo = llm_claude(prompt, cfg, a.get("claude"), imagens=folhas, cwd=trab)
    (trab / "llm-resposta.json").write_text(json.dumps(resposta, ensure_ascii=False, indent=2), encoding="utf-8")
    segs = normalizar_segmentos(resposta, words, info["duracao"])
    if not segs:
        raise Falha("o LLM não devolveu nenhum trecho aproveitável (veja llm-resposta.json na bancada)")
    return segs, resposta, custo, provedor


# ------------------------------------------------------------------ edição da timeline
def segmentos_da_edicao(ed, duracao, minimo=0.1):
    """Trechos que o editor do app mandou, como estão (quem encaixou nas palavras foi a pessoa): dentro
    do vídeo, em ordem, sem sobreposição. Cada um pode trazer `layout` (enquadramento próprio) e
    `transicao` de entrada."""
    segs = []
    for item in ed.get("segmentos") or []:
        try:
            a0, b0 = max(0.0, float(item["inicio"])), min(float(duracao), float(item["fim"]))
        except (KeyError, TypeError, ValueError):
            continue
        s = {"inicio": round(a0, 3), "fim": round(b0, 3), "motivo": str(item.get("motivo") or "").strip()}
        if item.get("layout"):
            s["layout"] = item["layout"]
        if item.get("transicao") in TRANSICOES[1:]:
            s["transicao"] = item["transicao"]
        segs.append(s)
    segs.sort(key=lambda s: s["inicio"])
    out = []
    for s in segs:
        if out and s["inicio"] < out[-1]["fim"]:
            s["inicio"] = out[-1]["fim"]
        if s["fim"] - s["inicio"] >= minimo:
            out.append(s)
    return out


def palavras_da_edicao(ed, cfg, info):
    """Palavras revisadas no editor (texto corrigido, palavra apagada, locutor trocado), no tempo do
    bruto. Devolve como `locutores()`: (palavras, origem, faixas isoladas)."""
    words = []
    for w in ed.get("palavras") or []:
        try:
            t, fim = float(w["t"]), float(w["fim"])
        except (KeyError, TypeError, ValueError):
            continue
        txt = str(w.get("texto") or "").strip()
        if txt:
            words.append({"texto": txt, "t": round(t, 3), "fim": round(max(t, fim), 3),
                          "locutor": str(w.get("locutor") or "Locutor 1"), "duvidosa": bool(w.get("duvidosa"))})
    words.sort(key=lambda w: w["t"])
    origem = ed.get("origem_locutores") or "edição"
    isoladas = {}
    if origem == "faixas de áudio":
        nomes = {w["locutor"] for w in words}
        for f in cfg.get("faixas_locutores") or []:
            nome = (f.get("nome") or f"Faixa {f.get('faixa')}").strip()
            if nome in nomes and 1 <= int(f.get("faixa") or 0) <= len(info["audios"]):
                isoladas[nome] = int(f["faixa"])
    return words, origem, isoladas


def estilo_da_edicao(ed):
    """O painel Legenda do editor → as chaves da configuração que `gerar_ass`/`blocos` leem."""
    est = ed.get("estilo") or {}
    mapa = {"tamanho": "legenda_tamanho", "maiusculas": "legenda_maiusculas", "fonte": "legenda_fonte",
            "palavras": "palavras_por_bloco", "margem": "legenda_margem", "cores": "legenda_cores",
            "estatica": "legenda_estatica"}
    out = {chave: est[k] for k, chave in mapa.items() if est.get(k) is not None}
    if ed.get("gancho_duracao"):
        out["gancho_duracao"] = ed["gancho_duracao"]
    return out


def edicao_gravada(ed, segs):
    """A edição como entra no corte.json: tudo menos as palavras (essas ficam no palavras.json da bancada)."""
    return {**{k: v for k, v in ed.items() if k != "palavras"}, "segmentos": segs}


def mix_da_edicao(ed, info):
    """None = o áudio de sempre (faixa 1, sem mexer). Senão {faixas: [(faixa, ganho_db)], fades, normalizar}."""
    au = (ed or {}).get("audio") or {}
    if not au:
        return None
    cfg_faixas = au.get("faixas") or {}
    faixas = []
    for f in info["audios"]:
        c = cfg_faixas.get(str(f["faixa"])) or {}
        # sem configuração, só a faixa 1 entra, como no corte normal
        if c.get("mudo", f["faixa"] != 1):
            continue
        faixas.append((f["faixa"], float(c.get("ganho") or 0)))
    mix = {"faixas": faixas, "fade_entrada": float(au.get("fade_entrada") or 0),
           "fade_saida": float(au.get("fade_saida") or 0), "normalizar": bool(au.get("normalizar"))}
    neutro = (faixas == [(1, 0.0)] and not mix["fade_entrada"] and not mix["fade_saida"] and not mix["normalizar"])
    return None if neutro else mix


def executar(a):
    """Monta a entrega numa pasta oculta ao lado e só a renomeia para
    ``Corte 9x16 vN`` no fim: um job que falha ou é cancelado no meio do
    ffmpeg não deixa um MP4 pela metade aparecendo como entrega."""
    final = Path(a["entrega"])
    montando = final.with_name(f".{final.name}.montando")
    if final.exists():
        raise Falha(f"a pasta de entrega já existe: {final.name}")
    shutil.rmtree(montando, ignore_errors=True)
    try:
        msg, custo = _executar(a, montando, final.name)
        montando.rename(final)
    finally:
        shutil.rmtree(montando, ignore_errors=True)
    return msg, custo


def _executar(a, entrega, nome_final):
    trab = Path.cwd()
    cfg = {**DEFAULTS, **(a.get("corte") or {})}
    video = a["video"]
    custo = 0.0

    passo("Lendo o vídeo (resolução, duração e faixas de áudio)")
    info = probe(video)
    if not info["audios"]:
        raise Falha("o clipe não tem faixa de áudio")
    faixas_txt = ", ".join(f"{f['faixa']}{' (' + f['titulo'] + ')' if f['titulo'] else ''}" for f in info["audios"])
    texto(f"Vídeo {info['largura']}x{info['altura']}, {info['duracao']:.1f} s, faixas de áudio: {faixas_txt}.")

    ed = a.get("edicao")
    if ed:
        cfg.update(estilo_da_edicao(ed))
    if ed and ed.get("palavras") is not None:
        words, origem, faixas_isoladas = palavras_da_edicao(ed, cfg, info)
        texto(f"Edição da timeline: {len(ed.get('segmentos') or [])} trechos, {len(words)} palavras revisadas.")
    else:
        words, origem, faixas_isoladas = _transcrever_e_separar(a, cfg, video, info, trab)
    nomes = list(dict.fromkeys(w["locutor"] for w in words))
    if nomes:
        texto(f"Locutores ({origem}): {', '.join(nomes)}.")
    (trab / "palavras.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")

    if ed:
        provedor = "edição"
        segs = segmentos_da_edicao(ed, info["duracao"])
        if not segs:
            raise Falha("a timeline não tem nenhum trecho (de pelo menos 0,1 s) dentro do vídeo")
        resposta = {"titulo": ed.get("titulo") or "", "resumo": ed.get("resumo") or "", "gancho": ed.get("gancho") or "",
                    "legenda": ed.get("legenda", True), "descartado": ed.get("descartado") or []}
    else:
        segs, resposta, custo, provedor = _escolher_trechos(a, cfg, video, info, trab, words)
    total = duracao_total(segs)
    resposta = resposta or {}
    titulo = str(resposta.get("titulo") or "").strip()
    gancho = str(resposta.get("gancho") or "").strip()
    legendar = bool(words) and resposta.get("legenda") is not False
    if resposta.get("resumo") and not ed:
        texto(f"O que o LLM viu: {str(resposta['resumo']).strip()}")
    texto(f"Corte{' “' + titulo + '”' if titulo else ''}: {len(segs)} trechos, {total:.1f} s.\n"
          + "\n".join(f"• {s['inicio']:.1f}–{s['fim']:.1f} s: {s.get('motivo') or '—'}" for s in segs))
    fora = [d for d in resposta.get("descartado") or [] if isinstance(d, dict)]
    if fora and not ed:
        texto("Saiu do corte:\n" + "\n".join(f"• {_num(d.get('inicio'))}–{_num(d.get('fim'))} s: "
                                              f"{str(d.get('motivo') or '—').strip()}" for d in fora))
    if total > float(cfg["duracao_max"]) * 1.25 and not ed:
        texto(f"aviso: o corte passou do máximo pedido ({cfg['duracao_max']} s).")

    entrega.mkdir(parents=True, exist_ok=True)
    saida = entrega / "Corte 9x16.mp4"
    passo(f"Cortando e enquadrando em {LARGURA}x{ALTURA} a {FPS} fps")
    layout = a.get("layout") or cfg.get("layout")
    cadeia = cadeia_video(info["largura"], info["altura"], cfg.get("formato"), cfg.get("enquadramento", 0.5), layout)
    # Enquadramento próprio num trecho (zoom, outro modo): uma cadeia por trecho.
    cadeias = None
    if any(s.get("layout") for s in segs):
        cadeias = [cadeia_video(info["largura"], info["altura"], layout=s["layout"]) if s.get("layout") else cadeia
                   for s in segs]
    capa = _montar_capa(layout, video, cadeia, info, entrega)
    mix = mix_da_edicao(ed, info)
    grafo = grafo_corte(segs, cadeia, tem_audio=mix is None, capa=capa is not None, cadeias=cadeias,
                        cor=cadeia_cor((ed or {}).get("cor")))
    if mix is not None:
        grafo += ";" + grafo_mix(segs, mix["faixas"], mix["fade_entrada"], mix["fade_saida"], mix["normalizar"])
    rodar(["ffmpeg", "-v", "error", "-y", "-i", video, *(["-i", capa] if capa else []),
           "-filter_complex", grafo, "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-movflags", "+faststart", saida])

    palavras_corte = remapear(words, segs)
    cues = blocos(palavras_corte, max_palavras=int(cfg.get("palavras_por_bloco") or 4))  # também isola as vozes
    mai = bool(cfg.get("legenda_maiusculas", True))
    if legendar:
        passo("Escrevendo as legendas (.ass animada e .srt por locutor)")
        (entrega / "Legenda animada.ass").write_text(gerar_ass(cues, nomes, cfg, gancho), encoding="utf-8")
        (entrega / "Legenda.srt").write_text(gerar_srt(cues, com_locutor=len(nomes) > 1, maiusculas=mai), encoding="utf-8")
        if len(nomes) > 1:
            for nome in nomes:
                so = [c for c in cues if c["locutor"] == nome]
                if so:
                    (entrega / f"Legenda - {_nome_arquivo(nome)}.srt").write_text(gerar_srt(so, maiusculas=mai), encoding="utf-8")
    elif gancho:
        texto("Sem fala para legendar; o gancho vai sozinho em Gancho.ass.")
        (entrega / "Gancho.ass").write_text(gerar_ass([], [], cfg, gancho), encoding="utf-8")
    if palavras_corte and legendar:
        (entrega / "Transcrição.txt").write_text(transcricao_corte(palavras_corte), encoding="utf-8")

    passo("Exportando as faixas de áudio cortadas")
    arquivos_audio = []
    nome_por_faixa = {f: n for n, f in faixas_isoladas.items()}
    for f in info["audios"]:
        rotulo = nome_por_faixa.get(f["faixa"]) or f["titulo"]
        nome = f"Áudio - Faixa {f['faixa']}{' (' + _nome_arquivo(rotulo) + ')' if rotulo else ''}.wav"
        if len(info["audios"]) == 1:
            nome = "Áudio - Original.wav"
        rodar(["ffmpeg", "-v", "error", "-y", "-i", video, "-filter_complex", grafo_audio(segs, f["faixa"]),
               "-map", "[aout]", "-c:a", "pcm_s16le", "-ar", "48000", entrega / nome])
        arquivos_audio.append(nome)
    if len(nomes) > 1 and not faixas_isoladas:
        # Um microfone só: a voz de cada um é a faixa principal com o resto em silêncio.
        for nome in nomes:
            regioes = regioes_locutor(cues, nome)
            if not regioes:
                continue
            arq = f"Áudio - {_nome_arquivo(nome)} (isolado).wav"
            rodar(["ffmpeg", "-v", "error", "-y", "-i", video, "-filter_complex", grafo_audio(segs, 1, regioes),
                   "-map", "[aout]", "-c:a", "pcm_s16le", "-ar", "48000", entrega / arq])
            arquivos_audio.append(arq)

    (entrega / "corte.json").write_text(json.dumps({
        "titulo": titulo, "resumo": str(resposta.get("resumo") or "").strip(), "gancho": gancho, "legenda": legendar,
        "video": video, "duracao": total, "formato": f"{LARGURA}x{ALTURA} {FPS}fps",
        "locutores": nomes, "origem_locutores": origem, "segmentos": segs, "descartado": fora,
        "enquadramento": normalizar_layout(layout, info["largura"], info["altura"]) if layout else
        {"formato": cfg.get("formato"), "enquadramento": cfg.get("enquadramento")},
        "capa": (layout or {}).get("capa") if capa else None,
        # o editor do app reabre o corte com isto: o enquadramento como foi desenhado e, numa edição, tudo dela
        "layout_editor": layout, "faixas_isoladas": faixas_isoladas, "edicao": edicao_gravada(ed, segs) if ed else None,
        "llm": ({"provedor": "edição na timeline", "modelo": "—"} if ed else
                {"provedor": provedor, "modelo": cfg.get("openrouter_modelo") if provedor == "openrouter" else cfg.get("modelo_llm") or "padrão"}),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    extras = "legendas" if legendar else "gancho" if gancho else "sem legenda"
    texto(f"Entrega: {nome_final}/ — {saida.name}, {extras} e {len(arquivos_audio)} áudio(s).")
    return f"Corte vertical pronto em {nome_final}: {len(segs)} trechos, {total:.1f} s.", custo


def so_transcrever(video, saida, modelo, idioma):
    """Palavras do bruto, sem corte nenhum: o estúdio usa nos cortes inteligentes e na trilha Fala."""
    trab = Path.cwd()
    info = probe(video)
    if not info["audios"]:
        raise Falha("o vídeo não tem áudio")
    audio = trab / "audio.wav"
    extrair_audio(video, 1, str(audio))
    words = marcar_duvidosas(transcrever(audio, trab, modelo, idioma, info["duracao"]))
    audio.unlink(missing_ok=True)
    out = [{"texto": w["texto"], "t": w["t"], "fim": w["fim"], "locutor": "Locutor 1", "duvidosa": w["duvidosa"]}
           for w in words]
    tmp = Path(saida).with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(saida)
    return len(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    inicio = time.time()
    if argv[:1] == ["--transcrever"] and len(argv) >= 3:
        try:
            n = so_transcrever(argv[1], argv[2], argv[3] if len(argv) > 3 else DEFAULTS["asr_modelo"],
                               argv[4] if len(argv) > 4 else DEFAULTS["idioma"])
        except (Falha, subprocess.TimeoutExpired) as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
        fim(True, f"{n} palavras", inicio)
        return 0
    if "--args" not in argv:
        print(__doc__)
        return 2
    a = json.loads(Path(argv[argv.index("--args") + 1]).read_text(encoding="utf-8"))
    _emit({"type": "system", "subtype": "auto", "modo": "corte"})
    try:
        msg, custo = executar(a)
    except Falha as exc:
        fim(False, str(exc), inicio)
        return 1
    except subprocess.TimeoutExpired as exc:
        fim(False, f"passo demorou demais: {Path(str(exc.cmd[0])).name}", inicio)
        return 1
    except Exception as exc:  # o painel precisa de uma linha result, senão o job fecha sem motivo
        fim(False, f"erro inesperado: {exc.__class__.__name__}: {exc}", inicio)
        return 1
    fim(True, msg, inicio, custo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
