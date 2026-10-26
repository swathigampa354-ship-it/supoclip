#!/usr/bin/env python3
"""Provision SupoClip on Render (https://render.com).

This script talks to the Render REST API and is meant to run from GitHub
Actions, because the Render API is not reachable from every environment.
It is idempotent: resources are looked up by name first, so re-running it
reconciles an existing deployment instead of creating duplicates.

Inputs
------
`.render-ops/request.json`   {"mode": "probe"|"deploy", "region": "...", "plan": "..."}
`RENDER_API_KEY` (env)       Render API key. Falls back to `.render-ops/key.txt`.
`.render-ops/secrets.env`    extra KEY=VALUE pairs pushed to the Render services
                             (LLM keys, transcription keys, ...).

Outputs
-------
`.render-ops/state/*.json`   machine-readable report, published to the
                             `render-state` branch by the workflow.

Nothing secret is ever printed: log lines only mention variable *names*.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import time
import traceback

import requests

API = "https://api.render.com/v1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS_DIR = os.path.join(ROOT, ".render-ops")
STATE_DIR = os.path.join(OPS_DIR, "state")

# --- Deployment shape -------------------------------------------------------
REPO_URL = os.environ.get("SUPOCLIP_REPO_URL", "https://github.com/swathigampa354-ship-it/supoclip")
BRANCH = os.environ.get("SUPOCLIP_BRANCH", "arena/2e6027b7-supoclip")
BACKEND_NAME = os.environ.get("SUPOCLIP_BACKEND_NAME", "supoclip-backend")
FRONTEND_NAME = os.environ.get("SUPOCLIP_FRONTEND_NAME", "supoclip-web")
POSTGRES_NAME = os.environ.get("SUPOCLIP_POSTGRES_NAME", "supoclip-db")
REDIS_NAME = os.environ.get("SUPOCLIP_REDIS_NAME", "supoclip-redis")
# Both components own tables named users/tasks/sources, so the backend keeps
# its models in a dedicated schema of the shared free Postgres instance.
BACKEND_DB_SCHEMA = os.environ.get("SUPOCLIP_BACKEND_DB_SCHEMA", "supoclip_backend")

DEFAULT_REQUEST = {
    "mode": "deploy",
    "region": "oregon",
    "plan": "free",
    "llm": "ollama:gpt-oss:120b",
    "rerank_llm": "ollama:gpt-oss:20b",
    "ollama_base_url": "https://ollama.com/v1",
    "transcription_provider": "whisper",
    "whisper_model": "base",
    "poll_timeout_seconds": 2700,
}

# Provider keys that may be supplied through GitHub Actions secrets.
SECRET_ENV_NAMES = (
    "OLLAMA_API_KEY",
    "ASSEMBLY_AI_API_KEY",
    "PEXELS_API_KEY",
    "APIFY_API_TOKEN",
    "YOUTUBE_DATA_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "BACKEND_AUTH_SECRET",
    "APP_SETTINGS_ENCRYPTION_KEY",
    "BETTER_AUTH_SECRET",
)

_SECRETS: list[str] = []


# --- small helpers ----------------------------------------------------------
def log(message: str) -> None:
    print(message, flush=True)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret and len(secret) > 6:
            text = text.replace(secret, "<redacted>")
    return text


def load_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def random_secret(length: int = 48) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets_choice(alphabet) for _ in range(length))


def secrets_choice(alphabet: str) -> str:
    # Tiny indirection so the module-level name does not shadow `secrets`.
    import secrets as _secrets_module

    return _secrets_module.choice(alphabet)


def read_request() -> dict:
    path = os.path.join(OPS_DIR, "request.json")
    request = dict(DEFAULT_REQUEST)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            request.update(json.load(handle))
    request["mode"] = os.environ.get("RENDER_MODE", request.get("mode", "deploy"))
    request["region"] = os.environ.get("RENDER_REGION", request.get("region", "oregon"))
    request["plan"] = os.environ.get("RENDER_PLAN", request.get("plan", "free"))
    return request


def unwrap(payload, key: str):
    """Render wraps single objects as {"<key>": {...}} in some responses."""
    if isinstance(payload, dict) and key in payload and isinstance(payload[key], dict):
        return payload[key]
    return payload


def unwrap_list(payload, key: str) -> list:
    if not isinstance(payload, list):
        if isinstance(payload, dict):
            for candidate in (key, "items", "data"):
                if isinstance(payload.get(candidate), list):
                    return [unwrap(item, key) for item in payload[candidate]]
            return []
        return []
    return [unwrap(item, key) for item in payload]


class RenderClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.calls: list[dict] = []

    def request(self, method: str, path: str, *, expect=(200, 201, 202), **kwargs):
        url = path if path.startswith("http") else f"{API}{path}"
        label = path if not path.startswith("http") else "<absolute-url>"
        try:
            response = self.session.request(method, url, timeout=120, **kwargs)
        except Exception as exc:  # network level failure
            self.calls.append({"request": f"{method} {label}", "status": "network-error", "detail": str(exc)})
            log(f"  ! {method} {label} failed: {exc}")
            raise

        try:
            body = response.json()
        except ValueError:
            body = response.text[:4000]

        self.calls.append(
            {
                "request": f"{method} {label}",
                "status": response.status_code,
                "response": body if not isinstance(body, str) else body[:2000],
            }
        )
        if response.status_code not in expect:
            log(f"  ! {method} {label} -> {response.status_code}: {json.dumps(body)[:800] if not isinstance(body, str) else body[:800]}")
        else:
            log(f"  · {method} {label} -> {response.status_code}")
        return response.status_code, body


# --- Render resources -------------------------------------------------------
def pick_owner(client: RenderClient) -> tuple[str, str]:
    status, body = client.request("GET", "/owners?limit=20")
    if status != 200:
        raise RuntimeError(f"cannot list Render owners (HTTP {status})")
    owners = unwrap_list(body, "owner")
    if not owners:
        raise RuntimeError("the Render API key is not attached to any workspace")
    owner = owners[0]
    log(f"workspace: {owner.get('name')} ({owner.get('id')})")
    return owner["id"], owner.get("name", "")


def list_services(client: RenderClient, owner_id: str) -> list[dict]:
    _, body = client.request("GET", f"/services?limit=100&ownerId={owner_id}")
    return unwrap_list(body, "service")


def list_postgres(client: RenderClient, owner_id: str) -> list[dict]:
    _, body = client.request("GET", f"/postgres?limit=100&ownerId={owner_id}")
    return unwrap_list(body, "postgres")


def list_keyvalue(client: RenderClient, owner_id: str) -> list[dict]:
    for path in (f"/key-value?limit=100&ownerId={owner_id}", f"/redis?limit=100&ownerId={owner_id}"):
        status, body = client.request("GET", path, expect=(200, 404, 405))
        if status == 200:
            return unwrap_list(body, "keyValue") or unwrap_list(body, "redis")
    return []


def ensure_postgres(client: RenderClient, owner_id: str, request: dict) -> dict:
    existing = list_postgres(client, owner_id)
    for database in existing:
        if database.get("name") == POSTGRES_NAME:
            log(f"postgres {POSTGRES_NAME} already exists ({database.get('id')})")
            return database

    log(f"creating postgres {POSTGRES_NAME} in {request['region']} on plan {request['plan']} (this takes a few minutes)")
    payload = {
        "ownerId": owner_id,
        "name": POSTGRES_NAME,
        "plan": request["plan"],
        "region": request["region"],
        "version": "16",
        "databaseName": "supoclip",
        "databaseUser": "supoclip",
    }
    status, body = client.request("POST", "/postgres", json=payload)
    if status not in (200, 201, 202):
        raise RuntimeError(f"postgres creation failed (HTTP {status}): {json.dumps(body)[:600]}")
    return unwrap(body, "postgres")


def wait_postgres_available(client: RenderClient, database_id: str, timeout: int = 900) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = client.request("GET", f"/postgres/{database_id}", expect=(200,))
        database = unwrap(body, "postgres")
        state = (database or {}).get("status")
        log(f"  postgres status: {state}")
        if state in ("available", "ready"):
            return database
        if state in ("creation_failed", "unavailable"):
            raise RuntimeError(f"postgres entered state {state}")
        time.sleep(20)
    raise RuntimeError("timed out waiting for Postgres to become available")


def postgres_connection_info(client: RenderClient, database_id: str) -> dict:
    _, body = client.request("GET", f"/postgres/{database_id}/connection-info")
    info = unwrap(body, "connectionInfo")
    if isinstance(info, dict) and "internalConnectionString" not in info and isinstance(info.get("connectionInfo"), dict):
        info = info["connectionInfo"]
    return info or {}


def ensure_key_value(client: RenderClient, owner_id: str, request: dict) -> dict:
    existing = list_keyvalue(client, owner_id)
    for instance in existing:
        if instance.get("name") == REDIS_NAME:
            log(f"key value {REDIS_NAME} already exists ({instance.get('id')})")
            return instance

    serialized = {"ownerId": owner_id, "name": REDIS_NAME, "plan": request["plan"], "region": request["region"]}
    for path, body_payload in (
        ("/key-value", {**serialized, "maxmemoryPolicy": "noeviction"}),
        ("/redis", {**serialized, "maxmemoryPolicy": "noeviction"}),
    ):
        status, body = client.request("POST", path, json=body_payload, expect=(200, 201, 202, 404, 405, 400))
        if status in (200, 201, 202):
            return unwrap(unwrap(body, "keyValue"), "redis")
    raise RuntimeError("could not create a Render Key Value (Redis) instance")


def wait_key_value_available(client: RenderClient, instance_id: str, timeout: int = 600) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = client.request("GET", f"/key-value/{instance_id}", expect=(200, 404))
        if status == 404:
            status, body = client.request("GET", f"/redis/{instance_id}", expect=(200,))
        instance = unwrap(unwrap(body, "keyValue"), "redis")
        state = (instance or {}).get("status")
        log(f"  key value status: {state}")
        if state in ("available", "ready"):
            return instance
        if state in ("creation_failed", "unavailable"):
            raise RuntimeError(f"key value entered state {state}")
        time.sleep(15)
    raise RuntimeError("timed out waiting for Key Value to become available")


def key_value_connection_info(client: RenderClient, instance_id: str) -> dict:
    for path in (f"/key-value/{instance_id}/connection-info", f"/redis/{instance_id}/connection-info"):
        status, body = client.request("GET", path, expect=(200, 404))
        if status == 200:
            info = unwrap(body, "connectionInfo")
            if isinstance(info, dict) and "internalConnectionString" not in info and isinstance(info.get("connectionInfo"), dict):
                info = info["connectionInfo"]
            return info or {}
    return {}


def find_service(services: list[dict], name: str) -> dict | None:
    for service in services:
        if service.get("name") == name:
            return service
    return None


def create_web_service(client: RenderClient, owner_id: str, request: dict, name: str, *, env: str,
                       root_dir: str, dockerfile: str | None, build_command: str | None,
                       start_command: str | None, health_check: str, env_vars: list[dict]) -> dict:
    details: dict = {
        "env": env,
        "plan": request["plan"],
        "region": request["region"],
        "healthCheckPath": health_check,
        "envSpecificDetails": {},
    }
    if env == "docker":
        details["envSpecificDetails"] = {
            "dockerfilePath": dockerfile,
            "dockerContext": ".",
        }
    else:
        details["envSpecificDetails"] = {
            "buildCommand": build_command,
            "startCommand": start_command,
        }

    payload = {
        "type": "web_service",
        "name": name,
        "ownerId": owner_id,
        "repo": REPO_URL,
        "branch": BRANCH,
        "rootDir": root_dir,
        "autoDeploy": "no",
        "envVars": env_vars,
        "serviceDetails": details,
    }
    status, body = client.request("POST", "/services", json=payload)
    if status not in (200, 201, 202):
        raise RuntimeError(f"creating service {name} failed (HTTP {status}): {json.dumps(body)[:800]}")
    service = unwrap(body, "service")
    log(f"created service {name} ({service.get('id')}) -> {service_url(service)}")
    return service


def service_url(service: dict) -> str:
    details = (service or {}).get("serviceDetails") or {}
    url = details.get("url") or service.get("url") or ""
    if url and not url.startswith("http"):
        url = f"https://{url}"
    return url.rstrip("/")


def set_env_vars(client: RenderClient, service_id: str, env_vars: list[dict]) -> None:
    payload = [{"key": item["key"], "value": item["value"]} for item in env_vars]
    client.request("PUT", f"/services/{service_id}/env-vars", json=payload)


def trigger_deploy(client: RenderClient, service_id: str) -> str | None:
    status, body = client.request("POST", f"/services/{service_id}/deploys", json={"clearCache": "clear"})
    if status not in (200, 201, 202):
        return None
    deploy = unwrap(body, "deploy")
    return (deploy or {}).get("id")


def latest_deploy(client: RenderClient, service_id: str) -> dict | None:
    status, body = client.request("GET", f"/services/{service_id}/deploys?limit=1", expect=(200, 404))
    if status != 200:
        return None
    deploys = unwrap_list(body, "deploy")
    return deploys[0] if deploys else None


def wait_for_deploy(client: RenderClient, service_id: str, name: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        deploy = latest_deploy(client, service_id)
        if deploy:
            state = deploy.get("status")
            if state != last_status:
                log(f"  {name} deploy status: {state}")
                last_status = state
            if state in ("live", "build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"):
                return deploy
        time.sleep(25)
    return {"status": "timeout", "service": name}


def service_events(client: RenderClient, service_id: str, limit: int = 60) -> list[dict]:
    status, body = client.request("GET", f"/services/{service_id}/events?limit={limit}", expect=(200, 400, 404))
    if status != 200:
        return []
    events = unwrap_list(body, "event")
    return events[:limit]


# --- HTTP smoke tests -------------------------------------------------------
def http_probe(url: str, *, attempts: int = 4, timeout: int = 90) -> dict:
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, timeout=timeout, allow_redirects=False)
            return {"url": url, "status": response.status_code, "body": response.text[:400]}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(10)
    return {"url": url, "status": None, "error": last_error}


def probe_ollama(base_url: str, api_key: str, models: list[str]) -> dict:
    report: dict = {"base_url": base_url, "models": {}}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    for url in (f"{root}/api/tags", f"{base_url.rstrip('/')}/models"):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            report.setdefault("list", {})[url] = {"status": response.status_code, "body": response.text[:1500]}
        except Exception as exc:
            report.setdefault("list", {})[url] = {"error": f"{type(exc).__name__}: {exc}"}

    for model in models:
        try:
            response = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                    "max_tokens": 8,
                },
                timeout=60,
            )
            report["models"][model] = {"status": response.status_code, "body": response.text[:400]}
        except Exception as exc:
            report["models"][model] = {"error": f"{type(exc).__name__}: {exc}"}
    return report


# --- state publishing -------------------------------------------------------
def write_state(name: str, payload) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, name), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)


def publish_state(tag: str) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo or not os.path.isdir(STATE_DIR):
        log("state publishing skipped (no token/repository in env)")
        return

    workdir = tempfile.mkdtemp()
    try:
        target = os.path.join(workdir, "state")
        shutil.copytree(STATE_DIR, target)
        url = f"https://x-access-token:{token}@github.com/{repo}.git"
        commands = [
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "-c", "user.email=render-ops@users.noreply.github.com",
             "-c", "user.name=render-ops", "commit", "-qm", f"state: {tag}"],
            ["git", "push", "-qf", url, "HEAD:refs/heads/render-state"],
        ]
        for command in commands:
            result = subprocess.run(command, cwd=workdir, capture_output=True, text=True)
            if result.returncode != 0:
                log(f"  state push step failed ({command[1:3]}): {result.stderr.strip()[:300]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --- main -------------------------------------------------------------------
def main() -> int:
    started = time.time()
    request = read_request()
    mode = request["mode"]

    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    if not api_key:
        key_path = os.path.join(OPS_DIR, "key.txt")
        if os.path.exists(key_path):
            api_key = open(key_path, "r", encoding="utf-8").read().strip()
    if not api_key:
        log("ERROR: no Render API key (set RENDER_API_KEY or .render-ops/key.txt)")
        return 2
    _SECRETS.append(api_key)

    # Provider keys can come from repository secrets (preferred) or from the
    # throwaway `.render-ops/secrets.env` file.
    injected = load_env_file(os.path.join(OPS_DIR, "secrets.env"))
    for env_name in SECRET_ENV_NAMES:
        if os.environ.get(env_name):
            injected[env_name] = os.environ[env_name]
    _SECRETS.extend(value for value in injected.values() if value)

    log(f"SupoClip -> Render | mode={mode} region={request['region']} plan={request['plan']} branch={BRANCH}")

    client = RenderClient(api_key)
    report: dict = {"mode": mode, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": {}}

    try:
        owner_id, workspace = pick_owner(client)
        report["workspace"] = {"id": owner_id, "name": workspace}

        services = list_services(client, owner_id)
        report["existing_services"] = [
            {"id": s.get("id"), "name": s.get("name"), "type": s.get("type"),
             "url": service_url(s), "suspended": s.get("suspended")}
            for s in services
        ]
        postgres_instances = list_postgres(client, owner_id)
        report["existing_postgres"] = [
            {"id": d.get("id"), "name": d.get("name"), "plan": d.get("plan"), "status": d.get("status")}
            for d in postgres_instances
        ]
        key_value_instances = list_keyvalue(client, owner_id)
        report["existing_key_value"] = [
            {"id": i.get("id"), "name": i.get("name"), "plan": i.get("plan"), "status": i.get("status")}
            for i in key_value_instances
        ]

        if mode == "probe":
            report["ok"] = True
            report["api_calls"] = client.calls
            write_state("report.json", report)
            publish_state("probe")
            log("probe complete")
            return 0

        # 1. Data stores -----------------------------------------------------
        log("\n[1/5] Postgres")
        database = ensure_postgres(client, owner_id, request)
        database = wait_postgres_available(client, database["id"])
        connection = postgres_connection_info(client, database["id"])
        internal_db_url = connection.get("internalConnectionString", "")
        if not internal_db_url:
            raise RuntimeError("Render did not return an internal Postgres connection string")
        report["steps"]["postgres"] = {
            "id": database.get("id"),
            "name": database.get("name"),
            "plan": (database.get("postgres") or database).get("plan"),
            "status": database.get("status"),
            "database": database.get("databaseName"),
            "region": database.get("region"),
        }

        log("\n[2/5] Key Value (Redis)")
        key_value = ensure_key_value(client, owner_id, request)
        key_value = wait_key_value_available(client, key_value["id"])
        redis_info = key_value_connection_info(client, key_value["id"])
        redis_url = redis_info.get("internalConnectionString", "")
        if not redis_url:
            raise RuntimeError("Render did not return an internal Key Value connection string")
        redis_parts = urllib_parse(redis_url)
        report["steps"]["key_value"] = {
            "id": key_value.get("id"),
            "name": key_value.get("name"),
            "plan": (key_value.get("keyValue") or key_value).get("plan"),
            "status": key_value.get("status"),
            "host": redis_parts["host"],
        }

        # 2. Shared secrets --------------------------------------------------
        shared = {
            "BACKEND_AUTH_SECRET": injected.get("BACKEND_AUTH_SECRET") or random_secret(),
            "APP_SETTINGS_ENCRYPTION_KEY": injected.get("APP_SETTINGS_ENCRYPTION_KEY") or random_secret(),
            "BETTER_AUTH_SECRET": injected.get("BETTER_AUTH_SECRET") or random_secret(),
        }
        _SECRETS.extend(shared.values())

        # The backend normalises postgres:// to the asyncpg driver itself, so
        # both services can take the connection string Render hands out.
        backend_db_url = internal_db_url
        prisma_db_url = internal_db_url.replace("postgres://", "postgresql://", 1)

        # 3. Services --------------------------------------------------------
        log("\n[3/5] Services")
        services = list_services(client, owner_id)

        backend = find_service(services, BACKEND_NAME)
        if backend:
            log(f"service {BACKEND_NAME} already exists ({backend.get('id')})")
        else:
            backend = create_web_service(
                client, owner_id, request, BACKEND_NAME,
                env="docker", root_dir="backend", dockerfile="./render.Dockerfile",
                build_command=None, start_command=None, health_check="/health", env_vars=[],
            )
        backend_url = service_url(backend)

        frontend = find_service(services, FRONTEND_NAME)
        if frontend:
            log(f"service {FRONTEND_NAME} already exists ({frontend.get('id')})")
        else:
            frontend = create_web_service(
                client, owner_id, request, FRONTEND_NAME,
                env="node", root_dir="frontend", dockerfile=None,
                build_command="npm install -g pnpm@10.27.0 && pnpm install --frozen-lockfile && pnpm run build",
                start_command="pnpm exec prisma migrate deploy && pnpm exec next start -p $PORT",
                health_check="/", env_vars=[],
            )
        frontend_url = service_url(frontend)
        log(f"backend  -> {backend_url}")
        log(f"frontend -> {frontend_url}")

        # 4. Environment -----------------------------------------------------
        log("\n[4/5] Environment variables")
        backend_env = {
            "PYTHONPATH": "/app",
            "PYTHONUNBUFFERED": "1",
            "SUPOCLIP_SERVICE": "backend",
            "SELF_HOST": "true",
            "RUN_WORKER_INLINE": "true",
            "DATABASE_URL": backend_db_url,
            "DB_SCHEMA": BACKEND_DB_SCHEMA,
            "REDIS_HOST": redis_parts["host"],
            "REDIS_PORT": redis_parts["port"],
            "CORS_ORIGINS": frontend_url,
            "NEXT_PUBLIC_APP_URL": frontend_url,
            "TEMP_DIR": "/app/uploads",
            "MEDIA_STORAGE": "local",
            "LLM": request["llm"],
            "RERANK_LLM": request["rerank_llm"],
            "OLLAMA_BASE_URL": request["ollama_base_url"],
            "TRANSCRIPTION_PROVIDER": request["transcription_provider"],
            "WHISPER_MODEL_SIZE": request["whisper_model"],
            "MAX_CLIPS": "10",
            "MAX_VIDEO_DURATION": "5400",
            "AUTH_SIGNATURE_TTL_SECONDS": "300",
            "QUEUED_TASK_TIMEOUT_SECONDS": "180",
            **shared,
        }
        if redis_parts["password"]:
            backend_env["REDIS_PASSWORD"] = redis_parts["password"]

        frontend_env = {
            "NODE_VERSION": "22.23.4",
            "NODE_ENV": "production",
            "NEXT_TELEMETRY_DISABLED": "1",
            "DATABASE_URL": prisma_db_url,
            "BACKEND_INTERNAL_URL": backend_url,
            "NEXT_PUBLIC_API_URL": backend_url,
            "NEXT_PUBLIC_APP_URL": frontend_url,
            "NEXT_PUBLIC_SELF_HOST": "true",
            "SELF_HOST": "true",
            "BETTER_AUTH_URL": frontend_url,
            "BETTER_AUTH_TRUSTED_ORIGINS": frontend_url,
            "DISABLE_SIGN_UP": "false",
            "NEXT_PUBLIC_LANDING_ONLY_MODE": "false",
            "NEXT_PUBLIC_MAX_VIDEO_UPLOAD_BYTES": "4000000000",
            **shared,
        }

        # Optional provider keys, only when they were supplied.
        passthrough = {
            "OLLAMA_API_KEY", "ASSEMBLY_AI_API_KEY", "PEXELS_API_KEY", "APIFY_API_TOKEN",
            "YOUTUBE_DATA_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
        }
        for key in passthrough:
            if injected.get(key):
                backend_env[key] = injected[key]
        for key in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
            if injected.get(key):
                frontend_env[key] = injected[key]

        set_env_vars(client, backend["id"], [{"key": k, "value": v} for k, v in backend_env.items()])
        set_env_vars(client, frontend["id"], [{"key": k, "value": v} for k, v in frontend_env.items()])
        log(f"  backend env: {len(backend_env)} vars | frontend env: {len(frontend_env)} vars")

        # 5. Deploy ----------------------------------------------------------
        log("\n[5/5] Deploys (this is the slow part)")
        backend_deploy = trigger_deploy(client, backend["id"])
        frontend_deploy = trigger_deploy(client, frontend["id"])
        log(f"  backend deploy {backend_deploy} | frontend deploy {frontend_deploy}")

        backend_result = wait_for_deploy(client, backend["id"], BACKEND_NAME, request["poll_timeout_seconds"])
        frontend_result = wait_for_deploy(client, frontend["id"], FRONTEND_NAME, request["poll_timeout_seconds"])
        report["steps"]["backend_deploy"] = backend_result
        report["steps"]["frontend_deploy"] = frontend_result

        if backend_result.get("status") != "live":
            report["steps"]["backend_events"] = service_events(client, backend["id"])
        if frontend_result.get("status") != "live":
            report["steps"]["frontend_events"] = service_events(client, frontend["id"])

        # Smoke tests --------------------------------------------------------
        log("\nSmoke tests")
        smoke = {
            "backend_health": http_probe(f"{backend_url}/health"),
            "backend_db": http_probe(f"{backend_url}/health/db"),
            "backend_redis": http_probe(f"{backend_url}/health/redis"),
            "frontend_root": http_probe(f"{frontend_url}/"),
        }
        report["smoke"] = smoke

        ollama_key = injected.get("OLLAMA_API_KEY", "")
        if ollama_key:
            report["ollama"] = probe_ollama(
                request["ollama_base_url"], ollama_key,
                [request["llm"].split(":", 1)[1], request["rerank_llm"].split(":", 1)[1]],
            )

        report["ok"] = (
            backend_result.get("status") == "live"
            and frontend_result.get("status") == "live"
            and smoke["backend_health"].get("status") == 200
        )
        report["urls"] = {"frontend": frontend_url, "backend": backend_url}
        report["duration_seconds"] = round(time.time() - started, 1)
        report["api_calls"] = client.calls
        write_state("report.json", report)
        publish_state("deploy")
        log("\nURL: " + frontend_url)
        return 0 if report["ok"] else 1

    except Exception as exc:
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = redact(traceback.format_exc())[:8000]
        report["api_calls"] = client.calls
        write_state("report.json", report)
        publish_state("error")
        log("\nFAILED: " + report["error"])
        return 1


def urllib_parse(url: str) -> dict:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "",
        "port": str(parsed.port or 6379),
        "password": parsed.password or "",
    }


if __name__ == "__main__":
    sys.exit(main())
