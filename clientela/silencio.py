"""Cortar silêncio → XML do Premiere (xmeml v4). O bruto não é tocado nem re-encodado: o XML monta uma
sequência só com os trechos de fala, cada clipitem apontando (`in`/`out`) para o arquivo original.

O silêncio é o dos cortes inteligentes do estúdio (`autocorte.cortes_inteligentes`), com gaguejo,
repetição e hesitação desligados por padrão: ele já resolve o respiro dos dois lados, o começo e o fim do
arquivo e junta pedaço menor que `minimo` — o que `silencios` sozinho deixaria para fazer de novo aqui."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from . import autocorte, editor, history, store
from . import projects as projects_module
from .persistence import safe_write_text
from .projeto_ia import _rate
from .transcricao import _nome_livre

# (mínimo, máximo) aceitos do frontend; fora disso o valor é puxado para dentro
LIMITES = {"silencio_db": (-70.0, -10.0), "silencio_min": (0.1, 5.0), "respiro": (0.0, 1.0)}


def params_validos(body: dict[str, Any]) -> dict[str, Any]:
    """Os três números do modal (limitados) + `gaguejos` liga as três disfluências juntas."""
    p = {}
    for k, (lo, hi) in LIMITES.items():
        v = body.get(k, autocorte.CORTES_PADRAO[k])
        try:
            p[k] = min(hi, max(lo, float(v)))
        except (TypeError, ValueError):
            raise ValueError(f"{k} precisa ser um número") from None
    dis = bool(body.get("gaguejos"))
    p.update(gaguejos=dis, repeticoes=dis, hesitacoes=dis)
    return p


def trechos_de_fala(envelope: list[float], passo: float, params: dict[str, Any],
                    palavras: list[dict[str, Any]] | None = None) -> list[tuple[float, float]]:
    """Envelope (dBFS por janela de `passo` s) → [(início, fim)] do que fica, em segundos do bruto."""
    r = autocorte.cortes_inteligentes(len(envelope) * passo, envelope, passo, palavras, None, params)
    return [(s["inicio"], s["fim"]) for s in r["segmentos"]]


def resumo(duracao: float, trechos: list[tuple[float, float]]) -> dict[str, Any]:
    final = sum(b - a for a, b in trechos)
    bordas = [0.0] + [x for t in trechos for x in t] + [duracao]
    cortes = sum(1 for a, b in zip(bordas[::2], bordas[1::2]) if b - a > 0.001)
    return {"duracao": round(duracao, 3), "final": round(final, 3), "cortes": cortes, "trechos": len(trechos),
            "removido_pct": round(100 * (1 - final / duracao), 1) if duracao > 0 else 0.0}


def xml_sem_silencio(video: Path, info: dict[str, Any], trechos: list[tuple[float, float]], nome: str) -> str:
    """Uma sequência no fps/resolução do vídeo: um clipitem por trecho em V1 e o mesmo trecho em A1 (e A2 se
    o áudio é estéreo), encostados na timeline e ligados entre si (mover um move os outros no Premiere)."""
    fps = info.get("fps") or 30.0
    tb, ntsc = _rate(fps)
    canais = min(2, int(((info.get("audios") or [{}])[0]).get("canais") or 0)) if info.get("audios") else 0
    dur_arq = max(1, round(info["duracao"] * fps))
    rate = f"<rate><timebase>{tb}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"
    amostra = (f"<samplecharacteristics>{rate}<width>{info['largura']}</width>"
               f"<height>{info['altura']}</height></samplecharacteristics>")
    audio_arq = f"<audio><channelcount>{canais}</channelcount></audio>" if canais else ""
    arquivo = (f'<file id="file-1"><name>{escape(video.name)}</name><pathurl>{escape(video.as_uri())}</pathurl>'
               f"{rate}<duration>{dur_arq}</duration><media><video>{amostra}</video>{audio_arq}</media></file>")
    trilhas = [("v", "video", 1)] + [(f"a{k}", "audio", k) for k in range(1, canais + 1)]

    pistas: dict[str, list[str]] = {t[0]: [] for t in trilhas}
    pos = 0
    for i, (a, b) in enumerate(trechos):
        ent, sai = round(a * fps), min(dur_arq, round(b * fps))
        if sai <= ent:
            continue
        n = len(pistas["v"]) + 1
        links = "".join(f"<link><linkclipref>{p}-{n}</linkclipref><mediatype>{m}</mediatype>"
                        f"<trackindex>{1 if m == 'video' else k}</trackindex><clipindex>{n}</clipindex></link>"
                        for p, m, k in trilhas)
        for p, m, k in trilhas:
            fonte = arquivo if n == 1 and p == "v" else '<file id="file-1"/>'
            if m == "audio":
                fonte += f"<sourcetrack><mediatype>audio</mediatype><trackindex>{k}</trackindex></sourcetrack>"
            pistas[p].append(f'<clipitem id="{p}-{n}"><name>{escape(video.name)}</name><duration>{dur_arq}</duration>'
                             f"{rate}<start>{pos}</start><end>{pos + sai - ent}</end><in>{ent}</in><out>{sai}</out>"
                             f"{fonte}{links}</clipitem>")
        pos += sai - ent
    trilha = lambda p: f"<track>{''.join(pistas[p])}</track>"  # noqa: E731
    audio = "".join(trilha(p) for p, m, _ in trilhas if m == "audio")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
            f'<sequence id="seq-1"><name>{escape(nome)}</name><duration>{pos}</duration>{rate}'
            f"<media><video><format>{amostra}</format>{trilha('v')}</video><audio>{audio}</audio></media>"
            "</sequence></xmeml>\n")


def cortar_silencio(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Prévia (só números) ou grava `Sem silêncio - <vídeo>.xml` na pasta do projeto, com nome livre."""
    project = projects_module.get_project(project_id)
    if not project:
        raise editor.EditorError("projeto não encontrado", 404)
    if not project.get("folder_available"):
        raise editor.EditorError("pasta do projeto não encontrada no disco", 409)
    try:
        params = params_validos(body)
    except ValueError as exc:
        raise editor.EditorError(str(exc), 400) from None
    video = body.get("video")
    if not video:
        orig = ((store.load(project_id) or {}).get("midia_original") or {}).get("arquivo")
        cands = (editor.list_candidates(project_id) or {}).get("videos") or []
        video = orig or (cands[0]["path"] if cands else None)
    if not video:
        raise editor.EditorError("nenhum vídeo na pasta do projeto", 409)
    alvo = editor._media_for_frame(project_id, video)
    env = editor.audio_envelope(project_id, video, 1)   # sem ffmpeg: EditorError 503 com o aviso
    # palavras da transcrição do estúdio (data/editor/transcricoes/), se já existe; só entram com `gaguejos`
    palavras = editor.transcription_status(project_id, video).get("palavras")
    trechos = trechos_de_fala(env["db"], env["passo"], params, palavras if params["gaguejos"] else None)
    out = {**resumo(env["duracao"], trechos), "video": alvo.name, "tem_palavras": palavras is not None}
    if body.get("previa"):
        return out
    if not trechos:
        raise editor.EditorError("não sobrou fala: o vídeo inteiro ficou abaixo do limiar", 400)
    info = editor.media_info(project_id, video)
    base = Path(project["path"])
    destino = _nome_livre(base, f"Sem silêncio - {alvo.stem}.xml")
    safe_write_text(destino, xml_sem_silencio(alvo, info, trechos, f"Sem silêncio - {alvo.stem}"))
    history.append_event(project_id, "xml_sem_silencio",
                         {"arquivo": destino.name, "video": alvo.name, "cortes": out["cortes"]})
    return {**out, "arquivo": str(destino), "nome": destino.name,
            "relativo": destino.relative_to(base).as_posix()}
