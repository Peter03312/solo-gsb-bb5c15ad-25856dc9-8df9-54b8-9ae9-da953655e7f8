"""摘要补正单端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

覆盖：
- 补正单创建/拒绝（404/422/409）、幂等重放与异载荷冲突（幂等优先于状态校验）；
- 同预期值并发补正仅一笔生效；
- 交接查询同时呈现原摘要/当前有效摘要/补正轨迹；
- 审计复算交接与补正两条链并定位首个失效环（篡改/删除/锚点失效）；
- 补正不推进交接链、不使既有预演令牌失效；
- 原提交接口与响应字段保持兼容。
"""
from __future__ import annotations

import os
import threading
import unittest
import uuid

import psycopg

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - dev 依赖缺失
    TestClient = None

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://app:app@localhost:5432/seals"
)


def _db_available() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


D1 = "sha256:" + "ab" * 32
D2 = "sha256:" + "cd" * 32
D3 = "sha256:" + "ef" * 32
D4 = "sha256:" + "01" * 32


def _digest(i: int) -> str:
    return "sha256:" + f"{i:02x}" * 32


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestDigestCorrectionsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app.db as db
        db.DATABASE_URL = DATABASE_URL
        import app.main as main
        cls.main = main
        cls.client_cm = TestClient(main.app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    def setUp(self):
        self.ex = f"EX-CR-{uuid.uuid4().hex[:12]}"
        r = self.client.post("/api/v1/exhibits", json={
            "exhibit_id": self.ex, "name": "补正测试展品",
            "home_location": "库房A", "custodian": "张伟",
            "initial_seal": "SEAL-1",
        })
        self.assertEqual(r.status_code, 201, r.text)

    # -- 辅助 ----------------------------------------------------------------

    def _submit_handover(self, digest=D1, from_person="张伟", to_person="李静",
                         location="库房A", when="2026-09-01T09:00:00+08:00"):
        r = self.client.post("/api/v1/handover-batches", json={
            "exhibit_id": self.ex,
            "records": [{"location": location, "from_person": from_person,
                         "to_person": to_person, "seal_no": "SEAL-1",
                         "occurred_at": when, "voucher_digest": digest}],
        }, headers={"Idempotency-Key": f"b-{uuid.uuid4()}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED", r.text)
        return r.json()

    def _correct(self, expected=D1, new=D2, reason="誊抄错误", chain_seq=1, key=None,
                 exhibit_id=None, client=None):
        client = client or self.client
        return client.post("/api/v1/digest-corrections", json={
            "exhibit_id": exhibit_id or self.ex,
            "chain_seq": chain_seq,
            "expected_digest": expected,
            "new_digest": new,
            "reason": reason,
        }, headers={"Idempotency-Key": key or f"k-{uuid.uuid4()}"})

    def _record(self, seq=1):
        rows = self.client.get(f"/api/v1/exhibits/{self.ex}/handovers").json()["records"]
        return next(r for r in rows if r["chain_seq"] == seq)

    # -- 基础创建与拒绝 -------------------------------------------------------

    def test_create_correction_201_and_fields(self):
        self._submit_handover()
        original = self._record()
        r = self._correct()
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["exhibit_id"], self.ex)
        self.assertEqual(body["target_chain_seq"], 1)
        self.assertEqual(body["correction_seq"], 1)
        self.assertEqual(body["prev_hash"], "GENESIS")
        self.assertEqual(body["expected_digest"], D1)
        self.assertEqual(body["new_digest"], D2)
        self.assertFalse(body["idempotent_replay"])
        # 锚定被补正的原记录哈希，并快照原始摘要
        self.assertEqual(body["target_record_hash"], original["record_hash"])
        self.assertEqual(body["original_digest"], D1)

    def test_unknown_exhibit_404(self):
        r = self._correct(exhibit_id=f"EX-NO-{uuid.uuid4().hex[:8]}")
        self.assertEqual(r.status_code, 404)

    def test_target_record_missing_404(self):
        self._submit_handover()
        r = self._correct(chain_seq=99)
        self.assertEqual(r.status_code, 404, r.text)

    def test_blank_reason_rejected_422(self):
        self._submit_handover()
        r = self._correct(reason="   ")
        self.assertEqual(r.status_code, 422)

    def test_same_old_and_new_digest_rejected_422(self):
        self._submit_handover()
        r = self._correct(expected=D1, new=D1)
        self.assertEqual(r.status_code, 422)

    def test_malformed_digests_rejected_422(self):
        self._submit_handover()
        self.assertEqual(self._correct(expected="not-a-digest", new=D2).status_code, 422)
        self.assertEqual(self._correct(expected=D1, new="bad").status_code, 422)

    def test_expected_mismatch_is_409(self):
        self._submit_handover()
        self.assertEqual(self._correct().status_code, 201)
        # 另一笔仍以旧的当前有效摘要为预期 -> 冲突
        r = self._correct(expected=D1, new=D3)
        self.assertEqual(r.status_code, 409, r.text)

    # -- 幂等 ----------------------------------------------------------------

    def test_same_key_same_payload_replays_200(self):
        self._submit_handover()
        key = f"k-{uuid.uuid4()}"
        first = self._correct(key=key)
        self.assertEqual(first.status_code, 201)

        replay = self._correct(key=key)
        self.assertEqual(replay.status_code, 200)
        body = replay.json()
        self.assertTrue(body["idempotent_replay"])
        self.assertEqual(body["correction_id"], first.json()["correction_id"])
        # 没有重复追加
        trail = self.client.get(
            f"/api/v1/exhibits/{self.ex}/digest-corrections").json()
        self.assertEqual(len(trail), 1)

    def test_same_key_different_payload_409(self):
        self._submit_handover()
        key = f"k-{uuid.uuid4()}"
        self.assertEqual(self._correct(key=key, new=D2).status_code, 201)
        self.assertEqual(self._correct(key=key, new=D3, reason="另一个原因").status_code, 409)

    def test_replay_takes_precedence_even_after_later_correction(self):
        """即使后续补正已发生，同键同载荷仍先按幂等键重放原单。"""
        self._submit_handover()
        key = f"k-{uuid.uuid4()}"
        first = self._correct(key=key, expected=D1, new=D2)
        self.assertEqual(first.status_code, 201)
        # 链上又发生一笔补正，当前有效摘要变为 D3
        self.assertEqual(self._correct(expected=D2, new=D3).status_code, 201)
        # 原请求重放：其 expected=D1 已过期，但幂等优先，返回原单
        replay = self._correct(key=key, expected=D1, new=D2)
        self.assertEqual(replay.status_code, 200)
        body = replay.json()
        self.assertTrue(body["idempotent_replay"])
        self.assertEqual(body["correction_id"], first.json()["correction_id"])
        self.assertEqual(body["new_digest"], D2)

    # -- 并发 ----------------------------------------------------------------

    def test_concurrent_same_expected_only_one_succeeds(self):
        self._submit_handover()
        n = 6
        barrier = threading.Barrier(n)
        outcomes: list[int] = []
        lock = threading.Lock()

        def worker(i):
            local = TestClient(self.main.app)
            barrier.wait()
            r = local.post("/api/v1/digest-corrections", json={
                "exhibit_id": self.ex, "chain_seq": 1,
                "expected_digest": D1, "new_digest": _digest(i + 10),
                "reason": f"并发补正{i}",
            }, headers={"Idempotency-Key": f"conc-{uuid.uuid4()}"})
            with lock:
                outcomes.append(r.status_code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(outcomes).count(201), 1, outcomes)
        self.assertEqual(sorted(outcomes).count(409), n - 1, outcomes)
        trail = self.client.get(
            f"/api/v1/exhibits/{self.ex}/digest-corrections").json()
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["correction_seq"], 1)

    # -- 查询与轨迹 -----------------------------------------------------------

    def test_handovers_show_original_effective_and_trail(self):
        self._submit_handover()
        self.assertEqual(self._correct(expected=D1, new=D2).status_code, 201)
        self.assertEqual(self._correct(expected=D2, new=D3).status_code, 201)

        rec = self._record()
        self.assertEqual(rec["voucher_digest"], D1)          # 原摘要不变
        self.assertEqual(rec["effective_voucher_digest"], D3)  # 当前有效
        self.assertEqual(len(rec["corrections"]), 2)
        self.assertEqual([c["new_digest"] for c in rec["corrections"]], [D2, D3])
        self.assertEqual([c["expected_digest"] for c in rec["corrections"]], [D1, D2])

    def test_correction_list_and_get_endpoints(self):
        self._submit_handover()
        created = self._correct().json()
        r = self.client.get(f"/api/v1/digest-corrections/{created['correction_id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["correction_id"], created["correction_id"])

        missing = self.client.get(f"/api/v1/digest-corrections/{uuid.uuid4()}")
        self.assertEqual(missing.status_code, 404)

        listing = self.client.get(
            f"/api/v1/exhibits/{self.ex}/digest-corrections").json()
        self.assertEqual(len(listing), 1)
        filtered = self.client.get(
            f"/api/v1/exhibits/{self.ex}/digest-corrections",
            params={"target_chain_seq": 1}).json()
        self.assertEqual(len(filtered), 1)
        filtered_empty = self.client.get(
            f"/api/v1/exhibits/{self.ex}/digest-corrections",
            params={"target_chain_seq": 9}).json()
        self.assertEqual(filtered_empty, [])

        r = self.client.get(
            f"/api/v1/exhibits/EX-NO-{uuid.uuid4().hex[:8]}/digest-corrections")
        self.assertEqual(r.status_code, 404)

    # -- 不推进交接链 / 不使令牌失效 --------------------------------------------

    def test_correction_does_not_advance_handover_chain(self):
        self._submit_handover()
        before = self.client.get(f"/api/v1/exhibits/{self.ex}").json()
        self.assertEqual(self._correct().status_code, 201)
        after = self.client.get(f"/api/v1/exhibits/{self.ex}").json()
        self.assertEqual(after["chain_length"], before["chain_length"])
        self.assertEqual(after["chain_head_hash"], before["chain_head_hash"])
        self.assertEqual(after["custodian"], before["custodian"])
        self.assertEqual(after["current_seal"], before["current_seal"])
        self.assertEqual(after["last_time"], before["last_time"])
        self.assertEqual(after["correction_seq"], 1)
        self.assertNotEqual(after["correction_head_hash"], "GENESIS")

    def test_existing_rehearsal_token_still_valid_after_correction(self):
        self._submit_handover()
        # 补正前签发的令牌
        token = self.client.post("/api/v1/handover-batches/rehearse", json={
            "exhibit_id": self.ex,
            "records": [{"location": "展厅B", "from_person": "李静", "to_person": "王强",
                         "seal_no": "SEAL-1", "occurred_at": "2026-09-02T09:00:00+08:00",
                         "voucher_digest": D2}],
        }).json()["state_token"]["token"]
        self.assertEqual(self._correct().status_code, 201)
        # 补正后令牌仍被受理（补正不推进交接链）
        r = self.client.post("/api/v1/handover-batches", json={
            "exhibit_id": self.ex,
            "records": [{"location": "展厅B", "from_person": "李静", "to_person": "王强",
                         "seal_no": "SEAL-1", "occurred_at": "2026-09-02T09:00:00+08:00",
                         "voucher_digest": D2}],
        }, headers={"Idempotency-Key": f"b-{uuid.uuid4()}", "Handover-Token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED")

    # -- 审计复算 --------------------------------------------------------------

    def test_audit_verifies_both_chains(self):
        self._submit_handover()
        self._correct(expected=D1, new=D2)
        self._correct(expected=D2, new=D3)
        v = self.client.get(f"/api/v1/exhibits/{self.ex}/audit/verify").json()
        self.assertTrue(v["valid"], v)
        self.assertIsNone(v["first_invalid_seq"])
        self.assertIsNone(v["first_invalid_correction_seq"])
        self.assertTrue(v["chains"]["handovers"]["valid"])
        self.assertTrue(v["chains"]["corrections"]["valid"])
        self.assertEqual(v["chains"]["corrections"]["chain_length"], 2)

    def _exec_sql(self, sql, *params):
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(sql, params)
            conn.commit()

    def test_audit_detects_correction_field_tamper(self):
        self._submit_handover()
        self._correct()
        self._exec_sql(
            "UPDATE digest_corrections SET reason = reason || '-x' WHERE correction_seq = 1"
        )
        v = self.client.get(f"/api/v1/exhibits/{self.ex}/audit/verify").json()
        self.assertFalse(v["valid"])
        self.assertTrue(v["chains"]["handovers"]["valid"])
        self.assertFalse(v["chains"]["corrections"]["valid"])
        self.assertEqual(v["first_invalid_correction_seq"], 1)
        self.assertIsNone(v["first_invalid_seq"])

    def test_audit_detects_handover_tamper_via_anchor(self):
        self._submit_handover()
        self._correct()
        self._exec_sql(
            "UPDATE handover_records SET voucher_digest = %s WHERE exhibit_id = %s",
            D4, self.ex,
        )
        v = self.client.get(f"/api/v1/exhibits/{self.ex}/audit/verify").json()
        self.assertFalse(v["valid"])
        self.assertEqual(v["first_invalid_seq"], 1)
        # 交接链先失效；原始摘要快照不一致也会使补正链在第 1 环失效
        self.assertEqual(v["first_invalid_correction_seq"], 1)

    def test_audit_detects_deleted_middle_correction(self):
        self._submit_handover()
        self._correct(expected=D1, new=D2)
        self._correct(expected=D2, new=D3)
        self._correct(expected=D3, new=D4)
        self._exec_sql(
            "DELETE FROM digest_corrections WHERE exhibit_id = %s AND correction_seq = 2",
            self.ex,
        )
        v = self.client.get(f"/api/v1/exhibits/{self.ex}/audit/verify").json()
        self.assertFalse(v["valid"])
        # 环 3 的序号与 prev_hash 衔接首先在第 3 环暴露（第 2 环已被删除）
        self.assertEqual(v["first_invalid_correction_seq"], 3)

    def test_audit_detects_tail_cursor_mismatch(self):
        self._submit_handover()
        self._correct()
        # 游标被人为改写（补正链本体完好）
        self._exec_sql(
            "UPDATE exhibits SET correction_head_hash = %s WHERE exhibit_id = %s",
            "TAMPERED", self.ex,
        )
        v = self.client.get(f"/api/v1/exhibits/{self.ex}/audit/verify").json()
        self.assertFalse(v["valid"])
        self.assertFalse(v["chains"]["corrections"]["valid"])
        self.assertIn("链尾", v["chains"]["corrections"]["detail"])

    def test_original_record_hash_never_rewritten(self):
        self._submit_handover()
        before = self._record()
        self._correct()
        self._correct(expected=D2, new=D3)
        after = self._record()
        self.assertEqual(before["record_hash"], after["record_hash"])
        self.assertEqual(before["prev_hash"], after["prev_hash"])
        self.assertEqual(before["voucher_digest"], after["voucher_digest"])


if __name__ == "__main__":
    unittest.main()
