import json, os, re, shutil, subprocess, threading, uuid
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent.resolve()
DATA = ROOT / "data"; DATA.mkdir(exist_ok=True)
JOBS: dict = {}
app = FastAPI(title="Recorta")


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-600:])


def set_job(jid, **kw):
    JOBS[jid].update(kw)


def transcribe(src):
    from faster_whisper import WhisperModel
    model = WhisperModel(os.getenv("WHISPER_MODEL", "small"), compute_type="int8")
    segs, _ = model.transcribe(str(src), word_timestamps=True, vad_filter=True)
    words, sents = [], []
    for s in segs:
        sents.append({"s": s.start, "e": s.end, "t": s.text.strip()})
        words += [{"s": w.start, "e": w.end, "w": w.word.strip()} for w in (s.words or [])]
    return words, sents


def pick_clips(sents, n, lo, hi):
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            tx = "\n".join(f"[{x['s']:.1f}-{x['e']:.1f}] {x['t']}" for x in sents)
            prompt = (
                f"Você é editor de vídeos curtos virais. Do transcript com tempos abaixo, escolha até {n} momentos "
                f"autossuficientes de {lo} a {hi} segundos, com gancho forte no início e fim em frase completa. "
                'Responda SOMENTE com um array JSON: [{"start":0.0,"end":0.0,"title":"","score":0,"reason":""}]. '
                "score de 0 a 100. Título e motivo no idioma do vídeo.\n\n" + tx)
            r = anthropic.Anthropic().messages.create(
                model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=3000,
                messages=[{"role": "user", "content": prompt}])
            arr = json.loads(re.search(r"\[.*\]", r.content[0].text, re.S).group(0))
            out = [c for c in arr if lo * 0.6 <= c["end"] - c["start"] <= hi * 1.3]
            if out:
                return out[:n]
        except Exception as ex:
            print("IA indisponível, usando heurística:", ex)
    cands, i = [], 0
    while i < len(sents):
        j = i
        while j < len(sents) - 1 and sents[j]["e"] - sents[i]["s"] < lo:
            j += 1
        d = sents[j]["e"] - sents[i]["s"]
        if d >= lo * 0.6:
            txt = " ".join(x["t"] for x in sents[i:j + 1])
            cands.append({"start": sents[i]["s"], "end": sents[j]["e"], "title": txt[:60],
                          "score": min(99, int(len(txt.split()) / d * 30)), "reason": "Trecho com fala densa"})
        i = j + 1
    return sorted(cands, key=lambda c: -c["score"])[:n]


def ts(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def make_ass(words, start, end, path):
    ws = [w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05]
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,"
            "StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            "Style: D,DejaVu Sans,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,7,2,2,60,60,520,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    ev = []
    for k in range(0, len(ws), 3):
        chunk = ws[k:k + 3]
        for i, w in enumerate(chunk):
            a = max(w["s"] - start, 0)
            b = (chunk[i + 1]["s"] if i + 1 < len(chunk) else w["e"]) - start
            txt = " ".join((r"{\c&H3DB4FF&}" + x["w"].upper() + r"{\c&HFFFFFF&}") if m == i else x["w"].upper()
                           for m, x in enumerate(chunk))
            ev.append(f"Dialogue: 0,{ts(a)},{ts(max(b, a + 0.05))},D,,0,0,0,,{txt}")
    path.write_text(head + "\n".join(ev), encoding="utf-8")


def render(src, c, words, d, idx, hi):
    s = max(0.0, c["start"]); e = min(c["end"], s + hi)
    make_ass(words, s, e, d / f"clip{idx}.ass")
    sh(["ffmpeg", "-y", "-ss", f"{s:.2f}", "-i", str(src), "-t", f"{e - s:.2f}", "-vf",
        f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles=clip{idx}.ass",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", f"clip{idx}.mp4"], cwd=d)


def process(jid, url, n, lo, hi):
    d = DATA / jid; src = d / "src.mp4"
    try:
        if url:
            set_job(jid, status="Baixando vídeo", progress=5)
            sh(["yt-dlp", "-f", "mp4/bv*+ba/b", "--merge-output-format", "mp4", "-o", str(src), url])
        set_job(jid, status="Transcrevendo (pode demorar)", progress=20)
        words, sents = transcribe(src)
        if not sents:
            raise RuntimeError("Nenhuma fala detectada no vídeo.")
        set_job(jid, status="Escolhendo os melhores momentos", progress=55)
        picks = pick_clips(sents, n, lo, hi)
        clips = []
        for i, c in enumerate(picks, 1):
            set_job(jid, status=f"Renderizando clipe {i} de {len(picks)}", progress=60 + int(35 * i / len(picks)))
            render(src, c, words, d, i, hi)
            clips.append({"url": f"/files/{jid}/clip{i}.mp4", "title": c.get("title", ""),
                          "score": c.get("score", 0), "reason": c.get("reason", ""),
                          "start": round(c["start"]), "end": round(c["end"])})
            set_job(jid, clips=list(clips))
        set_job(jid, status="Pronto", progress=100, done=True)
    except Exception as ex:
        set_job(jid, status="Erro", error=str(ex), done=True)


@app.post("/api/jobs")
async def create(file: Optional[UploadFile] = File(None), url: str = Form(""), clips: int = Form(5),
                 min_len: int = Form(30), max_len: int = Form(60)):
    if not file and not url.strip():
        raise HTTPException(400, "Envie um vídeo ou cole um link.")
    jid = uuid.uuid4().hex[:10]; d = DATA / jid; d.mkdir()
    if file:
        with open(d / "src.mp4", "wb") as f:
            shutil.copyfileobj(file.file, f)
    JOBS[jid] = {"status": "Na fila", "progress": 1, "clips": [], "done": False, "error": None}
    threading.Thread(target=process, args=(jid, "" if file else url.strip(), clips, min_len, max_len),
                     daemon=True).start()
    return {"id": jid}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404, "Trabalho não encontrado.")
    return JOBS[jid]


app.mount("/files", StaticFiles(directory=DATA), name="files")
INDEX = next(p for p in (ROOT / "static" / "index.html", ROOT / "index.html") if p.exists())


@app.get("/")
def home():
    return FileResponse(INDEX)
