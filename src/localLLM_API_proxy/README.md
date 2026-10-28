# 내부 테스트용 Llama 어댑터

로컬 CUDA 환경에서는 원본 `llama_cpp.Llama`를 직접 반환하고, 그 외 환경에서는
`https://satisshin.iptime.org:4000`의 Gemma 4 E4B QAT 서버를 사용합니다.
macOS, CUDA GPU가 없는 환경, GPU VRAM이 7GiB보다 작은 환경은 원격으로 실행됩니다.
테스트용 자가 서명 인증서를 사용하며 라이브러리는 인증서 검증을 생략합니다.

## 설치

이 README가 있는 폴더에서 진행하세요. 저장소 루트라면 먼저 `cd src/localLLM_API_proxy`를 실행합니다.
가상환경을 만든 뒤 그 안의 Python으로 설치하는 방식을 권장합니다.

### 1. 가상환경 구성

Windows:

```powershell
python -m venv .venv
```

Linux/macOS:

```bash
python3 -m venv .venv
```

### 2. 설치 스크립트 실행

Windows:

```powershell
.venv\Scripts\python.exe install.py
```

Linux/macOS:

```bash
.venv/bin/python install.py
```

`python install.py`만 실행해도 설치기 폴더의 `.venv`를 자동 생성·재사용해 그 안에 설치합니다. 다른 가상환경이 활성화되어 있어도 기본 설치 대상은 이 `.venv`이며, 사전 활성화는 필요하지 않습니다.
다른 기존 가상환경을 사용하려면 `python install.py --venv "C:\path\to\venv"`로 지정하세요.

CUDA 환경에서는 공식 CUDA wheel을 먼저 사용하고, 맞는 wheel이 없으면 설치된 CUDA Toolkit·C++ 컴파일러로 빌드합니다.
빌드 도구가 없거나 설치에 실패하면 오류를 알려줍니다. 로컬 실행용 GGUF 모델은 별도로 준비합니다.
GPU 번호를 설치 때 직접 고를 필요는 없습니다. 설치기는 보이는 CUDA GPU 중 총 VRAM이 7GiB 이상인 장치를 확인해 호환 빌드를 설치하며, 실행 GPU를 저장·고정하지 않습니다.

## 인증키 배치

다른 앱에서 가져다 쓸 때는 `llama_wrapper.py`를 실행 파일(`app.py`) 옆에 복사하세요.
전달받은 키 파일은 실행할 Python 파일 옆이나 하위 폴더에 복사합니다.
```text
my_app/
├─ app.py
├─ llama_wrapper.py
└─ keys/
   └─ llama_api_key_01.key
```

현재 작업 폴더가 아니라 **실행한 Python 파일의 폴더**를 기준으로 그 하위폴더까지 찾습니다.
로컬 실행시에는 키를 확인하지 않습니다.

## 호출

```python
from contextlib import closing
from llama_wrapper import Llama

with closing(Llama(model_path="models/gemma.gguf", n_ctx=65536)) as llm:
    response = llm.create_chat_completion(
        messages=[{"role": "user", "content": "대한민국의 수도는 어디야?"}],
        max_tokens=512,
        temperature=0.2,
    )
    print(response["choices"][0]["message"]["content"])
```

호출 시에는 보이는 적격 GPU 중 첫 번째를 골라 GPU 하나만 사용합니다. 직접 고르려면 `Llama(model_path="models/gemma.gguf", main_gpu=1)`처럼 지정하세요.
`CUDA_VISIBLE_DEVICES`로 제한하면 보이는 순서의 논리 번호를 사용하므로 첫 장치는 `main_gpu=0`입니다. 필터는 설치·실행 모두 같은 값으로 Python 프로세스 시작 전에 적용하세요.
기본 자동 모드에서 지정한 번호의 GPU가 존재하지 않거나 총 VRAM이 7GiB 미만이면 원격으로 실행합니다.

`response`는 dict이며, 실제 서버 호출에서 다음 필드를 확인했습니다.

| 접근 방법 | 의미 |
| --- | --- |
| `response["choices"][0]["message"].get("content")` | 최종 답변. 이번 응답: `대한민국의 수도는 서울입니다.` |
| `response["choices"][0]["finish_reason"]` | 종료 이유. `stop`은 정상 종료, `length`는 생성 한도 도달 |
| `response["usage"]` | `prompt_tokens` 입력 / `completion_tokens` 생성 / `total_tokens` 합계. 이번 호출: 28 / 9 / 37 |
| `response["model"]`, `response["id"]` | 모델 이름과 요청 ID |
| `response.get("timings", {})` | 서버의 속도·캐시·MTP 통계. 원본 라이브러리에는 보장되지 않음 |

