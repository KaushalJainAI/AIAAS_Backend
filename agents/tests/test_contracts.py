"""
Output contracts, and whether configuration can actually replace code.

The claim under test: a `SubAgent` configured with the `research` contract can
produce the same wire shape as the hardcoded `deep_research` tool, so the
frontend source panels render either without knowing which produced it. If that
is not true, the architecture has not earned the right to retire the tool.
"""
from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from agents import contracts, stock
from agents.agent.runtime import build_system_prompt
from agents.models import SubAgent


class ResolutionTests(SimpleTestCase):
    def test_no_schema_means_prose(self):
        self.assertIsNone(contracts.resolve(None))
        self.assertIsNone(contracts.resolve({}))

    def test_a_named_contract_resolves(self):
        self.assertIs(contracts.resolve({'contract': 'research'}), contracts.RESEARCH)

    def test_an_unknown_contract_resolves_to_nothing(self):
        """A closed registry: the UI can only render shapes it has a panel for."""
        self.assertIsNone(contracts.resolve({'contract': 'invented'}))

    def test_the_instruction_is_empty_for_prose(self):
        self.assertEqual(contracts.instruction_for(None), '')


class CoercionTests(SimpleTestCase):
    def test_a_well_formed_answer_passes_through(self):
        payload = contracts.coerce(
            json.dumps({'text': 'findings', 'queries': ['a'],
                        'sources': [{'url': 'u', 'title': 't'}]}),
            contracts.RESEARCH,
        )

        self.assertEqual(payload['text'], 'findings')
        self.assertEqual(payload['queries'], ['a'])

    def test_optional_keys_are_filled_in(self):
        payload = contracts.coerce(json.dumps({'text': 'findings'}),
                                   contracts.RESEARCH)

        self.assertEqual(payload['queries'], [])
        self.assertEqual(payload['sources'], [])
        # The `type` discriminator the frontend switches on.
        self.assertEqual(payload['type'], 'deep_research')

    def test_a_code_fence_is_tolerated(self):
        """Models fence JSON despite being told not to; cheaper to accept."""
        fenced = '```json\n{"text": "findings"}\n```'
        self.assertEqual(
            contracts.coerce(fenced, contracts.RESEARCH)['text'], 'findings',
        )

    def test_common_key_aliases_are_repaired(self):
        payload = contracts.coerce(json.dumps({'summary': 'findings'}),
                                   contracts.RESEARCH)
        self.assertEqual(payload['text'], 'findings')

    def test_bare_url_strings_become_source_objects(self):
        payload = contracts.coerce(
            json.dumps({'text': 'x', 'sources': ['https://a.example']}),
            contracts.RESEARCH,
        )
        self.assertEqual(payload['sources'][0]['url'], 'https://a.example')

    def test_prose_is_a_failure_not_a_silent_wrap(self):
        """The line that keeps the mechanism from being decorative.

        Wrapping prose in `{"text": ...}` would make every agent appear to
        satisfy every contract it was given.
        """
        with self.assertRaises(contracts.ContractError) as caught:
            contracts.coerce('Here is what I found...', contracts.RESEARCH)
        self.assertIn('prose', str(caught.exception))

    def test_a_missing_required_key_is_a_failure(self):
        with self.assertRaises(contracts.ContractError):
            contracts.coerce(json.dumps({'queries': ['a']}), contracts.RESEARCH)

    def test_a_json_array_is_not_an_object(self):
        with self.assertRaises(contracts.ContractError):
            contracts.coerce('[1, 2, 3]', contracts.RESEARCH)


class PromptTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='contracted', password='pw')

    def _prompt(self, agent) -> str:
        return build_system_prompt(
            agent, {'skills': [], 'knowledge_bases': [], 'ctx': {}},
        )

    def test_a_contract_agent_is_told_what_shape_to_return(self):
        agent = SubAgent(name='R', prompt='research things',
                         tool_grants={}, guardrails={},
                         output_schema={'contract': 'research'})

        prompt = self._prompt(agent)

        self.assertIn('OUTPUT FORMAT', prompt)
        self.assertIn('"sources"', prompt)

    def test_a_prose_agent_gets_no_output_section(self):
        agent = SubAgent(name='R', prompt='just answer',
                         tool_grants={}, guardrails={}, output_schema={})

        self.assertNotIn('OUTPUT FORMAT', self._prompt(agent))


class SupersessionTests(TestCase):
    """Can a configured agent stand in for the hardcoded `deep_research`?"""

    def setUp(self):
        self.user = User.objects.create_user(username='super', password='pw')

    def test_the_stock_research_agent_is_configured_end_to_end(self):
        agent = stock.build(self.user)

        # The three things a prose-only agent could never express, and the
        # reason `deep_research` could not be replaced before they existed.
        self.assertEqual(agent.output_schema, {'contract': 'research'})
        self.assertEqual(agent.fanout['parallel'], 4)
        self.assertTrue(agent.tool_grants['webSearch'])
        self.assertTrue(agent.tool_grants['scrape'])

    def test_it_cannot_delegate_or_run_code(self):
        """A research agent that can shell out is a different risk entirely."""
        agent = stock.build(self.user)

        self.assertFalse(agent.tool_grants['subAgents'])
        self.assertFalse(agent.tool_grants['codeExecution'])
        self.assertFalse(agent.tool_grants['shell'])

    def test_its_answer_matches_the_hardcoded_tool_s_wire_shape(self):
        """The actual supersession test: same keys, so the same panel renders.

        `chat/turn/agent.py::_on_deep_research` and the frontend source panels
        switch on `type` and read `queries`/`sources`/`text`. If a configured
        agent cannot produce that, the tool cannot be retired however good the
        prompt is.
        """
        model_answer = json.dumps({
            'text': 'Findings about the topic.',
            'queries': ['angle one', 'angle two'],
            'sources': [{'url': 'https://a.example', 'title': 'A'}],
        })

        produced = contracts.coerce(model_answer, contracts.RESEARCH)

        hardcoded_keys = {'type', 'text', 'queries', 'sources'}
        self.assertTrue(hardcoded_keys.issubset(produced.keys()))
        self.assertEqual(produced['type'], 'deep_research')

    def test_the_hardcoded_tool_still_exists(self):
        """Deliberate: it makes zero model calls and this makes at least one.

        Retiring it is a decision to pay more per research call, and that
        should be made on measurements rather than as a side effect of the
        configured version starting to work.
        """
        from chat import tools

        self.assertIn('deep_research', [t['function']['name']
                                        for t in tools.AVAILABLE_TOOLS])
