"""
The request/response pipeline every HTTP call passes through.

`middleware` (logging, input sanitisation, rate-limit headers), `throttling`,
and `pagination`. Registered by dotted path in `settings.base`, so moving a
class here means updating that string too.
"""
