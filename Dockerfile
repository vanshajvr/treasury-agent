# Demo server only: replays the scenario offline. No Airwallex keys belong in this image or its environment.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 PORT=8000 TRUST_PROXY=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir requests python-dotenv

COPY server.py scenario.json policy.json evidence.json ./
COPY treasury/ treasury/
COPY web/ web/

RUN useradd --system --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health',timeout=2)"
CMD ["python", "server.py"]
