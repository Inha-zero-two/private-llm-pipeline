# ChatManager

`server/app/data/chat_manager.py`의 SQLite 저장 계층. HTTP 서버나 모델을 생성하지 않으며,
Main·PrivacySettings·ConsentManager·API 계층에서 같은 객체를 공유한다.
공유 자료형은 `app/models/schemas.py`에 있고, `Rule`은 기존 `llm_results.py`의 모델을 사용한다.
`pd_detector.PDItem`, `PDTable`, `PDId` import도 호환성을 위해 유지한다.

## 생성과 종료

명령은 `server/`에서 실행한다. 저장 파일의 상위 폴더는 호출자가 준비한다.

```python
from app.data.chat_manager import ChatManager
from app.models.enums import EndReason
from app.models.schemas import RunConfig

with ChatManager("chat.sqlite3") as chats:
    # 단일 서버 부팅 때만 실행. Main.init()에서도 호출한다.
    chats.recover_interrupted_runs()
    chat_id = chats.create_chat()
    message_id = chats.save_message(chat_id, "user", "질문")
    run_id = chats.start_run(
        chat_id, message_id, None,
        RunConfig(call_limit=3, regen_limit=2, budget_limit=1.0),
    )
    answer_id = chats.save_message(chat_id, "assistant", "답변")
    chats.finish_run(run_id, EndReason.INFO_ENOUGH, answer_id, "최종 메모")
    detail = chats.get_run(run_id)
    print(detail.model_dump(mode="json", by_alias=True))
```

- 엔티티 ID는 최신 화면 API에 맞춘 SQLite `INTEGER PRIMARY KEY AUTOINCREMENT`다. 이벤트 ID는
  실행마다 1부터 증가하고 `(runId, eventId)`가 기본 키다. 내부 필드는 snake_case,
  API·DB 필드는 camelCase다. 시각은 UTC 오프셋을 포함한 ISO 8601 문자열이다.
- 연결은 `check_same_thread=False`로 만들고 객체의 `RLock`으로 트랜잭션 전체를 보호한다.
  모든 메서드는 동기 함수이며 Main은 별도 스레드에서 호출한다. WAL·외래 키를 사용한다.
  조회는 `BEGIN`, 쓰기는 `BEGIN IMMEDIATE`로 구분해 다른 연결의 쓰기 중에도 확정된 데이터를 조회한다.
- 프로세스 재시작 후에도 진행 중 기록은 생성자에서 변경하지 않는다. SSE 재접속은
  `get_pending_consent()`로 같은 프로세스의 대기 요청을 조회한다.
- 실제 서버 재시작 시 `recover_interrupted_runs()`는 이전 실행을 `finished`와
  `RUN_INTERRUPTED`로 종료하고 미응답 동의를 취소한다. 이전 모델 작업을 재개하지 않는다.
  실행 작업을 받기 전에 한 번 호출한다. 다른 서버가 같은 DB로 실행 중일 때 호출하면 안 된다.
- `close()`는 여러 번 호출할 수 있다. 버전 없는 기존 테이블 또는 지원하지 않는 버전의 DB는
  `SCHEMA_VERSION` 오류로 열기를 거부하며 기존 데이터를 덮어쓰지 않는다.
- 스키마 v1은 열 때 v2로 원자적으로 마이그레이션한다. 기존 이벤트 순서를 유지하면서 실행별로
  번호를 다시 부여하고, 끝난 실행의 종료 이벤트를 보완한다. 엔티티와 실행 기록은 유지한다.
  마이그레이션 후 이전 이벤트 커서는 재사용하지 않고 처음부터 재전송해야 한다.

## 저장 책임과 인터페이스

