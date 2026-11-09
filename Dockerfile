FROM packages.maestra.io/docker-hub/library/python:3.14-alpine3.24@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72 AS builder

# maestra: uv is bind-mounted (not pip-installed from PyPI) and every package
# comes from nora, the only index (pyproject.toml), with the BuildKit secret
# PRIVATE_PACKAGES_TOKEN as its password (issues-maestra#1649).
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_INDEX_MAESTRA_USERNAME=token

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,from=packages.maestra.io/ghcr/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0,source=/uv,target=/usr/local/bin/uv \
    --mount=type=secret,id=PRIVATE_PACKAGES_TOKEN,required=true \
    UV_INDEX_MAESTRA_PASSWORD="$(cat /run/secrets/PRIVATE_PACKAGES_TOKEN)" \
    uv sync --locked --no-install-project --no-dev

COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,from=packages.maestra.io/ghcr/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0,source=/uv,target=/usr/local/bin/uv \
    --mount=type=secret,id=PRIVATE_PACKAGES_TOKEN,required=true \
    UV_INDEX_MAESTRA_PASSWORD="$(cat /run/secrets/PRIVATE_PACKAGES_TOKEN)" \
    uv sync --locked --no-dev


FROM packages.maestra.io/docker-hub/library/python:3.14-alpine3.24@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72
LABEL org.opencontainers.image.title="NetBox MCP Server" \
      org.opencontainers.image.description="A read-only MCP server for NetBox" \
      org.opencontainers.image.url="https://github.com/netboxlabs/netbox-mcp-server" \
      org.opencontainers.image.source="https://github.com/netboxlabs/netbox-mcp-server" \
      org.opencontainers.image.vendor="NetBox Labs" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONUNBUFFERED=1

RUN --mount=type=secret,id=PRIVATE_PACKAGES_TOKEN,required=true \
    --mount=type=bind,from=public.ecr.aws/g5f9s8a4/nora-apk:1.0.0@sha256:55df38687c301e6fefa230dc1c71a8841618214cc549ade71be740b25822a00e,target=/nora-apk \
    /nora-apk/nora-apk upgrade --no-cache \
    && /nora-apk/nora-apk add --no-cache ca-certificates \
    && python -m pip uninstall --root-user-action=ignore -y pip \
    && addgroup -g 1000 appuser \
    && adduser -D -u 1000 -G appuser appuser

COPY --from=builder --chown=appuser:appuser /app /app

WORKDIR /app

USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["netbox-mcp-server"]
