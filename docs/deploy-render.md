# Deploying SupoClip on Render

SupoClip is two services plus two datastores:

| Resource | What it is | Render type |
|---|---|---|
| `supoclip-web` | Next.js app (UI, auth, billing, request proxying) | Web service (Node) |
| `supoclip-backend` | FastAPI API + ARQ worker (ffmpeg rendering) | Web service (Docker, `backend/render.Dockerfile`) |
| `supoclip-db` | Postgres for both components | Render Postgres |
| `supoclip-redis` | Queue/SSE backend for ARQ | Render Key Value |

The browser only talks to `supoclip-web`; its route handlers forward to the API
with signed headers (`BACKEND_AUTH_SECRET`), so the API does not need to be
public for the UI to work.

Two components own tables named `users`, `tasks` and `sources`, so on a shared
database the backend keeps its models in the `supoclip_backend` schema
(`DB_SCHEMA`) while the Prisma migrations stay in `public`.

## Option A — automated provisioning (recommended)

`render.yaml` is a blueprint, but this repository also ships an idempotent
provisioner that creates and wires everything through the Render API:

- `.render-ops/deploy.py` — the provisioner (creates Postgres, Key Value, both
  services, all environment variables, then deploys and smoke-tests the result)
- `.github/workflows/render-deploy.yml` — runs it on GitHub Actions, because
  the Render API is not reachable from every network. It publishes its report to
  the `render-state` branch under `state/report.json`.

### 1. Create a Render API key

Render dashboard → **Account Settings → API Keys → Create API Key**.

### 2. Give the key to the workflow

Preferred: repository **Settings → Secrets and variables → Actions → New
repository secret** named `RENDER_API_KEY`. Add any provider keys you use
(`OLLAMA_API_KEY`, `ASSEMBLY_AI_API_KEY`, `PEXELS_API_KEY`, ...) the same way.

Throwaway alternative: put the key in `.render-ops/key.txt` and force-add it.
**Do not commit a key you intend to keep** — anything in a public repository is
public. Rotate the key in the dashboard once the deployment is finished.

```bash
printf '%s' "$RENDER_API_KEY" | tr -d '\n' > .render-ops/key.txt
git add -f .render-ops/key.txt
git commit -m "chore: temporary Render key for provisioning"
git push
```

### 3. Choose the deployment shape

Edit `.render-ops/request.json` and push. Any push touching it re-runs the
workflow, and re-runs are safe: existing resources are reused, not duplicated.

```json
{
  "mode": "probe",
  "region": "oregon",
  "plan": "free",
  "llm": "ollama:gpt-oss:120b",
  "rerank_llm": "ollama:gpt-oss:20b",
  "ollama_base_url": "https://ollama.com/v1",
  "transcription_provider": "whisper",
  "whisper_model": "base",
  "poll_timeout_seconds": 2700
}
```

`"mode": "probe"` only reads (workspace, existing services and databases) and is
a good first check. `"mode": "deploy"` creates and deploys everything.

### 4. Read the result

The workflow publishes `state/report.json` to the `render-state` branch:

```bash
git fetch origin render-state
git show FETCH_HEAD:state/report.json | head -60
```

It contains the service URLs, deploy statuses, health-check results, provider
(`Ollama`) probe results, and the full Render API call log — enough to debug a
failed build without opening the dashboard.

## Option B — blueprint from the dashboard

Render dashboard → **New → Blueprint** → pick this repository. Render reads
`render.yaml` and asks for the values marked `sync: false`:

| Variable | Where the value comes from |
|---|---|
| `CORS_ORIGINS`, `NEXT_PUBLIC_APP_URL` (backend) | public URL of `supoclip-web` once created |
| `BACKEND_INTERNAL_URL`, `NEXT_PUBLIC_API_URL` (frontend) | public URL of `supoclip-backend` |
| `NEXT_PUBLIC_APP_URL`, `BETTER_AUTH_URL`, `BETTER_AUTH_TRUSTED_ORIGINS` | public URL of `supoclip-web` |
| `OLLAMA_API_KEY`, `ASSEMBLY_AI_API_KEY` | your provider keys |

