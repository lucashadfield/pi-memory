#!/usr/bin/env python3
"""
Tests for the memory store. Standard library only.

    python3 -m unittest discover -s tests -v

These are the invariants that used to be prose the dreamer had to remember.
Each test here corresponds to a rule that has been broken at least once, or a
rule whose violation would be silent.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import memlib  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "memory.db"
        self.conn = memlib.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def grad(self, text="a fact worth keeping", **kw):
        r = memlib.graduate(self.conn, text, **kw)
        self.assertTrue(r["ok"], r)
        return r["id"]


class TestEstimator(Base):
    def test_estimate_rounds_up(self):
        self.assertEqual(memlib.estimate_tokens(3.6), 1)
        self.assertEqual(memlib.estimate_tokens(3.61), 2)
        self.assertEqual(memlib.estimate_tokens(0), 0)

    def test_inverse_never_exceeds_the_budget(self):
        for tok in (1, 10, 100, 11111):
            chars = memlib.tokens_to_chars(tok)
            self.assertLessEqual(memlib.estimate_tokens(chars), tok)


class TestIds(Base):
    def test_format(self):
        mid = memlib.new_id(self.conn)
        self.assertTrue(memlib.valid_id(mid), mid)
        self.assertTrue(mid.startswith("m"))

    def test_never_reuses_an_id(self):
        mids = set()
        for i in range(40):
            r = memlib.graduate(self.conn, f"fact {i}")
            mids.add(r["id"])
        self.assertEqual(len(mids), 40)
        # A dropped id is still taken, because agents may hold the handle.
        victim = sorted(mids)[0]
        memlib.drop(self.conn, victim)
        for i in range(60):
            r = memlib.graduate(self.conn, f"more {i}")
            self.assertNotEqual(r["id"], victim)


class TestGraduate(Base):
    def test_always_enters_at_l1(self):
        """Graduating straight in at a compressed level destroys the full text
        before anything has recorded it. It has happened (mayc, mayk, 23/08)."""
        mid = self.grad()
        row = memlib.memory_any(self.conn, mid)
        self.assertEqual(row["level"], 1)
        self.assertIsNotNone(memlib.version_at_level(self.conn, mid, 1))

    def test_graduate_has_no_level_option(self):
        """There must be no way to ask for another level."""
        import inspect
        sig = inspect.signature(memlib.graduate)
        self.assertNotIn("level", sig.parameters)

    def test_first_evidence_is_one(self):
        mid = self.grad()
        self.assertEqual(memlib.memory_any(self.conn, mid)["uses"], 1)

    def test_refuses_empty(self):
        self.assertFalse(memlib.graduate(self.conn, "   ")["ok"])

    def test_refuses_an_id_in_use(self):
        mid = self.grad()
        self.assertEqual(memlib.graduate(self.conn, "other", memory_id=mid)["error"], "id_in_use")


class TestRecall(Base):
    def test_bumps_evidence_and_records_the_date(self):
        mid = self.grad()
        r = memlib.recall(self.conn, mid, session_id="s1")
        self.assertTrue(r["ok"])
        row = memlib.memory_any(self.conn, mid)
        self.assertEqual(row["uses"], 2)
        self.assertEqual(row["last_recalled"], memlib.today())

    def test_dedupes_per_session(self):
        """A speculative vote is worse than a missing one: it keeps dead weight
        alive forever."""
        mid = self.grad()
        memlib.recall(self.conn, mid, session_id="s1")
        second = memlib.recall(self.conn, mid, session_id="s1")
        self.assertTrue(second["already_counted"])
        self.assertEqual(memlib.memory_any(self.conn, mid)["uses"], 2)
        # a different session counts
        memlib.recall(self.conn, mid, session_id="s2")
        self.assertEqual(memlib.memory_any(self.conn, mid)["uses"], 3)

    def test_unknown_id_is_reported_not_invented(self):
        r = memlib.recall(self.conn, "mzzz")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "unknown_id")

    def test_expands_a_compressed_entry_from_the_full_text(self):
        mid = self.grad("the long original text with all the specifics")
        memlib.compress(self.conn, mid, source="short form")
        r = memlib.recall(self.conn, mid)
        self.assertTrue(r["expanded"])
        self.assertIn("all the specifics", r["text"])
        self.assertEqual(r["level"], 2)

    def test_a_dropped_memory_is_not_recallable(self):
        mid = self.grad()
        memlib.drop(self.conn, mid)
        self.assertEqual(memlib.recall(self.conn, mid)["error"], "unknown_id")

    def test_handles_are_accepted_with_brackets(self):
        mid = self.grad()
        self.assertTrue(memlib.recall(self.conn, f"[{mid}]")["ok"])


class TestCompress(Base):
    def test_moves_exactly_one_level(self):
        mid = self.grad()
        r = memlib.compress(self.conn, mid, source="shorter")
        self.assertEqual((r["from"], r["to"]), (1, 2))
        self.assertEqual(memlib.memory_any(self.conn, mid)["level"], 2)

    def test_the_old_text_is_still_there(self):
        """Compression is paging, not destruction."""
        mid = self.grad("the complete original wording")
        memlib.compress(self.conn, mid, source="keywords")
        self.assertIn("complete original", memlib.fullest_version(self.conn, mid)["text"])

    def test_refuses_core(self):
        mid = self.grad()
        memlib.promote(self.conn, mid, force=True)
        self.assertEqual(memlib.compress(self.conn, mid, source="x")["error"],
                         "core_is_permanent")

    def test_refuses_twice_in_one_night(self):
        mid = self.grad()
        memlib.compress(self.conn, mid, source="shorter")
        self.assertEqual(memlib.compress(self.conn, mid, source="shorter still")["error"],
                         "already_compressed_today")

    def test_refuses_to_step_past_l3(self):
        """L3 to gone is a drop, which is a decision stated explicitly rather
        than a side effect of compressing."""
        mid = self.grad()
        memlib.compress(self.conn, mid, source="l2")
        conn = self.conn
        conn.execute("UPDATE events SET ts = ts - 100000 WHERE verb = 'compress'")
        conn.commit()
        memlib.compress(self.conn, mid, source="l3")
        self.assertEqual(memlib.compress(self.conn, mid, source="l4")["error"], "at_l3_use_drop")

    def test_requires_the_compressed_text(self):
        mid = self.grad()
        self.assertEqual(memlib.compress(self.conn, mid, source="")["error"],
                         "compressed_text_required")


class TestPromote(Base):
    def test_refuses_below_the_threshold(self):
        mid = self.grad()
        r = memlib.promote(self.conn, mid)
        self.assertFalse(r["ok"])
        self.assertEqual(r["threshold"], 50)

    def test_earned_at_fifty(self):
        mid = self.grad()
        self.conn.execute("UPDATE memories SET uses = 50 WHERE id = ?", (mid,))
        self.conn.commit()
        self.assertTrue(memlib.promote(self.conn, mid)["ok"])
        self.assertTrue(memlib.memory_any(self.conn, mid)["core"])

    def test_stipulated_by_the_user(self):
        mid = self.grad()
        self.assertTrue(memlib.promote(self.conn, mid, force=True)["ok"])

    def test_core_can_never_be_dropped_or_demoted(self):
        mid = self.grad()
        memlib.promote(self.conn, mid, force=True)
        self.assertEqual(memlib.drop(self.conn, mid)["error"], "core_is_permanent")
        self.assertEqual(memlib.compress(self.conn, mid, source="x")["error"],
                         "core_is_permanent")


class TestDropAndMerge(Base):
    def test_drop_keeps_every_version(self):
        mid = self.grad("the text we are about to forget")
        memlib.drop(self.conn, mid, reason="noise")
        self.assertEqual(memlib.memory_any(self.conn, mid)["state"], "dropped")
        self.assertIn("about to forget", memlib.fullest_version(self.conn, mid)["text"])

    def test_dropped_memory_leaves_the_live_view(self):
        mid = self.grad()
        memlib.drop(self.conn, mid)
        live = {r["id"] for r in self.conn.execute("SELECT id FROM live_memories")}
        self.assertNotIn(mid, live)

    def test_merge_sums_evidence_and_retires_the_absorbed(self):
        a, b = self.grad("one"), self.grad("two")
        self.conn.execute("UPDATE memories SET uses = 5 WHERE id IN (?,?)", (a, b))
        self.conn.commit()
        r = memlib.merge(self.conn, a, [b], text="one and two")
        self.assertEqual(r["uses"], 10)
        self.assertEqual(memlib.memory_any(self.conn, b)["state"], "merged")
        self.assertEqual(memlib.memory_any(self.conn, b)["merged_into"], a)


class TestRender(Base):
    def test_includes_live_entries_and_excludes_dropped(self):
        a = self.grad("keep this one")
        b = self.grad("forget this one")
        memlib.drop(self.conn, b)
        block, ids, _ = memlib.render(self.conn)
        self.assertIn("keep this one", block)
        self.assertNotIn("forget this one", block)
        self.assertIn(a, ids)
        self.assertNotIn(b, ids)

    def test_renders_at_the_current_level(self):
        mid = self.grad("the long original")
        memlib.compress(self.conn, mid, source="short")
        block, _, _ = memlib.render(self.conn)
        self.assertIn("short", block)
        self.assertNotIn("the long original", block)

    def test_only_injected_bytes_count_against_the_budget(self):
        """The pre-database accounting measured the markdown line including the
        metadata span that was stripped before injection, over-counting by about
        a tenth of the store."""
        mid = self.grad("x" * 1000)
        occ = memlib.occupancy(self.conn)
        expected = len(f"- [{mid}] ") + 1000
        self.assertEqual(occ["decayable_chars"], expected)

    def test_truncates_at_an_entry_boundary(self):
        for i in range(200):
            self.grad("y" * 400 + f" #{i}")
        block, ids, _ = memlib.render(self.conn)
        self.assertIn("[truncated]", block)
        # every retained entry is complete, never half a memory
        for line in block.split("\n"):
            if line.startswith("- [m"):
                self.assertTrue(line.endswith("#" + line.split("#")[-1]))

    def test_records_exposure(self):
        mid = self.grad()
        block, ids, tokens = memlib.render(self.conn)
        memlib.record_exposures(self.conn, "session-1", ids, tokens)
        n = self.conn.execute("SELECT COUNT(*) c FROM exposures WHERE memory_id = ?",
                              (mid,)).fetchone()["c"]
        self.assertEqual(n, 1)


class TestVerify(Base):
    def test_clean_store_has_no_problems(self):
        self.grad()
        self.assertEqual(memlib.verify(self.conn), [])

    def test_catches_a_versionless_memory(self):
        """An entry whose text is unrecoverable is the violation to care about
        most, because compression is only safe while the full text exists."""
        mid = self.grad()
        self.conn.execute("DELETE FROM versions WHERE memory_id = ?", (mid,))
        self.conn.commit()
        problems = memlib.verify(self.conn)
        self.assertTrue(any("unrecoverable" in p for p in problems), problems)

    def test_catches_an_invalid_level(self):
        mid = self.grad()
        self.conn.execute("UPDATE memories SET level = 9 WHERE id = ?", (mid,))
        self.conn.commit()
        self.assertTrue(any("invalid level" in p for p in memlib.verify(self.conn)))

    def test_catches_a_done_thought_with_no_disposition(self):
        memlib.remember(self.conn, "a raw observation")
        self.conn.execute("UPDATE thoughts SET state = 'done'")
        self.conn.commit()
        self.assertTrue(any("no disposition" in p for p in memlib.verify(self.conn)))


class TestRemember(Base):
    def test_records_provenance(self):
        memlib.remember(self.conn, "an observation", session_id="s9",
                        transcript="/tmp/s.jsonl", project="/tmp/proj")
        row = self.conn.execute("SELECT * FROM thoughts").fetchone()
        self.assertEqual(row["session_id"], "s9")
        self.assertEqual(row["transcript"], "/tmp/s.jsonl")
        self.assertEqual(row["project"], "/tmp/proj")
        self.assertEqual(row["state"], "pending")

    def test_refuses_empty(self):
        self.assertFalse(memlib.remember(self.conn, "  ")["ok"])


class TestAuditTrail(Base):
    def test_every_mutation_is_recorded(self):
        """The dreamer cannot act without being recorded, because the event row
        is written by the same function that makes the change."""
        mid = self.grad()
        memlib.reinforce(self.conn, mid, text="more")
        memlib.compress(self.conn, mid, source="less")
        memlib.drop(self.conn, mid)
        verbs = [r["verb"] for r in self.conn.execute(
            "SELECT verb FROM events WHERE memory_id = ? ORDER BY id", (mid,))]
        self.assertEqual(verbs, ["graduate", "reinforce", "compress", "drop"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
