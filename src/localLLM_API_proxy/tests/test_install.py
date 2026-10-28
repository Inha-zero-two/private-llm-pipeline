"""Installer plans are verified without installing native packages or calling GPUs."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import install


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
