# The song maker that runs on AWS Fargate: the choir_solfa package plus its models.
# Built from the repository root:  docker build -f cloud/worker.Dockerfile .

# wgcf (registers a free Cloudflare WARP device) and wireproxy (runs the WARP tunnel as an HTTP
# proxy without a VPN device), so links can be downloaded: YouTube refuses AWS addresses.
# Base images are pinned by digest so a rebuild gets exactly the same system.
FROM golang:1.26-bookworm@sha256:dc40a13b61304647ed7e5d581d246b86413dfc29d63c6d018310feb93cd98fc0 AS warp
ENV CGO_ENABLED=0 GOBIN=/out
RUN go install github.com/ViRb3/wgcf/v2@v2.3.1 && go install github.com/windtf/wireproxy/cmd/wireproxy@v1.1.3

FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TORCH_HOME=/opt/models/torch UV_SYSTEM_PYTHON=1 \
    NUMBA_CACHE_DIR=/tmp/numba
RUN pip install --no-cache-dir uv==0.9.30

WORKDIR /app
# Dependencies first, so code changes don't reinstall them. Every package is pinned with its hash
# (cloud/lock-worker.sh makes the lists): torch and torchaudio from PyTorch's CPU-only index,
# everything else from PyPI only, so no index can slip in a different package of the same name.
COPY cloud/worker-torch.txt cloud/worker-requirements.txt ./
RUN uv pip install --no-cache --no-deps --require-hashes --index-url https://download.pytorch.org/whl/cpu -r worker-torch.txt \
 && uv pip install --no-cache --no-deps --require-hashes -r worker-requirements.txt \
 && uv pip check

# Bake the voice-separation model in, so no song waits for a download.
RUN for i in 1 2 3 4 5; do \
      python -c "from demucs.pretrained import get_model; get_model('htdemucs')" && exit 0; \
      echo "model download failed, trying again"; sleep 15; \
    done; exit 1

# And the model that tells the backing singers from the lead singer (checked against its hash;
# keep these in step with src/choir_solfa/karaoke.py).
ARG KARA_URL=https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/UVR_MDXNET_KARA_2.onnx
ARG KARA_SHA256=bf32e15105a09c0f7dddd2b67346146334d6f3ecb399ed7638eba2ab07cbf5f4
RUN mkdir -p "$TORCH_HOME/hub/checkpoints" && cd "$TORCH_HOME/hub/checkpoints" && \
    for i in 1 2 3 4 5; do \
      python -c "import urllib.request as u; u.urlretrieve('$KARA_URL', 'kara.part')" \
      && echo "$KARA_SHA256  kara.part" | sha256sum -c - && mv kara.part UVR_MDXNET_KARA_2.onnx && exit 0; \
      echo "model download failed, trying again"; rm -f kara.part; sleep 15; \
    done; exit 1

COPY --from=warp /out/ /usr/local/bin/
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv pip install --no-cache --no-deps . && python -c "import choir_solfa.cloud_worker, choir_solfa.pipeline"

# Songs are made as an ordinary user, not root.
RUN useradd --create-home --uid 10001 solfa
USER solfa
WORKDIR /home/solfa

# The worker stops itself after MAX_TASK_MINUTES (60); timeout is a hard stop in case Python hangs.
CMD ["timeout", "-s", "KILL", "65m", "python", "-m", "choir_solfa.cloud_worker"]
