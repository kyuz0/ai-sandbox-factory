ARG BASE_IMAGE
FROM public.ecr.aws/aws-cli/aws-cli:2.42.0 AS awscli
FROM ${BASE_IMAGE}
COPY --from=awscli /usr/local/aws-cli /usr/local/aws-cli
USER root
COPY requirements.txt /opt/aws-python/requirements.txt
RUN ln -s /usr/local/aws-cli/v2/current/bin/aws /usr/local/bin/aws \
    && python3 -m pip install --no-cache-dir --no-compile --only-binary=:all: \
         --target /opt/aws-python/lib -r /opt/aws-python/requirements.txt \
    && python3 -c 'import pathlib, sysconfig; (pathlib.Path(sysconfig.get_path("purelib")) / "sandbox-aws.pth").write_text("/opt/aws-python/lib\n")'

USER node
RUN aws --version && python3 -I -c "import boto3; boto3.Session().get_available_services()"
