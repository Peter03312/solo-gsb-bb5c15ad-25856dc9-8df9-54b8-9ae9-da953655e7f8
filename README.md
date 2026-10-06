# 文物借展封签交接核验服务

Python (FastAPI) + PostgreSQL 实现的借展交接核验服务。调用方提交展品编号及按时间排列的交接记录（地点、交出人、接收人、封签号、时间、凭证摘要），服务完成规则核验、幂等写入与哈希链审计。

## 功能特性

- **规则核验**：首站校验、人员衔接、时间严格递增、封签连续性（换签必须带授权理由）、凭证摘要格式
- **提交前预演**：`/rehearse` 只读核验整批记录，返回逐条结果、首个断点与展品状态令牌；不写批次报告、交接记录，也不推进链状态
- **状态令牌**：正式提交可携带预演令牌；令牌属于该展品且链状态未变化才受理——格式无效报 400，跨展品/链已推进报 409
- **幂等提交**：`Idempotency-Key` 请求头去重，相同请求重试返回同一结果；同键不同载荷返回 409（令牌计入载荷身份）
- **事务原子性**：核验失败的批次**不写入任何交接记录**、不推进链上状态（仅落核验报告供幂等重试与审计追溯）
- **可查询审计链**：每条交接记录携带 `prev_hash` / `record_hash` 构成哈希链，提供链查询与完整性复算接口
- **凭证摘要补正**：已入链交接记录的凭证摘要录错可提交补正单；原记录与 `record_hash` **永不改写**，补正按展品形成独立哈希链，每环锚定被补正原记录；乐观预期值防并发覆盖，幂等重放优先
- **逐项诊断**：输出核验状态、首个断点（first_break）及每条记录的全部不合格原因

## 快速开始

```bash
docker compose up --build
```

启动后：

- API: http://localhost:8000 （交互式文档 http://localhost:8000/docs ）
- PostgreSQL: localhost:5432 （用户/密码 `app`/`app`，库 `seals`）
- `db/init.sql` 在数据库首次启动时自动执行，建表并写入两件种子展品（`EX-0001`、`EX-0002`）

一键跑通完整演示（登记展品 → 预演取令牌 → 凭令牌提交 → 幂等重试 → 令牌失效/非法 → 409 冲突 → 违规批次 → 链查询 → 审计校验 → 摘要补正全流程 → 补正后旧令牌再交接 → 双链审计）：

```bash
./examples/demo.sh
```

重置数据：`docker compose down -v`（清空数据卷后重新初始化）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://app:app@localhost:5432/seals` | PostgreSQL 连接串 |
| `DB_CONNECT_RETRIES` | `30` | 启动等待数据库的重试次数 |
| `DB_CONNECT_DELAY` | `1.0` | 重试间隔（秒） |
| `TOKEN_SECRET` | `dev-insecure-rehearsal-secret-change-me` | 预演状态令牌的 HMAC 签名密钥。**仅供本地演示**；生产部署必须设置为高熵随机值，且多实例共享同一密钥（可用 `TOKEN_SECRET=$(openssl rand -hex 32)` 生成，经 Compose 同名环境变量传入） |

## 核验规则

| 规则码 | 说明 |
|---|---|
| `FIRST_STATION_LOCATION` | 展品首条链的第一站地点必须等于登记馆藏地 |
| `FIRST_STATION_CUSTODIAN` | 首站交出人必须是登记保管人 |
| `CUSTODIAN_CHAIN_BREAK` | 交出人必须等于上一环接收人（批次首环则等于当前保管人） |
| `TIME_NOT_INCREASING` | 交接时间必须严格晚于上一环（批次首环则晚于链上最近时间） |
| `SEAL_CHANGED_WITHOUT_AUTH` | 封签号与上一环（或当前封签）不一致时，必须提供换签授权理由 |
| `INVALID_VOUCHER_DIGEST` | 凭证摘要须为 `算法:十六进制`，如 `sha256:<64hex>` |

