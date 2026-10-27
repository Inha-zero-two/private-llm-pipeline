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
