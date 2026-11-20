ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS knowledge-build
USER root
WORKDIR /opt/pi-knowledge
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ make \
    && rm -rf /var/lib/apt/lists/*
COPY package.json package-lock.json ./
RUN npm_config_onnxruntime_node_install_cuda=skip npm ci --omit=dev --no-audit --no-fund

# pi-knowledge 0.16.0 loads the fp32 model from this Transformers.js layout.
RUN set -eu; \
    source=https://huggingface.co/Xenova/multilingual-e5-small/resolve/761b726dd34fb83930e26aab4e9ac3899aa1fa78; \
    target=models/Xenova/multilingual-e5-small; \
    mkdir -p "$target/onnx"; \
    for asset in config.json tokenizer.json tokenizer_config.json onnx/model.onnx; do \
        curl -fsSL --retry 3 --proto '=https' --proto-redir '=https' "$source/$asset" --output "$target/$asset"; \
    done

FROM ${BASE_IMAGE}
COPY --from=knowledge-build /opt/pi-knowledge /opt/pi-knowledge
COPY extension.mjs /opt/pi-knowledge/extension.mjs
ENV PI_KNOWLEDGE_EMBEDDING=local:multilingual-e5-small \
    PI_KNOWLEDGE_OFFLINE=true \
    PI_KNOWLEDGE_MODEL_CACHE_DIR=/opt/pi-knowledge/models
