# Network cameras (P3c design §2, D30): one Loopi agent per camera, supervised by loopi-cameras. Build context: the
# repository root (see .dockerignore: only agent/ is sent).
FROM rust:1.93-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libasound2-dev libopus-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY agent/ ./agent/
RUN cd agent && cargo build --release --locked --bin loopi-agent --bin loopi-cameras

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libasound2 libopus0 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 1000 loopi && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin loopi
COPY --from=build /src/agent/target/release/loopi-agent /src/agent/target/release/loopi-cameras /usr/local/bin/
USER loopi
CMD ["loopi-cameras", "--root", "/srv/loopi/cameras"]
