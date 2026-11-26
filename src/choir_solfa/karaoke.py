"""Split the sung voices into the lead singer and the backing singers.

Uses the UVR "karaoke" MDX-Net model (UVR_MDXNET_KARA_2, from Ultimate Vocal
Remover's public model collection). The model is an ONNX file; it uses only a
handful of operations, so it is read here and run with torch (on the Mac's GPU
when there is one) instead of adding an ONNX runtime.
"""
from __future__ import annotations

import hashlib
import struct
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np

MODEL_NAME = "UVR_MDXNET_KARA_2.onnx"
MODEL_URL = f"https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/{MODEL_NAME}"
MODEL_SHA256 = "bf32e15105a09c0f7dddd2b67346146334d6f3ecb399ed7638eba2ab07cbf5f4"
# The model's settings (from UVR's model data for this file).
N_FFT, HOP, DIM_F, DIM_T, COMPENSATE = 5120, 1024, 2048, 256, 1.065
OVERLAP = 0.25


def model_path() -> Path:
    """The model file, downloaded (and checked) the first time it is needed."""
    import torch
    path = Path(torch.hub.get_dir()) / "checkpoints" / MODEL_NAME
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        with urllib.request.urlopen(MODEL_URL, timeout=60) as r:
            while chunk := r.read(1 << 20):
                tmp.write(chunk)
    tmp_path = Path(tmp.name)
    if hashlib.sha256(tmp_path.read_bytes()).hexdigest() != MODEL_SHA256:
        tmp_path.unlink()
        raise RuntimeError(f"The downloaded {MODEL_NAME} is not the expected file.")
    tmp_path.replace(path)
    return path


# --- Reading the ONNX file (protobuf wire format, only the parts this model uses) ---

def _fields(buf: bytes):
    """Yield (field number, wire type, value) for each field in a protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, i)
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        elif wire == 2:
            size, i = _varint(buf, i)
            val, i = buf[i:i + size], i + size
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield num, wire, val


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _ints(wire: int, val) -> list[int]:
    if wire == 0:
        return [val]
    out, i = [], 0
    while i < len(val):
        v, i = _varint(val, i)
        out.append(v)
    return out


def _signed(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


def read_onnx(data: bytes) -> tuple[list[dict], dict[str, np.ndarray], str, str]:
    """The graph's nodes, its weights, and the names of its input and output."""
    graph = next(val for num, _, val in _fields(data) if num == 7)
    nodes, weights, inputs, outputs = [], {}, [], []
    for num, wire, val in _fields(graph):
        if num == 1:  # node
            node = {"inputs": [], "outputs": [], "op": "", "attrs": {}}
            for f, w, v in _fields(val):
                if f == 1:
                    node["inputs"].append(v.decode())
                elif f == 2:
                    node["outputs"].append(v.decode())
                elif f == 4:
                    node["op"] = v.decode()
                elif f == 5:
                    name, value, ints = "", None, []
                    for af, aw, av in _fields(v):
                        if af == 1:
                            name = av.decode()
                        elif af == 2:
                            value = struct.unpack("<f", av)[0]
                        elif af == 3:
                            value = _signed(av)
                        elif af == 8:
                            ints += [_signed(x) for x in _ints(aw, av)]
                    node["attrs"][name] = ints if ints else value
            nodes.append(node)
        elif num == 5:  # initializer
            dims, dtype, name, raw = [], 1, "", b""
            for f, w, v in _fields(val):
                if f == 1:
                    dims += _ints(w, v)
                elif f == 2:
                    dtype = v
                elif f == 8:
                    name = v.decode()
                elif f == 9:
                    raw = v
            if dtype != 1:
                raise ValueError(f"unsupported weight type {dtype} in {name}")
            weights[name] = np.frombuffer(raw, dtype="<f4").reshape(dims).copy()
        elif num == 11:
            inputs.append(next(v.decode() for f, _, v in _fields(val) if f == 1))
        elif num == 12:
            outputs.append(next(v.decode() for f, _, v in _fields(val) if f == 1))
    inputs = [name for name in inputs if name not in weights]
    return nodes, weights, inputs[0], outputs[0]