逐项判定均以「提交的前一条记录」为参照（首条以展品当前链上状态为参照），因此链条中途断裂后，后续各项仍能给出独立可定位的原因。批次任一记录不合格即整体 `REJECTED`。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/exhibits` | 登记展品（馆藏地/保管人/初始封签） |
| GET | `/api/v1/exhibits/{id}` | 查询展品当前链上状态（含交接链与补正链游标） |
| POST | `/api/v1/handover-batches/rehearse` | **预演交接批次（只读）**：同载荷规则核验，返回逐条结果、首个断点与状态令牌，不写任何数据 |
| POST | `/api/v1/handover-batches` | **提交交接批次并核验**（需 `Idempotency-Key` 头，可选 `Handover-Token` 头） |
| GET | `/api/v1/handover-batches/{batch_id}` | 查询批次核验报告 |
| GET | `/api/v1/exhibits/{id}/handovers` | 查询完整交接链（原摘要/当前有效摘要/补正轨迹，含哈希） |
| GET | `/api/v1/exhibits/{id}/audit/verify` | 复算交接链与补正链两条哈希链，校验完整性并定位首个失效环 |
| GET | `/api/v1/exhibits/{id}/batches` | 批次历史（含被驳回批次，可按 `status` 过滤） |
| POST | `/api/v1/digest-corrections` | **提交凭证摘要补正单**（需 `Idempotency-Key` 头），原交接记录不改写 |
| GET | `/api/v1/digest-corrections/{correction_id}` | 按补正单号查询单张补正单 |
| GET | `/api/v1/exhibits/{id}/digest-corrections` | 查询展品补正轨迹（可按 `target_chain_seq` 过滤） |

## 调用示例

### 1. 登记展品

```bash
curl -X POST http://localhost:8000/api/v1/exhibits \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"EX-1001","name":"宋代青瓷莲花尊","home_location":"南京博物院库房B区","custodian":"张伟","initial_seal":"SEAL-NJ-0001"}'
```

### 2. 预演交接批次（正式提交前只读核验）

预演接受与正式提交**完全相同**的展品编号与按序记录（记录格式仍按现有规则校验，格式非法返回 `422`），返回逐条核验结果、首个断点 `first_break` 与该展品当前状态的令牌 `state_token`。预演**不写入**批次报告、交接记录，也不推进展品状态；未知展品返回 `404`。

```bash
curl -X POST http://localhost:8000/api/v1/handover-batches/rehearse \
  -H 'Content-Type: application/json' \
  -d @examples/accept.json
```

响应（即使 `REJECTED` 也照常签发令牌——驳回不改变链状态）：

```json
{
  "exhibit_id": "EX-0001",
  "status": "ACCEPTED",
  "record_count": 3,
  "first_break": null,
  "items": [
    {"seq": 1, "ok": true, "reasons": []},
    {"seq": 2, "ok": true, "reasons": []},
    {"seq": 3, "ok": true, "reasons": []}
  ],
  "rehearsed_at": "2026-10-05T08:00:00.000000Z",
  "state_token": {
    "token": "stv1.eyJ2IjoxLCJleCI6IkVYLTAwMDEiLCJuIjowLCJoIjoiR0VORVNJUyIsImlhdCI6MTc4...<HMAC>",
    "exhibit_id": "EX-0001",
    "chain_length": 0,
    "chain_head_hash": "GENESIS",
    "issued_at": "2026-10-05T08:00:00.000000Z"
  }
}
```

令牌为自包含 HMAC 结构 `stv1.<base64url(payload)>.<base64url(hmac)>`，载荷只绑定展品身份与签发时的链快照（`chain_length` + `chain_head_hash`），不绑定提交内容、不入库。

### 3. 携带令牌正式提交

取出上一步的 `state_token.token`，通过 `Handover-Token` 请求头提交（`Idempotency-Key` 规则不变）：

```bash
curl -X POST http://localhost:8000/api/v1/handover-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7b8f9c2e-2026-0001' \
  -H 'Handover-Token: stv1.eyJ...<预演返回的完整令牌>' \
  -d @examples/accept.json
