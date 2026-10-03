# The Dockerfile production builds the hub with. Installed root:root 0644 at
# /opt/dashboard-x-deploy/Dockerfile and passed to `docker build -f`.
#
# Not one from a delivered commit: this takes only FILES from the commit --
# requirements.txt and shared/ -- never build instructions
# (SERVER_DEPLOY_RULES.md, rule 4).
#
# Base image pinned by digest (python:3.12-slim, the same one DealOps uses,
# 30 Sep 2026), so a moved tag cannot change what runs.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/tmp

WORKDIR /app

RUN groupadd --gid 10001 hub \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin hub

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Root-owned and world-readable: the hub can read its code but not change it.
COPY shared/hub ./hub
COPY shared/templates ./templates
COPY shared/static ./static

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "--factory", "hub.server:build", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header", "--workers", "1"]
