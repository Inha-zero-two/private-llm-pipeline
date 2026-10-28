"""Synthetic startup checks: no real GPU, key files, models or servers are used."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "start_llm.py"
SPEC = importlib.util.spec_from_file_location("boot_start_llm", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
boot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = boot
SPEC.loader.exec_module(boot)

SECRET = "synthetic-test-key-never-log"
GPU_OUTPUT = "startup diagnostic\nAvailable devices:\n  CUDA0: Test GPU (8192 MiB)\n"


@pytest.fixture
def profile(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    binary = tmp_path / "llama-server"
    binary.write_text("synthetic binary; never execute", encoding="utf-8")
    binary.chmod(0o755)
    directory = tmp_path / "existing-project"
    directory.mkdir()
    key = directory / "existing-api-keys.key"
    key.write_text(f"# existing project key\n{SECRET}\n", encoding="utf-8")
    values = {
        "llama_server": str(binary),
        "working_directory": str(directory),
        "api_key_file": str(key),
        "model_arguments": ["--model", "existing model.gguf", "-ngl", "99"],
    }
    path = tmp_path / "startup.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path, values


def save_profile(profile: tuple[Path, dict[str, Any]], **changes: Any) -> Path:
    path, values = profile
    values.update(changes)
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_default_only_validates_and_reuses_existing_key(
    profile: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, values = profile
    key = Path(values["api_key_file"])
    original = key.read_bytes()
    run = Mock(side_effect=AssertionError("default mode must not probe"))
    execute = Mock(side_effect=AssertionError("default mode must not execute"))
    change_directory = Mock(side_effect=AssertionError("must not change directory"))
    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot.os, "execve", execute)
    monkeypatch.setattr(boot.os, "chdir", change_directory)

    assert boot.main(["--config", str(path)]) == 0

    run.assert_not_called()
    execute.assert_not_called()
    change_directory.assert_not_called()
    assert key.read_bytes() == original
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_run_probes_same_binary_then_replaces_process_with_original_arguments(
    profile: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, values = profile
    events: list[str] = []

    def fake_probe(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        events.append("probe")
        return subprocess.CompletedProcess(args[0], 0, stdout=GPU_OUTPUT, stderr="")

    run = Mock(side_effect=fake_probe)
    execute = Mock(side_effect=lambda *args: events.append("exec"))
    change_directory = Mock(side_effect=lambda *args: events.append("chdir"))
    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot.os, "execve", execute)
    monkeypatch.setattr(boot.os, "chdir", change_directory)
    monkeypatch.setenv("LLAMA_API_KEY", SECRET)
    monkeypatch.setenv("LLAMA_ARG_API_KEY", SECRET)
    monkeypatch.setenv("LLAMA_ARG_API_KEY_FILE", "unrelated-key-path")
    monkeypatch.setenv("LLAMA_ARG_SSL_CERT_FILE", "old-certificate-path")
    monkeypatch.setenv("LLAMA_ARG_SSL_KEY_FILE", "old-private-key-path")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "2")
    monkeypatch.setenv("LLAMA_ARG_DEVICE", "CUDA0")
    monkeypatch.setenv("LLAMA_ARG_N_GPU_LAYERS", "99")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/synthetic/cuda/libraries")

    assert boot.main(["--config", str(path), "--run"]) == 0

    assert events == ["probe", "chdir", "exec"]
    run.assert_called_once()
    assert run.call_args.args[0] == [values["llama_server"], "--list-devices"]
    assert run.call_args.kwargs["cwd"] == values["working_directory"]
    assert run.call_args.kwargs["timeout"] == 10
    assert run.call_args.kwargs["capture_output"] is True
    assert "shell" not in run.call_args.kwargs
    change_directory.assert_called_once_with(values["working_directory"])
    executable, command, environment = execute.call_args.args
    assert executable == values["llama_server"]
    assert command == [
        values["llama_server"],
        *values["model_arguments"],
        "--host",
        "127.0.0.1",
        "--port",
        "4001",
        "--api-key-file",
        values["api_key_file"],
    ]
    assert SECRET not in command
    assert environment["CUDA_VISIBLE_DEVICES"] == "2"
    assert environment["LLAMA_ARG_DEVICE"] == "CUDA0"
    assert environment["LLAMA_ARG_N_GPU_LAYERS"] == "99"
    assert environment["LD_LIBRARY_PATH"] == "/synthetic/cuda/libraries"
    assert environment == run.call_args.kwargs["env"]
    assert not any(name.startswith("LLAMA_ARG_SSL_") for name in environment)
    assert not any(
        "API_KEY" in name for name in environment if name.startswith("LLAMA")
    )


@pytest.mark.parametrize(
    "output",
    [
        "",
        "unknown output",
        "CUDA0: Device mentioned before header\nAvailable devices:\n",
        "Available devices:\n",
        "Available devices:\n  CPU0: CPU backend\n",
        "Available devices:\n CUDA0: Wrong indentation\n",
        "Available devices:\n  CUDA0\n",
        "Available devices:\nOther section:\n  CUDA0: Unrelated diagnostic\n",
    ],
)
def test_absent_or_unknown_gpu_never_starts_cpu(
    profile: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    path, _ = profile
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, stdout=output, stderr=GPU_OUTPUT
        )
    )
    execute = Mock()
    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot.os, "execve", execute)

    assert boot.main(["--config", str(path), "--run"]) != 0
    execute.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired("probe", 10, output=SECRET, stderr=SECRET),
        OSError(SECRET),
        subprocess.CompletedProcess([], 1, stdout=GPU_OUTPUT, stderr=SECRET),
    ],
)
def test_failed_probe_is_bounded_and_does_not_leak_diagnostics(
    profile: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception | subprocess.CompletedProcess[str],
) -> None:
    path = save_profile(profile, probe_timeout=0.5)
    run = (
        Mock(side_effect=failure)
        if isinstance(failure, Exception)
        else Mock(return_value=failure)
    )
    execute = Mock()
    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot.os, "execve", execute)

    assert boot.main(["--config", str(path), "--run"]) != 0

    assert run.call_args.kwargs["timeout"] == 0.5
    execute.assert_not_called()
    captured = capsys.readouterr()
    assert captured.err
    assert SECRET not in captured.out + captured.err


def test_driver_ready_on_later_service_attempt_can_start(
    profile: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = profile
    run = Mock(
        side_effect=[
            subprocess.TimeoutExpired("probe", 10),
            subprocess.CompletedProcess([], 0, stdout=GPU_OUTPUT, stderr=""),
        ]
    )
    execute = Mock()
    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot.os, "execve", execute)
    monkeypatch.setattr(boot.os, "chdir", Mock())

    assert boot.main(["--config", str(path), "--run"]) != 0
    execute.assert_not_called()
    assert boot.main(["--config", str(path), "--run"]) == 0
    execute.assert_called_once()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLAMA_ARG_DEVICE", "none"),
        ("LLAMA_ARG_DEVICE", "CPU"),
        ("LLAMA_ARG_N_GPU_LAYERS", "0"),
    ],
)
def test_inherited_cpu_settings_fail_before_probe(
    profile: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    path, _ = profile
    monkeypatch.setenv(name, value)
    run, execute = Mock(), Mock()
    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot.os, "execve", execute)

    assert boot.main(["--config", str(path), "--run"]) != 0

    run.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--host", "0.0.0.0"],
        ["--port=4000"],
        ["--api-key", SECRET],
        ["--api-key-file=another-file"],
        ["--api_key", SECRET],
        ["--api_key_file=another-file"],
        ["--ssl-cert-file", "old-cert"],
        ["--ssl-key-file=old-key"],
        ["--ssl_key_file", "old-key"],
        ["--list-devices"],
        ["-l"],
        ["--"],
        ["--device=none"],
        ["--device", "CPU"],
        ["--n-gpu-layers=0"],
        ["--n_gpu_layers", "0"],
        ["--gpu-layers", "0"],
        ["-ngl", "0"],
    ],
)
def test_managed_and_cpu_arguments_fail_before_probe(
    profile: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    path = save_profile(profile, model_arguments=arguments)
    run, execute = Mock(), Mock()
    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot.os, "execve", execute)

    assert boot.main(["--config", str(path), "--run"]) != 0

    run.assert_not_called()
    execute.assert_not_called()
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


@pytest.mark.parametrize(
    "missing", ["llama_server", "working_directory", "api_key_file"]
)
def test_missing_required_paths_stop_before_probe(
    profile: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    path, values = profile
    target = Path(values[missing])
    if target.is_dir():
        values[missing] = str(target / "missing-directory")
        save_profile(profile)
    else:
        target.unlink()
    run, execute = Mock(), Mock()
    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot.os, "execve", execute)

    assert boot.main(["--config", str(path), "--run"]) != 0

    run.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize(
    "contents", ["", "# comment\n  \n", " key-with-space\n", "\x00"]
)
def test_invalid_existing_key_does_not_get_replaced(
    profile: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    contents: str,
) -> None:
    path, values = profile
    key = Path(values["api_key_file"])
    key.write_text(contents, encoding="utf-8")
    run, execute = Mock(), Mock()
    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot.os, "execve", execute)

    assert boot.main(["--config", str(path), "--run"]) != 0

    run.assert_not_called()
    execute.assert_not_called()
    assert key.read_text(encoding="utf-8") == contents


def test_unreadable_existing_key_stops_before_probe(
    profile: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, values = profile
    original_access = os.access
    monkeypatch.setattr(
        boot.os,
        "access",
        lambda candidate, mode: (
            str(candidate) != values["api_key_file"]
            and original_access(candidate, mode)
        ),
    )
    run = Mock()
    monkeypatch.setattr(boot.subprocess, "run", run)

    assert boot.main(["--config", str(path), "--run"]) != 0
    run.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"llama_server": "relative/binary"},
        {"model_arguments": []},
        {"model_arguments": ["--model", 1]},
        {"probe_timeout": 0},
        {"probe_timeout": float("nan")},
        {"probe_timeout": True},
        {"gpu_device_pattern": "["},
        {"unrecognized_option": SECRET},
    ],
)
def test_incomplete_configuration_fails_before_probe(
    profile: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    path = save_profile(profile, **changes)
    run, execute = Mock(), Mock()
    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot.os, "execve", execute)

    assert boot.main(["--config", str(path), "--run"]) != 0

    run.assert_not_called()
    execute.assert_not_called()


def test_exec_failure_does_not_echo_command_or_key(
    profile: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _ = profile
    monkeypatch.setattr(
        boot.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, stdout=GPU_OUTPUT)),
    )
    monkeypatch.setattr(boot.os, "chdir", Mock())
    monkeypatch.setattr(boot.os, "execve", Mock(side_effect=OSError(SECRET)))

    assert boot.main(["--config", str(path), "--run"]) != 0

    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