```

受理与拒绝规则：

| 情形 | 结果 |
|---|---|
| 令牌属于该展品且链状态未变化 | 按现有规则核验处理（`ACCEPTED` / `REJECTED`） |
| 令牌格式无效（无法解析、签名不符） | `400` 参数错误，不生成批次报告 |
| 令牌属于其他展品 | `409` 状态冲突，不生成批次报告 |
| 预演后该展品链已被其他成功批次推进 | `409` 状态冲突，不生成批次报告，**调用方须重新预演** |
| 未携带 `Handover-Token` | 行为与此前完全一致，继续可用 |

说明：

- 只有成功（`ACCEPTED`）批次会推进链；`REJECTED` 批次只落报告、链不动，因此**驳回后凭同一令牌修正载荷重提仍然有效**。
- 令牌**计入幂等载荷身份**：同键同载荷（含相同令牌）的重试直接返回原报告——即使其间链已推进、令牌快照已过期，`idempotent_replay=true` 且 `batch_id` 不变；同键但有/无令牌或令牌不同，仍按异载荷返回 `409`。
- 令牌无过期时间约束，唯一有效性条件是「展品对得上 + 链状态未变」。

### 4. 幂等重试与冲突（不带令牌的既有语义）

- **相同请求重试**（同 `Idempotency-Key`、同载荷）：返回与首次完全一致的报告，`idempotent_replay=true`，不重复写入。
- **同键不同载荷**：返回 `409`，说明幂等键被复用。

### 5. 提交违规批次

```bash
curl -X POST http://localhost:8000/api/v1/handover-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7b8f9c2e-2026-0002' \
  -d @examples/reject.json
```

响应（`status=REJECTED`，给出首个断点与逐项原因；**未写入任何交接记录**）：

```json
{
  "status": "REJECTED",
  "first_break": {"seq": 2, "rule": "CUSTODIAN_CHAIN_BREAK", "message": "交出人「孙倩」与上一站接收人「赵磊」不一致，人员衔接断裂"},
  "items": [
    {"seq": 1, "ok": true, "reasons": []},
    {"seq": 2, "ok": false, "reasons": [
      {"rule": "CUSTODIAN_CHAIN_BREAK", "message": "交出人「孙倩」与上一站接收人「赵磊」不一致，人员衔接断裂"},
      {"rule": "TIME_NOT_INCREASING", "message": "交接时间 ... 未严格晚于上一环节 ..."},
      {"rule": "SEAL_CHANGED_WITHOUT_AUTH", "message": "封签号由「SEAL-BJ-0002」变更为「SEAL-XA-2001」，但未提供换签授权理由"},
      {"rule": "INVALID_VOUCHER_DIGEST", "message": "凭证摘要格式非法，应为「算法:十六进制摘要」，如 sha256:<64位hex>"}
    ]}
  ]
}
```

### 6. 查询审计链并校验完整性

```bash
curl http://localhost:8000/api/v1/exhibits/EX-0001/handovers
curl http://localhost:8000/api/v1/exhibits/EX-0001/audit/verify
```

`audit/verify` 逐条复算 `record_hash = sha256(prev_hash ‖ 规范化业务字段)` 并比对链头，任何篡改、删除、乱序都会定位到首个失效环：

```json
{
  "exhibit_id": "EX-0001",
  "chain_length": 3,
  "valid": true,
  "first_invalid_seq": null,
  "first_invalid_correction_seq": null,
  "detail": "交接链与补正链均完整，与展品当前状态一致",
  "chains": {
    "handovers": {"chain_length": 3, "valid": true, "first_invalid_seq": null, "detail": "哈希链完整，与展品当前状态一致"},
    "corrections": {"chain_length": 0, "valid": true, "first_invalid_seq": null, "detail": "补正链完整，各环锚点与预期摘要均一致"}
  }
}
```

> 顶层字段保持原有含义（`first_invalid_seq` 仍指向交接链首个失效环）；补正链的复算结果见 `chains.corrections` 与 `first_invalid_correction_seq`。

### 7. 已入链凭证摘要补正

已入链交接记录的凭证摘要录错后，馆方可提交补正单。**原交接记录及其 `record_hash` 永不改写**，补正按展品追加一条独立哈希链（与交接链互不推进）。

请求字段：展品编号 `exhibit_id`、被补正记录的**链序号** `chain_seq`、`expected_digest`（预期的当前有效摘要）、`new_digest`（新摘要）、`reason`（补正原因），并携带 `Idempotency-Key` 请求头：

```bash
curl -X POST http://localhost:8000/api/v1/digest-corrections \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: corr-2026-1001-01' \
  -d @examples/correct-digest.json
