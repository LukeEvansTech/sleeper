# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9

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
