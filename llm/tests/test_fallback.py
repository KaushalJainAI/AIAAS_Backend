"""
The platform fallback model: what it is, when it applies, and what it never does.

The fallback is a staff-edited row, not an env var, so nothing here may
require a restart or a rebuild to take effect — reads go through
`get_fallback` with a short TTL. And substitution never rewrites the stored
configuration: `resolve_with_fallback` returns a pair to *run on*, while the
agent row, session, or profile keeps what the owner chose.
"""
import pathlib

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from llm import fallback as _fallback
from llm.models import AIModel, AIProvider, ModelFallback


class GetFallbackTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_no_row_means_the_router_default(self):
        self.assertEqual(
            _fallback.get_fallback(), ('openrouter', 'openrouter/free'))

    def test_a_saved_row_wins_over_the_default(self):
        _fallback.set_fallback('openai', 'gpt-5.6-luna')
        self.assertEqual(
            _fallback.get_fallback(), ('openai', 'gpt-5.6-luna'))

    def test_a_blank_model_in_the_row_falls_back_to_the_default(self):
        ModelFallback.objects.create(pk=1, provider='openrouter', model='')
        self.assertEqual(
            _fallback.get_fallback(), ('openrouter', 'openrouter/free'))

    def test_saving_clears_the_cache(self):
        self.assertEqual(
            _fallback.get_fallback(), ('openrouter', 'openrouter/free'))
        _fallback.set_fallback('openai', 'gpt-5.6-luna')
        # No cache.clear() here: saving must invalidate on its own, or a
        # staff edit would need a restart to take effect.
        self.assertEqual(
            _fallback.get_fallback(), ('openai', 'gpt-5.6-luna'))

    def test_saving_a_retired_model_warns_but_saves(self):
        provider = AIProvider.objects.create(name='OR', slug='openrouter')
        AIModel.objects.create(
            provider=provider, name='Dead', value='dead/model',
            is_active=False)
        row, warning = _fallback.set_fallback('openrouter', 'dead/model')
        self.assertEqual(row.model, 'dead/model')
        self.assertIn('retired', warning)


class ResolveWithFallbackTests(TestCase):
    def setUp(self):
        cache.clear()
        self.provider = AIProvider.objects.create(name='OR', slug='openrouter')
        AIModel.objects.create(
            provider=self.provider, name='Shiny', value='new/shiny',
            is_active=True)
        AIModel.objects.create(
            provider=self.provider, name='Busted', value='old/busted',
            is_active=False)

    def tearDown(self):
        cache.clear()

    def test_an_active_model_passes_through(self):
        self.assertEqual(
            _fallback.resolve_with_fallback('openrouter', 'new/shiny'),
            ('openrouter', 'new/shiny', False, ''))

    def test_a_retired_model_is_substituted(self):
        provider, model, substituted, reason = (
            _fallback.resolve_with_fallback('openrouter', 'old/busted'))
        self.assertTrue(substituted)
        self.assertEqual((provider, model), ('openrouter', 'openrouter/free'))
        self.assertIn('retired', reason)

    def test_an_unknown_model_is_substituted_while_the_provider_has_a_catalogue(self):
        provider, model, substituted, reason = (
            _fallback.resolve_with_fallback('openrouter', 'typo/model'))
        self.assertTrue(substituted)
        self.assertEqual((provider, model), ('openrouter', 'openrouter/free'))
        self.assertIn('typo/model', reason)

    def test_an_unknown_model_passes_through_when_no_catalogue_is_held(self):
        # Fresh install (or a provider we do not seed): refusing every model
        # would make the product unusable until someone seeds it — the same
        # rule `AgentSerializer._model_problem` applies.
        self.assertEqual(
            _fallback.resolve_with_fallback('ollama', 'qwen3:8b'),
            ('ollama', 'qwen3:8b', False, ''))

    def test_a_blank_model_is_not_judged(self):
        # Blank means "account default" upstream of here.
        self.assertEqual(
            _fallback.resolve_with_fallback('openrouter', ''),
            ('openrouter', '', False, ''))

    def test_the_fallback_itself_is_never_substituted(self):
        self.assertEqual(
            _fallback.resolve_with_fallback('openrouter', 'openrouter/free'),
            ('openrouter', 'openrouter/free', False, ''))

    def test_a_custom_fallback_row_is_honoured(self):
        _fallback.set_fallback('openai', 'gpt-5.6-luna')
        provider, model, substituted, reason = (
            _fallback.resolve_with_fallback('openrouter', 'old/busted'))
        self.assertTrue(substituted)
        self.assertEqual((provider, model), ('openai', 'gpt-5.6-luna'))
        self.assertIn('retired', reason)


class FallbackPinningTests(SimpleTestCase):
    """The default fallback is only real if the seed actually carries it.

    Mirrors `ShippedDefaultTests`: the declaration (code default here) and
    the catalogue row (seed script) live in different files, so nothing but
    a test connects them. Read out of the seed's source rather than the
    database, because the seed is a script and not a migration.
    """

    def test_the_default_fallback_is_in_the_seed_with_effort(self):
        source = (
            pathlib.Path(__file__).resolve().parents[2]
            / 'populate_models.py'
        ).read_text(encoding='utf-8')
        row = next(
            (line for line in source.splitlines()
             if f'"{_fallback.DEFAULT_FALLBACK_MODEL}"' in line),
            None,
        )
        self.assertIsNotNone(
            row,
            f'{_fallback.DEFAULT_FALLBACK_MODEL} is not in the seed catalogue')
        self.assertIn(
            'effort=', row,
            'the default fallback declares no effort levels, so a default '
            'effort would be snapped away on the first fallback run',
        )
