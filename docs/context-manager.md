# ContextManager 사용과 처리 흐름

**대상:** [context_manager.py](../server/app/runtime/context_manager.py)의 `ContextManager`. 설계 5.4의 이전 대화 저장, 토큰 수 계산 및 대화 압축 처리. 대화 압축은 기존 대화를 모델로 요약해 저장 내용을 줄이는 기능.

**객체 수:** 이전 대화를 사용하는 실행 ID(run_id)마다 1개를 생성해 재사용. 프로그램 전체에서 여러 객체를 사용할 수 있으며 모두 [LocalLLMRunner 1개](local-llm-runner.md)의 모델을 공유. 각 객체 안에서 채팅 ID(chat_id)와 작업 종류(prompt_idx)에 따라 대화 내용 분리.

## 처리 흐름

```mermaid
flowchart TD
    A["append: 기존 대화에 새 내용 추가"] --> B{"28,000토큰 이상인가"}
    B -->|아니오| C["합산 내용을 메모리에 반영"]
    B -->|예| D["기존 대화만 5,000토큰 목표로 요약"]
    D --> E["요약과 새 내용을 합쳐 28,000토큰 미만인지 확인"]
    E --> F["메모리의 대화 교체 후 요약 로그 함수 호출"]
```

- 요약 응답은 최대 7,000토큰. 5,000토큰은 목표로만 사용하며 초과한 요약도 사용
- 요약 요청에도 이 객체의 run_id 전달. `runner.stop(run_id)`로 중지 신호를 받아도 요약 완료까지 수신
- 일반 요청 로그를 생략하고 저장된 요약으로 별도 로그 함수 호출. 실제 데이터베이스 저장 코드는 미구현
- 새 내용만으로 토큰 한도에 도달하거나 요약 후 합산 내용도 한도 이상인 경우 예외 발생. 요약은 manual_request로 한 번 요청하며, 요청 오류·생성 실패·빈 요약은 예외로 전달하고 기존 대화 유지

## 사용법과 입출력

```python
manager = runner.get_context_manager("run-1")
context_id = manager.get_id("chat-1", "ANSWER")
manager.append(context_id, "\nUser: 질문\nAssistant: 답변\n")
count = manager.get_context_length(context_id)
manager.compact_context(context_id)
summary = manager.get_context(context_id)
```

`request(..., use_context=True)`는 형식 검사를 통과한 답변과 해당 입력을 자동으로 저장. 위 append는 직접 대화 내용을 관리할 때 사용. request로 저장한 대화를 append로 다시 추가하면 중복 저장되므로 한 가지 방식 선택.

| 호출 | 반환과 사용 기준 |
| --- | --- |
| `get_id(chat_id, prompt_idx)` | 채팅·작업에 해당하는 대화 번호 조회 또는 생성. 반환값은 int |
| `new_id()` | 채팅·작업에 연결하지 않은 새 대화 번호 발급 |
| `get_context(context_id)` | 해당 대화 번호에 저장된 문자열 반환 |
| `get_context_length(context_id)` | 저장된 대화를 응답 생성 모델 기준으로 토큰화해 토큰 수 반환 |
| `append(context_id, text)` | 새 내용 추가 후 토큰 한도에 따라 기존 대화 요약. 반환값은 None |
| `compact_context(context_id)` | 저장된 대화를 바로 요약. 빈 대화는 생략. 반환값은 None이며 결과는 get_context로 조회 |

- `ContextManager(runner, run_id, ...)`로 객체를 직접 만들 경우 request 호출 전에 `runner.context_managers[run_id]`에 등록. 기본 설정으로 사용할 때는 get_context_manager로 조회
- context_id는 해당 객체 안에서 사용하는 대화 번호. 서로 다른 실행의 대화 번호가 같아도 저장 내용은 별개
- 대화 내용은 객체의 메모리에 저장. 다른 run_id로 요청하면 기존 실행의 대화는 자동으로 사용하지 않음

## 설계서 대비 추가 및 변경

- **객체 관리:** Runner의 `get_context_manager(run_id)`에서 실행별 객체 조회 또는 생성. 대화 번호와 문자열은 dict에 저장. 같은 스레드에서 다시 획득할 수 있는 RLock으로 메모리 접근 보호
- **자동 요약 시점:** 설계 37쪽의 기존 대화 길이만 검사하는 방식에서 새 내용을 포함해 28,000토큰 이상인지 검사하는 방식으로 변경. 실제 요약 요청은 `_compact_text()`에서 처리
- **요약 길이:** 목표 5,000토큰과 최대 응답 7,000토큰을 구분. 목표를 넘긴 요약도 사용
- **중지 처리:** `runner.stop(manager.run_id)`로 해당 실행에 중지 요청. 요약 완료 후 메모리에 반영하고 이미 완성된 입력·답변은 유지. 자동 요약은 이후 request에서 stopped 반환하며, 직접 호출한 compact_context는 요약 반영 후 None 반환
- **반환값:** 설계서 함수 표의 ‘반환값 없음’에 맞춰 compact_context는 None 반환. 요약 결과는 get_context로 조회
- **데이터베이스 기록:** 최신 [테이블 구조](db-schema.md)에 따라 별도 컨텍스트 테이블은 사용하지 않음. 요약 반영 후 `messages.role="context_compacted"`, `content=요약`으로 한 번 저장할 코드를 주석 처리. 저장 시각은 ChatManager에서 createdAt에 기록할 예정. 요약 전 원문은 저장하지 않으며, new_id로 직접 발급한 대화 번호는 먼저 채팅과 연결 필요
