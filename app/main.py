"""HTTP API：文物借展封签交接核验服务。"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Query, Response
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from . import __version__
from .db import get_conn, wait_for_db
from .hashing import (
    GENESIS_HASH,
    correction_hash,
    correction_request_hash,
    record_hash,
    request_hash,
)
from .schemas import (
    AuditVerifyOut,
    BatchReport,
    BatchRequest,
    BatchSummary,
    ChainOut,
    CorrectionOut,
    CorrectionRequest,
    ExhibitCreate,
    ExhibitOut,
    RehearsalOut,
)
from .tokens import (
    InvalidTokenError,
    TOKEN_SECRET,
    issue_token,
    parse_token,
    state_matches,
)
from .validation import ExhibitState, validate_batch


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()
    yield


app = FastAPI(
    title="文物借展封签交接核验服务",
    version=__version__,
    lifespan=lifespan,
    description="校验借展交接链的首站、人员衔接、时间递增与封签连续性；"
                "幂等写入，失败批次不落任何交接记录，保留可查询的哈希审计链。",
)

# ---------------------------------------------------------------- 工具


def _exhibit_state(row: dict) -> ExhibitState:
    return ExhibitState(
        exhibit_id=row["exhibit_id"],
        home_location=row["home_location"],
        custodian=row["custodian"],
        current_seal=row["current_seal"],
        last_time=row["last_time"],
        chain_length=row["chain_length"],
    )


def _serialize_items(result) -> list[dict]:
    return [
        {
            "seq": item.seq,
            "ok": item.ok,
            "reasons": [{"rule": r.rule, "message": r.message} for r in item.reasons],
        }
        for item in result.items
    ]


def _serialize_first_break(result) -> dict | None:
    fb = result.first_break
    if fb is None:
        return None
    seq, reason = fb
    return {"seq": seq, "rule": reason.rule, "message": reason.message}


def _build_report(batch_id: uuid.UUID, exhibit_id: str, result, record_count: int, now: datetime) -> dict:
    return {
        "batch_id": str(batch_id),
        "exhibit_id": exhibit_id,
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "record_count": record_count,
        "first_break": _serialize_first_break(result),
        "items": _serialize_items(result),
        "idempotent_replay": False,
        "created_at": now.isoformat(),
    }


def _replay_report(row: dict) -> dict:
    report = dict(row["report"])
    report["idempotent_replay"] = True
    return report


def _serialize_correction(row: dict, idempotent_replay: bool = False) -> dict:
    return {
        "correction_id": str(row["correction_id"]),
        "exhibit_id": row["exhibit_id"],
        "correction_seq": row["correction_seq"],
        "target_chain_seq": row["target_chain_seq"],
        "expected_digest": row["expected_digest"],
        "new_digest": row["new_digest"],
        "reason": row["reason"],
        "original_digest": row["original_digest"],
        "target_record_hash": row["target_record_hash"],
        "prev_hash": row["prev_hash"],
        "correction_hash": row["correction_hash"],
        "created_at": row["created_at"],
        "idempotent_replay": idempotent_replay,
    }


# ---------------------------------------------------------------- 健康检查


@app.get("/health", tags=["meta"])
def health():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------- 展品


@app.post("/api/v1/exhibits", response_model=ExhibitOut, status_code=201, tags=["exhibits"])
def create_exhibit(payload: ExhibitCreate):
    """登记展品（馆藏地/保管人/初始封签为首站与连续性校验的基准）。"""
    try:
        with get_conn() as conn:
            row = conn.execute(
                """
                INSERT INTO exhibits (exhibit_id, name, home_location, custodian, current_seal)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (payload.exhibit_id, payload.name, payload.home_location, payload.custodian, payload.initial_seal),
            ).fetchone()
            return row
    except UniqueViolation:
        raise HTTPException(status_code=409, detail=f"展品「{payload.exhibit_id}」已存在")


