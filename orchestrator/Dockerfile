# Orchestrator dashboard API — container build.
#
# SCOPE: the API + UI over the mounted SQLite DB (board, CRM, roadmap,
# metrics, healthz — the read/manage surface). Session probing degrades
# gracefully in-container: tmux/tailscale/transcripts live on hosts, not in
# here — the Sessions tab simply shows none. For the full experience run the
# systemd service on the GPU box; THIS image is the portable/replica deploy
# (e.g. the Coolify hub's read-only dashboard).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard/ dashboard/
COPY mcp_server.py mcp_sse_server.py ./

# The modules resolve the DB at ~/.hermes/kanban.db — in-container, HOME is
# /root, so compose mounts the host's data dir at /root/.hermes.
ENV DASHBOARD_BIND=0.0.0.0 \
    DASHBOARD_PORT=5555

EXPOSE 5555
# Liveness from inside the container (slim has no curl): /healthz gates on
# the mounted DB being readable — exactly what should fail the container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('DASHBOARD_PORT','5555')}/healthz\", timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "dashboard.api"]