추론은 `response["choices"][0]["message"].get("reasoning_content")`로 확인합니다. `reasoning_content`, `tool_calls`(도구), 스트림의 `delta`는 해당 기능 사용 시에만 생길 수 있습니다. 요청이 성공해도 답변이 비어 있으면 해당 필드와 토큰 한도를 확인하세요.

특정 키는 `Llama(server_token_file="keys/llama_api_key_02.key")`처럼 상대 경로로, 또는 `Llama(server_token_file=r"C:\keys\llama_api_key_02.key")`처럼 절대 경로로 지정할 수 있습니다.
상대 경로는 현재 작업 폴더가 아닌 **실행한 Python 파일의 폴더**를 기준으로 해석합니다.

## 예제

```powershell
.venv\Scripts\python.exe -m example.example_chat
.venv\Scripts\python.exe -m example.example_completion
.venv\Scripts\python.exe -m example.example_stream
.venv\Scripts\python.exe -m example.example_json
```

예제용 키는 `example/keys/`에 복사하세요. 기존 상위 `컴종설/keys/`는 자동 검색하지 않습니다.
`example/example_chat.py`에서 실제 전체 응답과 최종 답변을 확인할 수 있습니다.

최종 제품에서 원본 라이브러리로 교체할 때는 `from llama_cpp import Llama`로 가져오면 됩니다.
공통 호출 코드는 원본 `llama_cpp.Llama`의 인자와 응답 구조를 사용합니다.

## 코드 컨벤션과 호환성 범위

[PR #9의 코드 컨벤션](https://github.com/Inha-zero-two/private-llm-pipeline/blob/de871642f8d8ab37701351fe2af1efe1c2513cb2/docs/convention-code.md)에 맞춰
함수·변수는 `snake_case`, 클래스는 `PascalCase`, 상수는 대문자와 `_`를 사용합니다.
모든 함수의 인자와 반환값에 타입 힌트를 붙이고, import 순서와 서식은 Ruff로 검사합니다.
변경되는 내부 상태는 상수가 아니므로 `_last_native_diagnostic`처럼 소문자로 씁니다.

이 모듈은 llama-cpp-python을 대체하는 개발용 어댑터이므로 다음은 호환성을 우선합니다.

- `from llama_wrapper import Llama`, 기존 클래스·예외 이름, 메서드와 키워드 인자,
  기본값을 유지합니다. 특히 `create_embedding(input=...)`와 `embed(input=...)`의
  `input`은 원본 API 이름이므로 `input_`으로 바꾸지 않습니다.
- 요청·응답·스트리밍 청크는 기존 dict 형식과 추가 필드를 그대로 유지합니다.
  Pydantic 검증이나 필드 변환을 추가하지 않습니다.
- 공개된 `CudaDevice`, `RoutingInfo`와 설치기의 데이터 구조는 위치 인자 생성과
  불변성을 유지하기 위해 기존 frozen dataclass를 사용합니다. 새로운 런타임 의존성은 없습니다.
- 기존 설치 명령, 예제 실행 경로와 단일 파일 복사를 유지하기 위해
  `src/localLLM_API_proxy/` 경로와 `llama_wrapper.py` 단일 파일 구성을 유지합니다.
  서버의 `app/runtime/` 구조로 옮기는 작업은 이 어댑터의 호환성 변경과 별도로 다룹니다.
- 네이티브 객체를 직접 반환하는 팩터리, 네이티브 도우미 전달, 확장 가능한 JSON 값과
  `*args`/`**kwargs`에는 의도적으로 `Any`를 사용합니다. 타입 힌트가 런타임 검증을 추가하지는 않습니다.

## 개발 검증

이 폴더에서 가상환경의 Python으로 실행합니다.

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest -q
```

`tests/test_llama_wrapper.py`와 `tests/test_install.py`는 HTTP·네이티브 백엔드를
테스트 대역으로 바꿔 호출 규격, dict 응답, 스트리밍, GPU 선택과 설치 계획을 검사합니다.
실제 인증키·서버·GPU 모델이나 네이티브 패키지 설치는 필요하지 않습니다.
`tests/public_api.json`은 컨벤션 적용 전 커밋 `30a447f`의 호출 이름·인자·기본값을 기록합니다.
이는 기존 어댑터와의 호환성 검사이며, llama-cpp-python의 모든 기능을 검증한다는 뜻은 아닙니다.
