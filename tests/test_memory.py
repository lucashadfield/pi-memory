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

    def test_ids_are_not_sequential(self):
        """Enumerated ids (m000, m001, ...) are guessable, and an invented handle
        that hits a real memory casts a vote for it — the exact failure recall is
        meant to surface."""
        ids = [memlib.graduate(self.conn, f"fact {i}")["id"] for i in range(25)]
        numeric = [i for i in ids if i[1:].isdigit()]
        self.assertLess(len(numeric), 5, f"ids look sequential: {ids[:8]}")


class TestDiscard(Base):
    def test_discards_a_thought_without_creating_a_memory(self):
        """There was no path for this in the first version, so a thought whose
        disposition was 'drop' could not be recorded at all."""
        memlib.remember(self.conn, "one-off trivia")
        tid = self.conn.execute("SELECT id FROM thoughts").fetchone()["id"]
        before = self.conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
        r = memlib.discard(self.conn, [tid], reason="trivia")
        self.assertTrue(r["ok"])
        row = self.conn.execute("SELECT * FROM thoughts WHERE id = ?", (tid,)).fetchone()
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["disposition"], "drop")
        after = self.conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
        self.assertEqual(before, after)

    def test_the_thought_and_its_archive_survive(self):
        memlib.remember(self.conn, "still here afterwards")
        tid = self.conn.execute("SELECT id FROM thoughts").fetchone()["id"]
        memlib.discard(self.conn, [tid])
        row = self.conn.execute("SELECT text FROM thoughts WHERE id = ?", (tid,)).fetchone()
        self.assertIn("still here", row["text"])

    def test_reports_an_unknown_thought(self):
        self.assertFalse(memlib.discard(self.conn, [999])["ok"])

    def test_accepts_several_at_once(self):
        for i in range(3):
            memlib.remember(self.conn, f"trivia {i}")
        tids = [r["id"] for r in self.conn.execute("SELECT id FROM thoughts")]
        r = memlib.discard(self.conn, tids)
        self.assertEqual(sorted(r["discarded"]), sorted(tids))


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

    def test_the_block_is_a_template_with_an_entries_slot(self):
        """prompts/inject.jinja2 is the whole block, with {{memories}} where the
        entries go, so the policy text and the layout are one editable file."""
        template = memlib._inject_template_path().read_text()
        self.assertIn(memlib.MEMORIES_SLOT, template)
        mid = self.grad("a specific fact")
        block, ids, _ = memlib.render(self.conn)
        self.assertIn("a specific fact", block)
        self.assertNotIn(memlib.MEMORIES_SLOT, block)
        self.assertIn("# Memory", block)
        self.assertIn(mid, ids)

    def test_verify_reports_a_template_without_the_slot(self):
        """Without the slot every session silently loses the store, so it is a
        fault rather than a silent append."""
        import unittest.mock as mock
        self.grad()
        with mock.patch.object(memlib.Path, "read_text", return_value="# Memory\n\nno slot here"):
            problems = memlib.verify(self.conn)
        self.assertTrue(any(memlib.MEMORIES_SLOT in p for p in problems), problems)

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


class TestLongEntryBudget(Base):
    def test_a_verbose_graduation_shows_up_in_occupancy(self):
        """A validation run graduated six entries averaging well over the ~50-word
        guide and pushed the store over budget in one night. The budget has to
        reflect that immediately, so the next pass knows to squeeze."""
        for i in range(6):
            memlib.graduate(self.conn, "word " * 800 + str(i))
        occ = memlib.occupancy(self.conn)
        self.assertTrue(occ["over_total"])
        self.assertEqual(occ["next_to_squeeze"], 1)


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


class TestToolDefinitions(Base):
    """The tools' wording is data, not code, so it can be reviewed and changed
    without editing TypeScript. These tests hold the file to its contract."""

    def test_tools_json_parses_with_the_required_keys(self):
        import json
        path = memlib.REPO_DIR / "extensions" / "pi-memory" / "tools.json"
        tools = json.loads(path.read_text())
        for name in ("remember", "recall"):
            self.assertIn(name, tools)
            self.assertTrue(tools[name]["promptSnippet"])
            self.assertIsInstance(tools[name]["description"], list)
            self.assertGreater(len(tools[name]["description"]), 3)
            self.assertTrue(tools[name]["parameters"])

    def test_descriptions_do_not_carry_the_policy(self):
        """What a tool is and how to call it belongs here. When to call it is
        prompts/inject.jinja2, and duplicating it in two injected surfaces is how the
        two drift apart."""
        import json
        tools = json.loads((memlib.REPO_DIR / "extensions" / "pi-memory" / "tools.json").read_text())
        remember = " ".join(tools["remember"]["description"]).lower()
        for phrase in ("at the start of a turn", "before other work", "after answering a question"):
            self.assertNotIn(phrase, remember)
        prose = (memlib._inject_template_path().read_text()).lower()
        self.assertIn("at the start of any conversation or turn", prose)  # the policy, in one place

    def test_verify_reports_broken_tool_definitions(self):
        import unittest.mock as mock
        self.grad()
        with mock.patch.object(memlib.Path, "read_text", return_value='{"remember": {}}'):
            problems = memlib.verify(self.conn)
        self.assertTrue(any("tools.json" in p for p in problems), problems)


