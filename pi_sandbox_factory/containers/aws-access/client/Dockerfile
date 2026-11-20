ARG BASE_IMAGE
FROM public.ecr.aws/aws-cli/aws-cli:2.42.0 AS awscli
FROM ${BASE_IMAGE}
COPY --from=awscli /usr/local/aws-cli /usr/local/aws-cli
USER root
RUN ln -s /usr/local/aws-cli/v2/current/bin/aws /usr/local/bin/aws
USER node