| 담당 | 메서드 | 책임 |
| --- | --- | --- |
| Chat API | `create_chat`, `list_chats`, `get_chat`, `delete_chat` | 채팅 CRUD, 메시지·실행 요약 복원 |
| Main | `save_message`, `start_run`, `finish_run` | 질문·답변, 실행 설정·종료 사유·최종 메모 |
| Main | `set_pd_items`, `get_pd_table` | 검출 결과 저장 후 DB ID가 있는 개인정보 테이블 조회 |
| PrivacySettings | `save_setting`, `load_setting`, `load_settings` | 해석 완료된 규칙 저장과 조회 |
| PrivacySettings | `load_run_setting`, `update_pd_allowed` | 실행 시작 시 고정된 설정과 사전 설정 기반 허용 상태 |
| ConsentManager | `save_consent`, `update_consent`, `get_pending_consent` | 동의 안내·항목·응답 원문·항목별 결정·대기 상태 |
| Main / FrontierClient | `save_frontier_query`, `finish_frontier_query` | 전송 전 요청·허용 상태 보존 후 응답·비용 갱신 |
| Main | `record_regeneration` | 요청문 재생성 횟수 누적 |
| 각 모듈 / API | `log_event`, `get_events` | 동작 기록과 이벤트 ID 이후의 조회 |
| 실행 상세 API | `get_run` | 실행 통계·종료 상태·질의·동의 전체 조회 |

모듈 간 구조화된 입력·반환값은 Pydantic 모델이다. 이벤트의 확장 가능한 payload만
`JsonValue`를 받는다. Main의 기존 dict 실행 설정도 `start_run()`에서 `RunConfig`로 검증할 수 있지만,
새 호출부는 `RunConfig`를 전달한다. HTTP 응답 모델은 API 명세에 필요한 필드를 선택해 구성한다.
`get_chat()`은 `context_compacted` 등 내부 기록을 화면 메시지에서 제외한다.

질문은 첫 사용자 메시지의 앞 30자로 채팅 제목을 정한다. 목록은 최근 갱신 순이며,
메시지·실행·질의·동의는 각각 생성 ID 또는 회차 순으로 반환한다.

## 공개 설정과 임시 동의

- 설정 저장은 새 버전을 추가한다. 기존 실행의 `settingId`가 참조하는 규칙은 바뀌지 않는다.
  설정을 해석할 수 없으면 PrivacySettings가 `save_setting()`을 호출하지 않는다.
- `load_run_setting(run_id)`는 실행 시작 시 설정이 없었다면 이후 설정을 저장해도 빈 설정을 반환한다.
- 개인정보는 `(chatId, text, category)`로 중복을 제거하고 최초 메시지를 유지한다.
  `set_pd_items()`는 전달받은 허용 상태를 저장하지 않고 기본 금지로 새 항목을 만든다.
- 영구 상태 변경은 `update_pd_allowed(chat_id, items)`만 사용한다. `allowed_by`는 `none` 또는
  `setting`만 허용하며, 허용 상태에는 `setting` 근거가 필요하다. 적용할 전체 테이블을 전달한다.
- 이번 실행의 동의·거부는 실행별 PDTable 복사본에서만 변경한다. `consent`·`denied` 등 임시 근거는
  영구 상태 저장이 거부된다. ConsentManager가 종료 시 원래 상태로 되돌린다.
- 전송 당시 상태는 `save_frontier_query(run_id, query, items)`가 별도 `query_items`에 복사한다.
  이후 설정 변경이나 동의 복구로 과거 전송 기록이 바뀌지 않는다.

## 동의와 질의 처리

`save_consent(run_id, items, purpose, benefit, risk)`는 ID가 발급된 `ConsentRequest`를 반환한다.
한 실행에 미응답 동의는 하나만 허용한다. `update_consent(run_id, consent_id, decisions,
response_text)`는 요청에 포함된 각 항목의 결정을 정확히 하나씩 받아 원문과 함께 원자적으로 저장한다.
언급하지 않은 항목을 금지로 해석하는 책임은 ConsentManager에 있다. 해석 실패 시 갱신하지 않는다.
취소는 `decisions=[]`, `response_text=None`, `cancelled=True`로 호출한다.
종료·중지와 응답이 경합하면 먼저 커밋한 상태를 기준으로 뒤늦은 응답을 거부한다.

