"""
The permissions vocabulary: which tools each grant unlocks, what every agent
gets regardless, the autonomy levels and who may start a run.

Pure data, no Django models and no imports from the rest of the app, so any
module can read the policy without importing the agent runtime to get it. The
builder validates against these tables, the capabilities endpoint renders them,
the tool library page lists them, evals and chat's agent-authoring tools check
against them, and `agents/agent/runtime.py` enforces them. Before this module
existed they lived at the top of the 2,600-line runtime, and reading one table
meant importing the whole of it.

**A tool absent from every value of `GRANT_TOOLS` is unreachable by an agent**
no matter what the model asks for. Adding a tool for agents means adding its
name here.
"""
from __future__ import annotations


#: Grant key -> the built-in tool names it unlocks. This map *is* the
#: permissions model; a tool absent from every value is unreachable by an agent
#: no matter what the model asks for.
GRANT_TOOLS: dict[str, tuple[str, ...]] = {
    'webSearch': ('web_search', 'deep_research', 'image_search', 'video_search'),
    # `download_file` is here rather than with the file tools: what it needs
    # permission for is reaching the web, and where it writes is already
    # bounded by the file scope.
    'scrape': ('scrape_webpage', 'read_url', 'download_file'),
    # All five, not two. `list_knowledge_bases` tells the model to use
    # `keyword_search` on a keyword KB and `list_documents` + `read_document` on
    # a raw one — tools this grant did not unlock, so the catalogue was
    # instructing the agent to call things it would then be refused, and an
    # agent whose KB was keyword- or raw-backed could not read it at all.
    'rag': ('list_knowledge_bases', 'knowledge_base_search', 'keyword_search',
            'list_documents', 'read_document', 'extract_data', 'ocr_document'),
    'codeExecution': ('execute_python', 'run_python_on_files'),
    # The virtual filesystem over the user's own Folder/Document tree
    # (`inference/vfs.py`). What the agent may actually do with these is a
    # second axis — `sandbox['fileAccess']` decides read-only vs. its own
    # folder vs. the whole tree, and `none` means the tools are never offered
    # even with the grant on. The grant says "may touch files at all"; the
    # scope says "which files".
    'fileOps': ('list_files', 'find_files', 'read_file', 'write_file',
                'edit_file', 'make_directory', 'delete_file',
                'move_file', 'copy_file',
                'file_versions', 'restore_file_version', 'export_file',
                'edit_document', 'edit_deck'),
    # Decks, workbooks and Word files rendered from a spec (`chat/tools/office`).
    # A grant of its own rather than part of `fileOps`, because "may produce a
    # presentation" and "may rewrite my documents" are different things to
    # hand out. It still needs a file scope to save into — `fileAccess` says
    # where, exactly as for `fileOps`, and with no scope it is withheld.
    'office': ('render_deck', 'render_workbook', 'render_document', 'render_pdf',
               'edit_workbook', 'read_workbook', 'render_diagram'),
    # Generated images (`chat/tools/media.py`). Its own grant because it
    # spends the user's money per call, and saved into the file scope, so it
    # needs `fileAccess` exactly as `office` does.
    'media': ('generate_image',),
    # Speech in both directions (`chat/tools/voice.py`): transcription of a
    # recording the user has, synthesis into an audio file. Behind one-door
    # engines; with no engine the tools are not offered. Both need a file
    # scope to read from and save into.
    'voice': ('transcribe_audio', 'text_to_speech'),
    # Documents out for e-signature (`chat/tools/esign.py`). Outward-facing
    # like `publish`, so the send is `sensitive` + `irreversible`; completion
    # arrives on the webhook. Needs a file scope to read the document.
    'esign': ('request_signature', 'signature_status'),
    # One tool set for every messaging channel (`chat/tools/talk.py`):
    # channels, search, read, draft, send. The grant says whether an agent
    # may message; `recipients` says whom an unattended run may reach.
    'talk': ('message_channels', 'message_search', 'message_read',
             'message_draft', 'message_send'),
    # The user's databases (`chat/tools/data.py`): list, describe, read, and
    # writes where the owner allowed them. `dataConnections` says which.
    'data': ('list_data_connections', 'describe_schema', 'query_sql',
             'execute_sql'),
    # The user's HTTP APIs (`chat/tools/apicaller.py`): operations and calls.
    # `apiConnections` says which, each in `read` or `all` mode.
    'api': ('list_api_operations', 'call_api'),
    # One persistent, isolated machine per user (`chat/tools/compute.py`,
    # `workspaces/engine.py`): short commands and detached jobs. `compute`
    # says whether; `workspaceEgress` says which hosts it may reach.
    'compute': ('workspace_exec', 'start_job', 'job_status', 'job_logs',
                'cancel_job', 'sync_files'),
    # Our own sandboxed coding agent (`chat/tools/code.py`): the workspace
    # file tools plus git. Serves the `shell` grant, which leaves
    # UNSERVED_GRANTS in the same change.
    'shell': ('ws_list', 'ws_read', 'ws_search', 'ws_write', 'ws_edit',
              'ws_apply_patch', 'ws_run', 'git_status', 'git_diff',
              'git_commit', 'git_push', 'open_pull_request'),
    # A remote browser (`chat/tools/browser.py`). Reading is harmless;
    # acting is scoped to `agent_context['browserDomains']` at dispatch.
    'browser': ('browse_page', 'browser_act'),
    # Hosted pages: snapshots shareable by link (`chat/tools/publish.py`).
    # Outward-facing, so the tool is `sensitive` + `irreversible` and the
    # ladder gates it with no new mechanism; above `link` visibility from an
    # unattended run is refused inside the tool, where the argument is.
    'publish': ('publish_page',),
    # Delegation. There is no 'orchestrator' kind of agent — an agent that
    # fans out to other agents is one holding this grant, so composition is
    # checked by the same mechanism as every other capability instead of by a
    # second code path that could disagree with it.
    # `answer_subagent` is how a manager answers a worker that stopped to ask.
    'subAgents': ('invoke_subagent', 'search_agents', 'answer_subagent',
                  'start_tasks', 'wait_tasks', 'task_status',
                  'steer_task', 'stop_task', 'revert_task'),
    # MCP tools are resolved per-user at runtime rather than named here, so the
    # grant unlocks the whole user-configured set. Their names are namespaced by
    # `mcp_integration.tool_provider`, which is what keeps them from colliding
    # with a built-in and slipping past the allow-list.
    #
    # The same grant unlocks the *native* connector tools (`connector=` on
    # `@tool`, e.g. Gmail over REST). They are added in `allowed_names` rather
    # than listed here because which ones exist is the registry's answer, and
    # whether each is live is the card's — see `mcp_integration/native.py`.
    'mcp': (),
}

