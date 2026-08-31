FROM python:3.11.9-slim-bookworm@sha256:2856e6af199e8128161abd320575eb9b341f3b76f017b5d0c9cd364f60d8a050

ARG SOURCE_DATE_EPOCH
ENV PYTHONHASHSEED=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} \
    FIBSEM_IAB_RUNTIME_DIR=/tmp/fibsem-iab-runtime \
    DRJIT_LIBLLVM_PATH=/usr/lib/x86_64-linux-gnu/libLLVM-15.so.1 \
    HOME=/tmp \
    MPLCONFIGDIR=/tmp/matplotlib

COPY fibsem-system-packages /build/fibsem-system-packages
RUN dpkg -i /build/fibsem-system-packages/*.deb

COPY openfibsem-wheelhouse /build/openfibsem-wheels
COPY openfibsem-requirements.lock /build/openfibsem-requirements.lock
COPY runtime-profile.json /build/runtime-profile.json
RUN python -c "import json; p=json.load(open('/build/runtime-profile.json')); assert p['profile']=='fibsem' and p['openfibsem_commit']" \
 && python -m pip install --no-index --require-hashes \
      --find-links=/build/openfibsem-wheels -r /build/openfibsem-requirements.lock

COPY openfibsem /build/openfibsem
RUN python -m pip install --no-index --no-deps --no-build-isolation /build/openfibsem \
 && cp -a /build/openfibsem/fibsem/. \
      /usr/local/lib/python3.11/site-packages/fibsem/ \
 && install -d \
      /usr/local/lib/python3.11/site-packages/fibsem/log/data/ml \
      /usr/local/lib/python3.11/site-packages/fibsem/log/data/crosscorrelation \
      /usr/local/lib/python3.11/site-packages/fibsem/log/data/tile \
      /usr/local/lib/python3.11/site-packages/fibsem/db

COPY docker-cli/docker /usr/local/bin/docker
COPY docker-buildx/docker-buildx /usr/libexec/docker/cli-plugins/docker-buildx
COPY evaluator /build/evaluator
RUN python -m pip install --no-index --no-deps --no-build-isolation /build/evaluator \
 && test "$(sha256sum /usr/local/bin/docker | cut -d ' ' -f 1)" = \
      "242c7a8de606afba2acada7c7af00d77f92c3601678b2f3a60911b49a892c722" \
 && test "$(sha256sum /usr/libexec/docker/cli-plugins/docker-buildx | cut -d ' ' -f 1)" = \
      "a5a4fbd515283ebf05c450bc5b5fabaeeea3f7ac55c322ec310a016005df45a0" \
 && chmod 0755 /usr/local/bin/docker /usr/libexec/docker/cli-plugins/docker-buildx \
 && docker buildx version | grep -F "v0.30.1" \
 && groupadd --gid 11001 evaluator \
 && useradd --uid 11001 --gid 11001 --no-create-home evaluator \
 && rm -rf /build /root/.cache

WORKDIR /run/evaluator
USER 11001:11001
ENTRYPOINT ["python", "-m", "instrument_benchmark_evaluator.cli"]