@app.get("/api/v1/exhibits/{exhibit_id}", response_model=ExhibitOut, tags=["exhibits"])
def get_exhibit(exhibit_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
    return row


# ---------------------------------------------------------------- 交接批次（核心）


@app.post("/api/v1/handover-batches/rehearse", response_model=RehearsalOut, tags=["handovers"])
def rehearse_handover_batch(payload: BatchRequest):
    """预演一批交接：只读核验，不写入批次报告、交接记录，也不推进展品状态。

    - 接受与正式提交完全相同的展品编号与按序记录，记录格式仍按现有规则校验
      （载荷格式非法时返回 422）。
    - 返回逐条核验结果、首个断点，以及绑定展品当前链状态的 `state_token`。
    - 未知展品返回 404。
    - 正式提交时可在 `Handover-Token` 头携带令牌；链状态未变化才被受理。
    """
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT * FROM exhibits WHERE exhibit_id = %s",
            (payload.exhibit_id,),
        ).fetchone()
    if not exhibit:
        raise HTTPException(status_code=404, detail=f"展品「{payload.exhibit_id}」不存在")

    result = validate_batch(_exhibit_state(exhibit), payload.records)
    now = datetime.now(timezone.utc)
    token = issue_token(
        TOKEN_SECRET,
        payload.exhibit_id,
        exhibit["chain_length"],
        exhibit["chain_head_hash"],
        issued_at=int(now.timestamp()),
    )
    return {
        "exhibit_id": payload.exhibit_id,
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "record_count": len(payload.records),
        "first_break": _serialize_first_break(result),
        "items": _serialize_items(result),
        "rehearsed_at": now.isoformat(),
        "state_token": {
            "token": token,
            "exhibit_id": payload.exhibit_id,
            "chain_length": exhibit["chain_length"],
            "chain_head_hash": exhibit["chain_head_hash"],
            "issued_at": now.isoformat(),
        },
    }