프론티어 전송 직전에 `FrontierQueryInput(request=...)`를 저장한다. 기본 상태 `uncertain`은
전송 여부·응답을 확정하지 못한 상태이며, 중지·오류가 발생해도 시도한 요청은 남는다.
수신 후 같은 요청문과 `status="ok"`·응답·비용으로 `finish_frontier_query()`를 호출한다.
확정된 실패는 `failed`로 기록한다. 호출 횟수는 전송 시도 기록마다 한 번 증가하고,
비용은 최초 저장과 결과 갱신의 차액으로 누적되어 중복 응답을 이중 합산하지 않는다.
FrontierClient가 토큰·컨텍스트 정보를 제공하면 `FrontierQueryInput`에 함께 전달한다.
현재 Main 연결에서는 응답·비용·소요 시간을 저장한다.

Main은 실행·토큰·중지 이벤트를 외부로 보내기 전에 저장한다. 오류 정보는 `run_error` 동작 로그로
기록하고, 마지막 `error` 이벤트는 실행 종료와 함께 확정한다.
`LocalLLMRunner(chat_manager=chats, ...)`로 저장소를 주입하면 실행 ID가 있는 모델 호출 로그도
저장한다. 인증키·시스템 프롬프트·이전 대화 전체는 로그에 추가하지 않는다.
종료 후 새 로그는 `RUN_FINISHED`로 거부하며, 취소된 Runner의 늦은 호출 로그는 생략한다.
일반 메시지와 검출 항목의 저장은 Main이 담당하므로 PDDetector가 중복 저장하지 않는다.

## 종료와 삭제 정책

`finish_run()`은 정상·상한·거부·중지·오류 모두 `status="finished"`로 바꾸고 종료 시각을 기록한다.
오류 종료는 `end_reason=None`이며 기본 오류 코드는 `INTERNAL_ERROR`다. 오류 이벤트의 메시지는
채팅 요약의 `notice`로 복원한다. 미응답 동의는 같은 트랜잭션에서 취소한다.
이미 끝난 실행에 대한 종료 호출은 기존 기록을 유지한다.
`finish_run()`은 같은 트랜잭션에서 마지막 `done` 또는 `error` 이벤트까지 저장하고 그 `RunEvent`를
반환한다. 재호출해도 같은 이벤트를 반환하며 중복 생성하지 않는다. Main은 반환된 이벤트를
다시 저장하지 않고 실시간 연결에 전달한다. 전달에 실패해도 저장된 이벤트로 재전송할 수 있다.
재시작 복구도 종료 상태와 마지막 오류 이벤트를 함께 저장한다.

Main은 실행 생성 중 취소돼도 생성 작업의 결과 ID를 확보한 뒤 중지 상태로 종료한다.
메모를 시작하기 전이었다면 메모 조회·정리를 생략한다.

실행 중 채팅 삭제는 `CHAT_RUNNING`으로 거부한다. 종료 후 삭제하면 메시지·개인정보·실행·동의·질의·
질의 항목·로그를 함께 삭제한다. 앱 공통 설정과 그 이력은 유지한다.
다른 채팅의 메시지·개인정보를 연결하려 하면 저장을 취소한다.

`StorageError.code`는 `NOT_FOUND`, `CHAT_RUNNING`, `RUN_FINISHED`, `CONSENT_PENDING`,
`NO_PENDING_CONSENT`, `QUERY_FINISHED`, `SCHEMA_VERSION`을 구분한다. HTTP 상태로의 변환은 API 계층의 책임이다.

## 후속 작업

현재 Main은 하나의 MemoManager를 공유하므로 여러 실행을 동시에 지원하지 않는다.
실행별 메모 분리는 별도 작업이다. 이번 변경은 Main의 동의 판단 분기와 MemoManager를 수정하지 않는다.

## 검증

저장소 컨벤션에 따라 ChatManager 신규 테스트는 저장소 외부에 둔다. 임시 SQLite 파일과
모델 대역으로 재시작 복원·삭제 연쇄·동의 경합·설정 버전·허용 상태 스냅샷·Main의
정상/중지/오류/연결 실패를 검사한다. 기존 Main 테스트 대역은 추가 저장 인터페이스만 맞춘다.

```powershell
python -m ruff check app tests
python -m ruff format --check app
# 저장소 루트와 server를 PYTHONPATH에 추가한 뒤 기존 테스트 실행
python -m pytest tests -q
```
