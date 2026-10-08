# Release image and test image of mail-dispatch (SPEC §12, [D45], [D46]).
#
# Debian 13 (trixie) pinned by codename for every stage: the build stage runs on the same
# Debian as the runtime, so compiled wheels meet the glibc and shared libraries they were
# installed for. Deliberately buildable with the CLASSIC builder as well as BuildKit - no
# RUN --mount, no heredocs, no COPY --link - and CI enforces that with DOCKER_BUILDKIT=0.
ARG PYTHON_IMAGE=python:3.13-slim-trixie

FROM ghcr.io/astral-sh/uv:0.10.4 AS uv

# Dependencies and the project, installed with uv into a virtualenv.
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# The same, with the development dependencies, for the test stage.
FROM build AS build-dev
RUN uv sync --frozen --no-editable

# What every image shares: the interpreter, a non-root user, nothing writable needed.
FROM ${PYTHON_IMAGE} AS base
RUN useradd --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --uid 10001 app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The full suite, run in CI with --read-only --tmpfs /tmp --network none [D46].
FROM base AS test
COPY --from=build-dev --chown=root:root /app/.venv /app/.venv
COPY pyproject.toml ./
COPY tests ./tests
USER app
# Coverage is measured on the runner; here the point is the read-only file system.
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

# The release image: the last stage, so a plain `docker build` makes it.
FROM base AS runtime
LABEL org.opencontainers.image.source="https://github.com/janwychowaniak/mail-dispatch"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.description="Composes one email message from JSON and sends it through one configured SMTP server"
COPY --from=build --chown=root:root /app/.venv /app/.venv
USER app
EXPOSE 8000

# python:slim ships neither curl nor wget, so the check is the standard library. Health
# answers 200 whenever the service is alive, whatever the SMTP server's state (§6), so the
# check only says that the process serves; its timeout leaves room for the probe's deadline.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/v1/health' % (os.environ.get('PORT') or '8000'), timeout=9)"]

CMD ["python", "-m", "mail_dispatch"]
