# Qasedak Docker Runtime

Build the reusable runtime image from the repository root:

```sh
docker build -t qasedak-core:latest .
```

Each container is one isolated customer tenant. Provide secrets and tenant config at runtime with a per-container env file, never in the image:

```sh
docker run -d --name qasedak-tenant-1 --add-host host.docker.internal:host-gateway --env-file tenants/tenant-1.env -p 127.0.0.1:8001:8000 qasedak-core:latest
docker run -d --name qasedak-tenant-2 --add-host host.docker.internal:host-gateway --env-file tenants/tenant-2.env -p 127.0.0.1:8002:8000 qasedak-core:latest
docker run -d --name qasedak-tenant-3 --add-host host.docker.internal:host-gateway --env-file tenants/tenant-3.env -p 127.0.0.1:8003:8000 qasedak-core:latest
```

Required runtime env:

- `TENANT_ID`
- `DJANGO_SECRET_KEY`
- `DATABASE_URL` or PostgreSQL vars (`DATABASE_ENGINE=postgres`, `POSTGRES_HOST`, `POSTGRES_PASSWORD`, optional `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PORT`)
- `TELEGRAM_BOT_TOKEN`
- `REVENUE_ENGINE_DRY_RUN=true` by default
- `QASEDAK_BOT_ENABLED=false` during safe first deploys; enable only after owner smoke checks
- `QASEDAK_WORKER_ENABLED=false` during safe first deploys; enable only when revenue sending is intentionally configured
- `PORT=8000` by default
- `DOMAIN` optional

Startup flow:

- load `/app/.env` or `ENV_FILE`
- run migrations
- collect static files
- start `python manage.py run_bot`
- start the background worker
- start gunicorn

Health check:

```sh
curl http://127.0.0.1:8001/health/
```

The health endpoint checks Django and database reachability and reports bot configuration state without calling Telegram or X-UI.
