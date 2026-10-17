# Recorta — clone do OpusClip

Vídeo longo (upload ou link) → transcrição → IA escolhe os melhores momentos → clipes 9:16 com legendas palavra a palavra.

## Rodar
1. Instale o ffmpeg (`sudo apt install ffmpeg` / `brew install ffmpeg`) e Python 3.10+
2. `pip install -r requirements.txt`
3. (Recomendado) `export ANTHROPIC_API_KEY=sua_chave` — sem ela, usa uma heurística simples
4. `uvicorn app:app --port 8000` e abra http://localhost:8000

Variáveis opcionais: `WHISPER_MODEL` (tiny/base/small/medium, padrão small), `CLAUDE_MODEL` (padrão claude-sonnet-5-5).
Os clipes ficam em `data/<id>/clipN.mp4`.

## Deploy (Docker)
`docker build -t recorta . && docker run -p 8000:8000 -e ANTHROPIC_API_KEY=... recorta`
Em Railway, Render (plano pago) ou Fly.io: conecte o repositório, ele detecta o Dockerfile. Use 2 GB+ de RAM.
