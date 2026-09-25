"""
The only writer of `logs.CostEntry`.

Priced tools call `record(...)` at dispatch time — from the rows rather than
counted in memory, for the reason the turn rollup gives: a resumed run starts
with a fresh process but the same rows. Recording is idempotent per
`(execution, source, kind)` call site through `dedupe_key`: a step re-run on
resume updates its row instead of adding a second charge.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


def record(
    *,
    user,
    kind: str,
    amount_inr: int,
    execution=None,
    session=None,
    units: Decimal | float | int | str = 0,
    unit: str = '',
    estimated: bool = True,
    source: str = '',
    dedupe_key: str = '',
) -> Any | None:
    """Write one cost row. Never raises — a failed ledger write must not fail
    the tool call it accounts for."""
    from logs.models import CostEntry

    try:
        amount = int(amount_inr or 0)
        if amount <= 0:
            # Unpriced is estimated, never free — but zero is zero, not a
            # charge. Callers that cannot price must pass at least 1.
            return None
        kwargs: dict[str, Any] = dict(
            user=user,
            kind=kind,
            amount_inr=amount,
            execution=execution,
            session=session,
            units=Decimal(str(units or 0)),
            unit=unit,
            estimated=estimated,
            source=source,
        )
        if dedupe_key and execution is not None:
            # One charge per step attempt: `source` carries the step's call id.
            entry, created = CostEntry.objects.update_or_create(
                execution=execution,
                source=dedupe_key,
                kind=kind,
                defaults=kwargs,
            )
            return entry
        return CostEntry.objects.create(**kwargs)
    except Exception:  # noqa: BLE001
        logger.exception('[Costs] Failed to record %s cost', kind)
        return None


