FROM python:3.12-slim-trixie
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 INSTALLER_STATE=/state
WORKDIR /app
RUN groupadd -g 10001 installer && useradd -u 10001 -g installer -M installer \
    && install -d -m 0700 -o installer -g installer /state \
    && touch /state/.initialized && chown installer:installer /state/.initialized
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY installer ./installer
COPY node ./node
USER installer
CMD ["python", "-m", "installer"]
