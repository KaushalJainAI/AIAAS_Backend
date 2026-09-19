"""
External adapters. Registration is the schema: one entry per dataset, no second list.
"""
from __future__ import annotations

from . import dabench, gaia, ifeval, simpleqa
from .base import Adapter

ADAPTERS: dict[str, Adapter] = {
    'ifeval': Adapter(
        slug='ifeval', name='IFEval', source_url='https://github.com/google-research/google-research/tree/master/ifeval',
        licence='Apache-2.0', hf_repo='google/IFEval', agent='assistant',
        default_sample=30, load=ifeval.load,
    ),
    'gaia': Adapter(
        slug='gaia', name='GAIA L1', source_url='https://gaia-benchmark-leaderboard-spaces.hf.space/',
        licence='gated (see source terms)', hf_repo='gaia-benchmark/GAIA',
        agent='generalist', default_sample=30, gated=True, redact_gold=True,
        load=gaia.load,
    ),
    'dabench': Adapter(
        slug='dabench', name='InfiAgent-DABench',
        source_url='https://github.com/InfiAgent/InfiAgent-DABench',
        licence='see source', hf_repo='', agent='analyst',
        default_sample=30, load=dabench.load,
    ),
    'simpleqa': Adapter(
        slug='simpleqa', name='SimpleQA',
        source_url='https://github.com/openai/simple-evals',
        licence='MIT', hf_repo='', agent='researcher',
        default_sample=30, load=simpleqa.load,
    ),
}

__all__ = ['ADAPTERS', 'Adapter']
