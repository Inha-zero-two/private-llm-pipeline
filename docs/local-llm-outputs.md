# 로컬 모델 공유 결과 타입

**대상:** [local_llm_outputs.py](../server/app/models/local_llm_outputs.py)의 결과 모델과 타입 별칭. 설계 5.2의 작업별 출력과 Planner·개인정보 모듈의 데이터 형식을 연결한다. 코드 기준은 `7b0b2f8`이다.

**인스턴스:** 상주 서비스 인스턴스를 만들지 않는다. 파싱 결과마다 Plan·ConsentText 객체 하나 또는 목록 항목 수만큼 결과 객체가 만들어진다. 정의는 여러 호출이 공유한다.

## 처리 흐름

```mermaid
flowchart LR
    A["JSON 출력"] --> B["parser의 TypeAdapter 검증"]
    B --> C["공유 결과 객체 또는 기본 타입"]
    C --> D["request의 첫 반환값"]
```

## 사용법과 입출력

```python
plan, success, _, _, _ = runner.request(
    "run-1", "판단 자료", "JUDGE", "chat-1", system_prompt="판단 프롬프트"
)
if success:
    print(plan.enough, plan.consent_items)
```

| 정의 | 필드와 값 |
| --- | --- |
| `Index` | 0 이상의 int. 입력 목록의 번호에 사용한다. |
| `ContextMode` | `"keep"` 또는 `"reset"`. |
| `DetectedItem` | `text: str`, `category: int`(0 식별·1 민감·2 속성). |
| `Plan` | `enough: bool`, `need_consent: bool`, `consent_items: list[int]`, `need: list[str]`. consent_items는 실제 PDId 목록이다. |
| `Rule` | `target: str`, `scope: "item" 또는 "category"`, `action: "allow" 또는 "deny"`. |
| `RuleMatch` | `item: Index`, `rules: list[Index]`. 각각 입력 항목·규칙 목록의 번호다. |
| `ConsentText` | `purpose: str`, `benefit: str`, `risk: str`. |
| `ParsedResult` | str, 위 객체·목록, `list[str]`, `list[int]`를 묶은 반환 타입 별칭. |

- Plan의 JSON 필드는 needConsent·consentItems이며 Python에서는 need_consent·consent_items로 읽는다. Python 필드 이름으로 생성하는 것도 허용한다.
- 엄격한 자료형 검사는 [parser](local-res-parser.md)가 `strict=True`로 수행한다. 모델에 없는 추가 필드는 기본적으로 무시한다.
- 번호 상한·PDId 존재 여부·판단 보정·규칙 충돌은 호출 모듈이 확인한다. 검출 결과에 DB ID와 실행 중 허용 상태를 자동으로 붙이지 않는다.

## 설계서 대비 추가 및 변경

- **파일 분리:** 설계서의 출력 표와 도메인 객체를 저장소 규칙에 따라 `app/models/`의 공유 Pydantic 모델로 정의했다. 별도의 파이프라인 단계가 추가된 것은 아니다.
- **이름 대응:** JSON의 camelCase 필드와 Python의 snake_case 필드를 별칭으로 연결했다.
- **검출 결과 분리:** DETECT의 text·category만 DetectedItem으로 받고, PDItem의 ID·저장·허용 상태 관리는 업무 모듈에 맡긴다. 최신 DB의 전송 당시 허용 상태는 query_items에 속한다.
- **타입 별칭:** 출력 표의 목록 번호·keep/reset·작업별 반환값을 Index·ContextMode·ParsedResult로 명시했다.
