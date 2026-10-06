"""请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .validation import VOUCHER_RE


class HandoverIn(BaseModel):
    """单条交接记录（按时间排列提交，序号由提交顺序决定）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    location: str = Field(min_length=1, max_length=200, description="交接地点")
    from_person: str = Field(min_length=1, max_length=100, description="交出人")
    to_person: str = Field(min_length=1, max_length=100, description="接收人")
    seal_no: str = Field(min_length=1, max_length=100, description="封签号")
    occurred_at: datetime = Field(description="交接时间（必须带时区）")
    voucher_digest: str = Field(min_length=1, max_length=200, description="凭证摘要，如 sha256:<64hex>")
    seal_change_reason: Optional[str] = Field(default=None, max_length=500, description="换签授权理由（换签时必填）")

    @field_validator("occurred_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at 必须携带时区，如 2026-09-01T09:00:00+08:00 或 2026-09-01T01:00:00Z")
        return v


class BatchRequest(BaseModel):
    exhibit_id: str = Field(min_length=1, max_length=64, description="展品编号")
    records: list[HandoverIn] = Field(min_length=1, max_length=1000, description="按时间排列的交接记录")


class ReasonOut(BaseModel):
    rule: str
    message: str


class ItemOut(BaseModel):
    seq: int
    ok: bool
    reasons: list[ReasonOut]


class FirstBreakOut(BaseModel):
    seq: int
    rule: str
    message: str


class BatchReport(BaseModel):
    """核验报告（POST 提交与 GET 查询共用）。"""

    batch_id: str
    exhibit_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    record_count: int
    first_break: Optional[FirstBreakOut]
    items: list[ItemOut]
    idempotent_replay: bool
    created_at: datetime


class ExhibitCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    exhibit_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    home_location: str = Field(min_length=1, max_length=200, description="登记馆藏地（首站校验基准）")
    custodian: str = Field(min_length=1, max_length=100, description="登记保管人")
    initial_seal: str = Field(min_length=1, max_length=100, description="初始封签号")


class ExhibitOut(BaseModel):
    exhibit_id: str
    name: str
    home_location: str
    custodian: str
    current_seal: str
    last_time: Optional[datetime]
    chain_length: int
    chain_head_hash: str
    correction_seq: int = 0
    correction_head_hash: str = "GENESIS"
    created_at: datetime


class HandoverRecordOut(BaseModel):
    chain_seq: int
    batch_id: str
    seq_in_batch: int
    location: str
    from_person: str
    to_person: str
    seal_no: str
    seal_changed: bool
    seal_change_reason: Optional[str]
    occurred_at: datetime
    voucher_digest: str
    effective_voucher_digest: str
    prev_hash: str
    record_hash: str
    created_at: datetime
    corrections: list["CorrectionTrailItem"] = []


class ChainOut(BaseModel):
    exhibit_id: str
    chain_length: int
    records: list[HandoverRecordOut]


class ChainVerifyOut(BaseModel):
    """单条链（交接链或补正链）的复算结果。"""

    chain_length: int
    valid: bool
    first_invalid_seq: Optional[int] = None
    detail: str


class AuditVerifyOut(BaseModel):
    exhibit_id: str
    chain_length: int
    valid: bool
    first_invalid_seq: Optional[int]
    detail: str
    chains: dict[str, ChainVerifyOut]
    first_invalid_correction_seq: Optional[int] = None


class BatchSummary(BaseModel):
    batch_id: str
    status: str
    record_count: int
    first_break: Optional[FirstBreakOut]
    created_at: datetime


class HandoverTokenOut(BaseModel):
    """预演签发的展品状态令牌（正式提交可通过 Handover-Token 头携带）。"""

    token: str = Field(description="自包含 HMAC 令牌，绑定展品与签发时的哈希链状态")
    exhibit_id: str
    chain_length: int
    chain_head_hash: str
    issued_at: datetime


class RehearsalOut(BaseModel):
    """预演核验结果：只读，不写批次报告/交接记录/展品状态。"""

    exhibit_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    record_count: int
    first_break: Optional[FirstBreakOut]
    items: list[ItemOut]
    rehearsed_at: datetime
    state_token: HandoverTokenOut


# ---------------------------------------------------------------- 摘要补正


class CorrectionRequest(BaseModel):
    """凭证摘要补正单。

    原交接记录与其 record_hash 永不改写；补正只追加独立的补正链。
    """

    exhibit_id: str = Field(min_length=1, max_length=64, description="展品编号")
    chain_seq: int = Field(ge=1, description="被补正记录的交接链序号")
    expected_digest: str = Field(min_length=1, max_length=200, description="预期当前有效摘要")
    new_digest: str = Field(min_length=1, max_length=200, description="新摘要")
    reason: str = Field(min_length=1, max_length=500, description="补正原因（不得为空白）")

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("补正原因不得为空白")
        return v

    @field_validator("expected_digest", "new_digest")
    @classmethod
    def _digest_format(cls, v: str) -> str:
        if not VOUCHER_RE.match(v or ""):
            raise ValueError("凭证摘要格式非法，应为「算法:十六进制摘要」，如 sha256:<64位hex>")
        return v

    @model_validator(mode="after")
    def _new_must_differ(self):
        # 旧摘要与新摘要相同无补正意义，直接拒绝（422）
        if self.new_digest == self.expected_digest:
            raise ValueError("新摘要与预期当前有效摘要相同，无需补正")
        return self


class CorrectionTrailItem(BaseModel):
    """挂在某条交接记录下的一条补正轨迹。"""

    correction_id: str
    correction_seq: int
    target_chain_seq: int
    expected_digest: str
    new_digest: str
    reason: str
    correction_hash: str
    created_at: datetime


class CorrectionOut(CorrectionTrailItem):
    """补正单完整响应（含原记录锚点与链上哈希，供审计核对）。"""

    exhibit_id: str
    original_digest: str
    target_record_hash: str
    prev_hash: str
    idempotent_replay: bool = False
