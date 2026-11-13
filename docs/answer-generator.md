# AnswerGenerator 사용과 처리 흐름

**대상:** [answer_generator.py](../server/app/pipeline/answer_generator.py)의 `AnswerGenerator`. 설계 3.4의 최종 답변 생성(`generate`)과 제한 안내(`notify_limited`).

**객체 수:** 프로그램당 1개. 생성자에 공유 `LocalLLMRunner`와 `PromptManager`를 넘김(`PromptManager`를 생략하면 Runner에 연결된 것을 씀). 상태를 저장하지 않음.

**역할:** 질문 원문과 메모(프론티어에서 모은 답)를 로컬 모델로 종합해 최종 답변을 토큰 단위로 내보냄. 로컬에서 처리하므로 질문의 개인정보를 그대로 써서 사용자 상황에 맞춘 답을 만들 수 있음. 답변 저장·동의 복구·메모 정리·실행 기록은 Main이 맡음.

## 함수

| 함수 | 입력 | 반환 | 내용 |
| --- | --- | --- | --- |
| `generate()` | `question`, `memo`, `run_id=""`, `chat_id=None` | `Iterator[str]` | 최종 답변 토큰을 순서대로 내보내는 반복자 |
| `notify_limited()` | `end_reason` | `str \| None` | 종료 사유별 안내 문구. `info_enough`는 None |

## generate() 동작

```mermaid
flowchart TD
    A["generate(question, memo, run_id)"] --> B["[질문] + [메모]로 입력 구성"]
    B --> C["작업 스레드: Runner.manual_request(ANSWER 프롬프트, on_token=큐에 넣기)"]
    C -->|토큰| Q["큐"]
    Q --> M["Main이 next()로 하나씩 꺼내 SSE token 전송"]
    C -->|정상 끝| E["반복 종료"]
    C -->|stop(run_id)| S["받은 토큰까지만 내보내고 조용히 종료"]
    C -->|실패·빈 답변·예외| X["반복 도중 예외 → Main이 SSE error와 종료 정리"]
```

- 생성은 `generate()`를 부르는 즉시 별도 스레드에서 시작. Main은 지금처럼 `next(tokens, None)`으로 하나씩 꺼냄
- **중지:** Main이 `run_id`를 넘기므로 `LocalLLMRunner.stop(run_id)`가 생성을 끊음. 반복자는 예외 없이 끝나고, 이미 나온 토큰은 Main의 `answer_parts`에 남아 부분 답변으로 저장됨
- **오류:** 모델 실패(`length_failed` 등)나 빈 답변은 `AnswerGenerationError`(`RuntimeError`), Runner에서 난 예외는 그대로, 반복 도중에 전달. 이미 내보낸 토큰은 그대로 둠
- 잘못된 호출(빈 질문, memo 자료형 오류)과 Runner·PromptManager 미연결은 `generate()` 호출 시점에 예외
- `chat_id`는 다른 모듈과 호출 규격을 맞추기 위한 자리이며 이전 대화는 쓰지 않음

## notify_limited() 문구

`LIMITED_MESSAGES` 상수가 단일 기준(설계 3.4 표). SSE `done`의 `limited` 필드 값.

| EndReason | 안내 |
| --- | --- |
| `info_enough` | 없음 (None) |
| `call_limit` | 질문 횟수 한도에 도달해 지금까지 모은 정보로 답했습니다. |
| `regen_limit` | 개인정보가 포함되지 않은 질문을 만들지 못해 외부 모델에 묻지 못했습니다. |
| `budget_limit` | 사용 비용 한도에 도달해 지금까지 모은 정보로 답했습니다. |
| `offline` | 외부 모델에 연결할 수 없어 현재까지 취합한 정보를 바탕으로 로컬 모델로 답했습니다. |
| `stopped` | 중지되었습니다. |
| `consent_denied` | 공개 동의가 필요한 정보는 보내지 않고 지금까지 모은 정보로 답했습니다. |

설계서 `offline` 문구의 "타방으로"는 "바탕으로"의 오타로 보고 고쳐 씀.

## Main 연결

| Main 위치 | 호출 |
| --- | --- |
| 7) 최종 답변 | `generate(question, memo, run_id, chat_id)` → 토큰마다 SSE `token` |
| 7) 정상 종료가 아닐 때 | `state.limited = notify_limited(end_reason)` → SSE `done`의 `limited`로 보낼 값 |

- 생성 동작 로그(`log_event`)는 ChatManager 구현 후 연결. 지금은 주석으로 위치만 표시

## 알려진 한계

- Runner의 `manual_request`는 출력 상한(5,000토큰)에 걸리면 같은 입력으로 다시 생성함. 이때 앞 시도에서 이미 보낸 토큰이 화면에 남은 채 처음부터 다시 흘러감. 상한이 커서 드물지만, 생기면 답변이 겹쳐 보일 수 있음
- 메모는 Main이 `memo_manager.read_all()`로 받은 그대로 넣음. 메모가 모델 입력 한도를 넘으면 Runner가 실패를 돌려주고 오류로 처리됨

## 사용법

```python
from app.pipeline.answer_generator import AnswerGenerator

gen = AnswerGenerator(runner, prompts)
for token in gen.generate(question, memo, run_id):
    print(token, end="")
limited = gen.notify_limited(end_reason)  # info_enough면 None
```
