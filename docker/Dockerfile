# Build context: repository root.  docker build -f docker/Dockerfile -t banana .

FROM ubuntu:24.04 AS native-build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build python3-dev python3-venv libopencv-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/build && /opt/build/bin/pip install --no-cache-dir build
COPY native /src/native
RUN /opt/build/bin/python -m build --wheel --outdir /wheels /src/native

# CUDA/cuDNN runtime is here for the ONNX Runtime GPU stages (Phase 3); Phase 1-2 run on CPU.
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 BANANA_CONFIG=/etc/banana/config.toml
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv libimage-exiftool-perl sane-utils \
        libopencv-core406t64 libopencv-imgproc406t64 libopencv-imgcodecs406t64 \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/banana
ENV PATH=/opt/banana/bin:$PATH

COPY --from=native-build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl
COPY pyproject.toml README.md /app/
COPY banana /app/banana
RUN pip install --no-cache-dir "/app[ocr,ner]"

WORKDIR /app
EXPOSE 8000
CMD ["banana", "serve"]
