FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=core.settings.production \
    PORT=8000 \
    REVENUE_ENGINE_DRY_RUN=true

WORKDIR /app

RUN groupadd --system qasedak \
    && useradd --system --gid qasedak --home-dir /app qasedak

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt

COPY . /app
RUN chmod +x /app/docker/entrypoint.sh /app/docker/start-web.sh /app/docker/start-bot.sh /app/docker/start-worker.sh \
    && mkdir -p /app/static_root /app/media \
    && chown -R qasedak:qasedak /app

USER qasedak

EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