A blueprint cannot compute `https://<service>.onrender.com` for another service,
which is why those four values are prompted instead of derived.

## Environment variables that matter

Everything in [configuration.md](configuration.md) applies. The ones that are
specific to a split frontend/backend deployment:

| Variable | Service | Purpose |
|---|---|---|
| `DATABASE_URL` | both | Render's `postgres://` string; the backend rewrites it for asyncpg |
| `DB_SCHEMA` | backend | schema for the API models when the database is shared (`supoclip_backend`) |
| `REDIS_HOST` / `REDIS_PORT` | backend | Render Key Value connection |
| `BACKEND_INTERNAL_URL` | frontend | API base URL used by the Next.js route handlers |
| `BACKEND_AUTH_SECRET` | both | signs frontend→backend requests; must match |
| `APP_SETTINGS_ENCRYPTION_KEY` | both | encrypts admin-managed provider keys stored in Postgres |
| `BETTER_AUTH_SECRET`, `BETTER_AUTH_URL` | frontend | session signing and public URL |
| `CORS_ORIGINS` | backend | allowed browser origins (the frontend URL) |
| `RUN_WORKER_INLINE` | backend | `true` runs the ARQ worker inside the API container |
| `LLM`, `OLLAMA_BASE_URL`, `OLLAMA_API_KEY` | backend | clip selection model |
| `TRANSCRIPTION_PROVIDER` | backend | `whisper` (local, no key) or `assemblyai` (fast, needs key) |

## Free plan expectations

- Web services sleep after 15 minutes of inactivity and wake in ~1 minute.
- 0.5 GB RAM / 0.1 CPU per service. Whisper `base` on CPU is slow and long
  videos may run out of memory; add `ASSEMBLY_AI_API_KEY` and set
  `TRANSCRIPTION_PROVIDER=assemblyai` for realistic throughput.
- Container storage is ephemeral, so uploaded sources and rendered clips are
  lost on every restart or deploy. For durable media, set `MEDIA_STORAGE=s3`
  with the `MEDIA_S3_*` variables.
- Free Postgres/Key Value instances are time-limited by Render; check the
  dashboard for the expiry of yours.

To move to a paid instance: upgrade the services, add a persistent disk mounted
at `/app/uploads` and `/app/clips`, and (optionally) deploy
`backend/render.Dockerfile` a second time as a **background worker** with the
start command `.venv/bin/arq src.workers.tasks.WorkerSettings` and
`RUN_WORKER_INLINE=false` on the API service.

## After the first deploy

1. Open the `supoclip-web` URL and create an account (sign-up is enabled by
   `DISABLE_SIGN_UP=false`).
2. Paste a YouTube link and generate a clip. The first request wakes both
   sleeping services, so give it a minute.
3. Check `https://<supoclip-backend>.onrender.com/health`, `/health/db`,
   `/health/redis` and `/health/worker` — all four should report healthy.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Build fails on `uv sync --frozen` | `backend/uv.lock` is out of date with `pyproject.toml`; run `uv lock` locally |
| Next.js build fails on Prisma | `pnpm exec prisma generate` must run before `next build`; the `postinstall` script handles it when `pnpm install` succeeds |
| Sign-in loops or 401s from the API | `BACKEND_AUTH_SECRET` differs between the two services |
| `prisma migrate deploy` fails at start | `DATABASE_URL` is missing or the database is still provisioning |
| Jobs stay queued forever | the worker is not running: `RUN_WORKER_INLINE=true`, or a worker service that shares `DATABASE_URL`/`REDIS_*` |
| `/health/worker` is unhealthy but jobs finish | the inline worker was restarted; the heartbeat expires after ~1 minute of the worker being down |
