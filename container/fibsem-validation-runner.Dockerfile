FROM python:3.11.9-slim-bookworm@sha256:2856e6af199e8128161abd320575eb9b341f3b76f017b5d0c9cd364f60d8a050

ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0

COPY wheelhouse/pyyaml-6.0.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl /build/pyyaml-6.0.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl
COPY wheelhouse/python_dotenv-1.2.3-py3-none-any.whl /build/python_dotenv-1.2.3-py3-none-any.whl
RUN echo "b8bb0864c5a28024fac8a632c443c87c5aa6f215c0b126c449ae1a150412f31d  /build/pyyaml-6.0.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl" \
      | sha256sum --check --strict \
 && echo "904552145e8bfed22162c09dab1c2b9b54fefa7b23ba780f4f26ca0316b0f0d9  /build/python_dotenv-1.2.3-py3-none-any.whl" \
      | sha256sum --check --strict \
 && python -m pip install --no-index \
      /build/pyyaml-6.0.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl \
      /build/python_dotenv-1.2.3-py3-none-any.whl \
 && rm -rf /build /root/.cache

COPY docker-cli/docker /usr/local/bin/docker
RUN test "$(sha256sum /usr/local/bin/docker | cut -d ' ' -f 1)" = \
      "242c7a8de606afba2acada7c7af00d77f92c3601678b2f3a60911b49a892c722" \
 && chmod 0755 /usr/local/bin/docker

ENTRYPOINT ["python"]
