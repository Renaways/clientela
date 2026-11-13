#!/usr/bin/env python3
"""Pipeline automático do estagiário: o que é template roda aqui, sem IA (zero token).

O Clientela roda este script direto, no lugar do `claude -p`, e ele fala o mesmo stream-json do Claude Code
(linhas assistant/result) para o painel acompanhar, cancelar e fechar o job sem saber a diferença.

  legendas  preparar (ASR large-v3 + texto da transcrição) → blocos e destaques automáticos → render → entrega
  gc        preparar (sem fala) → um GC por pessoa/entrada pedida no app → render → entrega
  motion    preparar a bancada e passar a vez ao Claude (exec): a IA começa com tudo pronto, só para criar
  bancada   só preparar (inserts montados à mão na prévia do app, com os templates universais)
  exportar  renderizar e entregar só as composições aprovadas na prévia (args: comps, recorte, pessoa)
  amostras  amostras dos templates do perfil (legenda + GC) para a aba Identidade

uso: python3 kit/auto.py <modo> --args <arquivo.json>
O estilo vem do template do cliente, <perfil>/marca.json (escrito na análise de IDV). Sem ele não há automático.
"""
import html as html_mod
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

KIT = Path(__file__).resolve().parent
TRAB = KIT.parent
PY = sys.executable or "python3"


# ------------------------------------------------------------------ stream-json (o que o painel lê)
def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def texto(msg):
    _emit({"type": "assistant", "message": {"content": [{"type": "text", "text": msg}]}})


def passo(desc):
    _emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"description": desc}}]}})


def fim(ok, msg, inicio):
    _emit({"type": "result", "subtype": "success" if ok else "error", "is_error": not ok, "result": msg,
           "total_cost_usd": 0, "num_turns": 0, "duration_ms": int((time.time() - inicio) * 1000)})


class Falha(Exception):
    pass


