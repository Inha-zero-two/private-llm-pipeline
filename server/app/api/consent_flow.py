"""동의 요청을 화면에 띄우고 API-08 응답을 기다리는 공용 흐름.

가짜 실행(fake_run)과 실제 Main용 대역 ConsentManager가 같은 흐름을 쓴다.
    1) 동의 기록을 만들고 consent_request 이벤트를 보낸다.
    2) API-08이 해석에 성공하면 run.consent_waiter에 결정을 넣어 깨운다.
    3) 기다리는 중에 중지되면 consent_result(cancelled)를 보내고 취소를 이어 간다.
"""

from __future__ import annotations

import asyncio

from app.api.schemas import Decision, PDItem
from app.api.store import RunRecord, Store, now_iso


async def ask_consent(
    store: Store,
    run: RunRecord,
    items: list[PDItem],
    *,
    purpose: str,
    benefit: str,
    risk: str,
) -> list[Decision]:
    """동의를 요청하고 항목별 결정을 돌려준다."""
    consent = store.add_consent(run, items, purpose, benefit, risk)
    run.consent_waiter = asyncio.get_running_loop().create_future()
    await run.events.push(
        "consent_request",
        consent.model_dump(
            by_alias=True, include={"consent_id", "items", "purpose", "benefit", "risk"}
        ),
    )
    try:
        return await run.consent_waiter
    except asyncio.CancelledError:
        if consent.result is None:
            consent.result = "cancelled"
            consent.responded_at = now_iso()
            await run.events.push(
                "consent_result",
                {"consentId": consent.consent_id, "result": "cancelled"},
            )
        raise
    finally:
        run.consent_waiter = None