#: Granted in the builder but with no implementation the runtime is willing to
#: serve. Empty since P6 served `shell` through the workspace code tools —
#: kept as the set (rather than deleted) so the next unserved grant has a
#: named place to land, and the capabilities endpoint keeps its `served` bit.
UNSERVED_GRANTS = frozenset()

#: Available whatever the grants say: no side effects, no egress, no reads of
#: anything the user owns.
#:
#: `update_todos` qualifies on all three counts — it writes the run's own plan
#: into the run's own state and touches nothing else. Putting it behind a grant
#: would mean an agent could be configured *not to be able to keep track of
#: what it was doing*, which is not a capability anyone would deliberately
#: withhold, and the tool matters most on exactly the long runs a cautious
#: grant set would be applied to.
#: `render_chart` is here for the same reason: it writes nothing, reaches
#: nothing, and hands the client a validated spec to draw. An agent that has
#: analysed something and can only describe the numbers in prose produces a
#: worse report than one that can show them, and there is no blast radius to
#: gate — the drawing happens in the reader's browser, from data the agent
#: already had.
#: `notify_user` joins them (2026-09-20): it reaches only the owner's own
#: notification feed, and an unattended agent that cannot say "this needs
#: you" is not safer, only quieter. Capped per run inside the tool.
#: `render_dashboard` joins them (P8): it writes nothing, reaches nothing,
#: and hands the client a validated spec to draw — the same terms as
#: `render_chart`.
#: `mission_status`, `wait_for`, `complete_mission`, `report_progress`
#: (P7) join them: they read or affect only the mission chain this run
#: belongs to — the run's own state, like `update_todos` — and answer with
#: an error outside one. Gating them behind a grant would mean a mission run
#: could be told what it is missing with no way to fetch it.
#: `save_dashboard` (P8) joins them: it writes a row the user owns, like
#: `notify_user`, reversible and scoped to the caller.
#: `list_user_runs` joins them: it reads only the caller's own rows, so there
#: is nothing to gate — an agent that may not see what the user's jobs are
#: doing cannot answer "is it done?" or notify about a completion.
#: The reminder trio joins them on the same terms: rows the user owns,
#: reversible (every one cancellable), scoped to the caller.
#: `ask_user` joins them because an agent that may not ask is not safer, only
#: more inclined to guess; it reaches nothing and only records the question.
ALWAYS_AVAILABLE = ('get_current_time', 'update_todos', 'render_chart',
                    'render_dashboard', 'notify_user', 'mission_status',
                    'wait_for', 'complete_mission', 'report_progress',
                    'save_dashboard', 'list_user_runs',
                    'schedule_notification', 'list_scheduled_notifications',
                    'cancel_scheduled_notification', 'ask_user')

