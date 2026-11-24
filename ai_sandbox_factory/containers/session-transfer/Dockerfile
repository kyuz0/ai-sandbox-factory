ARG CODEX_IMAGE
FROM ${CODEX_IMAGE} AS native-codex
FROM docker.io/library/python:3.13-slim-trixie
RUN pip install --no-cache-dir session-migrate==0.13.0
COPY --from=native-codex /usr/local/bin/factory-agent /usr/local/bin/factory-codex
COPY transfer.py /opt/transfer.py
ENTRYPOINT ["python3", "-I", "/opt/transfer.py"]
