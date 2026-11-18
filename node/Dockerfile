FROM python:3.12-slim-trixie AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /opt/aster
RUN groupadd -g 10001 aster && useradd -u 10001 -g aster -M aster

FROM base AS browser
RUN apt-get update && apt-get install -y --no-install-recommends chromium-headless-shell fonts-liberation fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-browser.txt .
RUN pip install --no-cache-dir -r requirements-browser.txt
COPY aster/__init__.py aster/browser.py ./aster/
USER aster
CMD ["python", "-m", "uvicorn", "aster.browser:app", "--host", "0.0.0.0", "--port", "8010", "--no-access-log"]

FROM base AS app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY aster ./aster
RUN install -d -m 0750 -o aster -g aster /data /workspace \
    && touch /data/.initialized /workspace/.initialized \
    && chown aster:aster /data/.initialized /workspace/.initialized
USER aster
CMD ["python", "-m", "uvicorn", "aster.server:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--limit-concurrency", "32"]
