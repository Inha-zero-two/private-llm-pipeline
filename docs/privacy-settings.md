# PrivacySettings

`server/app/privacy/privacy_settings.py`의 자연어 공개 설정 모듈이다. 최신
[화면 API 명세](screen-api-spec.md)의 API-09·10을 기준으로 한다.
`ChatManager`와 `LocalLLMRunner`를 생성자로 주입하며 DB나 모델을 직접 생성하지 않는다.

```python
privacy = PrivacySettings(chat_manager=chats, local_llm_runner=runner)
privacy.load_rules()  # 시작 시 설정 ID·규칙 복원
result = privacy.save("나이는 공개해도 돼")  # SaveResult(ok, rules, message)
current = privacy.load()  # PrivacySetting 전체, 모델 호출 없음
# 실행 생성 시 privacy.setting_id를 저장한 뒤:
privacy.apply_to_table(run_id, pd_table)
```

## 저장과 조회

- `parse_rules(text)`는 `list[Rule]`을 반환한다. 서버는 의미를 규칙으로 변환하지 않는다.
  빈 문자열·공백만 있는 입력은 모델 호출 없이 `[]`로 초기화하고 원문은 `""`로 저장한다.
  나머지 원문은 공백을 포함해 보존한다.
- `save(text)`는 해석 후 `save_setting(text, rules)`을 호출한다. 저장 성공 후에만 현재
  설정을 교체한다. 해석 실패는 `SaveResult(ok=False, rules=기존 규칙, message=수정 안내)`이며
  저장하지 않는다. `SaveResult`는 `app/models/privacy_results.py`에 있다.
- 명시적 의미 해석 실패는 모델의 `RuleParseFailure`로 전달된다. 형식·길이·입력 예산 실패도
  기존 설정을 보존한 `ok=False`다. 호출·수신 장애(`unavailable`)는 `ConnectionError`,
  중지는 `InterruptedError`, 저장·조회 오류는 원래 예외를 전달한다. API 계층은 연결 장애를
  `503 LOCAL_LLM_UNAVAILABLE`, 저장·조회 오류를 `500 INTERNAL_ERROR`로 매핑한다.
- `load(setting_id=None)`은 최신 또는 지정된 과거 설정 전체를 조회한다. 과거 설정 조회는
  현재 설정 ID를 바꾸지 않는다. `load_rules()`는 최신 설정을 현재 상태로 복원하며 규칙을 반환한다.
  반환된 객체를 수정해도 모듈 내부 설정이 바뀌지 않도록 복사한다.
- 저장 이력이 없을 때 `setting_id=None`, `text=""`, `rules=[]`, `updated_at=None`은
  저장 계층이 제공한다. 조회 실패를 빈 설정으로 바꾸지 않는다.
- 저장·복원은 직렬화하고 현재 ID 조회는 별도의 짧은 잠금으로 보호한다. 모델 호출이 오래
  걸려도 실행 시작과 중지를 담당하는 이벤트 루프를 ID 조회로 막지 않는다. 실행 시작 시 전달한
  ID가 그 실행의 기준이며, 이미 시작한 실행은 새 설정 저장의 영향을 받지 않는다.

## 실행 적용과 저장 계층 계약

사용하는 동기 인터페이스는 #24의 `save_setting`, `load_setting`, `load_run_setting`,
`update_pd_allowed`다. 기존 Main의 `apply_to_table(run_id, pd_table)` 호출을 유지하기 위해
채팅 ID는 #24에 있는 `get_run(run_id).chat_id`로 조회한다. 타입 확인용 Protocol만 정의하며
`chat_manager.py`나 `schemas.py`를 이 작업에서 만들지 않는다. 실제 공유 `PrivacySetting`
자료형은 #24의 것을 타입 검사 시 참조한다.

`apply_to_table()`은 `load_run_setting(run_id)`의 규칙만 사용한다. 실행 시작 때 설정이
없었다면 이후 새 설정을 저장해도 빈 설정을 유지한다. 이전 실행의 허용·임시 동의 상태는 먼저
전체 금지로 초기화한다. 모델에는 항목의 text·category와 고정된 규칙만 전달하며 이전 대화는
사용하지 않는다. 입력 배열의 인덱스와 개인정보 DB ID를 구분한다.

대응 결과는 항목별로 검사한다. 누락·중복·잘못된 규칙 인덱스·복수 규칙 선택이 있는 항목만
기본 금지로 두고 다른 항목의 정상 대응은 적용한다. 오류 항목의 일부 정상 규칙만 골라 허용하지 않는다.
입력 항목 범위를 벗어난 항목 번호는 무시하며, 그 번호를 DB ID로 해석하지 않는다.
응답 전체를 파싱하지 못하거나 Runner가 실패를 반환하면 전체를 기본 금지로 저장한다.
정상 대응의 명시적 허용·금지는 `allowed_by="setting"`,
미대응은 `allowed_by="none"`이다. 전체 테이블을 `update_pd_allowed(chat_id, items)`에 전달하고
저장 성공 후 실행 테이블에 결과를 반영한다. 저장 실패 시 메모리의 기본 금지를 유지하고 예외를 전달한다.
임시 동의의 적용·해제는 #22의 책임이다.

## 모델과 프롬프트 계약

`PARSE_RULES`는 표현된 허용·금지를 보존한다. 같은 대상에 허용·금지가 함께 있어도 저장하며
적용 시 모델이 금지를 선택한다. 조건부 공개처럼 target/scope/action으로
표현할 수 없는 요청, 대상이나 행동이 불명확한 요청, 설정과 무관한 내용은 부분 저장하지 않고
`{"ok":false,"rules":[],"message":"수정 안내"}`를 반환한다. 명시적 초기화는 빈 규칙 목록을
정상 반환한다. 프롬프트 버전은 `0.4`다.

`MATCH_RULES`는 항목별로 **최종 적용할 규칙 하나** 또는 미대응을 반환한다.
각 `RuleMatch.rules`는 `[규칙 인덱스]` 또는 `[]`다. 모델이 다음 정책을 적용한다.

1. 해당 항목에 적용되는 금지가 하나라도 있으면 scope와 관계없이 금지를 선택한다.
2. 적용되는 금지가 없고 명확한 허용이 있을 때만 허용 규칙을 선택한다.
3. 최종 행동이 같은 규칙이 여러 개면 원래 목록에서 앞선 인덱스를 반환한다.
4. 확실하게 대응하거나 최종 규칙을 선택할 수 없으면 `[]`로 금지한다.

따라서 전화번호 종류 전체를 금지하면 특정 전화번호를 허용한다는 규칙이 있어도 금지한다.
구체 항목 허용을 넓은 금지의 예외로 처리하지 않는다. ‘나이’ 허용은 같은
검출 분류 2에 속하는 ‘직장’을 허용하지 않는다. 서버의 `is_allowed(match, rules)`는 구조와
범위만 검사한 뒤 모델이 선택한 규칙의 action을 읽는다. 서버가 scope 우선순위나 의미상 충돌을
재판단하지 않는다.

## 검증

저장소 컨벤션에 따라 신규 테스트는 저장소 외부에 작성한다. 저장소 대역과 스크립트 모델로
설정 보존·복원, 실행 고정, 기본 금지, 개인정보 인덱스 대응, 저장 오류, Parser·Runner의
해석 불가·형식 재시도·호출 장애 분리를 확인한다. 실제 DB 구현 검증은 #24의 책임이며 실제
모델의 의미 판단 품질은 별도 모델 평가가 필요하다.
