#!/usr/bin/env python3
"""Validate an existing GPU server profile; start it only with explicit --run."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_CONFIG_BYTES = 1024 * 1024
MAX_KEY_BYTES = 1024 * 1024
DEFAULT_GPU_PATTERN = r"CUDA[0-9]+"
RETRY_EXIT_CODE = 1


class StartupError(Exception):
    """A safe operational message containing no file contents or subprocess output."""


@dataclass(frozen=True)
class BootConfig:
    llama_server: str
    working_directory: str
    api_key_file: str
    model_arguments: tuple[str, ...]
    gpu_device_pattern: str = DEFAULT_GPU_PATTERN
    probe_timeout: float = 10.0


def _absolute_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise StartupError("Configure all required absolute paths before starting.")
    path = Path(value)
    if not path.is_absolute():
        raise StartupError("Configure all required absolute paths before starting.")
    return path


def _reject_cpu_setting(option: str, value: str) -> None:
    if option in {"--device", "-dev"} and value.strip().lower() in {"none", "cpu"}:
        raise StartupError("This profile requires GPU inference; CPU mode is disabled.")
    if option in {"--n-gpu-layers", "--gpu-layers", "-ngl"}:
        try:
            cpu_only = int(value) == 0
        except ValueError:
            cpu_only = False
        if cpu_only:
            raise StartupError(
                "This profile requires GPU inference; CPU mode is disabled."
            )


def _validate_arguments(arguments: Any) -> tuple[str, ...]:
    if not isinstance(arguments, list) or not arguments:
        raise StartupError("Configure the existing GPU model argument list.")
    if any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in arguments):
        raise StartupError("Model arguments must be a nonempty list of text arguments.")
    for index, argument in enumerate(arguments):
        option, separator, inline_value = argument.partition("=")
        if option.startswith("--"):
            # llama.cpp accepts underscores as aliases for long-option hyphens.
            option = option.replace("_", "-")
        if option in {
            "--",
            "--host",
            "--port",
            "-l",
            "--list-devices",
        } or option.startswith(("--api-key", "--ssl-")):
            raise StartupError(
                "Connection, authentication and TLS arguments are managed."
            )
        value = (
            inline_value
            if separator
            else arguments[index + 1]
            if index + 1 < len(arguments)
            else ""
        )
        _reject_cpu_setting(option, value)
    return tuple(arguments)


def load_config(path: str | Path) -> BootConfig:
    """Read configuration and existing key metadata without creating any files."""
    try:
        with Path(path).open("r", encoding="utf-8-sig") as source:
            raw = source.read(MAX_CONFIG_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise StartupError("The startup configuration exceeds the size limit.")
        data = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        raise StartupError("The startup configuration could not be read.") from None
    required = {"llama_server", "working_directory", "api_key_file", "model_arguments"}
    if (
        not isinstance(data, dict)
        or not required <= data.keys()
        or data.keys() - required - {"gpu_device_pattern", "probe_timeout"}
    ):
        raise StartupError("The startup configuration has missing or unknown fields.")
    binary = _absolute_path(data["llama_server"])
    directory = _absolute_path(data["working_directory"])
    key_file = _absolute_path(data["api_key_file"])
    arguments = _validate_arguments(data["model_arguments"])
    pattern = data.get("gpu_device_pattern", DEFAULT_GPU_PATTERN)
    timeout = data.get("probe_timeout", 10.0)
    if not isinstance(pattern, str) or not pattern or len(pattern) > 256:
        raise StartupError("Configure a valid GPU device name pattern.")
    try:
        re.compile(pattern)
    except re.error:
        raise StartupError("Configure a valid GPU device name pattern.") from None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise StartupError("The GPU probe timeout must be finite and positive.")
    try:
        timeout = float(timeout)
    except OverflowError:
        raise StartupError(
            "The GPU probe timeout must be finite and positive."
        ) from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise StartupError("The GPU probe timeout must be finite and positive.")
    try:
        if (
            not binary.is_file()
            or binary.stat().st_size == 0
            or not os.access(binary, os.R_OK | os.X_OK)
        ):
            raise StartupError(
                "The configured server binary is not readable/executable."
            )
        if not directory.is_dir() or not os.access(directory, os.R_OK | os.X_OK):
            raise StartupError("The configured working directory is not accessible.")
        if not key_file.is_file() or not os.access(key_file, os.R_OK):
            raise StartupError("The existing API key file is not readable.")
        with key_file.open("r", encoding="utf-8-sig") as source:
            keys = source.read(MAX_KEY_BYTES + 1)
        if len(keys.encode("utf-8")) > MAX_KEY_BYTES or not any(
            line
            and not line.startswith("#")
            and line.isprintable()
            and not any(character.isspace() for character in line)
            for line in keys.splitlines()
        ):
            raise StartupError("The existing API key file has no valid key line.")
    except (OSError, UnicodeError):
        raise StartupError("The configured startup files are not readable.") from None
    return BootConfig(
        data["llama_server"],
        data["working_directory"],
        data["api_key_file"],
        arguments,
        pattern,
        timeout,
    )


def server_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Use the configured API key file and leave TLS to the separate HTTPS proxy."""
    environment = dict(os.environ if source is None else source)
    _reject_cpu_setting("--device", environment.get("LLAMA_ARG_DEVICE", ""))
    _reject_cpu_setting("--n-gpu-layers", environment.get("LLAMA_ARG_N_GPU_LAYERS", ""))
    for name in tuple(environment):
        if name.startswith(("LLAMA_ARG_SSL_", "LLAMA_SSL_")) or name in {
            "LLAMA_API_KEY",
            "LLAMA_ARG_API_KEY",
            "LLAMA_API_KEY_FILE",
            "LLAMA_ARG_API_KEY_FILE",
        }:
            environment.pop(name)
    return environment


def probe_gpu(config: BootConfig, environment: Mapping[str, str]) -> None:
    """Recognize a real device entry after the official --list-devices heading."""
    try:
        result = subprocess.run(
            [config.llama_server, "--list-devices"],
            cwd=config.working_directory,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.probe_timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise StartupError(
            "GPU detection is unavailable; the LLM was not started."
        ) from None
    if result.returncode != 0:
        raise StartupError("GPU detection failed; the LLM was not started.")
    in_devices = False
    for line in result.stdout.splitlines():
        if line.strip() == "Available devices:":
            in_devices = True
            continue
        if not in_devices:
            continue
        device = re.fullmatch(r"  ([^\s:]+):[ \t]+\S.*", line)
        if (
            device
            and not device[1].upper().startswith("CPU")
            and re.fullmatch(config.gpu_device_pattern, device[1])
        ):
            return
        if line and not line.startswith(" "):
            in_devices = False
    raise StartupError("No eligible GPU was detected; the LLM was not started.")


def start_server(config: BootConfig) -> None:
    environment = server_environment()
    probe_gpu(config, environment)
    command = [
        config.llama_server,
        *config.model_arguments,
        "--host",
        "127.0.0.1",
        "--port",
        "4001",
        "--api-key-file",
        config.api_key_file,
    ]
    try:
        os.chdir(config.working_directory)
        os.execve(config.llama_server, command, environment)
    except OSError:
        raise StartupError("The LLM server could not be started.") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Existing startup JSON file")
    parser.add_argument(
        "--run",
        action="store_true",
        help="Probe the configured GPU binary and start it",
    )
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        if arguments.run:
            start_server(config)
        else:
            print(
                "Startup configuration validated; GPU probe and LLM start were skipped."
            )
    except StartupError as error:
        print(f"LLM startup unavailable: {error}", file=sys.stderr)
        return RETRY_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
