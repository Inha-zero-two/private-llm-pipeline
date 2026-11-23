# 파이프라인 오류 계약

공통 정의는 `server/app/models/errors.py`의 `ErrorCode`, `ErrorStage`, `ErrorInfo`,
`PipelineError`, `ServiceError`다. 오류 문구나 Python 예외 이름의 문자열 비교로 분류하지 않는다.

## 실행 실패 코드

| 코드 | 의미 |
| --- | --- |
| LOCAL_LLM_UNAVAILABLE | 로컬 모델 연결·접근 또는 요청 수행 불가 |
| LOCAL_LLM_TIMEOUT | 로컬 모델 대기·생성 제한 시간 초과 |
| MODEL_RESPONSE_INVALID | 응답 형식·내용 규격 위반, 빈 최종 답변 |
| MODEL_OUTPUT_LIMIT | 출력 상한으로 응답을 완성하지 못함 |
| INPUT_LIMIT_EXCEEDED | 입력 예산 초과 |
| COMPACTION_FAILED | 유효한 요약으로도 필요한 공간을 확보하지 못함 |
| CONFIGURATION_ERROR | 필수 프롬프트·모델·접속 설정 오류 |
| STORAGE_ERROR | DB 읽기·쓰기·트랜잭션 실패 |
| STATE_CONSISTENCY_ERROR | 실행 순서·참조 관계·내부 상태 불일치 |
| CLEANUP_ERROR | 본 처리 이후 정리 실패 |
| RUN_INTERRUPTED | 재시작 시 이전 프로세스의 미완료 실행 발견 |
| INTERNAL_ERROR | 위 분류에 없는 예상 밖 구현 오류 |

모델 실패는 허용된 재시도 후 원인을 보존한다. `request()`의 `timeout`, `unavailable`,
`parse_failed`, `length_failed`, `context_full`을 호출 단계에서 각각 위 코드로 변환한다.
중지는 `CancelledError`와 `endReason=stopped`로 처리한다. `manual_request()`는 기존처럼
출력 길이 실패만 재시도하며 그 외 예외는 원인을 유지해 전달한다.

압축 중 모델 연결 실패는 LOCAL_LLM_UNAVAILABLE / COMPACTION 또는 MEMO_SUMMARY다.
요약이 생성됐지만 충분히 줄지 않을 때만 COMPACTION_FAILED를 사용한다. 메모의 다른
항목 요약으로 공간을 확보하는 기존 복구는 유지하고, 실패/복구 진단을 남긴다.

## 외부 질의 실패

FRONTIER_UNAVAILABLE, FRONTIER_TIMEOUT, FRONTIER_AUTH_ERROR, FRONTIER_RATE_LIMITED,
FRONTIER_QUOTA_EXCEEDED, FRONTIER_REQUEST_REJECTED, FRONTIER_RESPONSE_INVALID를 사용한다.
외부 주소·키 누락 등 설정 오류는 CONFIGURATION_ERROR / FRONTIER다.

429와 5xx 및 전송 장애는 기존 재시도 정책을 유지한다. 할당량 소진은 외부 응답의 명시적
`error.code`가 `insufficient_quota`, `quota_exceeded`, `budget_exceeded`인 경우에만 확정하며,
422 자체는 요청 거부로 분류한다. 연결 확인의 bool 인터페이스는 유지하되 `connection_failure`에
상세 원인을 보존한다.

외부 실패는 내부 diagnostic 기록에 남긴다. 질의 후 실패는 query_id로 추적한다. 로컬 최종
답변을 저장하면 recovered=true 기록을 추가하며 실행은 offline / errorCode=null로 완료한다.
로컬 답변도 실패하면 해당 실패가 주 오류이며, 앞선 외부 실패 기록은 남는다.

## 공개 응답과 내부 기록

- SSE `error`: `{code, stage, message}`. stage는 과거 기록에서는 null일 수 있다.
- API-11: `errorCode`, `errorStage`, `notice`. 오류이면 `endReason=null`.
- 사용자 message/notice는 MESSAGES의 고정 문구다. 예외 원문·경로·서비스 응답 원문은 서버 로그에만 남긴다.
- 과거 오류 이벤트를 재전송하거나 과거 실행을 조회할 때도 고정 안내문으로 표시한다.
- `llm_call`은 시도 번호·실패 이유·안전한 오류 정보를 기록한다. 실패 후 성공한 재시도는 실행 오류가 아니다.
- `diagnostic`은 내부 이벤트다. 고정 오류 정보, recovered, 필요한 query_id를 담고 SSE에는 보내지 않는다.
- 최초 주 오류의 code/stage를 보존한다. 이후 정리 실패는 별도 run_error/diagnostic으로 남긴다.
- 본 처리 성공 후 정리만 실패하면 CLEANUP_ERROR가 주 오류이며 하위 원인은 diagnostic에 남긴다.
- DB에 오류를 기록할 수 없으면 서버 로그가 최후 기록 수단이다.

프롬프트 누락, DB·모델 초기화 실패 등 run 생성 전 오류는 서버 시작 로그에 남긴다. 아직
없는 run의 오류 이벤트를 만들지 않는다. API 요청 충돌·검증 오류는 기존 별도 코드를 유지한다.

## 복구와 정상 결과

동의 안내 생성 실패는 고정 문구, 규칙 매칭 실패는 기본 금지, 해석 실패는 기존 설정·동의
대기 유지로 처리한다. SSE 연결 단절만으로 실행을 오류 종료하지 않는다.

LeakGuard가 실제 비허용 정보를 검출하면 재작성한다. 검사 모델 장애나 유효하지 않은 번호는
오류이며 외부 전송하지 않는다. 정상 검사 후 안전한 요청문 재작성 한도 소진만 regen_limit이다.
공개 전부 거부는 오류나 종료 조건이 아니며 금지 상태에서 재판단한다. askUser는 추가하지 않는다.

## 저장 구조와 검증

스키마 버전 2는 runs.errorStage를 추가한다. 신규 DB는 2로 만들며 기존 0은 기존 분류 갱신을
거쳐 1로, 1은 트랜잭션 안에서 2로 갱신한다. DB 삭제 없이 원문·설정·동의·질의 기록을 보존한다.
실패한 갱신은 롤백되며 재시작 시 재시도한다. 지원하지 않는 버전으로 실행하지 않는다.

서버 테스트는 실제 Runner/파서/모듈/API/SQLite에 모델·외부 전송 대역을 연결해 실패·재시도·
복구를 검사한다. 웹 테스트는 실제 어댑터의 실시간 오류와 기록 복원이 같은 코드·단계를 표시하는지
검사한다. 실제 모델·게이트웨이 장애 주입 검증을 대체하지는 않는다.
