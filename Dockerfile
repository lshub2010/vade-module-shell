# Build from the Vade REPOSITORY ROOT: the module depends on `sdk/python`.
#   docker build -f modules/shell/Dockerfile -t vade/shell:dev .
#
# No packaging step on purpose: the image only needs the two source trees on
# PYTHONPATH plus the two runtime wheels. The module is run as
# `python -m shell_module`, not imported from site-packages.
#
# `/data/workspace` is the ONLY writable area and the only place a command may
# start (`SHELL_ROOTS`). This image is still NOT a sandbox for arbitrary code:
# run it with `--network=none`, `--read-only`, `--pids-limit` and a memory cap,
# and treat every command as untrusted. See README §"容器" and §"安全模型".
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/opt/vade-sdk \
    MODULE_KEY_PATH=/data/module.key \
    VADE_DATA_DIR=/data \
    SHELL_DATA_DIR=/data/workspace \
    SHELL_ROOTS=/data/workspace \
    SHELL_ALLOWLIST=""

WORKDIR /app

RUN pip install --no-cache-dir "websockets>=13" "cryptography>=42"

COPY sdk/python /opt/vade-sdk
COPY modules/shell/src /app/src

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin vade \
    && mkdir -p /data/workspace && chown -R 10001:10001 /data

USER 10001:10001
VOLUME ["/data"]

# The Noise binding (vade_protocol) is not in this image: mount it at
# /opt/vade-sdk/vade_protocol.py + libvade_protocol.so, or rebuild the image
# with `protocol/core` present. Without it only --standalone works, which is
# intentional: a container that cannot authenticate must say so, not fall back.
STOPSIGNAL SIGTERM

# ENTRYPOINT, not just CMD: `docker run vade/shell:dev --standalone shell_exec ...`
# has to append to `python -m shell_module`, not replace it.
ENTRYPOINT ["python", "-m", "shell_module"]
CMD ["--core", "wss://core:8443"]
