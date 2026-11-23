# ContextManager 사용과 처리 흐름

[ContextManager](../server/app/runtime/context_manager.py)는 채팅의 이전 사용자 질문·실제 최종 답변을 복원하고 모델 입력 예산에 따라 압축한다. 내부 작업용 임시 대화는 별도로 관리한다.

## 채팅 대화의 저장과 복원

- 저장 기준은 `(chat_id, context_key="conversation")`이다. 질문·답변을 저장할 때 메시지와 context_entries의 context_seq를 같은 트랜잭션에서 추가한다. 원본 messageId의 중복 반영은 고유 제약으로 막는다.
- Main은 질문을 저장한 직후 `begin_turn_context(run_id, chat_id, user_message_id)`로 현재 질문 직전의 순번을 고정한다. 현재 질문은 각 모듈의 입력에 이미 있으므로 이전 대화에 중복 주입하지 않는다.
- NEEDS·JUDGE·WRITE와 ANSWER의 실제 모델 입력에 이전 대화를 자동 주입한다. `use_context=False`에서도 사용자 대화는 사용한다. DETECT·GUARD·REWRITE·동의·설정 해석에는 자동 주입하지 않는다.
- 최초 접근은 최신 유효 compaction과 `context_seq > covered_through_seq`인 증분 기록을 순서대로 읽는다. 요약이 없으면 모든 반영 기록부터 시작한다. 기존 DB는 갱신 중 저장된 user/assistant 메시지를 연결한다. 과거 context_compacted 메시지는 복원 기준으로 채택하지 않는다.
- 실행 중에는 캐시 경계 이후만 추가한다. 다른 객체에서 새 압축이 채택되면 DB의 최신 요약으로 다시 복원한다. 실행 중과 재시작 후에 같은 포맷을 사용한다: `[대화 요약]`, `[user]`, `[assistant]` 뒤에 원문, 이후 `[현재 입력]`.
- Runner는 채팅 캐시를 최대 8개 LRU로 유지한다. 퇴거·재시작 후에도 DB로 복원한다. 종료 시 실행 경계를 해제하고 채팅 삭제 시 캐시도 제거한다.
- 실패·재시도 llm_call 로그는 재생하지 않는다. 중지된 최종 답변은 Main이 사용자에게 제공한 부분과 중지 표시를 메시지로 저장한 경우에만 이후 대화에 포함된다.

## 압축 경계와 실패 처리

전체 컨텍스트는 32,768토큰, 일반·요약 출력 상한은 각각 5,000토큰이다. 이력 자동 압축 기준은 21,744토큰이며 요약 프롬프트의 실제 입력 예산도 확인한다. 최종 요청은 시스템 프롬프트·현재 입력·역할 형식·출력 상한을 포함해 tokenizer로 다시 확인한다.

일반 증분 추가는 기존 prefix만 요약하고 새 기록은 그대로 붙인다. 예를 들어 seq 20까지 요약하고 seq 21을 붙이면 covered_through_seq는 20이다. 압축 기록 자체에는 새 context_seq를 주지 않는다. 이후 모델 입력 예산이 부족하면 현재 경계 전체를 추가 압축할 수 있다.

이전 메시지 한 개가 이력 예산에 들어가지 않으면 원문을 DB에 보존하고 문자 경계로 분할한다. 각 조각은 모델 tokenizer로 길이를 확인하고 직전 요약과 합쳐 순서대로 요약한다. 중간 조각 요약은 체크포인트로 저장하지 않는다. 메시지 전체를 처리하고 길이 검사를 통과한 요약만 해당 메시지의 context_seq까지 포함한 기록으로 확정한다.

채택된 요약과 compaction 이벤트는 한 트랜잭션으로 저장하고 성공 후 캐시를 갱신한다. 저장 실패·빈 출력·모델 장애·length 재시도 소진·길이 미축소는 기존 유효 체크포인트를 유지한다. 최신 compaction ID 비교(CAS)가 충돌하면 캐시를 무효화하고 오류를 전달하며 다음 요청에서 DB를 다시 읽는다. 요약 범위를 과거 순번으로 되돌리는 저장도 거부한다.

요약 호출에는 현재 run_id를 전달한다. 압축 도중 stop 신호가 와도 진행 중인 요약은 완료하고 상위 요청은 다음 일반 모델 생성 전에 중지한다. DB에서 이미 종료된 실행의 늦은 요약 저장은 거부한다. 입력 자체가 한도를 넘으면 INPUT_LIMIT_EXCEEDED, 유효한 요약으로 공간을 확보하지 못하면 COMPACTION_FAILED이며 단계는 COMPACTION이다.

## 내부 작업용 API

`runner.get_context_manager(run_id)`와 `request(..., use_context=True)`는 실행·채팅·작업별 임시 호출 이력이다. 이 이력은 사용자 질문·최종 답변의 영속 대화와 별개이며 실행 종료 시 제거한다. 다른 실행에는 재사용하지 않는다. `regen(..., run_id=..., use_context=True, context_id=...)`도 같은 실행의 이력을 조회만 한다.

| 호출 | 동작 |
| --- | --- |
| get_chat_context_manager(chat_id) | 채팅의 DB 복원 가능한 캐시 조회 |
| prepare_input(current, system_prompt, through_seq, run_id) | 고정 경계까지 복원·압축 후 ContextInput(text, reference) 반환 |
| get_context_manager(run_id) | 내부 작업용 실행 객체 조회 |
| get_id(chat_id, prompt_idx) / new_id() | 임시 작업 공간 발급 |
| get_context(id) / get_context_length(id) | 임시 문자열과 모델 토큰 수 조회 |
| append(id, text, run_id=...) | 기존 임시 문자열을 필요시 압축하고 새 내용 추가 |
| compact_context(id, run_id=...) | 임시 문자열을 직접 압축 |

임시 API는 성공 시 True, 실패 시 원인별 예외를 전달한다. 임시 작업 압축은 영속 사용자 대화의 체크포인트로 저장하지 않는다.

## 개인정보 경계와 검증

과거 대화는 저장된 원문·순서 또는 채택된 요약·증분으로 복원하며 현재 보호 규칙으로 재검출하지 않는다. 현재 질문 검출, 기존 pd_items에 대한 현재 설정 적용, 외부 전송 전 LeakGuard, 실행별 일시적 동의 해제는 유지한다. 프론티어 이력은 실행별로 관리한다.

`server/tests/test_context_recovery.py`는 DB 갱신·원자성·복원·반복 압축·분할 요약·실패 및 CAS를 검증한다. `test_api.py`는 실제 모듈을 연결하고 모델 전송 대역에서 NEEDS/JUDGE/WRITE/ANSWER 입력을 캡처해 캐시·재시작·퇴거·압축 후 입력과 격리를 검증한다. 실제 모델의 후속 질문 답변 품질은 별도로 확인해야 한다.
