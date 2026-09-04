"""
Getting a turn's bytes on and off the wire.

`sse` is the frame format; `streaming_http` is request parsing, bearer auth for
views DRF cannot wrap, and rendering a run as `text/event-stream`. Neither knows
what a turn *is*, which is the point: the streaming and non-streaming endpoints
are the same pipeline call with a different sink.
"""
