# Built on linux/amd64 by the scorer, run with no network as user nobody.
# The base digest is the image every commit was labeled in: keep it, since its
# Python and SQLite decide which sqlglot tests run and how.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# sqlglot at the pinned commit, from its own repository (no apt, no git needed).
RUN python - <<'EOF'
import io, tarfile, urllib.request, shutil, os
sha = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as t:
    t.extractall("/opt", filter="data")
os.rename(f"/opt/sqlglot-{sha}", "/opt/sqlglot")
shutil.rmtree("/opt/sqlglot/sqlglot-integration-tests", ignore_errors=True)
EOF

# The exact packages the labels were made with.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

COPY selector /app/selector
COPY maps /app/maps

# Fingerprint of sqlglot's import-time state, made in this exact environment.
RUN python /app/selector/fingerprint.py /opt/sqlglot /app/maps/fp_base.pkl

# Pristine TPC-DS execution traces (tpctrace.py), made in this exact environment;
# the committed copy stays if this step cannot run.
RUN cd /opt/sqlglot && (python /app/selector/tpctrace.py /opt/sqlglot /app/maps/tpc_base.pkl.new \
    && mv /app/maps/tpc_base.pkl.new /app/maps/tpc_base.pkl || true)
# Pinned TPC-DS executor outputs (tpcexec.py) for executor-only patches.
RUN cd /opt/sqlglot && (SKIP_INTEGRATION=0 python /app/selector/tpcexec.py /opt/sqlglot /app/maps/tpc_exec_base.pkl.new \
    && mv /app/maps/tpc_exec_base.pkl.new /app/maps/tpc_exec_base.pkl || true)

# Precompile so imports at run time read .pyc; make everything readable by nobody.
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests /app/selector \
    && chmod -R a+rX /opt/sqlglot /app

ENV BR_DIAG_MAXSEC=1.5 BR_REPO=/opt/sqlglot BR_MAPS=/app/maps/maps.pkl SKIP_INTEGRATION=0 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "/app/selector/main.py"]
