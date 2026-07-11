# 3.13, not 3.11 (pyproject.toml only requires >=3.11): grype found several
# CVEs against the base image's system CPython 3.11 with fixes available
# only on the 3.13+ branch (task 1.8).
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280

# Layer Debian's current security patches on top of the pinned base digest
# (grype found several fixed-upstream perl-base/libc CVEs in task 1.8).
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# uv pinned independently of the local dev toolchain's uv version; bumped
# past 0.11.15 to clear GHSA-4gg8-gxpx-9rph. pip/wheel/setuptools upgraded
# too — the base image's bundled versions had their own known CVEs,
# including jaraco-context/wheel vendored *inside* old setuptools (a plain
# `pip install --upgrade wheel` doesn't touch that vendored copy).
RUN pip install --no-cache-dir --upgrade pip wheel setuptools==82.0.1 \
    && pip install --no-cache-dir uv==0.11.25 \
    && useradd --create-home --uid 1000 cairn

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY app ./app
RUN uv sync --locked --no-dev

RUN mkdir -p /data && chown -R cairn:cairn /app /data
USER cairn

ENV PATH="/app/.venv/bin:${PATH}" \
    DATABASE_PATH=/data/cairn.db

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