#: Offered only once this run has actually stored something — a tool result too
#: large to replay, or a step the curator removed. Both read back the run's own
#: transcript, so neither is a capability a grant should have to unlock: the
#: text was already shown to this agent, in this run, and the only question is
#: whether it can still see it.
#:
#: They were unreachable before curation existed, which made the notices that
#: name them dishonest — `tool_output.bound` has always told the model to "call
#: read_tool_output with that id", and no agent could, because the toolbox
#: filters `AVAILABLE_TOOLS` by the names its grants unlock and no grant named
#: it. An escape hatch nobody can open is worse than none, because the model
#: stops looking for another way out.
RETRIEVAL_TOOLS = ('read_tool_output', 'recall_context')


#: Command classes a coding agent's `ws_run` may reach. Closed on purpose:
#: a free-form string would let a template invent a class nothing enforces.
CODE_COMMAND_CLASSES = ('test', 'lint', 'build', 'run', 'install', 'any')


#: The autonomy ladder, strictest first. Each level is a *pair* — the tool
#: names that pause on sight, and the policy that judges the calls no name list
#: could contain — because MCP names are minted at runtime and a level defined
#: by names alone would silently exempt exactly the tools holding the user's
#: real credentials.
#:
#: `plan` and `auto` are the two rungs that make the ladder usable rather than
#: merely present. Without `plan` the only way to learn what an agent will do
#: is to let it do it; without `auto` the choice is between approving recycled
#: file writes one at a time and approving nothing at all, and a user faced
#: with that picks `full` and stops reading the prompts — which is the failure
#: this whole module is trying to avoid.
AUTONOMY_LADDER: tuple[str, ...] = ('plan', 'review', 'ask', 'auto', 'full')


#: Every way a run can begin. The runtime has exactly one entry point and these
#: are its callers; they differ in configuration, never in code path. A second
#: way to start a run is a second place for the guardrail checks to be
#: forgotten.
CALLERS = frozenset({'chat', 'orchestrator', 'trigger', 'api', 'eval', 'mission'})


#: Callers where no human is present at the moment the run starts. `chat` and
#: `api` are both a person pressing something; `orchestrator` inherits the
#: attendedness of whatever started *it*, and is treated as unattended because
#: the safe answer is the one that asks more often, not less. `mission` runs
#: on a sweep with nobody watching.
UNATTENDED_CALLERS = frozenset({'trigger', 'orchestrator', 'mission'})
