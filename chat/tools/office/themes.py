"""
The house styles every rendered file is drawn in.

A model picks a theme by name and nothing else: fonts, sizes, colours and
margins are decided here, once, the way `ChartArtifact.tsx` owns every visual
decision about a chart. That is the only reason two decks made a week apart
look like they came from the same product.

The series colours are the chart palette from `ChartArtifact.tsx` in the same
fixed order — validated there for separation under colour-vision deficiency —
so a chart in a deck and the same chart in the conversation match. `dark` uses
that component's dark-surface variants for the same reason.
"""
from __future__ import annotations

from dataclasses import dataclass

LIGHT_PALETTE = ('2A78D6', 'EB6834', '1BAF7A', 'EDA100', 'E87BA4', '008300', '4A3AA7', 'E34948')
DARK_PALETTE = ('3987E5', 'D95926', '199E70', 'C98500', 'D55181', '008300', '9085E9', 'E66767')


@dataclass(frozen=True)
class Theme:
    name: str
    #: Slide / page background.
    background: str
    #: Panels on the background: table stripes, stat cards, quote blocks.
    surface: str
    text: str
    muted: str
    #: Rules, bullets, headings' accent, title-slide band.
    accent: str
    #: Text drawn on `accent`.
    on_accent: str
    #: Title slides are full-bleed accent in `bold`; plain in the others.
    accent_title: bool
    heading_font: str
    body_font: str
    palette: tuple[str, ...]
    #: Gridlines and table borders.
    rule: str


THEMES: dict[str, Theme] = {
    'clean': Theme(
        name='clean', background='FFFFFF', surface='F3F5F8', text='1A1A1A',
        muted='5F6368', accent='2A78D6', on_accent='FFFFFF', accent_title=False,
        heading_font='Calibri', body_font='Calibri', palette=LIGHT_PALETTE,
        rule='D9DDE3',
    ),
    'bold': Theme(
        name='bold', background='FFFFFF', surface='F1EFFA', text='17142B',
        muted='5B5870', accent='4A3AA7', on_accent='FFFFFF', accent_title=True,
        heading_font='Arial', body_font='Arial', palette=LIGHT_PALETTE,
        rule='DCD8EE',
    ),
    'dark': Theme(
        name='dark', background='141414', surface='232323', text='F2F2F2',
        muted='A6A6A6', accent='3987E5', on_accent='FFFFFF', accent_title=False,
        heading_font='Calibri', body_font='Calibri', palette=DARK_PALETTE,
        rule='3A3A3A',
    ),
}

THEME_NAMES = tuple(THEMES)
DEFAULT_THEME = 'clean'
