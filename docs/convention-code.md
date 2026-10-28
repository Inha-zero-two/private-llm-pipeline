# 코드 컨벤션

## 변수/모듈 이름 규칙

Python 표준인 [PEP 8](https://peps.python.org/pep-0008/#naming-conventions)을 따릅니다.

| 대상 | 형식 | 예시 |
| --- | --- | --- |
| 파일 · 모듈 | snake_case | `leak_guard.py`, `memo_manager.py` |
| 함수 · 메서드 · 변수 | snake_case | `load_rules()`, `max_tokens` |
| 클래스 | PascalCase | `LeakGuard`, `MemoManager` |
| 상수 | 대문자 + `_` | `N_CTX = 32768` |

상세 설계서의 이름(`parseRules()`, `memoManager` 등)을 코드에서 어떻게 쓰는지는 [설계서 ↔ 코드 이름 대응표](naming-map.md)에 모듈별로 정리했습니다.

## 폴더 구조

서버와 화면을 루트에서 폴더 두 개로 나눕니다. 명령(`uv`, `npm`)은 각자 자기 폴더 안에서 실행합니다.

```
private-llm-pipeline/
├── docs/        컨벤션 문서
├── server/      서버 (Python · FastAPI · uv)
└── web/         화면 (React · Vite · TypeScript)
```

### 서버

모듈은 역할별로 폴더를 나눕니다.

```
server/
├── app/
│   ├── main.py                 컨트롤러 (단계 순서 · 종료 조건)
│   ├── api/                    화면 ↔ 서버 API · SSE
│   ├── models/                 여러 모듈이 같이 쓰는 데이터 구조 · 열거형
│   ├── pipeline/               파이프라인 핵심 모듈
│   │   ├── planner.py
│   │   ├── request_writer.py
│   │   ├── leak_guard.py
│   │   └── answer_generator.py
│   ├── privacy/                개인정보 통제 모듈
│   │   ├── pd_detector.py
│   │   ├── privacy_settings.py
│   │   └── consent_manager.py
│   ├── runtime/                실행 환경 모듈 (로컬 LLM · 프론티어 호출)
│   │   ├── local_llm_runner.py
│   │   ├── local_res_parser.py
│   │   ├── frontier_client.py
│   │   └── context_manager.py
│   └── data/                   데이터 관리 모듈 (프롬프트 · 메모 · 채팅)
│       ├── prompt_manager.py
│       ├── memo_manager.py
│       └── chat_manager.py
└── tests/
    ├── pipeline/
    │   └── test_planner.py
    └── ...
```

- 모듈 하나는 파일 하나입니다. 파일 이름은 모듈 이름을 snake_case로 씁니다. (`LeakGuard` → `leak_guard.py`)
- 테스트는 `tests/` 아래에 `app/`과 **같은 폴더 구조**로 두고, 파일 이름은 `test_` + 모듈 파일 이름입니다.

## 모듈 사이 데이터 형식

### 데이터 구조는 Pydantic 모델로

모듈끼리 주고받는 데이터 구조(`SaveResult`, `Rule`, `ConsentRequest`, `Memo` 등)는 [Pydantic](https://docs.pydantic.dev/) 모델로 만듭니다. dict로 주고받지 않습니다.

```python
from typing import Literal
from pydantic import BaseModel

class Rule(BaseModel):
    target: str
    action: Literal["allow", "deny"]
    scope: Literal["item", "category"]
```

- 필수 필드가 빠졌거나 `Literal`로 정한 값이 아닌 값이 들어오면 만드는 순간 에러가 나서, 담당자끼리 형식이 어긋나는 걸 바로 알 수 있습니다. 단, 모델에 없는 필드는 기본 설정에서 에러 없이 무시됩니다.
- 열거형(`EndReason`, `PromptIdx` 등)은 `StrEnum`으로 만듭니다.

### 각 모듈에서 주고받는 데이터 형식

| 구분 | 위치 | 예시 |
| --- | --- | --- |
| 두 모듈 이상이 주고받는 것 | `app/models/` | `EndReason`, `PromptIdx` → `app/models/enums.py` |
| 한 모듈 안에서만 쓰는 것 | 그 모듈 파일 안 | `Rule`을 PrivacySettings만 쓰면 `privacy_settings.py` 안 |

처음엔 모듈 안에 뒀다가 다른 모듈도 쓰게 되면 `app/models/`로 옮깁니다.

### 타입 힌트는 필수

모든 함수의 인자와 반환값에 타입을 적습니다.

```python
def parse_rules(text: str) -> list[Rule]:
    ...
```
