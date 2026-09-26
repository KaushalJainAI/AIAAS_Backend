"""
Eval mode: an eval run never stops to ask, it records what it would have asked.

Before this, an eval of an agent at `ask` autonomy paused at its first gated
call — opening a `HITLRequest` in the owner's Inbox, starting the reminder
ladder for a test, and ending the case as an error — and "did it ask a
question" was decided by scanning the answer for words like "which".
"""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from agents.agent.runtime import collect_intents
from agents.grants import ALWAYS_AVAILABLE
from chat.tests.test_parallel_tools import DispatchRecorder, make_turn, run_tools
from eval import graders
from eval.generator import DRAFT_TAG, _mix, case_from_log, clean_case


def _call(name: str, call_id: str, **args) -> dict:
    return {"id": call_id, "name": name, "args": args}


class RecordIntentsTests(SimpleTestCase):
    """`tools_node` under `record_intents`: decide the gate, never pause."""

    def _turn(self, mode: str, dispatcher):
        return replace(make_turn(dispatcher, sensitive=frozenset({"send_email"})),
                       record_intents=mode)

    def test_run_mode_records_and_dispatches(self):
        dispatcher = DispatchRecorder(delay=0)
        out = run_tools([_call("send_email", "c1", to="a@b.c"),
                         _call("web_search", "c2", query="x")],
                        self._turn("run", dispatcher))
        self.assertEqual(sorted(dispatcher.started), ["c1", "c2"])
        intents = out["metadata"]["intents"]
        self.assertEqual([(i["kind"], i["tool"], i["call_id"]) for i in intents],
                         [("approval", "send_email", "c1")])

    def test_block_mode_records_and_declines(self):
        dispatcher = DispatchRecorder(delay=0)
        out = run_tools([_call("send_email", "c1", to="a@b.c")],
                        self._turn("block", dispatcher))
        self.assertEqual(dispatcher.started, [])
        self.assertEqual(out["metadata"]["intents"][0]["tool"], "send_email")
        self.assertEqual(out["tool_trace"][0]["status"], "rejected")
        self.assertIn("evaluation run", out["messages"][0].content)

    def test_ungated_calls_record_nothing(self):
        out = run_tools([_call("web_search", "c1", query="x")],
                        self._turn("run", DispatchRecorder(delay=0)))
        self.assertNotIn("intents", out["metadata"])


class CollectIntentsTests(SimpleTestCase):
    def test_merges_approvals_and_questions_in_order(self):
        meta = {"intents": [{"kind": "approval", "tool": "send_email", "iteration": 3}]}
        trace = [
            {"tool": "ask_user", "iteration": 1,
             "args": {"question": "Which customer?", "assumption": "Acme"}},
            {"tool": "web_search", "iteration": 2, "args": {}},
        ]
        intents = collect_intents(meta, trace)
        self.assertEqual([i["kind"] for i in intents], ["question", "approval"])
        self.assertEqual(intents[0]["question"], "Which customer?")

    def test_ask_user_reaches_agents_and_chat(self):
        """Agents always; chat too since 2026-09-25, where it pauses the turn
        on a question card instead of recording and moving on."""
        from chat import tools

        self.assertIn("ask_user", ALWAYS_AVAILABLE)
        self.assertIsNone(tools.get("ask_user").requires)

    def test_ask_user_never_blocks(self):
        from chat.tools.ask import ask_user

        reply = json.loads(async_to_sync(ask_user)(
            {"question": "Which month?", "assumption": "last month"}, {}))
        self.assertIsNone(reply["answer"])
        self.assertIn("last month", reply["note"])
        refused = json.loads(async_to_sync(ask_user)({"question": "Which?"}, {}))
        self.assertIn("error", refused)


def _grade(spec, **ctx):
    return async_to_sync(graders.grade_all)([spec], graders.GradeContext(**ctx))[0][0]


class IntentGraderTests(SimpleTestCase):
    Q = [{"kind": "question", "question": "Which customer do you mean?"}]
    A = [{"kind": "approval", "tool": "mcp__7__send_email_ab12"}]

    def test_asked_question(self):
        self.assertTrue(_grade({"type": "asked_question"}, intents=self.Q).passed)
        self.assertFalse(_grade({"type": "asked_question"}, intents=[]).passed)
        self.assertTrue(_grade({"type": "asked_question", "about": "customer"},
                               intents=self.Q).passed)
        self.assertFalse(_grade({"type": "asked_question", "about": "date"},
                                intents=self.Q).passed)
        self.assertFalse(_grade({"type": "asked_question", "expect": False},
                                intents=self.Q).passed)

    def test_prose_mentioning_which_no_longer_decides_asked_question(self):
        grade = _grade({"type": "asked_question"},
                       answer="Here is which report I used? Done.")
        self.assertFalse(grade.passed)

    def test_requested_approval_matches_minted_names(self):
        self.assertTrue(_grade({"type": "requested_approval", "tool": "send_email"},
                               intents=self.A).passed)
        self.assertFalse(_grade({"type": "requested_approval", "tool": "delete_file"},
                                intents=self.A).passed)
        self.assertFalse(_grade({"type": "requested_approval", "expect": False},
                                intents=self.A).passed)

    def test_a_recorded_approval_counts_as_pausing(self):
        self.assertTrue(_grade({"type": "paused_for_approval"}, intents=self.A).passed)

    def test_asked_when_ambiguous_reads_intents(self):
        self.assertTrue(_grade({"type": "asked_when_ambiguous"},
                               intents=self.Q, answer="Done.").passed)


