#!/bin/sh
# Pins every package in the worker image to an exact version and hash, so the image build
# installs exactly what was tested and nothing that appears later on an index.
#   cloud/worker-torch.txt         torch and torchaudio, from PyTorch's CPU-only index
#   cloud/worker-requirements.txt  everything else, from PyPI only
# Run from the repository root after changing dependencies (it picks the newest versions that
# fit), then rebuild the image and make a song with it before deploying.
set -eu

TORCH=2.15.0
TORCHAUDIO=2.11.1
PLATFORM="--python-version 3.11 --python-platform aarch64-manylinux_2_28"
tmp=$(mktemp -d)
trap 'rm -r "$tmp"' EXIT

printf 'torch==%s+cpu\ntorchaudio==%s+cpu\n' "$TORCH" "$TORCHAUDIO" > "$tmp/torch.in"
uv pip compile "$tmp/torch.in" $PLATFORM --index-url https://download.pytorch.org/whl/cpu \
  --no-deps --generate-hashes --no-header --no-annotate -o cloud/worker-torch.txt

# torch from PyPI would bring CUDA libraries on ARM Linux. The CPU build doesn't need them, so
# they are left out of the lock (torch's other needs, such as sympy, stay in).
printf 'torch==%s\n' "$TORCH" > "$tmp/gpu.in"
uv pip compile "$tmp/gpu.in" $PLATFORM --no-header --no-annotate \
  | grep -oE '^(nvidia|cuda|triton)[^=]*' | sed 's/^/--no-emit-package=/' > "$tmp/skip"

# demucs 4.1 needs sphn, which has no ARM Linux wheel; 4.0.1 uses the same model.
# basic-pitch gets TensorFlow 2.15 here, which was built for NumPy 1.
printf 'boto3\ndemucs<4.1\nnumpy<2\n' > "$tmp/extra.in"
printf 'torch==%s\ntorchaudio==%s\n' "$TORCH" "$TORCHAUDIO" > "$tmp/pins.txt"
xargs uv pip compile pyproject.toml "$tmp/extra.in" $PLATFORM --constraint "$tmp/pins.txt" \
  --no-emit-package=torch --no-emit-package=torchaudio --generate-hashes --no-header --no-annotate \
  -o cloud/worker-requirements.txt < "$tmp/skip"
