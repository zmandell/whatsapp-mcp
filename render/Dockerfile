FROM golang:1.26-trixie AS bridge
WORKDIR /src
COPY whatsapp-bridge/go.mod whatsapp-bridge/go.sum ./
RUN go mod download
COPY whatsapp-bridge/main.go ./
RUN CGO_ENABLED=1 go build -trimpath -o /out/whatsapp-bridge .

FROM alpine:3 AS monarch-src
ARG MONARCH_REPO=https://github.com/zmandell/monarch-mcp-server.git
ARG MONARCH_REF=d32bde5c9bd2a4b385dbf4646f3dda86268edb75
RUN apk add --no-cache git \
    && git init /src \
    && git -C /src fetch --depth 1 "$MONARCH_REPO" "$MONARCH_REF" \
    && git -C /src checkout FETCH_HEAD \
    && rm -rf /src/.git

FROM ghcr.io/astral-sh/uv:0.12.10-python3.12-trixie-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PYTHON=3.12

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy
COPY --from=bridge /out/whatsapp-bridge /usr/local/bin/whatsapp-bridge
RUN UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin uv tool install mcp-proxy==0.11.0 --with mcp==1.27.0

WORKDIR /app/whatsapp-mcp-server
COPY whatsapp-mcp-server/pyproject.toml whatsapp-mcp-server/uv.lock ./
RUN uv sync --locked --no-dev
COPY whatsapp-mcp-server/*.py ./
RUN mkdir -p /app/whatsapp-bridge

COPY --from=monarch-src /src /opt/monarch
WORKDIR /opt/monarch
RUN uv sync --locked --no-dev --no-editable

COPY render/Caddyfile render/entrypoint.sh /opt/hub/
RUN chmod +x /opt/hub/entrypoint.sh

WORKDIR /
EXPOSE 10000
ENTRYPOINT ["/opt/hub/entrypoint.sh"]
