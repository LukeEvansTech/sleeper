# syntax=docker/dockerfile:1@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183
FROM python:3.15-slim-bookworm@sha256:498829c717b1e59fc42f0e7865eafe4e04ca0f70a3769d4ae70ba218df54507c

# App version, stamped from the release tag by CI (docker-publish.yml build-args).
ARG VERSION=0.0.0-dev
ENV APP_VERSION=${VERSION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SLEEPER_DATA=/data \
    HOME=/tmp

LABEL org.opencontainers.image.source="https://github.com/LukeEvansTech/sleeper" \
      org.opencontainers.image.title="sleeper" \
      org.opencontainers.image.description="Trading-card deal finder with a deal page per alert" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app
COPY requirements.txt ./
# Chromium and its system libraries come from Playwright's own installer, which pins the browser
# build to the playwright package version in requirements.txt.
# Playwright's bundled Node driver links libatomic, which the slim base image lacks; it has to be
# present before `playwright install` can run at all. Not version-pinned: the base image digest fixes
# the Debian release, and a pinned point version vanishes from the mirror after a security update.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

COPY sleeper ./sleeper

RUN mkdir -p /data && chown -R 65532:65532 /data
VOLUME ["/data"]

USER 65532:65532
# Kubernetes probes own liveness (/health); an image healthcheck would double-probe.
HEALTHCHECK NONE
EXPOSE 8080
ENTRYPOINT ["python", "-m", "sleeper"]
CMD ["serve"]
