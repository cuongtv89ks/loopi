FROM python:3.13.16-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
RUN groupadd --gid 1000 loopi \
 && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin loopi \
 && install -d -o loopi -g loopi /srv/loopi
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
USER loopi
EXPOSE 8000
CMD ["loopi-brain", "serve"]
