FROM python:3.11.9-slim-bookworm@sha256:2856e6af199e8128161abd320575eb9b341f3b76f017b5d0c9cd364f60d8a050

RUN groupadd --gid 10001 benchmark \
    && useradd --uid 10001 --gid 10001 --create-home benchmark

WORKDIR /workspace
USER 10001:10001
ENTRYPOINT ["python", "/runner/bootstrap.py"]
