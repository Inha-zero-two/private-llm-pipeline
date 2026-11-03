# PromptIdx 작업 이름 사용

**대상:** [enums.py](../server/app/models/enums.py)의 `PromptIdx`. 설계 6.1의 프롬프트 작업 이름을 공유한다. 현재 파일에는 PromptIdx만 구현되어 있다. 코드 기준은 `7b0b2f8`이다.

**인스턴스:** 실행마다 서비스 객체를 만들지 않는다. 모든 호출이 같은 StrEnum 정의와 작업 항목을 사용한다.

## 처리 흐름

```mermaid
flowchart LR
    A["작업 문자열 또는 PromptIdx"] --> B["PromptIdx로 작업 확인"]
    B --> C["request의 작업 구분과 parser의 출력 형식 선택"]
```

## 사용법과 입출력

```python
from app.models.enums import PromptIdx

task = PromptIdx.ANSWER
assert PromptIdx("ANSWER") == task
# runner.request(..., prompt_idx=task, ...) 또는 문자열 "ANSWER" 사용
```

| 항목 | 용도 |
| --- | --- |
| DETECT | 개인정보 검출 |
| NEEDS | 필요 정보 나누기 |
| JUDGE | 답변 가능성과 동의 필요 판단 |
| WRITE / REWRITE | 요청문 작성·재작성 |
| GUARD | 누출 검사 |
| ANSWER | 최종 답변 |
| PARSE_RULES / MATCH_RULES | 허용 설정 해석·규칙 대응 |
| CONSENT | 동의 문구 작성 |
| DECIDE_CONTEXT | 프론티어 이력의 keep/reset 판단 |
| FRONTIER | 외부 모델용 시스템 프롬프트 |
| MEMO_SUMMARY | 메모 요약 |

- 각 항목의 문자열 값은 이름과 같다. 목록에 없는 값은 PromptIdx 변환에서 ValueError다.
- FRONTIER는 외부 모델용이다. [로컬 parser](local-res-parser.md)에 넘기면 ValueError다.
- 작업 이름은 출력 형식을 고른다. 로컬 컨텍스트 사용 여부는 `request(..., use_context=...)`로 별도 지정한다.

## 설계서 대비 추가 및 변경

- **문자열 열거형:** 원설계의 작업 목록을 저장소 규칙에 따라 StrEnum으로 만들었다. 일부 함수 표의 int 인덱스 대신 이름이 같은 문자열·열거형을 받는다. 작업 13개의 이름과 용도는 유지한다.
- **공유 위치:** PromptManager 전용 상수 대신 Runner·파서도 사용하는 `app/models/enums.py`에 둔다.
- **컨텍스트 결정 분리:** 작업 목록으로 로컬 이력 사용을 고정하지 않는다. use_context는 호출별 옵션이며 DECIDE_CONTEXT의 프론티어 이력 판단과 구분한다.