def rodar(desc, cmd, timeout=3600):
    """Um passo: o painel mostra `desc`; a saída do script vira texto só nas linhas que importam."""
    passo(desc)
    # stdin=DEVNULL: o stdin deste script é o arquivo do prompt do app, e um ffmpeg neto que o herda lê o "c"
    # de "(modo automático" como a tecla de comando de filtro e gira a 100% de CPU para sempre.
    r = subprocess.run([str(c) for c in cmd], cwd=TRAB, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    linhas = [ln.strip() for ln in (r.stdout + "\n" + r.stderr).splitlines() if ln.strip()]
    if r.returncode != 0:
        raise Falha(f"{desc}: falhou.\n" + "\n".join(linhas[-12:]))
    uteis = [ln for ln in linhas if ln.startswith(("aviso", "Transcrição", "Vídeo", "ok legendas", "Reaproveitando",
                                                   "marca.json", "Entrega:")) or "palavras com início" in ln]
    for ln in uteis[:6]:
        texto(ln)
    return linhas


def preparar(a, extra=()):
    if (TRAB / "job.json").exists() and (TRAB / "palavras.json").exists() and a.get("reaproveitar", False):
        texto("Bancada já preparada: reaproveitando job.json e palavras.json.")
        return
    cmd = [PY, KIT / "preparar.py", "--video", a["video"], "--transcricao", a.get("transcricao") or "",
           "--perfil", a.get("perfil") or "", "--entrega", a["entrega"], "--versao", a["versao"],
           "--tarefa", a["tarefa"], "--cliente", a.get("cliente") or "", "--projeto", a.get("projeto") or "", *extra]
    rodar("Preparando a bancada: vídeo, transcrição e template do cliente", cmd)


def exigir_template():
    if not (TRAB / "marca.json").exists():
        raise Falha("Este cliente ainda não tem o template (marca.json no perfil _Estagiário). Gere os templates "
                    "na aba Identidade do cliente; sem eles o modo automático não sabe o estilo.")
    rodar("Conferindo o template do cliente (marca.json)", [PY, KIT / "check_marca.py"])


def anotar(linhas):
    dec = TRAB / "DECISOES.md"
    if dec.exists():
        s = dec.read_text(encoding="utf-8").replace("- (em andamento)\n", "", 1)
        s = s.replace("## Entregue\n", "## Entregue\n" + "".join(f"- {ln}\n" for ln in linhas), 1)
        dec.write_text(s, encoding="utf-8")


def entregues():
    job = json.loads((TRAB / "job.json").read_text(encoding="utf-8"))
    pasta = Path(job["entrega"])
    return pasta, sorted(f.name for f in pasta.glob("*.mov"))


# ------------------------------------------------------------------ modos
def legendas(a):
    preparar(a, ["--asr-modelo", a.get("asr_modelo") or "large-v3"])
    exigir_template()
    saida = rodar("Quebrando a fala em blocos e escolhendo os destaques", [PY, KIT / "legendas.py", "blocos"])
    o = a.get("legendas") or {}
    rodar("Montando a composição das legendas", [PY, KIT / "legendas.py", "html",
                                                 "--animacao", o.get("animacao") or "perfil", "--fonte", o.get("fonte") or ""])
    rodar("Renderizando as legendas (vídeo inteiro, 30 fps)", [PY, KIT / "render.py", "legendas"], timeout=7200)
    rodar("Conferindo e copiando para a entrega", [PY, KIT / "entregar.py"])
    pasta, movs = entregues()
    resumo = next((ln for ln in saida if ln.startswith("ok legendas")), "")
    anotar([f"`{m}`" for m in movs] + ([resumo.replace("ok ", "")] if resumo else []) + ["Feito no modo automático (sem IA)."])
    return f"Legendas entregues em {pasta.name}: {', '.join(movs)}. {resumo.replace('ok ', '')}".strip()


def _sem_acento(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _cargo(nome):
    job = json.loads((TRAB / "job.json").read_text(encoding="utf-8"))
    for p in (job.get("perfil") or {}).get("pessoas") or []:
        if p["nome"].strip().lower() == nome.strip().lower():
            return p.get("cargo") or ""
    return ""


def gc_por_template(tpl, gcs):
    """O GC do cliente feito à mão e validado (<perfil>/templates/gc/index.html, com {{NOME}} e {{CARGO}}): só
    troca os textos. template.json opcional: {"fps": 30}. Vence o GC genérico do gc.py."""
    base = (tpl / "index.html").read_text(encoding="utf-8")
    meta = json.loads((tpl / "template.json").read_text(encoding="utf-8")) if (tpl / "template.json").exists() else {}
    if (tpl / "assets").is_dir():
        shutil.copytree(tpl / "assets", TRAB / "hf" / "assets", dirs_exist_ok=True)
    jobs = []
    for g in gcs:
        cargo = g["cargo"] if g.get("cargo") is not None else _cargo(g["nome"])
        if not cargo:
            texto(f"aviso: {g['nome']} sem cargo no pessoas.csv; o GC sai com a barra do cargo vazia")
        out = base.replace("{{NOME}}", html_mod.escape(g["nome"])).replace("{{CARGO}}", html_mod.escape(cargo))
        comp = f"gc-{g['n']}"
        (TRAB / "hf" / f"{comp}.html").write_text(out, encoding="utf-8")
        jobs.append({"comp": comp, "inicio": g["inicio"], "nome": f"{g['n']} GC {_sem_acento(g['nome'])}",
                     "fps": int(meta.get("fps") or 30)})
    (TRAB / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")


def montar_gcs(perfil, gcs):
    tpl = Path(perfil or "") / "templates" / "gc"
    if perfil and (tpl / "index.html").is_file():
        passo("Montando os GCs no template do cliente (templates/gc)")
        gc_por_template(tpl, gcs)
        return "template do cliente"
    rodar("Montando os GCs no componente genérico (marca.json)", [PY, KIT / "gc.py"])
    return "componente genérico do kit"


def gc(a):
    gcs = [g for g in a.get("gcs") or [] if (g.get("nome") or "").strip()]
    if not gcs:
        raise Falha("Nenhum GC pedido: escolha a pessoa e a entrada no painel.")
    preparar(a, ["--sem-transcricao"])
    exigir_template()
    gcs = [{"n": i + 1, "nome": g["nome"].strip(), "cargo": g.get("cargo"), "inicio": float(g.get("inicio") or 0)}
           for i, g in enumerate(gcs)]
    (TRAB / "gcs.json").write_text(json.dumps(gcs, ensure_ascii=False, indent=1), encoding="utf-8")
    origem = montar_gcs(a.get("perfil"), gcs)
    rodar("Renderizando os GCs", [PY, KIT / "render.py"], timeout=3600)
    rodar("Conferindo e copiando para a entrega", [PY, KIT / "entregar.py"])
    pasta, movs = entregues()
    anotar([f"`{m}`" for m in movs] + [f"Feito no modo automático (sem IA), com o {origem}."])
    return f"GC entregue em {pasta.name} ({origem}): {', '.join(movs)}."


def motion(a):
    preparar(a, ["--asr-modelo", a.get("asr_modelo") or "small"])
    claude = a.get("claude") or []
    if not claude:
        raise Falha("sem o comando do Claude")
    texto("Bancada pronta. Passando a vez ao estagiário (IA) para criar o motion.")
    sys.stdout.flush()
    if a.get("projeto"):
        os.chdir(a["projeto"])  # o contrato: o claude roda com cwd na pasta do projeto
    os.execv(claude[0], claude)  # mesmo processo: o prompt continua no stdin e o app segue o mesmo pid


def bancada(a):
    """Inserts à mão: a bancada fica pronta (fala, marca, assets) e o editor monta tudo na prévia do app."""
    preparar(a, ["--asr-modelo", a.get("asr_modelo") or "small"])
    exigir_template()
    if not (TRAB / "inserts.json").exists():
        (TRAB / "inserts.json").write_text('{"inserts": []}\n', encoding="utf-8")
    rodar("Montando os templates universais", [PY, KIT / "universais.py"])
    return "Bancada pronta: monte os inserts na prévia (Adicionar insert)."


def exportar(a):
    """Só as composições aprovadas na prévia: render (120 fps → 30 fps ProRes 4444) e entrega."""
    comps = [str(c) for c in a.get("comps") or []]
    if not comps:
        raise Falha("Nenhum insert aprovado para exportar.")
    rodar(f"Renderizando {len(comps)} {'composição' if len(comps) == 1 else 'composições'} a 120 fps",
          [PY, KIT / "render.py", *comps], timeout=7200)
    nomes = rodar("Listando os arquivos", [PY, KIT / "render.py", "--lista", *comps])
    arquivos = [TRAB / "renders" / n for n in nomes if n.endswith(".mov")]
    extra = []
    recorte = TRAB / "recorte.mov"
    if a.get("recorte") and recorte.exists():
        extra = ["--recorte", recorte, "--pessoa", a.get("pessoa") or "Apresentador"]
    rodar("Conferindo e copiando para a entrega", [PY, KIT / "entregar.py", *extra, *arquivos])
    pasta, movs = entregues()
    anotar([f"`{m}`" for m in movs if any(m == f.name for f in arquivos)] + ["Exportado da prévia (aprovados no app)."])
    return f"{len(arquivos)} {'arquivo exportado' if len(arquivos) == 1 else 'arquivos exportados'} para {pasta.name}."


AMOSTRA_FALA = "Este é o modelo de legenda automática de {cliente} com destaque em 2026 e no estilo da marca."


def amostras(a):
    """Amostras do template em <perfil>/amostras/: legenda e GC sobre um fundo neutro, para conferir o estilo."""
    sys.path.insert(0, str(KIT))
    import preparar as prep  # noqa: E402  (só as funções: o main não roda)

    perfil = Path(a["perfil"])
    saida = Path(a["saida"])
    if not (perfil / "marca.json").exists():
        raise Falha("O perfil não tem marca.json (o template). Gere os templates primeiro.")
    w, h = int(a.get("largura") or 1080), int(a.get("altura") or 1920)
    video = {"caminho": None, "largura": w, "altura": h, "fps_origem": 30, "fps": 30, "duracao": 6.0, "tem_audio": False}
    passo("Preparando a bancada das amostras")
    prep.init_hf(video)
    prep.copiar_assets(perfil)
    shutil.copy2(perfil / "marca.json", TRAB / "marca.json")
    info = prep.ler_perfil(str(perfil))
    falas = AMOSTRA_FALA.format(cliente=a.get("cliente") or "este cliente").split()
    palavras = [{"texto": t, "t": round(0.4 + i * 0.32, 3), "fim": round(0.4 + i * 0.32 + 0.28, 3)} for i, t in enumerate(falas)]
    (TRAB / "palavras.json").write_text(json.dumps({"fonte": "amostra", "precisao": "palavra", "palavras": palavras},
                                                   ensure_ascii=False), encoding="utf-8")
    (TRAB / "job.json").write_text(json.dumps({"cliente": a.get("cliente"), "tarefa": "Amostras", "versao": "v0",
                                               "trabalho": str(TRAB), "entrega": str(TRAB / "entrega"), "video": video,
                                               "perfil": info, "recorte": None}, ensure_ascii=False), encoding="utf-8")
    exigir_template()
    pessoa = (info.get("pessoas") or [{"nome": "Nome da Pessoa", "cargo": "Cargo"}])[0]
    gcs = [{"n": 1, "nome": pessoa["nome"], "cargo": pessoa.get("cargo") or "Cargo", "inicio": 0}]
    (TRAB / "gcs.json").write_text(json.dumps(gcs, ensure_ascii=False), encoding="utf-8")
    montar_gcs(str(perfil), gcs)
    gc_jobs = json.loads((TRAB / "jobs.json").read_text(encoding="utf-8"))
    rodar("Montando a amostra de legenda", [PY, KIT / "legendas.py", "blocos"])
    rodar("Montando a amostra de legenda", [PY, KIT / "legendas.py", "html"])  # acrescenta "legendas" ao jobs.json
    jobs = json.loads((TRAB / "jobs.json").read_text(encoding="utf-8"))
    (TRAB / "jobs.json").write_text(json.dumps([j for j in jobs if j["comp"] == "legendas"] + gc_jobs, ensure_ascii=False),
                                    encoding="utf-8")
    rodar("Renderizando as amostras", [PY, KIT / "render.py"], timeout=1800)
    saida.mkdir(parents=True, exist_ok=True)
    feitos = []
    for nome, padrao in (("Legenda", "*Legendas*.mov"), ("GC", "*GC*.mov")):
        mov = next(iter(sorted((TRAB / "renders").glob(padrao))), None)
        if not mov:
            continue
        dst = saida / f"{nome}.mp4"
        passo(f"Montando a amostra de {nome} sobre fundo neutro")
        lado = 540 if h >= w else 960
        escala = f"scale={lado}:-2" if h >= w else f"scale=-2:{lado}"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x4a4d52:s={w}x{h}:r=30",
                        "-i", str(mov), "-filter_complex", f"[0][1]overlay=shortest=1,{escala},format=yuv420p",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-movflags", "+faststart", str(dst)],
                       check=True, capture_output=True)
        feitos.append(dst.name)
    if not feitos:
        raise Falha("O render não gerou as amostras.")
    return f"Amostras prontas em {saida}: {', '.join(feitos)}."


MODOS = {"legendas": legendas, "gc": gc, "motion": motion, "amostras": amostras, "bancada": bancada, "exportar": exportar}


def main():
    inicio = time.time()
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    if modo not in MODOS or "--args" not in sys.argv:
        print(__doc__)
        return 2
    a = json.loads(Path(sys.argv[sys.argv.index("--args") + 1]).read_text(encoding="utf-8"))
    _emit({"type": "system", "subtype": "auto", "modo": modo})
    try:
        msg = MODOS[modo](a)
    except Falha as exc:
        fim(False, str(exc), inicio)
        return 1
    except subprocess.TimeoutExpired as exc:
        fim(False, f"passo demorou demais: {exc.cmd[1] if len(exc.cmd) > 1 else exc.cmd}", inicio)
        return 1
    except Exception as exc:  # o painel precisa de uma linha result, senão o job fecha sem motivo
        fim(False, f"erro inesperado no modo automático: {exc!r}", inicio)
        return 1
    fim(True, msg, inicio)
    return 0


if __name__ == "__main__":
    sys.exit(main())
