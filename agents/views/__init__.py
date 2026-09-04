"""
Agents HTTP views.

One module per concern, so a route's neighbours are the routes it shares state
and failure modes with:

  agents         agent CRUD and the AgentConfig <-> columns mapping
  runs           execute, approve, reject, steer — intervening in a live run
  triggers       trigger CRUD and the one public webhook receiver
  hitl           pending approvals and the response that resumes a run
  conversations  the builder chat transcript
  system         orchestrator LLM settings
  responses      inline response serializers shared across the above

`urls.py` imports the submodules directly, so the routing table names the module
that owns each route. The re-exports below keep `agents.views.<name>` working
for the project URLconf and for callers that predate the split.
"""
from .conversations import conversation_messages
from .hitl import pending_hitl_requests, respond_to_hitl
from .system import update_orchestrator_settings

__all__ = [
    "conversation_messages",
    "pending_hitl_requests",
    "respond_to_hitl",
    "update_orchestrator_settings",
]