class TestExtensionSyntax(Base):
    """The extension is the one component whose failure is invisible from here: a
    syntax error takes the memory system down in every new session while the CLI,
    the store and every other test carry on passing. That happened once during
    development and nothing caught it.

    This parses the TypeScript without resolving imports, so it runs wherever node
    exists — including a fresh clone with no dependencies installed.
    """

    def test_extension_has_no_syntax_errors(self):
        import os
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        entry = memlib.REPO_DIR / "extensions" / "pi-memory" / "index.ts"
        script = (
            "const { stripTypeScriptTypes } = require('node:module');\n"
            "const fs = require('node:fs');\n"
            "const src = fs.readFileSync(process.env.TS_FILE, 'utf8');\n"
            "stripTypeScriptTypes(src);\n"
            "console.log('parsed');\n"
        )
        proc = subprocess.run(
            [node, "-e", script],
            capture_output=True, text=True, timeout=60,
            cwd=str(entry.parent),
            env={**os.environ, "TS_FILE": str(entry)},
        )
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("parsed", proc.stdout)


def _node_deps_available(entry_dir):
    """Whether the extension's imports resolve here. In a fresh clone they do not:
    node_modules is a symlink to pi's own modules, and it is gitignored because the
    dependencies belong to the pi installation rather than to this repository.

    Only typebox is checked, because it is the extension's only runtime import.
    @earendil-works/pi-coding-agent is imported as a type and erased before
    execution, so requiring it to resolve would skip these tests in an environment
    where the extension demonstrably works.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return False, "node not available"
    script = (
        "try { require.resolve('typebox'); } catch (e) {"
        " console.error('typebox unresolved'); process.exit(1); }\n"
        "console.log('resolved');\n"
    )
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True,
                          timeout=60, cwd=str(entry_dir))
    if proc.returncode != 0:
        return False, f"pi's modules are not installed here ({proc.stderr.strip()[:60]})"
    return True, ""


class TestExtensionLoads(Base):
    """The deeper check: actually import the module and confirm both tools
    register. Needs pi installed, so it skips rather than fails in a clone."""

    @classmethod
    def setUpClass(cls):
        entry = memlib.REPO_DIR / "extensions" / "pi-memory" / "index.ts"
        ok, why = _node_deps_available(entry.parent)
        cls._available, cls._why = ok, why

    def setUp(self):
        if not self._available:
            self.skipTest(self._why)
        super().setUp()

    def test_extension_registers_its_tools(self):
        import json as _json
        import shutil
        import subprocess

        node = shutil.which("node")
        entry = memlib.REPO_DIR / "extensions" / "pi-memory" / "index.ts"
        script = (
            "const tools = {};\n"
            "const fake = { on: () => {}, registerTool: (t) => { tools[t.name] = t; },"
            " registerCommand: () => {} };\n"
            f"const mod = await import({_json.dumps(str(entry))});\n"
            "mod.default(fake);\n"
            "for (const n of ['remember', 'recall']) {\n"
            "  if (!tools[n]) { console.error('missing tool ' + n); process.exit(3); }\n"
            "}\n"
            "console.log(Object.keys(tools).join(','));\n"
        )
        proc = subprocess.run(
            [node, "--experimental-strip-types", "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60, cwd=str(entry.parent),
        )
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("remember", proc.stdout)
        self.assertIn("recall", proc.stdout)

    def test_tool_descriptions_are_wired_from_the_json_file(self):
        import json as _json
        import shutil
        import subprocess

        node = shutil.which("node")
        entry = memlib.REPO_DIR / "extensions" / "pi-memory" / "index.ts"
        script = (
            "const tools = {};\n"
            "const fake = { on: () => {}, registerTool: (t) => { tools[t.name] = t; },"
            " registerCommand: () => {} };\n"
            f"const mod = await import({_json.dumps(str(entry))});\n"
            "mod.default(fake);\n"
            "console.log(JSON.stringify({ remember: tools.remember.description,"
            " recall: tools.recall.description }));\n"
        )
        proc = subprocess.run(
            [node, "--experimental-strip-types", "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60, cwd=str(entry.parent),
        )
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        wired = _json.loads(proc.stdout.strip().split("\n")[-1])
        file = _json.loads((entry.parent / "tools.json").read_text())
        for name in ("remember", "recall"):
            self.assertEqual(wired[name], "\n".join(file[name]["description"]))


class TestTemplates(Base):
    """The templates are input to a renderer, not documents. The .jinja2 extension
    is the signal an agent needs to stop treating them as prose. They carry no
    self-description: the policy is what a session reads, and a comment stripped
    before injection is one more thing to forget to strip."""

    def test_both_templates_use_the_jinja2_extension(self):
        for path in (memlib._inject_template_path(), memlib._dream_template_path()):
            self.assertTrue(str(path).endswith(".jinja2"), path)
            self.assertTrue(path.exists(), f"{path} is missing")

    def test_the_inject_template_carries_the_slot(self):
        raw = memlib._inject_template_path().read_text()
        self.assertIn(memlib.MEMORIES_SLOT, raw)

    def test_comments_never_reach_the_prompt(self):
        """A comment that leaked would be permanent tokens in every session."""
        self.grad("a fact")
        block, _, _ = memlib.render(self.conn)
        self.assertNotIn("{#", block)
        self.assertNotIn("#}", block)
        self.assertTrue(block.startswith("# Memory"), block[:40])

    def test_dream_instructions_are_comment_free(self):
        out = memlib.substitute(
            memlib._dream_template_path().read_text(), {"total_tokens": "5000"})
        self.assertNotIn("{#", out)
        self.assertTrue(out.lstrip().startswith("# Consolidation"))

    def test_strip_comments_handles_multiline_and_leaves_placeholders(self):
        self.assertEqual(memlib.strip_comments("a{# one\ntwo #}b"), "ab")
        self.assertEqual(memlib.strip_comments("x {{memories}} y"), "x {{memories}} y")
