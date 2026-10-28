#!/usr/bin/env python3
"""환경을 확인해 원격용 패키지 또는 CUDA 네이티브 패키지를 별도로 설치한다.

외부 패키지 없이 실행할 수 있다. --dry-run은 계획만 출력하며 설치하지 않는다.
CUDA 선택에는 GPU의 총 VRAM만 사용하고 시스템 RAM은 검사하지 않는다.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
NATIVE_VERSION = "0.3.36"
MIN_VRAM_BYTES = 7 * 1024**3
CPU_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cpu"
CUDA_VERSIONS = ((13, 2), (13, 0), (12, 5), (12, 4), (12, 3), (12, 2), (12, 1), (11, 8))
CACHE_ROOT = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    if platform.system() == "Windows"
    else Path.home() / ".cache"
) / "llama-wrapper/installer-cache"
_last_native_diagnostic = ""


class InstallError(RuntimeError):
    """설치나 CUDA 검증을 완료할 수 없을 때 발생한다."""


@dataclass(frozen=True)
class GPU:
    index: int
    name: str
    total_bytes: int
    compute_capability: tuple[int, int] | None = None


@dataclass(frozen=True)
class Probe:
    system: str
    devices: tuple[GPU, ...]
    driver_cuda: tuple[int, int] | None = None


@dataclass(frozen=True)
class InstallPlan:
    backend: str
    reason: str
    venv_dir: Path
    python_executable: str
    gpu: GPU | None = None
    cuda_indexes: tuple[str, ...] = ()


def _run(
    command: str | Sequence[object],
    *,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """인자 목록 또는 cmd 전용 명령줄을 실행한다. 환경변수는 출력하지 않는다."""
    if isinstance(command, str):
        display, program = command, "Windows 명령줄"
    else:
        command = [str(item) for item in command]
        display, program = subprocess.list2cmdline(command), command[0]
    if not capture:
        print("실행: " + display, flush=True)
    try:
        result = subprocess.run(
            command,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=capture,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise InstallError(f"명령 시간 초과: {program}") from None
    except OSError as error:
        raise InstallError(
            f"명령을 실행할 수 없습니다: {program} ({type(error).__name__})"
        ) from None
    if check and result.returncode:
        detail = (result.stderr or "").strip()[-2000:]
        raise InstallError(
            f"명령 실패: {program} (종료 코드 {result.returncode})"
            + ("\n" + detail if detail else "")
        )
    return result


def _driver_probe(system: str) -> Probe:
    """외부 패키지 없이 CUDA 드라이버의 표시 GPU와 최대 버전을 조회한다."""
    driver = ctypes.CDLL(
        "nvcuda.dll" if platform.system() == "Windows" else "libcuda.so.1"
    )
    driver.cuInit.argtypes = [ctypes.c_uint]
    if driver.cuInit(0):
        return Probe(system, ())
    version = ctypes.c_int()
    driver.cuDriverGetVersion.argtypes = [ctypes.POINTER(ctypes.c_int)]
    driver_version = None
    if not driver.cuDriverGetVersion(ctypes.byref(version)):
        driver_version = (version.value // 1000, version.value % 1000 // 10)
    driver.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    driver.cuDeviceComputeCapability.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_int,
    ]
    driver.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    memory_function = (
        getattr(driver, "cuDeviceTotalMem_v2", None) or driver.cuDeviceTotalMem
    )
    memory_function.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.c_int]
    count = ctypes.c_int()
    driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
    if driver.cuDeviceGetCount(ctypes.byref(count)):
        return Probe(system, ())
    devices = []
    for index in range(count.value):
        device, major, minor = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        memory, name = ctypes.c_size_t(), ctypes.create_string_buffer(256)
        if driver.cuDeviceGet(ctypes.byref(device), index) or memory_function(
            ctypes.byref(memory), device
        ):
            continue
        driver.cuDeviceGetName(name, len(name), device)
        capability = None
        if not driver.cuDeviceComputeCapability(
            ctypes.byref(major), ctypes.byref(minor), device
        ):
            capability = (major.value, minor.value)
        devices.append(
            GPU(index, name.value.decode("utf-8", "replace"), memory.value, capability)
        )
    return Probe(system, tuple(devices), driver_version)


def _smi_probe(system: str) -> Probe:
    """드라이버 라이브러리를 찾지 못했을 때 NVIDIA 도구를 보조적으로 사용한다."""
    executable = shutil.which("nvidia-smi")
    if not executable and system == "Windows":
        candidate = (
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/nvidia-smi.exe"
        )
        executable = str(candidate) if candidate.is_file() else None
    if not executable:
        return Probe(system, ())
    result = _run(
        [
            executable,
            "--query-gpu=index,uuid,name,memory.total,compute_cap",
            "--format=csv,noheader,nounits",
        ],
        capture=True,
        check=False,
        timeout=5,
    )
    if result.returncode:
        return Probe(system, ())
    records = []
    for line in result.stdout.splitlines():
        index, uuid, name, memory, capability = [
            item.strip() for item in line.split(",", 4)
        ]
        parts = tuple(int(part) for part in capability.split("."))
        records.append((index, uuid, name, int(memory) * 1024**2, parts))
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible is not None:
        selected = []
        for identifier in visible.split(","):
            match = next(
                (
                    item
                    for item in records
                    if item[0] == identifier.strip()
                    or item[1].startswith(identifier.strip())
                ),
                None,
            )
            if match is None:
                break
            selected.append(match)
        records = selected
    banner = _run([executable], capture=True, check=False, timeout=5)
    match = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", banner.stdout or "")
    driver_cuda = (int(match[1]), int(match[2])) if match else None
    return Probe(
        system,
        tuple(
            GPU(index, item[2], item[3], item[4]) for index, item in enumerate(records)
        ),
        driver_cuda,
    )


def probe_hardware() -> Probe:
    system = platform.system()
    if system not in {"Windows", "Linux"} or os.environ.get(
        "CUDA_VISIBLE_DEVICES", "unset"
    ).strip() in {"", "-1"}:
        return Probe(system, ())
    try:
        return _driver_probe(system)
    except (OSError, AttributeError, ValueError):
        try:
            return _smi_probe(system)
        except (InstallError, OSError, ValueError):
            return Probe(system, ())


def compatible_cuda_indexes(
    driver_cuda: tuple[int, int] | None, compute_capability: tuple[int, int] | None
) -> tuple[str, ...]:
    """확인된 드라이버 버전과 GPU에 맞는 공식 인덱스만 최신순으로 반환한다."""
    if driver_cuda is None or compute_capability is None:
        return ()
    result = []
    for version in CUDA_VERSIONS:
        if version > driver_cuda:
            continue
        if version[0] == 13:
            supported = compute_capability >= (7, 5)
        elif version[0] == 12:
            # Blackwell은 CUDA 12.8부터 공식 지원하므로 더 오래된 휠은 건너뛴다.
            supported = (6, 0) <= compute_capability < (10, 0)
        else:
            supported = (6, 0) <= compute_capability <= (8, 9)
        if supported:
            result.append(
                f"https://abetlen.github.io/llama-cpp-python/whl/cu{version[0]}{version[1]}"
            )
    return tuple(result)


def plan_install(
    probe: Probe,
    backend: str = "auto",
    venv_dir: str | Path | None = None,
    python_executable: str | Path | None = None,
) -> InstallPlan:
    if backend not in {"auto", "remote"}:
        raise ValueError("backend must be 'auto' or 'remote'")
    target = Path(venv_dir or PROJECT_ROOT / ".venv").expanduser().resolve()
    python_executable = str(python_executable or sys.executable)
    eligible = [
        device for device in probe.devices if device.total_bytes >= MIN_VRAM_BYTES
    ]
    if backend == "remote":
        reason = "원격 실행을 명시적으로 선택했습니다"
    elif probe.system == "Darwin":
        reason = "macOS는 원격 서버를 사용합니다"
    elif probe.system not in {"Windows", "Linux"}:
        reason = "이 운영체제에서는 원격 서버를 사용합니다"
    elif not eligible:
        reason = "표시된 CUDA GPU가 없거나 총 VRAM이 7 GiB 미만입니다"
    else:
        gpu = eligible[0]
        indexes = compatible_cuda_indexes(probe.driver_cuda, gpu.compute_capability)
        for device in eligible[1:]:
            supported = set(
                compatible_cuda_indexes(probe.driver_cuda, device.compute_capability)
            )
            indexes = tuple(index for index in indexes if index in supported)
        return InstallPlan(
            "cuda",
            "CUDA GPU의 총 VRAM이 7 GiB 이상입니다",
            target,
            python_executable,
            gpu,
            indexes,
        )
    return InstallPlan("remote", reason, target, python_executable)


def venv_python(directory: str | Path, system: str | None = None) -> Path:
    return Path(directory) / (
        "Scripts/python.exe"
        if (system or platform.system()) == "Windows"
        else "bin/python"
    )


def ensure_venv(plan: InstallPlan) -> Path:
    target = plan.venv_dir
    executable = venv_python(target)
    if target.exists():
        if not target.is_dir():
            raise InstallError("--venv 경로가 폴더가 아닙니다")
        if (target / "pyvenv.cfg").is_file() and executable.is_file():
            print(f"기존 가상환경을 사용합니다: {target}")
            return executable
        if any(target.iterdir()):
            raise InstallError(
                "--venv 경로가 비어 있지 않거나 가상환경이 불완전합니다. 기존 파일을 지우지 말고 다른 경로를 지정하세요"
            )
    _run([plan.python_executable, "-m", "venv", target])
    if not executable.is_file() or not (target / "pyvenv.cfg").is_file():
        raise InstallError("가상환경 생성 후 Python 실행 파일을 확인할 수 없습니다")
    return executable


def _native_works(python_executable: str | Path, require_cuda: bool) -> bool:
    global _last_native_diagnostic
    code = (
        "import sys,importlib.metadata; "
        f"assert importlib.metadata.version('llama-cpp-python').split('+')[0] == '{NATIVE_VERSION}'; "
        "import llama_cpp; "
        + (
            "llama_cpp.llama_backend_init(); info=llama_cpp.llama_print_system_info(); "
            "info=info.decode('utf-8','replace') if isinstance(info,bytes) else str(info); "
            "assert llama_cpp.llama_supports_gpu_offload() and 'CUDA' in info.upper(); "
            if require_cuda
            else ""
        )
        + "print('native-ok')"
    )
    result = _run([python_executable, "-c", code], capture=True, check=False)
    _last_native_diagnostic = (result.stderr or "").strip()[-2000:]
    return result.returncode == 0 and "native-ok" in (result.stdout or "")


def cuda_backend_works(python_executable: str | Path) -> bool:
    """설치된 동일 버전의 CUDA 백엔드를 매번 새 Python 프로세스에서 검증한다."""
    return _native_works(python_executable, True)


def native_import_works(python_executable: str | Path) -> bool:
    return _native_works(python_executable, False)


def _installed_runtime_major() -> int | None:
    """이미 검색 가능한 runtime과 cuBLAS의 공통 주 버전만 확인한다."""
    if platform.system() == "Linux":
        executable = shutil.which("ldconfig")
        if executable:
            result = _run([executable, "-p"], capture=True, check=False)
            runtime = set(re.findall(r"libcudart\.so\.(\d+)", result.stdout or ""))
            blas = set(re.findall(r"libcublas\.so\.(\d+)", result.stdout or ""))
            common = runtime & blas
            return max(int(item) for item in common) if common else None
    elif platform.system() == "Windows":
        folders = [
            Path(item) for item in os.environ.get("PATH", "").split(os.pathsep) if item
        ]
        if os.environ.get("CUDA_PATH"):
            folders.insert(0, Path(os.environ["CUDA_PATH"]) / "bin")
        for major in (13, 12, 11):
            runtime = "cudart64_110.dll" if major == 11 else f"cudart64_{major}.dll"
            if any((folder / runtime).is_file() for folder in folders) and any(
                (folder / f"cublas64_{major}.dll").is_file() for folder in folders
            ):
                return major
    return None


def order_cuda_indexes(
    indexes: Sequence[str], runtime_major: int | None = None
) -> tuple[str, ...]:
    """설치된 라이브러리에 맞는 후보를 먼저 시도하되 다른 후보도 보존한다."""
    if runtime_major is None:
        return tuple(indexes)
    prefix = f"cu{runtime_major}"
    return tuple(
        sorted(
            indexes, key=lambda index: not index.rsplit("/", 1)[-1].startswith(prefix)
        )
    )


def install_native_wheel(python_executable: str | Path, index: str) -> bool:
    """인덱스마다 캐시와 임시 다운로드 폴더를 분리해 CPU 휠 혼입을 막는다."""
    tag = index.rsplit("/", 1)[-1]
    if index not in {
        CPU_INDEX,
        *(
            f"https://abetlen.github.io/llama-cpp-python/whl/cu{major}{minor}"
            for major, minor in CUDA_VERSIONS
        ),
    }:
        raise InstallError("공식 llama-cpp-python 인덱스만 사용할 수 있습니다")
    cache = CACHE_ROOT / "wheels" / tag
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=cache) as temporary:
        try:
            _run(
                [
                    python_executable,
                    "-m",
                    "pip",
                    "--isolated",
                    "--cache-dir",
                    cache / "pip",
                    "download",
                    "--index-url",
                    index,
                    "--only-binary=:all:",
                    "--no-deps",
                    "--dest",
                    temporary,
                    f"llama-cpp-python=={NATIVE_VERSION}",
                ],
                capture=True,
            )
            wheels = list(Path(temporary).glob("*.whl"))
            if len(wheels) != 1:
                raise InstallError("다운로드한 네이티브 휠을 하나로 결정할 수 없습니다")
            _run(
                [
                    python_executable,
                    "-m",
                    "pip",
                    "install",
                    "--force-reinstall",
                    wheels[0],
                ]
            )
            return True
        except InstallError as error:
            print(f"{tag} 휠을 사용할 수 없습니다: {error}", flush=True)
            return False


def _find_nvcc(environment: Mapping[str, str]) -> Path | None:
    found = shutil.which("nvcc", path=environment.get("PATH"))
    if found:
        return Path(found)
    for variable in ("CUDA_PATH", "CUDA_HOME"):
        if environment.get(variable):
            candidate = (
                Path(environment[variable])
                / "bin"
                / ("nvcc.exe" if platform.system() == "Windows" else "nvcc")
            )
            if candidate.is_file():
                return candidate
    if platform.system() == "Windows":
        base = (
            Path(
                environment.get("PROGRAMFILES")
                or environment.get("ProgramFiles", r"C:\Program Files")
            )
            / "NVIDIA GPU Computing Toolkit/CUDA"
        )
        candidates = sorted(
            base.glob("v*/bin/nvcc.exe"),
            key=lambda path: tuple(
                int(item) for item in re.findall(r"\d+", path.parent.parent.name)
            ),
            reverse=True,
        )
    else:
        candidates = [
            Path("/usr/local/cuda/bin/nvcc"),
            *sorted(
                Path("/usr/local").glob("cuda-*/bin/nvcc"),
                key=lambda path: tuple(
                    int(item) for item in re.findall(r"\d+", path.parent.parent.name)
                ),
                reverse=True,
            ),
        ]
    return next((item for item in candidates if item.is_file()), None)


def _visual_studio_environment(environment: dict[str, str]) -> dict[str, str]:
    if shutil.which("cl.exe", path=environment.get("PATH")):
        return environment
    vswhere = (
        Path(
            environment.get("PROGRAMFILES(X86)")
            or environment.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        )
        / "Microsoft Visual Studio/Installer/vswhere.exe"
    )
    if not vswhere.is_file():
        return environment
    result = _run(
        [
            vswhere,
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        capture=True,
    )
    installation = result.stdout.strip()
    if not installation:
        return environment
    vcvars = Path(installation) / "VC/Auxiliary/Build/vcvarsall.bat"
    if not vcvars.is_file():
        return environment
    # 이 명령의 환경변수 출력은 파싱만 하고 로그나 파일에 기록하지 않는다.
    # cmd의 명령 문자열에는 list2cmdline의 역슬래시 인용 규칙을 적용하지 않는다.
    prefix = subprocess.list2cmdline(
        [environment.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"]
    )
    script = f'chcp 65001 >nul && call "{vcvars}" x64 >nul && set'
    result = _run(prefix + ' "' + script + '"', capture=True)
    updated = dict(environment)
    for line in result.stdout.splitlines():
        if "=" in line and not line.startswith("="):
            name, value = line.split("=", 1)
            updated[name.upper()] = value
    return updated


def prepare_source_environment() -> dict[str, str]:
    """이미 설치된 Toolkit과 C++ 도구만 사용하며 운영체제 도구를 설치하지 않는다."""
    environment = dict(os.environ)
    system = platform.system()
    if system == "Windows":
        environment = _visual_studio_environment(environment)
    nvcc = _find_nvcc(environment)
    if system == "Windows":
        compiler = shutil.which("cl.exe", path=environment.get("PATH"))
    else:
        # CUDA 12.0과 기본 GCC 13의 충돌을 피하려고 설치된 GCC 12를 먼저 고른다.
        compiler = (
            environment.get("CXX")
            or shutil.which("g++-12")
            or shutil.which("g++-11")
            or shutil.which("g++")
            or shutil.which("clang++")
        )
    missing = []
    if nvcc is None:
        missing.append("CUDA Toolkit의 nvcc")
    if not compiler:
        missing.append(
            "Visual Studio C++ Build Tools의 cl.exe"
            if system == "Windows"
            else "g++ 또는 clang++"
        )
    if missing:
        raise InstallError(
            "CUDA 휠 검증에 실패했고 소스 빌드 준비물이 없습니다: "
            + ", ".join(missing)
            + ". 드라이버만으로는 충분하지 않으며 Toolkit과 C++ 도구를 설치한 뒤 다시 실행하세요"
        )
    environment["PATH"] = str(nvcc.parent) + os.pathsep + environment.get("PATH", "")
    environment["CUDACXX"] = str(nvcc)
    environment["CMAKE_GENERATOR"] = "Ninja"
    arguments = (
        environment.get("CMAKE_ARGS", "")
        + ' -DGGML_CUDA=ON -DCMAKE_CUDA_COMPILER="'
        + nvcc.as_posix()
        + '"'
    )
    if system != "Windows":
        environment["CXX"] = str(compiler)
        arguments += ' -DCMAKE_CUDA_HOST_COMPILER="' + Path(compiler).as_posix() + '"'
    environment["CMAKE_ARGS"] = arguments.strip()
    return environment


def install_cuda_native(python_executable: str | Path, plan: InstallPlan) -> None:
    if cuda_backend_works(python_executable):
        print(f"이미 정상인 llama-cpp-python {NATIVE_VERSION} CUDA 빌드를 유지합니다")
        return
    indexes = order_cuda_indexes(plan.cuda_indexes, _installed_runtime_major())
    for index in indexes:
        if install_native_wheel(python_executable, index):
            if cuda_backend_works(python_executable):
                print("공식 CUDA 휠의 새 프로세스 검증을 통과했습니다")
                return
            print(
                "휠의 CUDA 검증에 실패했습니다. CUDA runtime/cuBLAS 라이브러리도 필요할 수 있습니다"
            )
            if _last_native_diagnostic:
                print(_last_native_diagnostic)
    print("사용할 수 있는 공식 CUDA 휠이 없어 소스 빌드를 확인합니다")
    environment = prepare_source_environment()
    _run([python_executable, "-m", "pip", "install", "cmake", "ninja"], env=environment)
    cache = CACHE_ROOT / "source"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as temporary:
        for name in ("TEMP", "TMP", "TMPDIR"):
            environment[name] = temporary
        _run(
            [
                python_executable,
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                "--no-binary=llama-cpp-python",
                "--no-cache-dir",
                f"llama-cpp-python=={NATIVE_VERSION}",
            ],
            env=environment,
        )
    if not cuda_backend_works(python_executable):
        raise InstallError(
            "소스 빌드 후에도 CUDA 검증에 실패했습니다. CUDA runtime/cuBLAS 검색 경로와 드라이버 호환성을 확인하세요\n"
            + _last_native_diagnostic
        )


def install_remote_native(python_executable: str | Path, plan: InstallPlan) -> None:
    if native_import_works(python_executable):
        print(f"기존 llama-cpp-python {NATIVE_VERSION} 설치를 유지합니다")
        return
    if install_native_wheel(python_executable, CPU_INDEX) and native_import_works(
        python_executable
    ):
        return
    print(
        "이 환경의 공식 CPU 휠을 사용할 수 없어 네이티브 패키지를 생략합니다. 원격 HTTP 호출에는 필요하지 않습니다"
    )


def execute_plan(plan: InstallPlan) -> None:
    python_executable = ensure_venv(plan)
    _run([python_executable, "-m", "ensurepip", "--upgrade"])
    _run([python_executable, "-m", "pip", "install", "--upgrade", "pip"])
    # 기본 의존성에는 네이티브 패키지가 없어 CUDA 빌드를 CPU 빌드로 덮어쓰지 않는다.
    _run(
        [
            python_executable,
            "-m",
            "pip",
            "install",
            "-r",
            PROJECT_ROOT / "requirements.txt",
        ]
    )
    if plan.backend == "cuda":
        install_cuda_native(python_executable, plan)
    else:
        install_remote_native(python_executable, plan)
    if plan.backend == "cuda" and not cuda_backend_works(python_executable):
        raise InstallError("의존성 설치 후 최종 CUDA 검증에 실패했습니다")
    _run([python_executable, "-m", "pip", "check"])
    print(f"설치 완료: {python_executable}\n실행 방식: {plan.backend}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("auto", "remote"), default="auto")
    parser.add_argument("--venv", type=Path, default=PROJECT_ROOT / ".venv")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = plan_install(probe_hardware(), args.backend, args.venv, args.python)
        summary = {
            "backend": plan.backend,
            "reason": plan.reason,
            "venv": str(plan.venv_dir),
            "python": plan.python_executable,
            "native_version": NATIVE_VERSION,
            "gpu": plan.gpu.name if plan.gpu else None,
            "gpu_vram_gib": round(plan.gpu.total_bytes / 1024**3, 2)
            if plan.gpu
            else None,
            "cuda_wheel_indexes": plan.cuda_indexes,
            "dry_run": args.dry_run,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        if args.dry_run:
            return 0
        execute_plan(plan)
        return 0
    except (InstallError, OSError) as error:
        print(f"설치 실패: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
