FROM brainicism/bgutil-ytdlp-pot-provider:2.0.0-deno AS pot_provider
FROM denoland/deno:bin-2.9.6 AS deno

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TEMP_DIR=/app/tmp \
    OUTPUT_DIR=/app/output \
    DENO_DIR=/app/.deno_cache \
    YT_DLP_BGUTIL_SERVER_HOME=/opt/bgutil-provider \
    YT_DLP_WPC_BROWSER_PATH=/usr/bin/chromium \
    PDF_FONT_REGULAR=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
    PDF_FONT_BOLD=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf

COPY --from=deno /deno /usr/local/bin/deno
COPY --from=pot_provider /app /opt/bgutil-provider

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg fonts-dejavu-core chromium xauth xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt
COPY . .

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/tmp /app/output /app/transcript_cache /app/.deno_cache \
    && chown -R bot:bot /app /opt/bgutil-provider
USER bot

CMD ["python", "phase12_entrypoint.py"]
