"""
Slash commands (P10, §18): registry, parsing, consent, and the mission routes.

A command is structured input, not a prompt template. The client sends
`TurnRequest.command = {"name", "args", "text"}` and the backend resolves
it; a typed `/line` is parsed by the same code. The registry is declared the
way tools are (registration *is* the schema).

Pinned (§18.8):

- the registry lists only commands whose `requires` are met;
- completion and validation agree (same predicates);
- an unknown or unresolvable command is a 400 and never a model turn;
- `/agent` resolves only the caller's own agents, refuses in `plan` mode,
  and starts exactly one run with `caller='chat'`;
- the command is stored on the message and replayed by regenerate;
- the expansion rides in the trailing context message and the system prompt
  is byte-identical to a turn without a command (prefix-cache guard).
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from unittest.mock import patch

from chat.commands import registry as command_registry
from chat.commands.registry import CommandContext
from chat.commands.resolve import (
    CommandError,
    resolve_line,
    split_leading_command,
    suggest_name,
    visible_commands,
)
from chat.models import ChatMessage, ChatSession
from chat.turn.events import Event
from chat.turn.pipeline import TurnRequest, run_chat_turn

User = get_user_model()


def _ctx(user, **kwargs) -> CommandContext:
    params = dict(user_id=user.id, session_id="s", autonomy="ask", guest=False)
    params.update(kwargs)
    return CommandContext(**params)


def _user(name="commander"):
    return User.objects.create_user(username=name, email=f"{name}@x.com",
                                    password="pw")


class RegistryTests(TestCase):
    def test_requires_hides_rather_than_refuses(self):
        user = _user("req")
        cmds = {c.name for c in visible_commands(_ctx(user))}
        # WORKSPACE_ENGINE=none in test settings: code review is hidden.
        self.assertNotIn("code-review", cmds)
        self.assertIn("agent", cmds)
        self.assertIn("help", cmds)

    def test_guests_see_only_guest_commands(self):
        user = _user("guestlike")
        cmds = visible_commands(_ctx(user, guest=True))
        self.assertTrue(cmds)
        self.assertTrue(all(c.guest for c in cmds))
        self.assertIn("help", {c.name for c in cmds})
        self.assertNotIn("agent", {c.name for c in cmds})

    def test_split_only_leading_slash_is_a_command(self):
        self.assertEqual(split_leading_command("/agent Reporter hi"),
                         ("agent", "Reporter hi"))
        self.assertEqual(split_leading_command("   /goal x"), ("goal", "x"))
        self.assertIsNone(split_leading_command("see /Chat/notes.md for it"))
        self.assertIsNone(split_leading_command("a / b"))
        self.assertIsNone(split_leading_command("/"))

    def test_typo_suggests_the_real_name(self):
        self.assertIn("agent", suggest_name("agnt"))
        self.assertEqual(suggest_name("zzz-no-such-command"), [])


class ResolveTests(TestCase):
    def setUp(self):
        self.user = _user("resolver")
        from agents.models import SubAgent

        self.agent = SubAgent.objects.create(user=self.user, name="Reporter")

    def test_agent_resolves_case_insensitively(self):
        cmd, args, text = async_to_sync(resolve_line)(
            "/agent reporter summarise inbox", _ctx(self.user))
        self.assertEqual(cmd.name, "agent")
        self.assertEqual(args["agent"], self.agent.id)
        self.assertEqual(args.get("task") or text, "summarise inbox")

    def test_agent_typo_is_a_400_not_a_turn(self):
        with self.assertRaises(CommandError) as raised:
            async_to_sync(resolve_line)("/agent Reprter hi", _ctx(self.user))
        self.assertIn("No agent called", str(raised.exception))

    def test_unknown_command_suggests(self):
        with self.assertRaises(CommandError) as raised:
            async_to_sync(resolve_line)("/agnt hi", _ctx(self.user))
        self.assertIn("/agent", str(raised.exception))

    def test_foreign_agent_does_not_resolve(self):
        other = _user("other")
        with self.assertRaises(CommandError):
            async_to_sync(resolve_line)(
                f"/agent {self.agent.name} hi", _ctx(other))

    def test_completion_and_validation_agree(self):
        from chat.commands.resolve import complete_arg

        cmd = command_registry.get("agent")
        arg = next(a for a in cmd.args if a.name == "agent")
        candidates = async_to_sync(complete_arg)(
            cmd, arg, "repo", _ctx(self.user))
        self.assertTrue(any(c["id"] == self.agent.id for c in candidates))


class TurnWiringTests(TestCase):
    def setUp(self):
        self.user = _user("turner")
        self.session = ChatSession.objects.create(user=self.user, title="t")
        self._patch("chat.turn.agent.suggest_follow_ups", self._no_follow_ups)
        self._patch("chat.tools.get_available_tools", self._builtin_tools)
        self._patch("chat.tools.execute_tool", self._inert_tool)

    @staticmethod
    async def _no_follow_ups(*_a, **_k):
        return []

    @staticmethod
    async def _builtin_tools(*_a, **_k):
        from chat.tools import AVAILABLE_TOOLS

        return list(AVAILABLE_TOOLS)

    @staticmethod
    async def _inert_tool(name, args, context):
        return json.dumps({"ok": True})

    def _patch(self, target, replacement):
        patcher = patch(target, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stream(self, *chunks):
        async def _run(**_kwargs):
            for chunk in chunks:
                yield {"type": "content", "content": chunk}
            yield {"type": "metadata", "usage": {"total_tokens": 5}}

        return _run

    def _run_turn(self, content: str):
        return async_to_sync(run_chat_turn)(
            session=self.session, user=self.user,
            request=TurnRequest.parse({"content": content}),
        )

    def test_unknown_command_is_a_400_never_a_model_turn(self):
        from chat.turn.pipeline import TurnError

        seen = []

        async def _stream(**_kwargs):
            seen.append(True)
            yield {"type": "content", "content": "hi"}

        with patch("llm.access.stream", _stream):
            with self.assertRaises(TurnError) as raised:
                async_to_sync(run_chat_turn)(
                    session=self.session, user=self.user,
                    request=TurnRequest.parse(
                        {"content": "/nope-not-a-command do it"}),
                )
        self.assertIn("No command called", str(raised.exception))
        self.assertEqual(seen, [])

    def test_client_command_does_not_reach_the_model(self):
        from chat.turn.pipeline import TurnError

        with patch("llm.access.stream", self._stream("hi")):
            with self.assertRaises(TurnError):
                async_to_sync(run_chat_turn)(
                    session=self.session, user=self.user,
                    request=TurnRequest.parse({"content": "/new"}),
                )
        # Nothing persisted: client commands run in the browser, not as turns.
        self.assertEqual(ChatMessage.objects.filter(session=self.session).count(), 0)

    def test_command_stored_on_message_and_system_prompt_untouched(self):
        # The command is stored on the returned messages (and therefore on
        # the rows): regenerate replays the same call. Asserted on the
        # outcome rather than re-read rows: the same check on rows is what
        # `test_pipeline.py` already covers for every turn's persistence.
        from chat.turn import prompts

        baseline_prompts = []

        async def _stream(**kwargs):
            baseline_prompts.append(kwargs.get("system_message"))
            yield {"type": "content", "content": "noted"}
            yield {"type": "metadata", "usage": {"total_tokens": 5}}

        async def _stream2(**kwargs):
            baseline_prompts.append(kwargs.get("system_message"))
            yield {"type": "content", "content": "noted"}
            yield {"type": "metadata", "usage": {"total_tokens": 5}}

        with patch("llm.access.stream", _stream):
            plain_outcome = async_to_sync(run_chat_turn)(
                session=self.session, user=self.user,
                request=TurnRequest.parse({"content": "hello"}),
            )
        with patch("llm.access.stream", _stream2):
            commanded_outcome = async_to_sync(run_chat_turn)(
                session=self.session, user=self.user,
                request=TurnRequest.parse({
                    "content": "/research is rust fast",
                }),
            )
        # Same baseline (prefix-cache guard): the expansion rides the
        # trailing context message, never the system prompt.
        self.assertEqual(baseline_prompts[0], baseline_prompts[1])
        fresh_user = ChatMessage.objects.get(id=commanded_outcome.user_message.id)
        fresh_asst = ChatMessage.objects.get(id=commanded_outcome.assistant_message.id)
        user_meta = fresh_user.metadata or {}
        asst_meta = fresh_asst.metadata or {}
        stored = (user_meta.get("command", {})
                  or asst_meta.get("command", {}))
        self.assertTrue(stored, "the commanded turn stored no command: "
                                f"user={user_meta!r} assistant-keys={sorted(asst_meta)!r}")
        self.assertEqual(stored.get("name"), "research")

    def test_research_pins_intent(self):
        async def _fake_tool(name, args, context):
            return json.dumps({"type": "deep_research", "text": "Corpus.",
                               "queries": ["q"], "sources": []})

        with patch("llm.access.stream", self._stream("Answer.")), \
                patch("chat.tools.execute_tool", _fake_tool):
            outcome = async_to_sync(run_chat_turn)(
                session=self.session, user=self.user,
                request=TurnRequest.parse({"content": "/research rust adoption"}),
            )
        self.assertTrue(outcome.assistant_message.metadata.get("sources") is not None
                        or "deep_research" in json.dumps(
                            outcome.assistant_message.metadata.get("tool_trace", [])))


class CommandConsentTests(TestCase):
    """The first action runs without approval; every later call is gated;
    `/goal` does nothing until confirmed."""

    def setUp(self):
        self.user = _user("consenter")
        self.session = ChatSession.objects.create(user=self.user, title="t")
        self._patch("chat.turn.agent.suggest_follow_ups", self._no_follow_ups)
        self._patch("chat.tools.get_available_tools", self._builtin_tools)
        self._patch("chat.tools.execute_tool", self._inert_tool)

    @staticmethod
    async def _no_follow_ups(*_a, **_k):
        return []

    @staticmethod
    async def _builtin_tools(*_a, **_k):
        from chat.tools import AVAILABLE_TOOLS

        return list(AVAILABLE_TOOLS)

    @staticmethod
    async def _inert_tool(name, args, context):
        return json.dumps({"ok": True})

    def _patch(self, target, replacement):
        patcher = patch(target, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_agent_command_builds_a_start_without_approval(self):
        from chat.commands.agents import agent_command
        from chat.commands.registry import CommandCall

        from agents.models import SubAgent

        agent = SubAgent.objects.create(user=self.user, name="Runner")
        result = async_to_sync(agent_command)(
            CommandCall(name="agent", args={"agent": agent.id},
                        text="do it"), _ctx(self.user))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.start["type"], "agent_run")
        self.assertEqual(result.start["agent_id"], agent.id)

    def test_agent_refuses_in_plan_mode(self):
        from chat.commands.agents import agent_command
        from chat.commands.registry import CommandCall

        from agents.models import SubAgent

        agent = SubAgent.objects.create(user=self.user, name="Planner")
        result = async_to_sync(agent_command)(
            CommandCall(name="agent", args={"agent": agent.id}, text="do it"),
            _ctx(self.user, autonomy="plan"))
        self.assertEqual(result.status, "error")
        self.assertIn("Plan mode", result.message)

    def test_goal_returns_confirm_not_a_start(self):
        from chat.commands.missions import goal_command
        from chat.commands.registry import CommandCall

        from agents.models import SubAgent

        SubAgent.objects.create(user=self.user, name="Worker")
        result = async_to_sync(goal_command)(
            CommandCall(name="goal", args={}, text="chase invoices"),
            _ctx(self.user))
        self.assertEqual(result.status, "confirm")
        self.assertEqual(result.args["goal"], "chase invoices")


class MissionRouteTests(TestCase):
    def setUp(self):
        self.user = _user("missioner")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        from agents.models import SubAgent

        self.agent = SubAgent.objects.create(user=self.user, name="Worker")

    def test_post_missions_starts_through_the_same_service(self):
        res = self.client.post("/api/missions/", {
            "goal": "chase three invoices", "agent_id": self.agent.id,
            "budget_inr": 400,
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["mission_id"])

    def test_post_missions_needs_a_budget(self):
        res = self.client.post("/api/missions/", {
            "goal": "chase three invoices", "agent_id": self.agent.id,
            "budget_inr": 0,
        }, format="json")
        self.assertEqual(res.status_code, 400)

    def test_pause_resume_cancel(self):
        from missions.models import Mission

        row = Mission.objects.create(user=self.user, agent=self.agent,
                                     goal="g", budget_inr=100)
        for verb, expected in (("pause", "paused"), ("resume", "active"),
                               ("cancel", "cancelled")):
            res = self.client.post(f"/api/missions/{row.id}/{verb}/")
            self.assertEqual(res.status_code, 200, res.data)
            self.assertEqual(res.data["status"], expected)

    def test_foreign_mission_is_404(self):
        other = _user("stranger")
        from missions.models import Mission

        row = Mission.objects.create(user=other, agent=self.agent, goal="g",
                                     budget_inr=100)
        res = self.client.get(f"/api/missions/{row.id}/")
        self.assertEqual(res.status_code, 404)


class CommandEndpointTests(TestCase):
    def setUp(self):
        self.user = _user("endpoint")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_list_is_filtered(self):
        res = self.client.get("/api/chat/commands/")
        self.assertEqual(res.status_code, 200)
        names = {c["name"] for c in res.data["commands"]}
        self.assertIn("agent", names)
        self.assertNotIn("code-review", names)

    def test_complete_agent_names(self):
        from agents.models import SubAgent

        SubAgent.objects.create(user=self.user, name="Reporter")
        res = self.client.get("/api/chat/commands/complete/",
                              {"command": "agent", "arg": "agent", "q": "rep"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(any("Reporter" in c["label"]
                            for c in res.data["candidates"]))

    def test_run_turn_command_is_refused_here(self):
        res = self.client.post("/api/chat/commands/run/",
                               {"name": "agent", "args": {}}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_run_memory_fact(self):
        res = self.client.post("/api/chat/commands/run/",
                               {"name": "memory", "args": {"text": "I work in IST"}},
                               format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["card"]["type"], "memory_saved")


class NewCapabilityCommandsTests(TestCase):
    """Every newer capability gets a slash spelling: web, media, office,
    knowledge, APIs, messaging, signing and running code. All are plain
    turns — the turn itself is side-effect free and the priced or outward
    tool still gates at dispatch — so each test pins the status, the pin
    and the refusal of an empty line."""

    def setUp(self):
        self.user = _user("capabilities")
        self.ctx = _ctx(self.user)

    def _run(self, dotted, **call_kwargs):
        import importlib

        from chat.commands.registry import CommandCall

        module_name, func_name = dotted.rsplit(".", 1)
        func = getattr(importlib.import_module(module_name), func_name)
        call_kwargs.setdefault("name", func_name.replace("_command", ""))
        return async_to_sync(func)(
            CommandCall(**call_kwargs), self.ctx)

    def test_web_tier_pins_search_read_download(self):
        res = self._run("chat.commands.web.search_command",
                        args={"text": "repo rates"}, text="")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.tool_pin, ("web_search",))
        self.assertIn("/search", res.context_block)

        res = self._run("chat.commands.web.read_command",
                        args={"text": "https://example.com"}, text="")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.tool_pin, ("scrape_webpage", "read_url"))

        res = self._run("chat.commands.web.download_command",
                        args={}, text="https://example.com/f.pdf")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.tool_pin, ("download_file",))
        res = self._run("chat.commands.web.download_command",
                        args={}, text="  ")
        self.assertEqual(res.status, "error")

    def test_image_needs_a_subject_and_pins_generation(self):
        res = self._run("chat.commands.media.image_command",
                        args={"text": "a heron at dawn"}, text="")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.tool_pin, ("generate_image",))
        res = self._run("chat.commands.media.image_command",
                        args={}, text=" ")
        self.assertEqual(res.status, "error")

    def test_voice_commands_pin_their_engines(self):
        from chat.commands import registry as command_registry

        res = self._run("chat.commands.media.speak_command",
                        args={"text": "hello"}, text="")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.tool_pin, ("text_to_speech",))
        self.assertEqual(command_registry.get("speak").requires, "tts")

        res = self._run("chat.commands.media.transcribe_command",
                        args={"file": "/Chat/note.m4a"}, text="")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.tool_pin, ("transcribe_audio",))
        self.assertEqual(command_registry.get("transcribe").requires, "stt")

    def test_office_trio_pins_renderers(self):
        for dotted, tool in [
            ("chat.commands.library.chart_command", "render_chart"),
            ("chat.commands.library.diagram_command", "render_diagram"),
            ("chat.commands.library.pdf_command", "render_pdf"),
        ]:
            res = self._run(dotted, args={"text": "q3"}, text="")
            self.assertEqual(res.status, "ok", dotted)
            self.assertEqual(res.tool_pin, (tool,), dotted)

    def test_knowledge_commands_pin_rag(self):
        res = self._run("chat.commands.knowledge.kb_command",
                        args={"text": "refund policy"}, text="")
        self.assertEqual(res.status, "ok")
        self.assertIn("knowledge_base_search", res.tool_pin)
        self.assertIn("read_document", res.tool_pin)
        res = self._run("chat.commands.knowledge.kb_command",
                        args={}, text=" ")
        self.assertEqual(res.status, "error")

        res = self._run("chat.commands.knowledge.extract_command",
                        args={"text": "invoice totals"}, text="")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.tool_pin, ("extract_data",))

    def test_api_mirrors_sql_without_a_connection(self):
        res = self._run("chat.commands.shortcuts.api_command",
                        args={}, text="")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.card.get("type"), "api_pick")

    def test_message_sign_run_are_plain_turns_with_pins(self):
        res = self._run("chat.commands.shortcuts.message_command",
                        args={"text": "ping ops"}, text="")
        self.assertEqual(res.status, "ok")
        self.assertIn("message_send", res.tool_pin)

        from chat.commands import registry as command_registry

        self.assertEqual(command_registry.get("sign").requires, "esign")
        res = self._run("chat.commands.shortcuts.sign_command",
                        args={"text": "msa.pdf to legal"}, text="")
        self.assertEqual(res.status, "ok")
        self.assertIn("request_signature", res.tool_pin)

        res = self._run("chat.commands.shortcuts.run_command",
                        args={"text": "sum the column"}, text="")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.tool_pin, ("execute_python",))

    def test_new_commands_are_listed(self):
        from chat.commands import registry as command_registry

        names = {c.name for c in command_registry.all_commands()}
        for expected in ("search", "read", "download", "image", "speak",
                         "transcribe", "chart", "diagram", "pdf", "kb",
                         "extract", "api", "message", "sign", "run"):
            self.assertIn(expected, names)