@app.post("/api/v1/handover-batches", response_model=BatchReport, tags=["handovers"])
def submit_handover_batch(
    payload: BatchRequest,
    idempotency_key: str = Header(min_length=1, max_length=128),
    handover_token: str | None = Header(
        default=None,
        alias="Handover-Token",
        max_length=2048,
        description="可选：预演（/rehearse）签发的展品状态令牌",
    ),
):
    """提交一批交接记录并核验。

    - 幂等：携带 `Idempotency-Key` 请求头；同键同载荷重试返回同一报告
      （`idempotent_replay=true`），同键不同载荷返回 409。
      携带 `Handover-Token` 时令牌计入载荷身份；同键成功重试即使链已推进，
      也直接返回原报告。
    - 预演令牌（可选）：令牌格式无效 -> 400；跨展品或链状态已变化 -> 409，
      冲突时不生成批次报告，调用方须重新预演。不带令牌的提交行为不变。
    - 事务：核验通过则批次报告与全部交接记录一并写入并推进链上状态；
      核验失败仅落核验报告（供幂等重试与审计），不写入任何交接记录。
    """
    # 1) 令牌先验格式/签名（纯本地校验，400 参数错误），再在展品行锁上比对状态
    parsed_token = None
    if handover_token and handover_token.strip():
        try:
            parsed_token = parse_token(TOKEN_SECRET, handover_token)
        except InvalidTokenError as exc:
            raise HTTPException(status_code=400, detail=f"Handover-Token 无效：{exc}")

    records_for_hash = [r.model_dump(mode="json") for r in payload.records]
    req_hash = request_hash(payload.exhibit_id, records_for_hash, handover_token)

    try:
        with get_conn() as conn:
            # 2) 幂等预检（同键成功重试即使链已推进/令牌已过期，也返回原报告）
            existing = conn.execute(
                "SELECT * FROM batches WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的请求载荷",
                    )
                return _replay_report(existing)

            # 3) 锁定展品行，串行化同一展品的批次，保证链式状态一致
            exhibit = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = %s FOR UPDATE",
                (payload.exhibit_id,),
            ).fetchone()
            if not exhibit:
                if parsed_token is not None:
                    # 令牌签名有效但展品对不上：按跨展品状态冲突处理
                    raise HTTPException(
                        status_code=409,
                        detail=f"令牌属于展品「{parsed_token.exhibit_id}」，"
                               f"不能用于展品「{payload.exhibit_id}」，请重新预演",
                    )
                raise HTTPException(status_code=404, detail=f"展品「{payload.exhibit_id}」不存在")

            # 4) 令牌归属与链状态比对（行锁内完成，与链推进互斥）
            if parsed_token is not None:
                if parsed_token.exhibit_id != payload.exhibit_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"令牌属于展品「{parsed_token.exhibit_id}」，"
                               f"不能用于展品「{payload.exhibit_id}」，请重新预演",
                    )
                if not state_matches(
                    parsed_token, exhibit["chain_length"], exhibit["chain_head_hash"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="展品链状态自预演后已变化（哈希链已推进），"
                               "令牌失效，请重新预演后再提交",
                    )

            # 5) 核验
            result = validate_batch(_exhibit_state(exhibit), payload.records)
            batch_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            report = _build_report(batch_id, payload.exhibit_id, result, len(payload.records), now)

            conn.execute(
                """
                INSERT INTO batches (batch_id, idempotency_key, exhibit_id, request_hash, status, report, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (batch_id, idempotency_key, payload.exhibit_id, req_hash,
                 report["status"], Jsonb(report), now),
            )

            # 6) 通过才写交接记录并推进链上状态；失败则只有批次报告落库
            if result.accepted:
                prev_hash = exhibit["chain_head_hash"]
                prev_seal = exhibit["current_seal"]
                chain_seq = exhibit["chain_length"]
                for seq_in_batch, rec in enumerate(payload.records, start=1):
                    chain_seq += 1
                    h = record_hash(
                        prev_hash=prev_hash,
                        exhibit_id=payload.exhibit_id,
                        chain_seq=chain_seq,
                        batch_id=batch_id,
                        location=rec.location,
                        from_person=rec.from_person,
                        to_person=rec.to_person,
                        seal_no=rec.seal_no,
                        seal_change_reason=rec.seal_change_reason,
                        occurred_at=rec.occurred_at,
                        voucher_digest=rec.voucher_digest,
                    )
                    conn.execute(
                        """
                        INSERT INTO handover_records
                            (batch_id, exhibit_id, chain_seq, seq_in_batch, location,
                             from_person, to_person, seal_no, seal_changed, seal_change_reason,
                             occurred_at, voucher_digest, prev_hash, record_hash)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (batch_id, payload.exhibit_id, chain_seq, seq_in_batch, rec.location,
                         rec.from_person, rec.to_person, rec.seal_no, rec.seal_no != prev_seal,
                         rec.seal_change_reason, rec.occurred_at, rec.voucher_digest, prev_hash, h),
                    )
                    prev_hash = h
                    prev_seal = rec.seal_no

                last = payload.records[-1]
                conn.execute(
                    """
                    UPDATE exhibits
                       SET custodian = %s, current_seal = %s, last_time = %s,
                           chain_length = %s, chain_head_hash = %s
                     WHERE exhibit_id = %s
                    """,
                    (last.to_person, last.seal_no, last.occurred_at, chain_seq,
                     prev_hash, payload.exhibit_id),
                )

            return report
    except UniqueViolation:
        # 并发下同键插入：回滚后按已存在的批次返回（等价于后到的重试）
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM batches WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
        if existing and existing["request_hash"] == req_hash:
            return _replay_report(existing)
        raise HTTPException(status_code=409, detail="同一 Idempotency-Key 提交了不同的请求载荷")


@app.get("/api/v1/handover-batches/{batch_id}", response_model=BatchReport, tags=["handovers"])
def get_handover_batch(batch_id: uuid.UUID):
    """按批次号查询核验报告。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM batches WHERE batch_id = %s", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"批次「{batch_id}」不存在")
    return row["report"]


# ---------------------------------------------------------------- 摘要补正单


def _get_correction_or_404(conn, correction_id: uuid.UUID) -> dict:
    row = conn.execute(
        "SELECT * FROM digest_corrections WHERE correction_id = %s",
        (correction_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"补正单「{correction_id}」不存在")
    return row


@app.post(
    "/api/v1/digest-corrections",
    response_model=CorrectionOut,
    status_code=201,
    tags=["corrections"],
)
def submit_digest_correction(
    payload: CorrectionRequest,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128),
):
    """提交凭证摘要补正单（原交接记录与 record_hash 永不改写）。

    - 目标交接记录不存在 -> 404；原因空白、新旧摘要相同、摘要格式非法 -> 422；
      `expected_digest` 与该记录当前有效摘要不符 -> 409（乐观并发，
      不同幂等键以同一预期值并发补正仅一笔生效）。
    - 幂等：同键同载荷重试返回原补正单（`idempotent_replay=true`，HTTP 200）；
      同键异载荷 -> 409。幂等重放先于一切状态校验，即使后续又发生补正也先按键重放。
    - 补正按展品追加独立哈希链，不推进交接链、不改变展品链头，
      既有预演令牌继续有效。
    """
    req_hash = correction_request_hash(
        payload.exhibit_id,
        payload.chain_seq,
        payload.expected_digest,
        payload.new_digest,
        payload.reason,
    )

    try:
        with get_conn() as conn:
            # 1) 幂等优先：先按键重放，即使链已变化/预期已失效也不做状态校验
            existing = conn.execute(
                "SELECT * FROM digest_corrections WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的请求载荷",
                    )
                if response is not None:
                    response.status_code = 200
                return _serialize_correction(existing, idempotent_replay=True)

            # 2) 锁定展品行：串行化同一展品的补正与链游标推进
            exhibit = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = %s FOR UPDATE",
                (payload.exhibit_id,),
            ).fetchone()
            if not exhibit:
                raise HTTPException(
                    status_code=404, detail=f"展品「{payload.exhibit_id}」不存在"
                )

            # 3) 目标交接记录必须存在
            target = conn.execute(
                """
                SELECT chain_seq, voucher_digest, record_hash
                  FROM handover_records
                 WHERE exhibit_id = %s AND chain_seq = %s
                """,
                (payload.exhibit_id, payload.chain_seq),
            ).fetchone()
            if not target:
                raise HTTPException(
                    status_code=404,
                    detail=f"展品「{payload.exhibit_id}」不存在链序号为 {payload.chain_seq} 的交接记录",
                )

            # 4) 当前有效摘要 = 该记录最新一笔补正的新摘要（无补正则为原始摘要）
            latest = conn.execute(
                """
                SELECT new_digest
                  FROM digest_corrections
                 WHERE exhibit_id = %s AND target_chain_seq = %s
                 ORDER BY correction_seq DESC
                 LIMIT 1
                """,
                (payload.exhibit_id, payload.chain_seq),
            ).fetchone()
            current_effective = latest["new_digest"] if latest else target["voucher_digest"]
            if payload.expected_digest != current_effective:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"预期当前有效摘要与实际不符：预期「{payload.expected_digest}」，"
                        f"当前有效为「{current_effective}」，请以最新有效摘要重新补正"
                    ),
                )

            # 5) 追加补正链一环（独立游标；交接链 chain_length/chain_head_hash 不变）
            correction_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            next_seq = exhibit["correction_seq"] + 1
            prev_hash = exhibit["correction_head_hash"]
            chash = correction_hash(
                prev_hash=prev_hash,
                exhibit_id=payload.exhibit_id,
                correction_seq=next_seq,
                correction_id=correction_id,
                target_chain_seq=payload.chain_seq,
                target_record_hash=target["record_hash"],
                expected_digest=payload.expected_digest,
                new_digest=payload.new_digest,
                reason=payload.reason,
                created_at=now,
            )
            conn.execute(
                """
                INSERT INTO digest_corrections
                    (correction_id, idempotency_key, exhibit_id, target_chain_seq,
                     expected_digest, new_digest, reason, target_record_hash,
                     original_digest, correction_seq, prev_hash, correction_hash,
                     request_hash, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (correction_id, idempotency_key, payload.exhibit_id, payload.chain_seq,
                 payload.expected_digest, payload.new_digest, payload.reason,
                 target["record_hash"], target["voucher_digest"], next_seq, prev_hash,
                 chash, req_hash, now),
            )
            conn.execute(
                """
                UPDATE exhibits
                   SET correction_seq = %s, correction_head_hash = %s
                 WHERE exhibit_id = %s
                """,
                (next_seq, chash, payload.exhibit_id),
            )
            row = _get_correction_or_404(conn, correction_id)
            if response is not None:
                response.status_code = 201
            return _serialize_correction(row)
    except UniqueViolation:
        # 并发下同键插入：回滚后按已存补正单重放（等价于后到的重试）
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM digest_corrections WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        if existing and existing["request_hash"] == req_hash:
            if response is not None:
                response.status_code = 200
            return _serialize_correction(existing, idempotent_replay=True)
        raise HTTPException(status_code=409, detail="同一 Idempotency-Key 提交了不同的请求载荷")


@app.get(
    "/api/v1/digest-corrections/{correction_id}",
    response_model=CorrectionOut,
    tags=["corrections"],
)
def get_digest_correction(correction_id: uuid.UUID):
    """按补正单号查询单张补正单（含原记录锚点与补正链哈希）。"""
    with get_conn() as conn:
        row = _get_correction_or_404(conn, correction_id)
    return _serialize_correction(row)


@app.get(
    "/api/v1/exhibits/{exhibit_id}/digest-corrections",
    response_model=list[CorrectionOut],
    tags=["corrections"],
)
def list_digest_corrections(exhibit_id: str, target_chain_seq: int | None = Query(default=None)):
    """查询展品的补正轨迹（按补正链顺序；可按被补正的链序号过滤）。"""
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT exhibit_id FROM exhibits WHERE exhibit_id = %s",
            (exhibit_id,),
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        if target_chain_seq is not None:
            rows = conn.execute(
                """
                SELECT * FROM digest_corrections
                 WHERE exhibit_id = %s AND target_chain_seq = %s
                 ORDER BY correction_seq
                """,
                (exhibit_id, target_chain_seq),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM digest_corrections WHERE exhibit_id = %s ORDER BY correction_seq",
                (exhibit_id,),
            ).fetchall()
    return [_serialize_correction(r) for r in rows]


# ---------------------------------------------------------------- 审计链


@app.get("/api/v1/exhibits/{exhibit_id}/handovers", response_model=ChainOut, tags=["audit"])
def list_handovers(exhibit_id: str):
    """查询展品的完整交接链（含每条记录的链式哈希）。

    每条记录同时呈现：
    - `voucher_digest`：原始交接凭证摘要（永不改写）；
    - `effective_voucher_digest`：当前有效摘要（最新一笔补正的新摘要，无补正则同原值）；
    - `corrections`：该记录的补正轨迹（按补正链顺序）。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT chain_seq, batch_id, seq_in_batch, location, from_person, to_person,
                   seal_no, seal_changed, seal_change_reason, occurred_at, voucher_digest,
                   prev_hash, record_hash, created_at
              FROM handover_records
             WHERE exhibit_id = %s
             ORDER BY chain_seq
            """,
            (exhibit_id,),
        ).fetchall()
        corrections = conn.execute(
            """
            SELECT correction_id, correction_seq, target_chain_seq, expected_digest,
                   new_digest, reason, correction_hash, created_at
              FROM digest_corrections
             WHERE exhibit_id = %s
             ORDER BY correction_seq
            """,
            (exhibit_id,),
        ).fetchall()

    trail: dict[int, list[dict]] = {}
    for c in corrections:
        trail.setdefault(c["target_chain_seq"], []).append({
            "correction_id": str(c["correction_id"]),
            "correction_seq": c["correction_seq"],
            "target_chain_seq": c["target_chain_seq"],
            "expected_digest": c["expected_digest"],
            "new_digest": c["new_digest"],
            "reason": c["reason"],
            "correction_hash": c["correction_hash"],
            "created_at": c["created_at"],
        })

    records = []
    for r in rows:
        r["batch_id"] = str(r["batch_id"])
        rec_trail = trail.get(r["chain_seq"], [])
        r["effective_voucher_digest"] = (
            rec_trail[-1]["new_digest"] if rec_trail else r["voucher_digest"]
        )
        r["corrections"] = rec_trail
        records.append(r)
    return {"exhibit_id": exhibit_id, "chain_length": len(rows), "records": records}


