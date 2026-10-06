"""哈希工具单元测试（纯标准库，python3 -m unittest 或 pytest 均可运行）。"""
import unittest
import uuid
from datetime import datetime, timezone, timedelta

from app.hashing import (
    canonical_json,
    correction_hash,
    correction_request_hash,
    record_hash,
)

T0 = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
D1 = "sha256:" + "ab" * 32
D2 = "sha256:" + "cd" * 32


class TestCorrectionRequestHash(unittest.TestCase):
    def test_stable(self):
        a = correction_request_hash("EX-1", 3, D1, D2, "誊抄错误")
        b = correction_request_hash("EX-1", 3, D1, D2, "誊抄错误")
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{64}$")

    def test_any_field_changes_hash(self):
        base = dict(exhibit_id="EX-1", target_chain_seq=3,
                    expected_digest=D1, new_digest=D2, reason="r")
        baseline = correction_request_hash(**base)
        for field, value in [
            ("exhibit_id", "EX-2"),
            ("target_chain_seq", 4),
            ("expected_digest", D2),
            ("new_digest", "sha256:" + "ee" * 32),
            ("reason", "other"),
        ]:
            changed = dict(base)
            changed[field] = value
            self.assertNotEqual(correction_request_hash(**changed), baseline, field)


class TestCorrectionHash(unittest.TestCase):
    def _kwargs(self, **over):
        kw = dict(
            prev_hash="GENESIS",
            exhibit_id="EX-1",
            correction_seq=1,
            correction_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
            target_chain_seq=2,
            target_record_hash="a" * 64,
            expected_digest=D1,
            new_digest=D2,
            reason="誊抄错误",
            created_at=T0,
        )
        kw.update(over)
        return kw

    def test_deterministic_and_hex(self):
        h1 = correction_hash(**self._kwargs())
        h2 = correction_hash(**self._kwargs())
        self.assertEqual(h1, h2)
        self.assertRegex(h1, r"^[0-9a-f]{64}$")

    def test_prev_hash_links_rings(self):
        first = correction_hash(**self._kwargs())
        second = correction_hash(**self._kwargs(correction_seq=2, prev_hash=first))
        self.assertNotEqual(first, second)

    def test_any_field_changes_hash(self):
        baseline = correction_hash(**self._kwargs())
        for field, value in [
            ("prev_hash", "other"),
            ("exhibit_id", "EX-2"),
            ("correction_seq", 2),
            ("correction_id", uuid.uuid4()),
            ("target_chain_seq", 3),
            ("target_record_hash", "b" * 64),
            ("expected_digest", D2),
            ("new_digest", "sha256:" + "ee" * 32),
            ("reason", "其他原因"),
            ("created_at", T0 + timedelta(seconds=1)),
        ]:
            self.assertNotEqual(
                correction_hash(**self._kwargs(**{field: value})), baseline, field
            )

    def test_timestamp_normalized_across_offsets(self):
        # 同一时刻不同时区表示必须得到相同哈希（写入与审计复算一致性）
        t_utc = datetime(2026, 9, 1, 1, 0, 0, tzinfo=timezone.utc)
        t_cst = t_utc.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(
            correction_hash(**self._kwargs(created_at=t_utc)),
            correction_hash(**self._kwargs(created_at=t_cst)),
        )

    def test_independent_from_handover_hash_but_anchors_it(self):
        # 补正链与交接链使用各自的字段集；原记录哈希仅作为锚点字段出现
        rh = record_hash(
            prev_hash="GENESIS", exhibit_id="EX-1", chain_seq=2,
            batch_id=uuid.uuid4(), location="库", from_person="张伟",
            to_person="李静", seal_no="S1", seal_change_reason=None,
            occurred_at=T0, voucher_digest=D1,
        )
        ch = correction_hash(**self._kwargs(target_record_hash=rh))
        self.assertNotEqual(ch, rh)
        self.assertNotEqual(
            ch, correction_hash(**self._kwargs(target_record_hash=rh[::-1]))
        )


class TestCanonicalJson(unittest.TestCase):
    def test_key_order_invariant(self):
        self.assertEqual(canonical_json({"a": 1, "b": 2}), canonical_json({"b": 2, "a": 1}))


if __name__ == "__main__":
    unittest.main()
