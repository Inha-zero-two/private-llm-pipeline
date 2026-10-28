"""Installer plans are verified without installing native packages or calling GPUs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import install


@pytest.mark.parametrize("timeout", [None, 0.25])
def test_run_forwards_timeout_without_limiting_install_commands_by_default(
    monkeypatch: pytest.MonkeyPatch, timeout: float | None
) -> None:
    command = ["python", "-m", "pip", "install", "package"]
    result = subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")
    run = Mock(return_value=result)
    monkeypatch.setattr(install.subprocess, "run", run)
    options = {} if timeout is None else {"timeout": timeout}

    assert install._run(command, capture=True, **options) is result
    assert run.call_args.kwargs["timeout"] == timeout


def test_run_timeout_becomes_install_error_without_sensitive_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "private-command-argument"
    command = ["nvidia-smi", secret]
    run = Mock(
        side_effect=subprocess.TimeoutExpired(command, 5, output=secret, stderr=secret)
    )
    monkeypatch.setattr(install.subprocess, "run", run)

    with pytest.raises(install.InstallError, match="nvidia-smi") as error:
        install._run(command, env={"API_TOKEN": secret}, capture=True, timeout=5)

    captured = capsys.readouterr()
    assert secret not in str(error.value) + captured.out + captured.err
    assert error.value.__suppress_context__


@pytest.mark.parametrize("system", ["Linux", "Windows"])
@pytest.mark.parametrize("timeout_call", [None, 0, 1])
def test_hardware_probe_bounds_both_smi_calls_and_falls_back_on_timeout(
    monkeypatch: pytest.MonkeyPatch, system: str, timeout_call: int | None
) -> None:
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(install.platform, "system", lambda: system)
    monkeypatch.setattr(install.shutil, "which", lambda name: "nvidia-smi")
    monkeypatch.setattr(install, "_driver_probe", Mock(side_effect=OSError))
    results = [
        subprocess.CompletedProcess(
            ["nvidia-smi"], 0, stdout="0, GPU-test, Test GPU, 8192, 8.6\n"
        ),
        subprocess.CompletedProcess(["nvidia-smi"], 0, stdout="CUDA Version: 12.5"),
    ]
    responses: list[subprocess.CompletedProcess[str] | subprocess.TimeoutExpired] = (
        list(results)
    )
    if timeout_call is not None:
        responses[timeout_call] = subprocess.TimeoutExpired("nvidia-smi", 5)
    run = Mock(side_effect=responses)
    monkeypatch.setattr(install.subprocess, "run", run)

    probe = install.probe_hardware()

    if timeout_call is None:
        assert probe == install.Probe(
            system, (install.GPU(0, "Test GPU", 8 * 1024**3, (8, 6)),), (12, 5)
        )
    else:
        assert probe == install.Probe(system, ())
    assert run.call_count == (2 if timeout_call is None else timeout_call + 1)
    assert all(call.kwargs["timeout"] == 5 for call in run.call_args_list)


@pytest.mark.parametrize("system", ["Linux", "Windows", "Darwin"])
def test_remote_plan_without_gpu(system: str, tmp_path: Path) -> None:
    plan = install.plan_install(install.Probe(system, ()), venv_dir=tmp_path)
    assert plan.backend == "remote"
    assert plan.gpu is None
    assert plan.venv_dir == tmp_path


def test_mixed_gpu_plan_preserves_common_wheel_indexes(tmp_path: Path) -> None:
    older = install.GPU(0, "older", 8 * 1024**3, (6, 1))
    newer = install.GPU(1, "newer", 8 * 1024**3, (8, 6))
    probe = install.Probe("Linux", (older, newer), (13, 2))
    plan = install.plan_install(probe, venv_dir=tmp_path, python_executable="python")
    assert plan.backend == "cuda"
    assert plan.gpu is older
    assert plan.cuda_indexes
    assert all("/cu13" not in index for index in plan.cuda_indexes)
    assert plan.cuda_indexes == tuple(
        index
        for index in install.compatible_cuda_indexes((13, 2), (6, 1))
        if index in install.compatible_cuda_indexes((13, 2), (8, 6))
    )


def test_forced_remote_plan(tmp_path: Path) -> None:
    device = install.GPU(0, "GPU", 8 * 1024**3, (8, 6))
    plan = install.plan_install(
        install.Probe("Linux", (device,), (12, 5)), "remote", tmp_path
    )
    assert plan.backend == "remote"
    assert plan.cuda_indexes == ()


@pytest.mark.parametrize(
    ("system", "relative"), [("Windows", "Scripts/python.exe"), ("Linux", "bin/python")]
)
def test_venv_python(system: str, relative: str, tmp_path: Path) -> None:
    assert install.venv_python(tmp_path, system) == tmp_path / relative


def test_dry_run_does_not_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(install, "probe_hardware", lambda: install.Probe("Darwin", ()))
    execute = Mock(side_effect=AssertionError("dry-run must not install"))
    monkeypatch.setattr(install, "execute_plan", execute)
    assert install.main(["--dry-run", "--venv", str(tmp_path / "new-venv")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["backend"] == "remote"
    assert summary["dry_run"] is True
    assert not (tmp_path / "new-venv").exists()
    execute.assert_not_called()
