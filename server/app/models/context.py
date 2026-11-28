"""채팅 대화 컨텍스트의 저장·복원 및 모델 입력 참조 자료형."""

from pydantic import BaseModel, ConfigDict, Field

CONVERSATION = "conversation"


class ContextRecord(BaseModel):
    context_seq: int
    message_id: int
    role: str
    content: str


class Compaction(BaseModel):
    compaction_id: int
    covered_through_seq: int
    summary: str


class ContextSnapshot(BaseModel):
    compaction: Compaction | None = None
    records: list[ContextRecord] = Field(default_factory=list)


class ContextBinding(BaseModel):
    chat_id: int
    through_seq: int


class ContextUse(ContextBinding):
    context_key: str = CONVERSATION
    compaction_id: int | None = None
    history_tokens: int


class ContextInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)
    text: str
    reference: ContextUse | None = None
