# memgres image — runs the HTTP server (default) or the MCP server
# (`docker run … memgres-mcp`). Light by design: cloud embeddings use the stdlib,
# so the base image needs no ML stack. For local (sentence-transformers)
# embeddings, build with:  --build-arg EXTRAS=server,mcp,qdrant,local
FROM python:3.12-slim

ARG EXTRAS=server,mcp,qdrant
WORKDIR /app

# Install deps first (better layer caching), then the package.
COPY pyproject.toml README.md LICENSE ./
COPY memgres ./memgres
RUN pip install --no-cache-dir ".[${EXTRAS}]"

ENV MEMGRES_HTTP_HOST=0.0.0.0 \
    MEMGRES_HTTP_PORT=8080
EXPOSE 8080

# Role-aware: probes /healthz on MEMGRES_HTTP_PORT for the REST entrypoint and
# /mcp on MEMGRES_MCP_PORT for the MCP one (see memgres/healthcheck.py). A fixed
# 8080 probe reported every MCP container unhealthy forever.
HEALTHCHECK --interval=10s --timeout=4s --start-period=20s --retries=5 \
    CMD memgres-healthcheck

# The server migrates the schema on startup (idempotent), then serves.
CMD ["memgres-server"]
