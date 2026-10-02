# ONE image, ONE container: Basic Memory 0.23.2 (127.0.0.1) + write shim (:27123, the only LAN listener)
# + committer, under a tiny stdlib supervisor (container/supervisor.py) that exits non-zero if any child dies.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git procps ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "basic-memory==0.23.2" \
 && mkdir -p /opt/vault-stack /app/shim /app/committer /etc/vault-stack /config /vault /backup /tmp/home \
 && chmod 1777 /tmp/home
ENV PYTHONUNBUFFERED=1 HOME=/tmp/home \
    BASIC_MEMORY_CONFIG_DIR=/config VAULT_MOUNT=/vault VAULT_ROOT=/vault STATE_DIR=/config/state \
    BM_HOST=127.0.0.1 BM_PORT=8000 BM_MCP_URL=http://127.0.0.1:8000/mcp \
    SHIM_BIND=0.0.0.0 SHIM_PORT=27123 VAULT_TOKEN_FILE=/run/secrets/vault_token \
    BLOCKED_TOOLS_FILE=/etc/vault-stack/blocked_tools.txt \
    NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
    WATCHFILES_FORCE_POLLING=1 WATCHFILES_POLL_DELAY_MS=3000
COPY bm/drift_guard.py bm/make_bmignore.py bm/entrypoint.sh /opt/vault-stack/
COPY config/config.json /opt/vault-stack/config.json
COPY config/blocked_tools.txt /etc/vault-stack/blocked_tools.txt
COPY shim/vault_shim.py /app/shim/
COPY committer/committer.py committer/staleness.py /app/committer/
COPY container/supervisor.py container/healthcheck.py /app/
RUN chmod +x /opt/vault-stack/entrypoint.sh
EXPOSE 27123
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 CMD ["python3", "/app/healthcheck.py"]
ENTRYPOINT ["python3", "/app/supervisor.py"]
