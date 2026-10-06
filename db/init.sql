-- 文物借展封签交接核验服务 —— 库结构初始化
-- 由 docker-entrypoint-initdb.d 在首次启动时执行（幂等，可重复执行）。

CREATE TABLE IF NOT EXISTS exhibits (
    exhibit_id      TEXT        PRIMARY KEY,
    name            TEXT        NOT NULL,
    home_location   TEXT        NOT NULL,               -- 登记馆藏地（首站校验基准）
    custodian       TEXT        NOT NULL,               -- 当前保管人（初始为登记保管人）
    current_seal    TEXT        NOT NULL,               -- 当前封签号（初始为登记初始封签）
    last_time       TIMESTAMPTZ,                        -- 最近一环交接时间
    chain_length    BIGINT      NOT NULL DEFAULT 0,
    chain_head_hash TEXT        NOT NULL DEFAULT 'GENESIS',
    -- 摘要补正链独立游标：补正不推进交接链，二者互不影响
    correction_seq        BIGINT NOT NULL DEFAULT 0,
    correction_head_hash  TEXT   NOT NULL DEFAULT 'GENESIS',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 兼容已初始化过的旧库（Compose 数据卷已存在时 init.sql 不会重跑，
-- 此处的 IF NOT EXISTS 供手工重放/既有环境升级使用）。
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS correction_seq BIGINT NOT NULL DEFAULT 0;
ALTER TABLE exhibits ADD COLUMN IF NOT EXISTS correction_head_hash TEXT NOT NULL DEFAULT 'GENESIS';

CREATE TABLE IF NOT EXISTS batches (
    batch_id        UUID        PRIMARY KEY,
    idempotency_key TEXT        NOT NULL,               -- 调用方幂等键
    exhibit_id      TEXT        NOT NULL REFERENCES exhibits (exhibit_id),
    request_hash    TEXT        NOT NULL,               -- 请求体指纹（同键不同载荷 -> 409）
    status          TEXT        NOT NULL CHECK (status IN ('ACCEPTED', 'REJECTED')),
    report          JSONB       NOT NULL,               -- 完整核验报告（含逐项原因）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 幂等约束：同一幂等键全局唯一
CREATE UNIQUE INDEX IF NOT EXISTS uq_batches_idempotency_key ON batches (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_batches_exhibit ON batches (exhibit_id, created_at);

CREATE TABLE IF NOT EXISTS handover_records (
    id                 BIGSERIAL   PRIMARY KEY,
    batch_id           UUID        NOT NULL REFERENCES batches (batch_id),
    exhibit_id         TEXT        NOT NULL REFERENCES exhibits (exhibit_id),
    chain_seq          BIGINT      NOT NULL,            -- 展品级链序号（1 起，连续）
    seq_in_batch       INT         NOT NULL,            -- 批次内序号
    location           TEXT        NOT NULL,
    from_person        TEXT        NOT NULL,
    to_person          TEXT        NOT NULL,
    seal_no            TEXT        NOT NULL,
    seal_changed       BOOLEAN     NOT NULL DEFAULT FALSE,
    seal_change_reason TEXT,
    occurred_at        TIMESTAMPTZ NOT NULL,
    voucher_digest     TEXT        NOT NULL,
    prev_hash          TEXT        NOT NULL,            -- 前一环 record_hash（首环为 GENESIS）
    record_hash        TEXT        NOT NULL,            -- 本环哈希（含 prev_hash，构成哈希链）
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (exhibit_id, chain_seq),
    UNIQUE (record_hash)
);
CREATE INDEX IF NOT EXISTS idx_records_exhibit ON handover_records (exhibit_id, chain_seq);

-- 摘要补正单：原交接记录与 record_hash 永不改写；补正按展品形成独立哈希链。
-- 每环 prev_hash 指向上一补正单的 correction_hash（首环为 GENESIS），
-- 同时以 target_record_hash 锚定被补正的原交接记录（锚错即补正链失效）。
CREATE TABLE IF NOT EXISTS digest_corrections (
    correction_id        UUID        PRIMARY KEY,
    idempotency_key      TEXT        NOT NULL,          -- 调用方幂等键（与批次幂等键分表互不冲突）
    exhibit_id           TEXT        NOT NULL REFERENCES exhibits (exhibit_id),
    target_chain_seq     BIGINT      NOT NULL,          -- 被补正记录在交接链上的链序号
    expected_digest      TEXT        NOT NULL,          -- 提交方预期的「当前有效摘要」
    new_digest           TEXT        NOT NULL,          -- 补正后的新摘要
    reason               TEXT        NOT NULL,          -- 补正原因（非空）
    target_record_hash   TEXT        NOT NULL,          -- 锚定：被补正原记录的 record_hash
    original_digest      TEXT        NOT NULL,          -- 锚定：被补正原记录的原始摘要（快照，不随后续补正变化）
    correction_seq       BIGINT      NOT NULL,          -- 展品级补正链序号（1 起，连续）
    prev_hash            TEXT        NOT NULL,          -- 前一环 correction_hash（首环为 GENESIS）
    correction_hash      TEXT        NOT NULL,          -- 本环哈希（含 prev_hash 与原记录锚点）
    request_hash         TEXT        NOT NULL,          -- 请求体指纹（同键不同载荷 -> 409）
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (exhibit_id, correction_seq),
    UNIQUE (correction_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_corrections_idempotency_key
    ON digest_corrections (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_corrections_exhibit
    ON digest_corrections (exhibit_id, correction_seq);
CREATE INDEX IF NOT EXISTS idx_corrections_target
    ON digest_corrections (exhibit_id, target_chain_seq, correction_seq);

-- 种子数据：两件已登记展品，便于开箱即测
INSERT INTO exhibits (exhibit_id, name, home_location, custodian, current_seal)
VALUES
    ('EX-0001', '商代青铜爵',   '故宫博物院地下库房A区', '张伟', 'SEAL-BJ-0001'),
    ('EX-0002', '唐代三彩马',   '国家博物馆一层库房',   '王芳', 'SEAL-BJ-0002')
ON CONFLICT (exhibit_id) DO NOTHING;
