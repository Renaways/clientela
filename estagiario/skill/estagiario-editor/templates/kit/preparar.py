#!/usr/bin/env python3
"""Prepara a bancada do estagiário em {trabalho}/ (a pasta onde este kit foi copiado: {trabalho}/kit/).

Escreve job.json, normaliza a transcrição em palavras.json + transcricao.txt (ou roda o ASR),
copia fontes/logos/texturas do perfil para hf/assets/, cria o projeto HyperFrames em hf/ e o DECISOES.md.

uso:
  python3 kit/preparar.py --video <bruto> --transcricao <arquivo | "nenhuma (transcreva o vídeo)"> \
      --perfil <cliente/_Estagiário> --entrega <projeto/Motion - X vN> --versao vN --tarefa Inserts --cliente <nome>
"""
import argparse
import csv
import difflib
import io
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

KIT = Path(__file__).resolve().parent
TRAB = KIT.parent
HF = TRAB / "hf"


def probe(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height,r_frame_rate:format=duration", "-of", "json", str(video)],
                         capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    num, den = (int(x) for x in v["r_frame_rate"].split("/"))
    return {"caminho": str(video), "largura": v["width"], "altura": v["height"],
            "fps_origem": round(num / den, 3) if den else None, "fps": 30,
            "duracao": float(d["format"]["duration"]),
            "tem_audio": any(s["codec_type"] == "audio" for s in d["streams"])}


# ------------------------------------------------------------------ transcrição
def _w(texto, t, fim):
    return {"texto": str(texto).strip(), "t": round(float(t), 3), "fim": round(float(fim), 3)}


def from_json(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    words = []
    if isinstance(d, dict) and d.get("segments"):  # Premiere (segments[].words[] com start/duration) ou whisper
        for seg in d["segments"]:
            for w in seg.get("words") or []:
                txt = w.get("text", w.get("word", ""))
                start = w.get("start", 0)
                fim = w.get("end", start + float(w.get("duration", 0)))
                if w.get("type", "word") == "word" and str(txt).strip():
                    words.append(_w(txt, start, fim))
        return words, ("premiere" if any("duration" in w for s in d["segments"] for w in s.get("words") or []) else "json")
    lst = d if isinstance(d, list) else d.get("words") or []
    for w in lst:  # hyperframes transcribe: [{text, start, end}]
        txt = w.get("text", w.get("word", ""))
        if str(txt).strip():
            words.append(_w(txt, w["start"], w.get("end", w["start"])))
    return words, "json"


def _ts(s):
    s = s.strip().replace(",", ".")
    parts = [float(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def cues_srt(path):
    """srt/vtt → [(t0, t1, texto)]. Só há tempo por legenda (e, se veio do Premiere, da sequência editada)."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    return [(_ts(m.group(1)), _ts(m.group(2)), " ".join(re.sub(r"<[^>]+>", "", m.group(3)).split()))
            for m in re.finditer(r"([\d:.,]+)\s*-->\s*([\d:.,]+)[^\n]*\n(.*?)(?:\n\s*\n|\Z)", text, re.S)]


def from_cues(path):
    """srt/vtt sem ASR: as palavras são distribuídas pelo número de letras dentro de cada legenda (aproximado)."""
    return [w for t0, t1, txt in cues_srt(path) for w in _distribuir(txt, t0, t1)]


def _distribuir(texto, t0, t1):
    """Palavras de um bloco com tempo só de começo e fim, pelo número de letras (aproximado)."""
    toks = texto.split()
    total = sum(len(t) + 1 for t in toks) or 1
    out, acc = [], t0
    for tok in toks:
        d = (t1 - t0) * (len(tok) + 1) / total
        out.append(_w(tok, acc, acc + d))
        acc += d
    return out


# Texto exportado do Premiere (Transcrição → Exportar para arquivo de texto):
#   00:00:11:16 - 00:00:23:06
#   Speaker 1
#   O primeiro debate do segundo turno …
# Os tempos são da SEQUÊNCIA em que o texto foi transcrito, que muitas vezes já é a edição (cortes de respiro,
# takes fora): no 049 a deriva chegava a 40 s no fim. Por isso o texto entra como correção do ASR, e os tempos
# só servem para conferir se a sequência é o próprio bruto.
_PREMIERE_TC = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})[:;](\d{2})\s*-\s*(\d{1,2}):(\d{2}):(\d{2})[:;](\d{2})$")


def from_premiere_txt(path, fps=30):
    """[(t0, t1, texto)] dos blocos com timecode; [] se o .txt não estiver nesse formato."""
    lines = [ln.strip() for ln in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()]
    cues, i = [], 0
    while i < len(lines):
        m = _PREMIERE_TC.match(lines[i])
        if not m:
            i += 1
            continue
        g = [int(x) for x in m.groups()]
        t0 = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / fps
        t1 = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / fps
        j, bloco = i + 1, []
        while j < len(lines) and not _PREMIERE_TC.match(lines[j]):
            if lines[j]:
                bloco.append(lines[j])
            j += 1
        # a primeira linha é o falante ("Speaker 1", "Ana") quando há texto depois dela
        if len(bloco) >= 2 and len(bloco[0].split()) <= 4 and not re.search(r"[.?!,;:…]$", bloco[0]):
            bloco = bloco[1:]
        if bloco:
            cues.append((t0, t1, " ".join(bloco)))
        i = j
    return cues


def _norm(tok):
    tok = unicodedata.normalize("NFKD", tok.lower())
    return re.sub(r"[^\w]", "", "".join(c for c in tok if not unicodedata.combining(c)))


def alinhar(words, texto, confiavel):
    """Corrige o texto do ASR pelo `texto` de referência mantendo os tempos do ASR (difflib por palavra).

    `confiavel` (transcrição do Premiere, que é da mesma fala): trechos que o ASR errou ou pulou recebem o texto
    de referência, distribuído no tempo do trecho. Roteiro comum (pode ter frase que não foi dita): só troca
    palavra por palavra onde as duas versões são parecidas. Fala que só existe no bruto (take cortado da
    sequência) fica como o ASR ouviu. O texto do Premiere também é transcrição automática: cada troca volta
    em `correcoes` para ser revisada. Devolve (palavras, correcoes)."""
    ref = [t for t in texto.split() if _norm(t)]
    a, b = [_norm(w["texto"]) for w in words], [_norm(t) for t in ref]
    out, correcoes = [], []

    def anota(antes, depois):
        if antes or depois:
            correcoes.append({"t": round(antes[0]["t"] if antes else depois[0]["t"], 2),
                              "asr": " ".join(w["texto"] for w in antes), "texto": " ".join(w["texto"] for w in depois)})

    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        antes = words[i1:i2]
        if op == "equal":  # mesmas letras: fica a grafia do ASR, que pontua melhor que o .txt do Premiere
            out.extend(antes)
            continue
        pedacos = _pedacos(a[i1:i2], b[j1:j2]) if op == "replace" else None
        if pedacos:  # mesmas letras, outra separação ("mal-estar." × "mal estar.", "Eaqui" × "E aqui")
            i, j = i1, j1
            for ni, nj in pedacos:
                ws, rs = words[i:i + ni], ref[j:j + nj]
                if ni == nj == 1 or any("-" in w["texto"] for w in ws):
                    out.extend(ws)  # mesma palavra, ou hífen do ASR (que está certo)
                else:
                    depois = _distribuir(" ".join(rs), ws[0]["t"], ws[-1]["fim"])
                    out.extend(depois)
                    anota(ws, depois)
                i, j = i + ni, j + nj
            continue
        if op == "replace" and i2 - i1 == j2 - j1:
            depois = []
            for k in range(i2 - i1):
                w, r = words[i1 + k], ref[j1 + k]
                parecido = difflib.SequenceMatcher(None, a[i1 + k], b[j1 + k]).ratio() >= 0.5
                depois.append(_w(r, w["t"], w["fim"]) if confiavel or parecido else w)
            out.extend(depois)
            anota(antes, depois) if depois != antes else None
        elif op == "insert" and j2 - j1 > 2:
            # frase inteira que o ASR não ouviu: no 049, "Mas vamos lá." era de outro take, e no bruto ali é silêncio.
            # Legenda só do que foi dito no bruto: fica de fora, listada para revisão.
            anota([], [_w(" ".join(ref[j1:j2]) + "  (não entrou: o ASR não ouviu no bruto)",
                          out[-1]["fim"] if out else 0.0, out[-1]["fim"] if out else 0.0)])
        elif op in ("replace", "insert") and confiavel:
            ini = words[i1]["t"] if i1 < len(words) else (out[-1]["fim"] if out else 0.0)
            fim = words[i2 - 1]["fim"] if i2 > i1 else (words[i1]["t"] if i1 < len(words) else ini)
            if fim - ini < 0.05 * (j2 - j1):  # palavra que o ASR pulou: cabe no respiro entre as vizinhas
                ini = out[-1]["fim"] if out else ini
            depois = _distribuir(" ".join(ref[j1:j2]), ini, fim)
            out.extend(depois)
            anota(antes, depois)
        else:  # delete (só no bruto) ou trecho de roteiro sem confiança
            out.extend(antes)
    return out, correcoes


def _pedacos(a, b):
    """Divide dois trechos com as MESMAS letras em pedaços correspondentes: ["malestar", "eaqui"] ×
    ["mal", "estar", "e", "aqui"] → [(1, 2), (1, 2)]. None se as letras não baterem."""
    if not a or not b or "".join(a) != "".join(b):
        return None
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        sa, sb, ni, nj = a[i], b[j], 1, 1
        while sa != sb:
            if len(sa) < len(sb) and sb.startswith(sa) and i + ni < len(a):
                sa += a[i + ni]
                ni += 1
            elif len(sb) < len(sa) and sa.startswith(sb) and j + nj < len(b):
                sb += b[j + nj]
                nj += 1
            else:
                return None
        out.append((ni, nj))
        i, j = i + ni, j + nj
    return out if i == len(a) and j == len(b) else None


def deriva(words, cues):
    """Diferença (s) entre o início de cada bloco do Premiere e a mesma fala no bruto, pelas 3 primeiras palavras."""
    a = [_norm(w["texto"]) for w in words]
    difs = []
    for t0, _, texto in cues:
        alvo = [_norm(t) for t in texto.split() if _norm(t)][:3]
        hits = [words[i]["t"] for i in range(len(a) - len(alvo) + 1) if alvo and a[i:i + len(alvo)] == alvo]
        if hits:
            difs.append(min(hits, key=lambda h: abs(h - t0)) - t0)
    return difs


ASR_MODELO = "small"  # o preparar.py troca por --asr-modelo (legendas automáticas usam large-v3)


def asr(video):
    """O transcribe grava o transcript.json AO LADO do arquivo de entrada: extraia o áudio para {trabalho}/ antes,
    senão ele escreve na pasta do projeto (que é só leitura). Um ASR já feito nesta bancada (mesmo modelo) é
    reaproveitado: refazer custa minutos."""
    audio = TRAB / "audio.wav"
    feito = TRAB / f"asr-{ASR_MODELO}.json"
    if feito.exists():
        print(f"Reaproveitando o ASR ({ASR_MODELO}) desta bancada", flush=True)
        return ajustar_silencios(from_json(feito)[0], audio)
    print(f"Rodando o ASR (whisper {ASR_MODELO}, pt) …", flush=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)], check=True)
    r = subprocess.run(["npx", "hyperframes", "transcribe", str(audio), "-e", "whisper", "-m", ASR_MODELO, "-l", "pt", "--json"],
                       cwd=TRAB, check=True, capture_output=True, text=True)
    path = TRAB / "transcript.json"
    for line in r.stdout.splitlines():
        try:
            path = Path(json.loads(line).get("transcriptPath") or path)
        except (ValueError, AttributeError):
            pass
    shutil.copy2(path, feito)
    words, _ = from_json(path)
    return ajustar_silencios(words, audio)


def silencios(audio, ruido="-35dB", minimo=0.25):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(audio), "-af", f"silencedetect=noise={ruido}:d={minimo}",
                        "-f", "null", "-"], capture_output=True, text=True)
    ini = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    fim = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return list(zip(ini, fim))


def ajustar_silencios(words, audio):
    """O whisper costuma começar a palavra no fim da anterior, dentro do respiro (no 049: "primeiro" em 12,06 s
    com silêncio até 12,78 s). A palavra que começa num silêncio e termina depois dele passa a começar no fim
    do silêncio. Palavra inteira dentro do silêncio fica como está (melhor não inventar)."""
    sil = silencios(audio) if Path(audio).exists() else []
    n = 0
    for w in words:
        for s0, s1 in sil:
            if s0 < w["t"] < s1 < w["fim"]:
                w["t"] = round(s1, 3)
                n += 1
                break
    if n:
        print(f"{n} palavras com início ajustado ao fim do silêncio", flush=True)
    return words


def transcricao_txt(words):
    """Uma linha por frase (pausa > 0,5 s, pontuação ou 16 palavras): '[00:11.30] primeiro@11.30 debate@11.54 …'."""
    lines, cur = [], []
    for i, w in enumerate(words):
        if cur and (w["t"] - words[i - 1]["fim"] > 0.5):
            lines.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.?!…]$", w["texto"]) or len(cur) >= 16:
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        t = ln[0]["t"]
        out.append(f"[{int(t // 60):02d}:{t % 60:05.2f}] " + " ".join(f'{w["texto"]}@{w["t"]:.2f}' for w in ln))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ perfil
def ler_perfil(perfil):
    p = Path(perfil) if perfil else None
    info = {"caminho": str(p) if p else None, "existe": bool(p and p.is_dir()), "status": "sem_perfil",
            "arquivos": [], "pessoas": [], "vazio": True}
    if not info["existe"]:
        return info
    meta = p / "perfil.json"
    if meta.exists():
        try:
            info["status"] = json.loads(meta.read_text(encoding="utf-8")).get("status", "em_revisao")
        except (ValueError, OSError):
            info["status"] = "ilegivel"
    else:
        info["status"] = "em_revisao"
    for f in ("marca.md", "frame.md", "regras.md", "pessoas.csv", "duvidas.md"):
        fp = p / f
        if fp.exists():
            info["arquivos"].append(f)
            if f in ("marca.md", "frame.md") and len(re.sub(r"[#\s`{}:\-]|yaml|cores|degrades|tipografia|eases|componentes", "",
                                                            fp.read_text(encoding="utf-8", errors="replace"))) > 200:
                info["vazio"] = False
    pc = p / "pessoas.csv"
    if pc.exists():
        rows = csv.DictReader(io.StringIO(pc.read_text(encoding="utf-8-sig")))
        info["pessoas"] = [{"nome": (r.get("nome") or "").strip(), "cargo": (r.get("cargo") or "").strip()}
                           for r in rows if (r.get("nome") or "").strip()]
    return info


def copiar_assets(perfil):
    copiados = {}
    for sub, dst in (("assets/fonts", "fonts"), ("assets/logos", "logos"), ("assets/texturas", "texturas")):
        src = Path(perfil) / sub
        out = HF / "assets" / dst
        out.mkdir(parents=True, exist_ok=True)
        n = 0
        if src.is_dir():
            for f in src.iterdir():
                if f.is_file() and not f.name.startswith("."):
                    shutil.copy2(f, out / f.name)
                    n += 1
        copiados[dst] = n
    return copiados


def init_hf(video):
    if (HF / "hyperframes.json").exists():
        return
    res = "portrait" if video["altura"] > video["largura"] else ("square" if video["altura"] == video["largura"] else "landscape")
    subprocess.run(["npx", "hyperframes", "init", "hf", "--non-interactive", "--example=blank", "--skip-transcribe",
                    f"--resolution={res}"], cwd=TRAB, check=True, stdout=subprocess.DEVNULL)


DECISOES = """# {tarefa} {versao} · {projeto} · {cliente}

> Perfil: {status}. Transcrição: {fonte}.
{aviso}
## Entregue
- (em andamento)

## Decisões (tomadas sem perguntar)

## Dúvidas (para o editor responder nas observações da próxima versão)

## Próximos / o que calibrar
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--transcricao", default="")
    ap.add_argument("--perfil", default="")
    ap.add_argument("--entrega", required=True)
    ap.add_argument("--versao", required=True)
    ap.add_argument("--tarefa", required=True)
    ap.add_argument("--cliente", default="")
    ap.add_argument("--projeto", default="")
    ap.add_argument("--asr-modelo", default="small", help="modelo do whisper: small (padrão) ou large-v3")
    ap.add_argument("--sem-transcricao", action="store_true", help="tarefa que não usa a fala (GC)")
    a = ap.parse_args()

    video = probe(a.video)
    print(f"Vídeo: {video['largura']}×{video['altura']}, {video['fps_origem']} fps, {video['duracao']:.2f} s")

    tr = Path(a.transcricao) if a.transcricao and Path(a.transcricao).is_file() else None
    roteiro, alinhamento = None, None
    global ASR_MODELO
    ASR_MODELO = a.asr_modelo
    if a.sem_transcricao:
        words, fonte, precisao = [], "nenhuma (tarefa sem fala)", "palavra"
    elif tr and tr.suffix.lower() == ".json":
        words, fonte = from_json(tr)
        precisao = "palavra"
    elif tr:
        # srt/vtt/txt: tempo só por bloco, e o do Premiere é da SEQUÊNCIA (no 049 a deriva chegava a 42 s). Os tempos
        # por palavra vêm do ASR do bruto e o texto do arquivo corrige o que o ASR ouviu errado ("Bundy" → "Band").
        roteiro = TRAB / ("roteiro" + tr.suffix.lower())
        shutil.copy2(tr, roteiro)
        if tr.suffix.lower() in (".srt", ".vtt"):
            cues = cues_srt(tr)
        else:
            cues = from_premiere_txt(tr, round(video["fps_origem"] or 30))
        texto = " ".join(c[2] for c in cues) if cues else tr.read_text(encoding="utf-8-sig", errors="replace")
        try:
            bruto = asr(a.video)
        except (subprocess.CalledProcessError, OSError) as exc:
            if not cues:
                raise
            print(f"aviso: o ASR falhou ({exc}); uso os tempos do arquivo, aproximados por frase", flush=True)
            bruto = None
        if bruto is None:
            words = [w for t0, t1, txt in cues for w in _distribuir(txt, t0, t1)]
            fonte, precisao = f"{tr.suffix[1:]} (tempos do arquivo)", "frase"
        else:
            words, correcoes = alinhar(bruto, texto, confiavel=bool(cues))
            fonte, precisao = (f"asr + texto do {tr.suffix[1:]}" if cues else "asr + roteiro"), "palavra"
            alinhamento = {"trocas": len(correcoes), "palavras_asr": len(bruto)}
            (TRAB / "correcoes.md").write_text(
                "# Correções do ASR pelo texto da transcrição (revise: o texto também pode estar errado)\n\n"
                "| t (s) | ASR ouviu | ficou |\n|---|---|---|\n"
                + "".join(f"| {c['t']:.2f} | {c['asr'] or '(nada)'} | {c['texto'] or '(nada)'} |\n" for c in correcoes),
                encoding="utf-8")
            if cues:
                difs = deriva(words, cues)
                pior = max((abs(d) for d in difs), default=None)
                alinhamento.update({"blocos": len(cues), "blocos_achados": len(difs),
                                    "deriva_max_s": round(pior, 2) if pior is not None else None,
                                    "tempos_do_txt_batem": pior is not None and pior < 1.0})
                if pior is not None and pior >= 1.0:
                    print(f"aviso: os tempos de {tr.name} são de uma sequência editada (deriva de até {pior:.1f} s no "
                          "bruto); usei só o texto dele, com os tempos do ASR", flush=True)
    else:
        words, fonte, precisao = asr(a.video), "asr", "palavra"
    (TRAB / "palavras.json").write_text(json.dumps({"fonte": fonte, "precisao": precisao, "palavras": words},
                                                   ensure_ascii=False, indent=1), encoding="utf-8")
    (TRAB / "transcricao.txt").write_text(transcricao_txt(words), encoding="utf-8")
    extra = f" · {alinhamento['trocas']} correções pelo texto (correcoes.md)" if alinhamento else ""
    print(f"Transcrição: {fonte}, {len(words)} palavras ({precisao}){extra}{f' + {roteiro.name}' if roteiro else ''}")

    perfil = ler_perfil(a.perfil)
    init_hf(video)
    if perfil["existe"]:
        perfil["assets_copiados"] = copiar_assets(a.perfil)
        # o template do cliente: marca.json validado, escrito na análise de IDV. Com ele ninguém traduz o frame.md
        tpl = Path(a.perfil) / "marca.json"
        if tpl.is_file() and not (TRAB / "marca.json").exists():
            shutil.copy2(tpl, TRAB / "marca.json")
            print("marca.json: o template do perfil (não precisa traduzir o frame.md)")
        perfil["template"] = tpl.is_file()
    (HF / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    print(f"Perfil: {perfil['status']}{' (vazio)' if perfil['vazio'] else ''}, arquivos: {', '.join(perfil['arquivos']) or 'nenhum'}")

    job = {"cliente": a.cliente, "tarefa": a.tarefa, "versao": a.versao, "projeto": a.projeto or str(Path(a.video).parent),
           "trabalho": str(TRAB), "entrega": a.entrega, "video": video,
           "transcricao": {"fonte": fonte, "precisao": precisao, "caminho": str(tr) if tr else None,
                           "roteiro": str(roteiro) if roteiro else None, "alinhamento": alinhamento},
           "perfil": perfil, "recorte": None}
    old = TRAB / "job.json"
    if old.exists():  # não perde o recorte já feito numa rodada anterior
        job["recorte"] = json.loads(old.read_text(encoding="utf-8")).get("recorte")
    old.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")

    dec = TRAB / "DECISOES.md"
    if not dec.exists():
        aviso = ("> **Trabalhei sem manual de marca (perfil vazio): visual neutro. Preencha o perfil e gere uma versão nova.**\n"
                 if perfil["vazio"] else "")
        if perfil["existe"] and perfil["status"] != "pronto" and not perfil["vazio"]:
            aviso += "> Perfil ainda não marcado como pronto: o que ele infere foi usado com cautela.\n"
        if alinhamento and alinhamento.get("tempos_do_txt_batem") is False:
            aviso += (f"> A transcrição ({roteiro.name if roteiro else 'arquivo'}) é de uma sequência editada (deriva de até {alinhamento['deriva_max_s']} s "
                      "no bruto): usei o texto dele e os tempos do ASR.\n")
        dec.write_text(DECISOES.format(tarefa=a.tarefa, versao=a.versao, projeto=Path(job["projeto"]).name, cliente=a.cliente,
                                       status=perfil["status"], fonte=fonte, aviso=aviso), encoding="utf-8")
    print("ok job.json, palavras.json, transcricao.txt, hf/, DECISOES.md")


if __name__ == "__main__":
    sys.exit(main())
