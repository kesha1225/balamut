# The PyMutBench base image: CPython 3.14 (pinned by digest), git, a C toolchain and a pinned uv.
# Subject images are built FROM it by `python3 -m pymutbench.orchestrate build-subject NAME`.
FROM python@sha256:f1b121a95d6295f7291a0fda8280f747efd657bd055e7c1f156936ed2dc5a112
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9.21 /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN git config --global user.email bench@example.invalid \
    && git config --global user.name pymutbench \
    && git config --global advice.detachedHead false \
    && git config --global --add safe.directory '*'
WORKDIR /work
