# 3.13, not 3.11 (pyproject.toml only requires >=3.11): grype found several
# CVEs against the base image's system CPython 3.11 with fixes available
# only on the 3.13+ branch (task 1.8).
FROM python:3.13.15-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

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
RUN pip install --no-cache-dir --upgrade pip wheel setuptools==83.0.0 \
    && pip install --no-cache-dir uv==0.11.30 \
    && useradd --create-home --uid 1000 cairn

WORKDIR /app

ARG CAIRN_INSTALL_GEMINI=false
ARG CAIRN_INSTALL_FIRESTORE=false
COPY pyproject.toml uv.lock ./
RUN case "$CAIRN_INSTALL_GEMINI" in false|true) ;; *) exit 1 ;; esac \
    && case "$CAIRN_INSTALL_FIRESTORE" in false|true) ;; *) exit 1 ;; esac
RUN if [ "$CAIRN_INSTALL_GEMINI" = true ] && [ "$CAIRN_INSTALL_FIRESTORE" = true ]; then \
        uv sync --locked --no-install-project --no-dev --extra gemini --extra firestore; \
    elif [ "$CAIRN_INSTALL_GEMINI" = true ]; then \
        uv sync --locked --no-install-project --no-dev --extra gemini; \
    elif [ "$CAIRN_INSTALL_FIRESTORE" = true ]; then \
        uv sync --locked --no-install-project --no-dev --extra firestore; \
    else \
        uv sync --locked --no-install-project --no-dev; \
    fi

COPY app ./app
RUN if [ "$CAIRN_INSTALL_GEMINI" = true ] && [ "$CAIRN_INSTALL_FIRESTORE" = true ]; then \
        uv sync --locked --no-dev --extra gemini --extra firestore; \
    elif [ "$CAIRN_INSTALL_GEMINI" = true ]; then \
        uv sync --locked --no-dev --extra gemini; \
    elif [ "$CAIRN_INSTALL_FIRESTORE" = true ]; then \
        uv sync --locked --no-dev --extra firestore; \
    else \
        uv sync --locked --no-dev; \
    fi

RUN mkdir -p /data && chown -R cairn:cairn /app /data
USER cairn

ENV PATH="/app/.venv/bin:${PATH}" \
    DATABASE_PATH=/data/cairn.db \
    CHROMA_PATH=/data/chroma

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
