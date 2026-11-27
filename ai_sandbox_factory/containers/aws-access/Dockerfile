FROM docker.io/library/debian:trixie-slim
# Refresh OS packages when applying a tested maintenance update, including local builds.
ARG FACTORY_APT_REFRESH=2026-11-27
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends python3-botocore ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY signer.py receive.py .
RUN python3 signer.py --build-catalogue
ENTRYPOINT ["python3", "/app/signer.py"]