class OnnxNet:
    """Runs the model's graph with torch."""

    def __init__(self, path: Path):
        self.nodes, weights, self.input, self.output = read_onnx(path.read_bytes())
        import torch
        self.weights = {k: torch.from_numpy(v) for k, v in weights.items()}

    def to(self, device) -> "OnnxNet":
        self.weights = {k: v.to(device) for k, v in self.weights.items()}
        return self

    def __call__(self, x):
        import torch.nn.functional as F
        values = dict(self.weights)
        values[self.input] = x
        for node in self.nodes:
            a = node["attrs"]
            args = [values[name] if name else None for name in node["inputs"]]
            op = node["op"]
            if op in ("Conv", "ConvTranspose"):
                pads = a.get("pads", [0, 0, 0, 0])
                if pads[:2] != pads[2:]:
                    raise ValueError("uneven padding is not supported")
                kw = dict(stride=a.get("strides", [1, 1]), padding=pads[:2], dilation=a.get("dilations", [1, 1]),
                          groups=a.get("group", 1))
                bias = args[2] if len(args) > 2 else None
                fn = F.conv2d if op == "Conv" else F.conv_transpose2d
                out = fn(args[0], args[1], bias, **kw)
            elif op == "BatchNormalization":
                x0, scale, b, mean, var = args
                out = F.batch_norm(x0, mean, var, scale, b, False, 0.0, a.get("epsilon", 1e-5))
            elif op == "Relu":
                out = F.relu(args[0])
            elif op == "Add":
                out = args[0] + args[1]
            elif op == "Mul":
                out = args[0] * args[1]
            elif op == "MatMul":
                out = args[0] @ args[1]
            elif op == "Transpose":
                out = args[0].permute(*a["perm"])
            else:
                raise ValueError(f"unsupported operation {op}")
            values[node["outputs"][0]] = out
        return values[self.output]


def split(vocals: np.ndarray, progress: Callable[[float], None] = lambda f: None) -> tuple[np.ndarray, np.ndarray]:
    """Split a 44.1 kHz stereo vocal track into (lead, backing)."""
    import torch

    devices = ["mps", "cpu"] if torch.backends.mps.is_available() else ["cpu"]
    net = OnnxNet(model_path())
    last_error = None
    for device in devices:
        try:
            with torch.no_grad():
                backing = _run(net.to(device), vocals, device, progress)
            break
        except Exception as e:  # MPS can fail on some ops; fall back to the CPU
            last_error = e
    else:
        raise RuntimeError(f"Could not split the lead singer from the backing singers: {last_error}")
    # The model gives the backing singers; the lead is what is left.
    return (vocals - COMPENSATE * backing).astype(np.float32), backing.astype(np.float32)


def _run(net: OnnxNet, mix: np.ndarray, device: str, progress: Callable[[float], None]) -> np.ndarray:
    import torch

    window = torch.hann_window(N_FFT, periodic=True)
    chunk = HOP * (DIM_T - 1)
    trim = N_FFT // 2
    gen = chunk - 2 * trim
    peak = float(np.abs(mix).max()) or 1.0
    x = (mix / peak).astype(np.float32)
    pad = gen + trim - x.shape[1] % gen
    padded = np.concatenate([np.zeros((2, trim), np.float32), x, np.zeros((2, pad), np.float32)], 1)
    step = int((1 - OVERLAP) * chunk)
    out = np.zeros_like(padded)
    weight = np.zeros(padded.shape[1], np.float32)
    starts = range(0, padded.shape[1], step)
    for k, start in enumerate(starts):
        end = min(start + chunk, padded.shape[1])
        part = padded[:, start:end]
        if part.shape[1] < chunk:
            part = np.pad(part, ((0, 0), (0, chunk - part.shape[1])))
        spec = torch.stft(torch.from_numpy(part), N_FFT, HOP, window=window, center=True, return_complex=True)
        spec = torch.view_as_real(spec).permute(0, 3, 1, 2).reshape(4, N_FFT // 2 + 1, -1)[:, :DIM_F]
        spec[:, :3] = 0
        pred = net(spec[None].to(device))[0].float().cpu()
        full = torch.zeros(4, N_FFT // 2 + 1, pred.shape[-1])
        full[:, :DIM_F] = pred
        full = full.reshape(2, 2, N_FFT // 2 + 1, -1).permute(0, 2, 3, 1).contiguous()
        wave = torch.istft(torch.view_as_complex(full), N_FFT, HOP, window=window, center=True).numpy()
        w = np.hanning(end - start).astype(np.float32)
        out[:, start:end] += wave[:, :end - start] * w
        weight[start:end] += w
        progress((k + 1) / len(starts))
    return (out / np.maximum(weight, 1e-8))[:, trim:trim + x.shape[1]] * peak
