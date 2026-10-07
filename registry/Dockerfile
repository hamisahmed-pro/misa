# Misa package registry server.
#
# Lean image: the Misa interpreter needs nothing beyond the Python
# standard library (impl/requirements.txt is only for `misa build`'s
# LLVM AOT backend, which is excluded here). No gcc, no build tools.
#
# Build (from the REPO ROOT — COPY paths are relative to it):
#   docker build -f registry/Dockerfile -t misa-registry .
# On Render: set Dockerfile Path to `registry/Dockerfile` (the repo
# root stays the build context).
# Run (Render/Heroku-style: the platform injects $PORT):
#   docker run -e PORT=8080 -p 8080:8080 \
#     -e REGISTRY_ADMIN_TOKEN=<token-from-admin.misa> \
#     -v regdata:/app/data misa-registry
#
# First boot (one-shot, before or alongside the server):
#   docker run --rm -v regdata:/app/data misa-registry \
#     python3 impl/bin/misa run registry/admin.misa

FROM python:3.12-slim

# curl: used by registry/seed.misa to POST publish payloads.
RUN apt-get update \
  && apt-get install -y --no-install-recommends curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Interpreter + registry sources + Misa standard library.
# (impl/misa/aot is the 65MB LLVM backend for `misa build` only —
#  excluded via .dockerignore; the server never needs it.)
COPY impl/ ./impl/
COPY registry/ ./registry/
COPY stdlib/ ./stdlib/

ENV MISA_ROOT=/app \
    REGISTRY_DATA=/app/data \
    PORT=8080

EXPOSE 8080

# Render and friends inject $PORT; the shell form lets it expand.
CMD ["sh", "-c", "python3 impl/bin/misa run registry/server.misa"]
