# Pocketful API - Stage 2
#
# Spec section 1: Python 3.11+, FastAPI, in-memory storage only, no external
# database. Password hashing via bcrypt (see app/security.py).
#
# D1: the image is self-contained. Everything is installed at BUILD time, so
# the running container needs no network at all - `docker run --network none`
# must serve /health. There is deliberately no runtime package fetch, no
# database, and no outbound call anywhere in app/.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Dependencies first so edits to app/ do not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY verify_stage1.py ./
COPY RUN.md ./

EXPOSE 8080

# Runtime PORT-aware: the shell expands ${PORT} at container start, the app
# reads $PORT with a default of 8080.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if b'\"ok\"' in urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health',timeout=2).read() else 1)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
