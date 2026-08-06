FROM python:3.12-slim

ARG GIT_COMMIT=
ENV GIT_COMMIT=${GIT_COMMIT:-unknown}

ARG BUILD_TIME=
ENV BUILD_TIME=${BUILD_TIME:-unknown}

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app/ /app/app/
COPY tidalwave.yaml /app/config/tidalwave.yaml
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /app/downloads /app/data /app/.config/tidal_dl_ng /app/import_staging

EXPOSE 19290

CMD ["/app/entrypoint.sh"]
