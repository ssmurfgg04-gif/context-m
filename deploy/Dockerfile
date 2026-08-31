# Context-M — production image (multi-stage, slim)
# Build:  docker build -t context-m:latest .
# Run:    docker run -p 8900:8900 -v ctxm-data:/data context-m:latest
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY cortexm ./cortexm
# v0.6.4: context_m.py was deleted in the v0.6.3 cleanup (e215723) but
# this COPY kept referencing it — every container build since then died
# with 'failed to calculate checksum: "/context_m.py": not found'.
RUN pip install --no-cache-dir --target=/deps . cryptography

FROM python:3.12-slim

LABEL org.opencontainers.image.title="Context-M"
LABEL org.opencontainers.image.description="Universal neuro-symbolic memory fabric — μ=0 ingest, bi-temporal provenance, enterprise governance"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/ssmurfgg04-gif/context-m"

# tini for correct signal handling (PID 1)
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /deps /usr/local/lib/python3.12/site-packages

RUN useradd --create-home --uid 10001 contextm \
    && mkdir -p /data \
    && chown -R contextm:contextm /data
USER contextm
WORKDIR /data
VOLUME /data

EXPOSE 8900

ENV CONTEXT_M_DB=/data/memory.db \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8900/healthz', timeout=2)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "cortexm.server.rest", "--db", "/data/memory.db", "--port", "8900"]
