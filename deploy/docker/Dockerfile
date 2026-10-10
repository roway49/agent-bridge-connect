# Local Mac-first vertical slice. Additional executors follow in task 04.
ARG PYTHON_IMAGE=docker.io/library/python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3
ARG NODE_IMAGE=docker.io/library/node:22-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9
FROM ${NODE_IMAGE} AS node
FROM ${PYTHON_IMAGE} AS build
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY scripts/build_backend.py scripts/build_backend.py
COPY webui webui
COPY src src
RUN python -m pip install --no-cache-dir setuptools==80.9.0 wheel==0.45.1 packaging==25.0 \
 && python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .
FROM scratch AS runtime-cache
FROM ${PYTHON_IMAGE} AS runtime-assets
ARG TARGETARCH
COPY deploy/docker/runtime-lock.json deploy/docker/fetch_runtime.py /build/
RUN --mount=from=runtime-cache,target=/cache python /build/fetch_runtime.py /build/runtime-lock.json opencode ${TARGETARCH} /opt/opencode
FROM ${PYTHON_IMAGE} AS opencode
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=runtime-assets /opt/opencode /opt/opencode
COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir --no-deps /wheels/*.whl \
 && rm -rf /wheels \
 && groupadd --gid 1000 agentbc && useradd --uid 1000 --gid 1000 --create-home agentbc \
 && mkdir -p /home/agentbc/.abc /home/agentbc/.local/share/opencode /home/agentbc/.local/state/opencode /data/workspace /run/agentbc /project \
 && chown -R 1000:1000 /home/agentbc/.abc /home/agentbc/.local /data/workspace /run/agentbc /project
COPY --chown=1000:1000 deploy/docker/container_config.toml /home/agentbc/.abc/config.toml
ENV AGENTBC_HEADLESS_NOTIFICATIONS=stdout HOME=/home/agentbc AGENTBC_CONFIG_PATH=/home/agentbc/.abc/config.toml AGENTBC_RUNNER_SPOOL=/run/agentbc PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/opt/opencode:/usr/local/bin:/usr/bin:/bin
USER 1000:1000
WORKDIR /project
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 CMD ["agentbc", "runner", "status"]
ENTRYPOINT ["agentbc"]
CMD ["runner", "serve", "--state-root", "/home/agentbc/.abc/runner", "--allow-root", "/project"]
