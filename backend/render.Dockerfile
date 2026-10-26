# SupoClip backend image for Render (https://render.com).
#
# Differences from ./Dockerfile (the Compose build):
#   * CPU-only PyTorch wheels. Render's free/starter instances have no GPU and
#     the CUDA wheels add several gigabytes to the image and the build time.
#   * Listens on $PORT, which Render injects, instead of a hard-coded 8000.
#   * Optionally starts the ARQ worker in the same container
#     (RUN_WORKER_INLINE=true) so a free-tier deployment needs one service
#     instead of two. Set RUN_WORKER_INLINE=false once you add a dedicated
#     worker service.
#   * libgl1/libglib2.0-0 are installed because opencv (used by the face
#     detection / reframing pipeline) needs them at import time on slim images.
#
# Build context is the backend/ directory.

FROM python:3.11.17-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg for rendering, noto fonts for multilingual captions + emoji fallback,
# libgl/libglib for opencv, curl for the healthcheck used by Render.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        unzip \
        ca-certificates \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        fontconfig \
        libgl1 \
        libglib2.0-0 \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# deno is required by yt-dlp for YouTube JS challenge solving.
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_DIR=/root/.deno
ENV PATH="/root/.deno/bin:${PATH}"

# uv resolves the same lockfile the rest of the repo uses.
RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv venv .venv && uv sync --frozen --extra cpu

COPY src/ ./src/
COPY fonts/ ./fonts/
COPY transitions/ ./transitions/
COPY bin/render-entrypoint.sh /app/render-entrypoint.sh
RUN chmod +x /app/render-entrypoint.sh \
    && mkdir -p /app/uploads /app/clips /app/logs

# Runtime defaults for a self-hosted, single-container deployment. Every value
# can be overridden from the Render dashboard.
ENV PORT=8000 \
    TEMP_DIR=/app/uploads \
    OUTPUT_DIR=/app/clips \
    RUN_WORKER_INLINE=true \
    SELF_HOST=true

EXPOSE 8000

CMD ["/app/render-entrypoint.sh"]
