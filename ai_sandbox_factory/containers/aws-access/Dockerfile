FROM docker.io/library/debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0
# Refresh OS packages when applying a tested maintenance update, including local builds.
ARG FACTORY_APT_REFRESH=46011051142-1
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends python3-botocore ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY signer.py receive.py .
RUN python3 signer.py --build-catalogue
ENTRYPOINT ["python3", "/app/signer.py"]
