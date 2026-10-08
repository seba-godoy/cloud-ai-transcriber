FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY dashboard/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && useradd --create-home --uid 10001 dashboard

COPY dashboard_app.py /app/dashboard_app.py
COPY dashboard/index.html /app/dashboard/index.html
RUN chown -R dashboard:dashboard /app

USER dashboard

CMD ["sh", "-c", "exec gunicorn --bind :${PORT} --workers 2 --threads 4 --timeout 30 dashboard_app:app"]