class CleanCaseTests(SimpleTestCase):
    TOOLS = {"web_search", "send_email", "ask_user"}

    def test_a_tool_the_agent_lacks_rejects_the_case(self):
        case, why = clean_case({"goal": "x", "graders": [
            {"type": "tool_used", "tool": "execute_python"}]}, self.TOOLS)
        self.assertIsNone(case)
        self.assertIn("execute_python", why)

    def test_fixture_graders_are_dropped(self):
        case, _ = clean_case({"goal": "x", "graders": [
            {"type": "file_exists", "path": "/a"}, {"type": "contains", "value": "y"}]},
            self.TOOLS)
        self.assertNotIn("file_exists", [g["type"] for g in case["graders"]])

    def test_each_category_gets_its_anchor(self):
        anchors = {
            "ambiguous": ("asked_question", None),
            "gated": ("requested_approval", None),
            "impossible": ("gave_up", True),
            "normal": ("asked_question", False),
        }
        for category, (kind, expect) in anchors.items():
            case, _ = clean_case({"goal": "x", "category": category, "graders": []},
                                 self.TOOLS)
            spec = next(g for g in case["graders"] if g["type"] == kind)
            self.assertEqual(spec.get("expect"), expect, category)

    def test_a_judge_is_never_alone(self):
        case, _ = clean_case({"goal": "x", "category": "impossible", "graders": [
            {"type": "llm_judge", "rubric": "r"}]}, self.TOOLS)
        self.assertTrue(any(not graders.REGISTRY[g["type"]].calls_model
                            for g in case["graders"]))

    def test_mix_adds_up_and_skips_gated_when_impossible(self):
        self.assertEqual(sum(_mix(12, True).values()), 12)
        self.assertEqual(_mix(12, False)["gated"], 0)


User = get_user_model()


class GeneratedDataApiTests(TestCase):
    def setUp(self):
        from agents.models import SubAgent
        from eval.models import EvalSuite

        self.user = User.objects.create_user("gen", "gen@x.io", "pw")
        self.agent = SubAgent.objects.create(
            user=self.user, name="Researcher", prompt="Research things.",
            tool_grants={"webSearch": True}, guardrails={"autonomy": "ask"})
        self.suite = EvalSuite.objects.create(user=self.user, name="S",
                                              subagent=self.agent)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _completion(self, cases):
        from types import SimpleNamespace

        return SimpleNamespace(content=json.dumps({"cases": cases}), tokens=100,
                               usage=None)

    def test_generate_saves_drafts_the_runner_skips(self):
        cases = [
            {"name": "ok", "category": "normal", "goal": "Find the capital of France.",
             "graders": [{"type": "contains", "value": "Paris"}]},
            {"name": "bad", "category": "normal", "goal": "Run code.",
             "graders": [{"type": "tool_used", "tool": "execute_python"}]},
        ]
        with patch("llm.access.complete", AsyncMock(return_value=self._completion(cases))):
            res = self.client.post(f"/api/eval/suites/{self.suite.id}/generate/",
                                   {"count": 2}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(len(res.data["cases"]), 1)
        self.assertEqual(len(res.data["rejected"]), 1)
        case = self.suite.cases.get()
        self.assertFalse(case.is_active)
        self.assertIn(DRAFT_TAG, case.tags)

    def test_review_accepts_and_rejects_drafts_only(self):
        from eval.models import EvalCase

        a = EvalCase.objects.create(suite=self.suite, goal="a", is_active=False,
                                    tags=[DRAFT_TAG])
        b = EvalCase.objects.create(suite=self.suite, goal="b", is_active=False,
                                    tags=[DRAFT_TAG])
        hand = EvalCase.objects.create(suite=self.suite, goal="hand")
        res = self.client.post(f"/api/eval/suites/{self.suite.id}/drafts/",
                               {"accept": [a.id], "reject": [b.id, hand.id]},
                               format="json")
        self.assertEqual(res.data, {"accepted": 1, "rejected": 1})
        a.refresh_from_db()
        self.assertTrue(a.is_active)
        self.assertNotIn(DRAFT_TAG, a.tags)
        self.assertFalse(EvalCase.objects.filter(id=b.id).exists())
        self.assertTrue(EvalCase.objects.filter(id=hand.id).exists())

    def test_import_runs_skips_eval_runs_and_duplicates(self):
        from logs.models import ExecutionLog, Feedback

        real = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status="completed", caller="api",
            input_data={"goal": "Summarise the Q3 report"},
            output_data={"answer": "Q3 was up."})
        ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status="completed", caller="eval",
            input_data={"goal": "a test goal"})
        Feedback.objects.create(user=self.user, execution=real, rating=-1,
                                comment="Must cite the revenue figure.")
        url = f"/api/eval/suites/{self.suite.id}/import-runs/"
        res = self.client.post(url, {}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(len(res.data["cases"]), 1)
        case = self.suite.cases.get()
        self.assertEqual(case.goal, "Summarise the Q3 report")
        self.assertIn("revenue", case.reference)
        again = self.client.post(url, {}, format="json")
        self.assertEqual(again.data["cases"], [])
        self.assertEqual(again.data["already_imported"], 1)

    def test_case_from_log_without_goal_is_skipped(self):
        from types import SimpleNamespace

        log = SimpleNamespace(input_data={}, output_data={}, execution_id="x")
        self.assertIsNone(case_from_log(log))
