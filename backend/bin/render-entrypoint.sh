#!/usr/bin/env bash
# Container entrypoint for Render deployments.
#
# Render gives every web service a single port ($PORT) and, on the free plan,
# no way to run a background worker. The API needs an ARQ worker to pick up
# processing jobs, so by default the worker runs inside this container next to
# uvicorn. Set RUN_WORKER_INLINE=false and deploy the worker service when you
# move to a paid instance type.
set -euo pipefail

PORT="${PORT:-8000}"
cd /app

worker_pid=""

start_worker() {
  if [ "${RUN_WORKER_INLINE:-true}" != "true" ]; then
    echo "[entrypoint] RUN_WORKER_INLINE is off; skipping in-container worker"
    return
  fi

  echo "[entrypoint] starting ARQ worker in the background"
  .venv/bin/arq src.workers.tasks.WorkerSettings &
  worker_pid=$!

  # Surface an early worker crash instead of failing silently later.
  sleep 2
  if ! kill -0 "$worker_pid" 2>/dev/null; then
    echo "[entrypoint] WARNING: ARQ worker exited during startup; continuing with the API only"
    worker_pid=""
  fi
}

shutdown() {
  if [ -n "$worker_pid" ] && kill -0 "$worker_pid" 2>/dev/null; then
    echo "[entrypoint] stopping ARQ worker"
    kill -TERM "$worker_pid" 2>/dev/null || true
  fi
}
trap shutdown EXIT TERM INT

start_worker

echo "[entrypoint] starting uvicorn on 0.0.0.0:${PORT}"
exec .venv/bin/uvicorn src.main:app --host 0.0.0.0 --port "${PORT}" --proxy-headers --forwarded-allow-ips="*"
