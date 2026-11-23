ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY profile.md /opt/sandbox/profile.md
COPY resources.json /opt/sandbox/resources.json
COPY skills/ /opt/research-skills/
ENV SANDBOX_PROFILE=deep-research
USER node
