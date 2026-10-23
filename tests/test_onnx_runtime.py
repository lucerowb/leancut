"""Execution-provider choice and CPU fallback, against a fake onnxruntime (no GPU or model needed)."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import onnx_runtime as ox  # noqa: E402

CPU, CUDA, DML, COREML = ox.CPU, ox.CUDA, ox.DML, ox.COREML


@pytest.fixture
def fake_ort(monkeypatch):
    """A stand-in onnxruntime: `available` providers, and `broken` ones that fail to load ("init") or to run ("run")."""
    ort = types.SimpleNamespace(available=[CPU], broken={}, made=[])

    class InferenceSession:
        def __init__(self, path, opts, providers):
            self.opts, self.providers = opts, [p for p, _ in providers]
            ort.made.append((self.providers, dict(providers)))
            if ort.broken.get(self.providers[0]) == "init":
                raise RuntimeError("no driver")

        def get_providers(self):
            return self.providers

        def run(self, outputs, feeds):
            if ort.broken.get(self.providers[0]) == "run":
                raise RuntimeError("kernel failed")
            if feeds.get("bad"):
                raise ValueError("bad input")
            return [self.providers[0]]

    ort.InferenceSession = InferenceSession
    ort.SessionOptions = lambda: types.SimpleNamespace(log_severity_level=0, enable_mem_pattern=True, execution_mode=None)
    ort.ExecutionMode = types.SimpleNamespace(ORT_SEQUENTIAL="sequential")
    ort.get_available_providers = lambda: ort.available
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.delenv(ox.ENV, raising=False)
    return ort


def test_choose_prefers_gpus_and_uses_coreml_only_when_a_model_opts_in(capsys):
    every = [CUDA, DML, COREML, CPU]
    assert ox.choose(every) == [CUDA, CPU]
    assert ox.choose([DML, CPU]) == [DML, CPU]
    assert ox.choose([COREML, CPU]) == [CPU]
    assert ox.choose([COREML, CPU], coreml=True) == [COREML, CPU]
    assert ox.choose([CPU], coreml=True) == [CPU]
    assert ox.choose(every, override="CoreML") == [COREML, CPU]  # the override beats the per-model choice
    assert ox.choose(every, override=" cpu ") == [CPU]
    assert ox.choose(every, override=DML) == [DML, CPU]
    assert capsys.readouterr().err == ""
    assert ox.choose([CPU], override="cuda") == [CPU]
    assert "not available" in capsys.readouterr().err


def test_session_uses_the_best_provider(fake_ort):
    fake_ort.available = [COREML, CPU]
    s = ox.session("face.onnx", coreml=True)
    assert s.run(None, {}) == [COREML]
    assert fake_ort.made[0][1][COREML] == ox.OPTIONS[COREML]
    assert ox.session("clip.onnx").run(None, {}) == [CPU]  # not opted in to CoreML


def test_directml_sessions_turn_off_memory_patterns(fake_ort):
    fake_ort.available = [DML, CPU]
    s = ox.session("m.onnx")
    assert s.get_providers()[0] == DML and not s.opts.enable_mem_pattern and s.opts.execution_mode == "sequential"


def test_falls_back_to_cpu_when_the_provider_fails_to_load(fake_ort, capsys):
    fake_ort.available, fake_ort.broken = [CUDA, CPU], {CUDA: "init"}
    assert ox.session("m.onnx").run(None, {}) == [CPU]
    assert "CUDAExecutionProvider failed to load" in capsys.readouterr().err


def test_falls_back_to_cpu_for_good_when_a_run_fails(fake_ort, capsys):
    fake_ort.available, fake_ort.broken = [DML, CPU], {DML: "run"}
    s = ox.session("m.onnx")
    assert s.run(None, {}) == [CPU] and s.get_providers() == [CPU]
    assert "failed running" in capsys.readouterr().err
    assert s.run(None, {}) == [CPU] and len(fake_ort.made) == 2  # rebuilt once, not on every call
    with pytest.raises(ValueError):  # a CPU error is a real error: no retry loop, no swallowing
        s.run(None, {"bad": True})


def test_env_override_forces_a_provider(fake_ort, monkeypatch):
    fake_ort.available = [CUDA, COREML, CPU]
    monkeypatch.setenv(ox.ENV, "cpu")
    assert ox.session("a.onnx", coreml=True).run(None, {}) == [CPU]
    monkeypatch.setenv(ox.ENV, "coreml")
    assert ox.session("a.onnx").run(None, {}) == [COREML]


def test_cpu_load_errors_are_not_hidden(fake_ort):
    fake_ort.broken = {CPU: "init"}
    with pytest.raises(RuntimeError, match="no driver"):
        ox.session("missing.onnx")