```

受理与拒绝规则：

| 情形 | 结果 |
|---|---|
| 目标记录存在、原因非空、新旧不同、预期值等于当前有效摘要 | `201` 创建补正单，追加补正链一环 |
| 同键同载荷重试 | `200` 返回**原补正单**，`idempotent_replay=true` |
| 同键异载荷（任一字段不同） | `409` |
| `expected_digest` 与当前有效摘要不符（含并发下的落败者） | `409`，请以最新有效摘要重新补正 |
| 目标交接记录不存在 / 展品不存在 | `404` |
| 原因为空白、新旧摘要相同、摘要格式非法 | `422` |

要点：

- **幂等重放优先**：即使提交后该记录又发生过新的补正，同键同载荷重试仍先按幂等键返回原补正单，不再校验预期值。
- **并发仅一笔生效**：不同幂等键以同一预期值并发补正同一记录时，展品行锁串行化，仅第一笔 `201`，其余 `409`。
- **当前有效摘要**：某条交接记录的当前有效摘要 = 其最新一笔补正的 `new_digest`；无补正则等于原始 `voucher_digest`。
- **补正不推进交接链**：`chain_length`/`chain_head_hash` 不变，已签发的预演令牌**继续有效**，原提交接口与响应字段保持兼容。

补正单响应（含被补正原记录锚点与补正链哈希）：

```json
{
  "correction_id": "…",
  "exhibit_id": "EX-0001",
  "correction_seq": 1,
  "target_chain_seq": 1,
  "expected_digest": "sha256:9f2c…",
  "new_digest": "sha256:0f1e…",
  "reason": "交接凭证誊录笔误，经馆方核对纸质凭证原件后补正（核准单 NJ-2026-102）",
  "original_digest": "sha256:9f2c…",
  "target_record_hash": "b22e17f3…",
  "prev_hash": "GENESIS",
  "correction_hash": "ad5e29d3…",
  "created_at": "2026-10-06T03:37:34.103149Z",
  "idempotent_replay": false
}
```

查询补正轨迹（可按目标链序号过滤）：

```bash
curl http://localhost:8000/api/v1/exhibits/EX-0001/digest-corrections
curl 'http://localhost:8000/api/v1/exhibits/EX-0001/digest-corrections?target_chain_seq=1'
curl http://localhost:8000/api/v1/digest-corrections/{correction_id}
```

### 8. 交接链查询中的原摘要 / 当前有效摘要 / 补正轨迹

`GET /handovers` 的每条记录在原有字段之外新增：

- `voucher_digest`：**原始**交接凭证摘要（永不改写，旧字段语义不变）；
- `effective_voucher_digest`：**当前有效**摘要；
- `corrections`：该记录的补正轨迹（按补正链顺序，含每笔的预期/新摘要、原因与哈希）。

### 9. 补正链审计

`GET /audit/verify` 同时复算两条链：

- 交接链复算 `record_hash`、`prev_hash` 衔接与链头一致性；
- 补正链按 `correction_seq` 复算 `correction_hash = sha256(prev_hash ‖ 补正字段 ‖ 原记录锚点)`，校验：链序连续、`prev_hash` 衔接、`target_record_hash` 与当前交接链记录一致、`original_digest` 快照一致、`expected_digest` 与重放得到的当时有效摘要一致、链尾与展品补正游标一致。

补正单被篡改、被删除、乱序，或被锚定的原记录被改写，都会定位到**首个失效环**（`first_invalid_correction_seq`）。

## 设计说明

- **预演**：`POST /rehearse` 仅做一次普通 `SELECT` 读取展品当前状态，复用同一套 `validate_batch` 规则引擎；不落批次报告、不落交接记录、不推进展品状态，可安全重复调用。
- **状态令牌**：`app/tokens.py` 实现的自包含 HMAC 令牌（`stv1.` 前缀），载荷为展品编号 + 签发时 `chain_length`/`chain_head_hash` 快照，密钥来自 `TOKEN_SECRET`，令牌不入库、无状态。提交时先在事务外校验格式/签名（无效即 `400`），再在展品行锁 `SELECT ... FOR UPDATE` 内比对归属与链快照（跨展品或链已推进即 `409`）——行锁保证「比对 → 推进」与其他成功批次互斥，不会读到推进途中的状态。
- **幂等**：`batches.idempotency_key` 唯一约束 + `request_hash`（请求体规范化 JSON 的 SHA-256，携带令牌时令牌计入指纹）。同键同载荷直接返回已存报告（即使链已推进，令牌比对也不执行）；同键不同载荷 409；并发同键插入由唯一约束兜底，后到者按重试返回。
- **原子性**：核验、批次报告、交接记录、展品链上状态推进全部在单事务内完成；`REJECTED` 批次仅落 `batches` 报告行（用于幂等重试返回同一结果与审计追溯），`handover_records` 与 `exhibits` 状态零变更；令牌 `400/409` 在报告写入前中止事务，批次表也不留痕。
- **并发一致性**：处理批次时对展品行 `SELECT ... FOR UPDATE`，串行化同一展品的链推进，防止并发批次造成链序号/链头错乱。
- **哈希链**：首环 `prev_hash = GENESIS`，之后每环指向前环 `record_hash`；时间统一规范化为 UTC 微秒精度 `Z` 格式后参与哈希，保证写入与复算一致。
- **摘要补正**：补正单写入 `digest_corrections` 表，原交接记录与其 `record_hash` 永不变更。补正按展品维护**独立哈希链**（展品上新增 `correction_seq` / `correction_head_hash` 游标），首环 `prev_hash=GENESIS`，每环还以 `target_record_hash` 锚定被补正原记录、以 `original_digest` 快照原始摘要；审计复算时重放每笔的 `expected_digest` 与当时有效摘要是否一致。
- **补正并发与幂等**：提交补正同样在展品行 `FOR UPDATE` 锁内完成「查当前有效摘要 → 比对预期值 → 追加补正环 → 推进补正游标」，不同幂等键以同一预期值并发时仅首笔生效、余者 409；幂等键唯一约束兜底并发插入。幂等查表在一切状态校验之前，因此**同键同载荷永远先重放原单**（即使后续补正已发生），同键异载荷 409。
- **补正不影响交接链/令牌**：补正只动补正链游标，不修改 `chain_length` / `chain_head_hash`，因此预演令牌的链快照比对继续成立；原提交接口路径、请求与响应字段均保持不变（交接链查询仅以新增字段呈现有效摘要与补正轨迹）。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/                 # 规则引擎 + 令牌单测（无数据库依赖）
uvicorn app.main:app --reload # 需本地 PostgreSQL 并执行 db/init.sql
```

`tests/test_api_rehearsal.py` 为预演/令牌端到端测试、`tests/test_api_corrections.py` 为补正单/双链审计端到端测试（含并发与篡改定位），需可达的 PostgreSQL，
通过 `DATABASE_URL` 指定；数据库不可达时自动跳过：

```bash
DATABASE_URL='postgresql://app:app@localhost:5432/seals' pytest tests/
```

## 目录结构

```
app/
  main.py        # FastAPI 路由与事务编排（批次、预演令牌、摘要补正、双链审计）
  validation.py  # 规则引擎（纯标准库，可独立单测）
  tokens.py      # 展品状态令牌：签发/验签/链快照比对（纯标准库，可独立单测）
  hashing.py     # 规范化序列化、请求指纹、交接链与补正链哈希
  schemas.py     # 请求/响应模型（含补正单模型）
  db.py          # 连接与启动等待
db/init.sql      # 建表（exhibits/batches/handover_records/digest_corrections）+ 种子展品
examples/        # accept/reject/correct-digest 示例载荷与端到端 demo.sh
tests/           # 规则引擎、令牌、哈希单元测试与预演/补正端到端测试
Dockerfile  docker-compose.yml  requirements*.txt
```