@app.get("/api/v1/exhibits/{exhibit_id}/audit/verify", response_model=AuditVerifyOut, tags=["audit"])
def verify_audit_chain(exhibit_id: str):
    """重放两条哈希链：交接链与摘要补正链各自独立复算，定位首个失效环。

    - 交接链：逐条复算 record_hash 并校验 prev_hash 衔接与链头一致性；
    - 补正链：逐条复算 correction_hash、校验 prev_hash 衔接、锚点 target_record_hash
      与原记录一致、expected_digest 与该环之前的当前有效摘要一致，再校验链尾游标；
    - 顶层 first_invalid_seq 仍指向交接链（兼容原字段），
      补正链首个失效环见 first_invalid_correction_seq 与 chains.corrections。
    """
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT * FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        rows = conn.execute(
            "SELECT * FROM handover_records WHERE exhibit_id = %s ORDER BY chain_seq",
            (exhibit_id,),
        ).fetchall()
        corr_rows = conn.execute(
            "SELECT * FROM digest_corrections WHERE exhibit_id = %s ORDER BY correction_seq",
            (exhibit_id,),
        ).fetchall()

    # -- 交接链 -------------------------------------------------------------
    handover_valid = True
    first_invalid_seq: int | None = None
    handover_detail = "哈希链完整，与展品当前状态一致"
    prev = GENESIS_HASH
    for row in rows:
        expected = record_hash(
            prev_hash=prev,
            exhibit_id=row["exhibit_id"],
            chain_seq=row["chain_seq"],
            batch_id=row["batch_id"],
            location=row["location"],
            from_person=row["from_person"],
            to_person=row["to_person"],
            seal_no=row["seal_no"],
            seal_change_reason=row["seal_change_reason"],
            occurred_at=row["occurred_at"],
            voucher_digest=row["voucher_digest"],
        )
        if row["prev_hash"] != prev or row["record_hash"] != expected:
            handover_valid = False
            first_invalid_seq = row["chain_seq"]
            handover_detail = (
                f"交接链第 {row['chain_seq']} 环哈希校验失败：记录被篡改、删除或顺序被调换"
            )
            break
        prev = row["record_hash"]

    if handover_valid and (
        prev != exhibit["chain_head_hash"] or len(rows) != exhibit["chain_length"]
    ):
        handover_valid = False
        first_invalid_seq = None
        handover_detail = "交接链尾哈希或链长与展品当前状态不一致"

    # -- 补正链 -------------------------------------------------------------
    # 原记录索引，供锚点与有效摘要复算
    records_by_seq = {r["chain_seq"]: r for r in rows}
    # 每个被补正目标的「当前有效摘要」随补正链推进重放
    effective: dict[int, str] = {}
    corr_valid = True
    first_invalid_corr: int | None = None
    corr_detail = "补正链完整，各环锚点与预期摘要均一致"
    corr_prev = GENESIS_HASH
    for idx, crow in enumerate(corr_rows, start=1):
        cseq = crow["correction_seq"]
        tseq = crow["target_chain_seq"]
        invalid_reason: str | None = None

        if cseq != idx:
            invalid_reason = f"补正链第 {cseq} 环链序号不连续（存在删除或乱序）"
        elif crow["prev_hash"] != corr_prev:
            invalid_reason = f"补正链第 {cseq} 环前驱哈希不衔接（补正单被删除、插入或调换）"
        else:
            target = records_by_seq.get(tseq)
            if target is None:
                invalid_reason = f"补正链第 {cseq} 环锚定的交接链第 {tseq} 环不存在"
            else:
                expected_corr = correction_hash(
                    prev_hash=corr_prev,
                    exhibit_id=crow["exhibit_id"],
                    correction_seq=cseq,
                    correction_id=crow["correction_id"],
                    target_chain_seq=tseq,
                    target_record_hash=crow["target_record_hash"],
                    expected_digest=crow["expected_digest"],
                    new_digest=crow["new_digest"],
                    reason=crow["reason"],
                    created_at=crow["created_at"],
                )
                if crow["correction_hash"] != expected_corr:
                    invalid_reason = f"补正链第 {cseq} 环哈希校验失败：补正单被篡改"
                elif crow["target_record_hash"] != target["record_hash"]:
                    invalid_reason = (
                        f"补正链第 {cseq} 环锚点失效：被补正原记录哈希与锚定值不一致"
                    )
                elif crow["original_digest"] != target["voucher_digest"]:
                    invalid_reason = (
                        f"补正链第 {cseq} 环记录的原始摘要快照与交接链第 {tseq} 环不一致"
                    )
                else:
                    prior_effective = effective.get(tseq, target["voucher_digest"])
                    if crow["expected_digest"] != prior_effective:
                        invalid_reason = (
                            f"补正链第 {cseq} 环预期摘要与重放得到的当前有效摘要不一致"
                            "（并发补正/状态被改写）"
                        )

        if invalid_reason is not None:
            corr_valid = False
            first_invalid_corr = cseq
            corr_detail = invalid_reason
            break

        corr_prev = crow["correction_hash"]
        effective[tseq] = crow["new_digest"]

    if corr_valid and (
        corr_prev != exhibit["correction_head_hash"]
        or len(corr_rows) != exhibit["correction_seq"]
    ):
        corr_valid = False
        first_invalid_corr = None
        corr_detail = "补正链尾哈希或链长与展品补正游标不一致"

    overall_valid = handover_valid and corr_valid
    if overall_valid:
        detail = "交接链与补正链均完整，与展品当前状态一致"
    elif not handover_valid and not corr_valid:
        detail = f"{handover_detail}；{corr_detail}"
    elif not handover_valid:
        detail = handover_detail
    else:
        detail = corr_detail

    return {
        "exhibit_id": exhibit_id,
        "chain_length": len(rows),
        "valid": overall_valid,
        "first_invalid_seq": first_invalid_seq,
        "detail": detail,
        "first_invalid_correction_seq": first_invalid_corr,
        "chains": {
            "handovers": {
                "chain_length": len(rows),
                "valid": handover_valid,
                "first_invalid_seq": first_invalid_seq,
                "detail": handover_detail,
            },
            "corrections": {
                "chain_length": len(corr_rows),
                "valid": corr_valid,
                "first_invalid_seq": first_invalid_corr,
                "detail": corr_detail,
            },
        },
    }


@app.get("/api/v1/exhibits/{exhibit_id}/batches", response_model=list[BatchSummary], tags=["audit"])
def list_batches(
    exhibit_id: str,
    status: str | None = Query(default=None, pattern="^(ACCEPTED|REJECTED)$"),
):
    """查询展品的批次历史（含被驳回批次，供审计追溯）。"""
    sql = """
        SELECT batch_id, status, report, created_at
          FROM batches
         WHERE exhibit_id = %s
    """
    params: list = [exhibit_id]
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY created_at, batch_id"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "batch_id": str(r["batch_id"]),
            "status": r["status"],
            "record_count": r["report"]["record_count"],
            "first_break": r["report"]["first_break"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
