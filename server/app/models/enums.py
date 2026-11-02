from enum import StrEnum

# 실행(run)이 끝난 이유. 설계서 2.4
class EndReason(StrEnum):
    INFO_ENOUGH = "info_enough"  # 정보 충분 (정상 종료)
    CALL_LIMIT = "call_limit"  # 질문 상한 도달
    REGEN_LIMIT = "regen_limit"  # 재생성 상한 도달
    BUDGET_LIMIT = "budget_limit"  # 비용 한도 도달
    OFFLINE = "offline"  # 프론티어 연결 불가
    STOPPED = "stopped"  # 사용자 중지
    CONSENT_DENIED = "consent_denied"  # 개인정보 공개 모두 거부
