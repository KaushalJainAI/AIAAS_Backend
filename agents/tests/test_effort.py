"""
The builder's effort knob, and the rule every knob added to that screen has to
follow: an agent saved before the knob existed must run exactly as it did.

That is the trap `connectors` documents and `notifyOnHitl` fell into twice —
a field lands in the builder, something starts reading it, and every agent that
never made a choice is silently narrowed. Here the safe value is `''`, meaning
the model's own default, and the tests below pin that it survives a round trip
without anyone having chosen it.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from agents.models import SubAgent
from agents.views.agents import AgentSerializer


class SerializerRoundTripTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='ag', email='ag@example.com', password='pw',
        )

    def _config(self, agent):
        return AgentSerializer.to_config(agent)

    def test_an_agent_that_predates_the_knob_reads_as_model_default(self):
        agent = SubAgent.objects.create(user=self.user, name='Legacy')
        self.assertEqual(self._config(agent)['effort'], '')

    def test_a_chosen_level_round_trips(self):
        serializer = AgentSerializer(data={'name': 'A', 'effort': 'high'})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        agent = SubAgent(user=self.user)
        AgentSerializer.apply(agent, serializer.validated_data)
        agent.save()
        self.assertEqual(agent.runtime_settings['effort'], 'high')
        self.assertEqual(self._config(agent)['effort'], 'high')

    def test_blank_is_accepted_as_a_real_choice(self):
        serializer = AgentSerializer(data={'name': 'A', 'effort': ''})
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_a_level_outside_the_ladder_is_refused(self):
        # Validated against the ladder rather than against a model's own rungs:
        # the model can change in the same PATCH, and `llm.access` snaps at call
        # time anyway. What is rejected here is a typo, not a mismatch.
        serializer = AgentSerializer(data={'name': 'A', 'effort': 'turbo'})
        self.assertFalse(serializer.is_valid())
        self.assertIn('effort', serializer.errors)


class BuilderKnobTests(TestCase):
    def test_the_knob_is_offered_and_takes_the_ladder_plus_blank(self):
        from agents.views.builder import KNOBS
        from llm.effort import LADDER

        knob = KNOBS['effort']
        for level in (*LADDER, ''):
            self.assertEqual(knob.coerce(level, None), level)

    def test_the_knob_refuses_a_level_that_is_not_one(self):
        from agents.views.builder import KNOBS, Reject

        with self.assertRaises(Reject):
            KNOBS['effort'].coerce('turbo', None)
