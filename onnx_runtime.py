"""onnxruntime sessions on the fastest execution provider this machine has, with a CPU fallback.

Order: CUDA (the onnxruntime-gpu package on NVIDIA, with CUDA 12 + cuDNN 9), DirectML (the onnxruntime-directml package
on Windows, any DirectX 12 GPU), CoreML (macOS, only for models that opt in because they measured faster and equal to
the CPU), then the CPU. If the accelerated provider fails to load or to run, the session moves to the CPU for good.
LEANCUT_ORT_PROVIDER=cpu|cuda|dml|coreml forces one provider (CPU when that one isn't installed).
"""
import os
import sys

ENV = "LEANCUT_ORT_PROVIDER"
CPU = "CPUExecutionProvider"
CUDA, DML, COREML = "CUDAExecutionProvider", "DmlExecutionProvider", "CoreMLExecutionProvider"
ALIASES = {"cpu": CPU, "cuda": CUDA, "dml": DML, "directml": DML, "coreml": COREML}
# MLProgram on CPU+GPU matched the CPU to ~1e-5 on the detectors; the older NeuralNetwork format runs in fp16 (off by
# up to 0.24, and it changed YOLO's detections), and the Neural Engine was no faster but compiled for ~1 s on first run.
# Static shapes only: CoreML fails on the empty tensors that dynamic parts (YOLO's built-in NMS) produce on frames
# without a detection, so those parts stay on the CPU.
OPTIONS = {COREML: {"ModelFormat": "MLProgram", "MLComputeUnits": "CPUAndGPU", "RequireStaticInputShapes": "1"}}


def choose(available, coreml=False, override=None):
    """Execution providers to try, best first, always ending with the CPU."""
    if override:
        want = ALIASES.get(override.strip().lower(), override.strip())
        if want not in available:
            _warn(f"{ENV}={override}: not available here (have {', '.join(available)}); using the CPU.")
        return [want, CPU] if want in available and want != CPU else [CPU]
    for p in (CUDA, DML) + ((COREML,) if coreml else ()):
        if p in available:
            return [p, CPU]
    return [CPU]


def _warn(msg):
    print(f"onnxruntime: {msg}", file=sys.stderr, flush=True)  # the desktop app sends stderr to leancut.log


def _make(path, providers):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    if DML in providers:  # DirectML supports neither memory patterns nor parallel execution
        opts.enable_mem_pattern = False
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return ort.InferenceSession(str(path), opts, providers=[(p, OPTIONS.get(p, {})) for p in providers])


class Session:
    """An onnxruntime.InferenceSession that falls back to the CPU when its accelerated provider fails."""

    def __init__(self, path, providers):
        self.path = str(path)
        try:
            self._s = _make(path, providers)
        except Exception as e:
            if providers == [CPU]:
                raise
            _warn(f"{providers[0]} failed to load {self.path} ({e}); using the CPU.")
            self._s = _make(path, [CPU])

    def __getattr__(self, name):
        return getattr(self._s, name)

    def run(self, *args, **kwargs):
        try:
            return self._s.run(*args, **kwargs)
        except Exception as e:
            if self._s.get_providers() == [CPU]:
                raise
            _warn(f"{self._s.get_providers()[0]} failed running {self.path} ({e}); using the CPU.")
            self._s = _make(self.path, [CPU])
            return self._s.run(*args, **kwargs)


def session(model_path, coreml=False):
    """A session for one model file. coreml=True lets macOS use CoreML (only for models measured faster)."""
    import onnxruntime as ort
    return Session(model_path, choose(ort.get_available_providers(), coreml, os.environ.get(ENV)))
