"""
Tools that reach the user's knowledge bases.

Retrieval is a choice the model makes, not a mechanism hidden from it. Each
KB declares a backend — semantic, keyword, raw, hybrid — and these tools let
the agent match strategy to question: `knowledge_base_search` for meaning,
`keyword_search` for exact strings and identifiers, and
`list_documents` → `read_document` for raw KBs where the answer is in the
whole document, not a fragment. Descriptions cross-reference so the model can
route itself; misroutes degrade to advice rather than errors.
"""
from __future__ import annotations

import json
import logging

from typing import Dict, List

from .registry import tool

logger = logging.getLogger(__name__)

#: How much of one document read_document hands back per call. Matches the
#: read_tool_output window on purpose: paging exists so the model can pick
#: what it needs, not reassemble whole documents in context.
READ_WINDOW_CHARS = 12_000


def _kb_backend_label(backend: str) -> str:
    return {
        'vector': 'semantic',
        'fulltext': 'keyword',
        'raw': 'raw',
        'hybrid': 'hybrid (semantic + keyword)',
    }.get(backend, backend or 'semantic')


@tool({
        "type": "function",
        "function": {
            "name": "list_knowledge_bases",
            "description": "List all knowledge bases (KBs) available to the user. Call this first to discover which KBs exist and their IDs before deciding which one to search. Each KB reports its retrieval backend: 'semantic' KBs suit natural-language questions via knowledge_base_search, 'keyword' KBs suit exact terms/IDs/code via keyword_search, 'raw' KBs have no search index at all — browse them with list_documents and read documents with read_document. Hybrid KBs support both searches.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False
            }
        }
    }, parallel=True, effect="read")
async def list_knowledge_bases(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async
    from inference.models import KnowledgeBase
    user_id = context.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context."})
    try:
        scope = kb_scope(context)

        def _list():
            rows = KnowledgeBase.objects.filter(user_id=user_id)
            if scope:
                # Listing every KB the *user* owns would hand a scoped agent the
                # names and ids of corpora it may not search, which it will then
                # try, and report the refusals to whoever reads the run.
                rows = rows.filter(id__in=scope)
            kbs = rows.values(
                'id', 'name', 'description', 'backend', 'doc_count', 'vector_count',
                'index_size_bytes', 'is_default', 'embedding_model',
            )
            return list(kbs)

        kbs = await sync_to_async(_list)()
        for kb in kbs:
            kb['retrieval'] = _kb_backend_label(kb.pop('backend', 'vector'))
            b = kb['index_size_bytes']
            for unit in ('B', 'KB', 'MB', 'GB'):
                if b < 1024:
                    kb['size_human'] = f'{b:.1f} {unit}'
                    break
                b /= 1024
            else:
                kb['size_human'] = f'{b:.1f} TB'
        return json.dumps({"knowledge_bases": kbs, "count": len(kbs)})
    except Exception as e:
        return json.dumps({"error": f"Failed to list KBs: {e}"})


def kb_scope(context: Dict) -> tuple[int, ...] | None:
    """The knowledge bases this caller may reach, or None for "any it owns".

    None is unrestricted and an empty tuple is impossible by construction: the
    agent runtime sends None when its selection is empty, because an agent built
    before the selection was enforced must not have its corpus silently emptied.
    Chat sends nothing at all, which reads the same way.
    """
    scope = context.get("kb_scope")
    if not scope:
        return None
    return tuple(int(kb_id) for kb_id in scope)


async def _resolve_kb(user_id: int, kb_id, context: Dict | None = None):
    """(kb_model, error_json) — exactly one is non-None. Scoped to owner, and to
    the caller's configured knowledge bases when it has any.

    Ownership was the only filter here, so an agent configured for one KB could
    search every other KB its owner had; the builder's selector narrowed the
    prompt and nothing else. Out-of-scope answers the same way as not-found: a
    distinct "exists but is not yours to search" would tell an agent what else
    the user keeps.

    The default-KB branch is where the old behaviour was quietly wrong rather
    than merely wide. Omitting `kb_id` fell through to the user's *default* KB,
    which need not be any of the ones this agent was given — and an answer from
    the wrong corpus looks exactly like an answer from the right one.
    """
    from asgiref.sync import sync_to_async
    from inference.models import KnowledgeBase

    scope = kb_scope(context or {})

    if not kb_id:
        if scope and len(scope) == 1:
            kb_id = scope[0]
        elif scope:
            return None, json.dumps({
                "error": (
                    f"This agent can search {len(scope)} knowledge bases "
                    f"({', '.join(str(i) for i in scope)}). Pass kb_id to say "
                    f"which one — there is no default among them."
                )
            })
        else:
            kb_model = await sync_to_async(
                lambda: KnowledgeBase.objects.filter(
                    user_id=user_id, is_default=True
                ).first()
            )()
            if kb_model is None:
                return None, json.dumps(
                    {"error": "No knowledge base found for this user yet."}
                )
            return kb_model, None

    if scope and int(kb_id) not in scope:
        return None, json.dumps({
            "error": (
                f"KB {kb_id} is not one this agent may search. Available: "
                f"{', '.join(str(i) for i in scope)}."
            )
        })

    kb_model = await sync_to_async(
        lambda: KnowledgeBase.objects.filter(id=kb_id, user_id=user_id).first()
    )()
    if kb_model is None:
        return None, json.dumps({"error": f"KB {kb_id} not found or not owned by user."})
    return kb_model, None


@tool({
        "type": "function",
        "function": {
            "name": "knowledge_base_search",
            "description": "Search a specific knowledge base (or the user's default KB) using SEMANTIC similarity — best for questions about meaning, topics, or concepts in uploaded documents ('what does the report say about margins?'). For exact strings, IDs, names, or code identifiers use keyword_search instead. Raw-backend KBs cannot be searched semantically — use list_documents + read_document there. Call list_knowledge_bases first if unsure which KB to search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language query to search for in the knowledge base."
                    },
                    "kb_id": {
                        "type": "integer",
                        "description": "ID of the specific KB to search (from list_knowledge_bases). Omit to search the user's default KB."
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of top results to return (default 5, max 20)."
                    }
                },
                "required": [
                    "query"
                ],
                "additionalProperties": False
            }
        }
    }, parallel=True, effect="read")
async def knowledge_base_search(args: Dict, context: Dict) -> str:
    query = args.get("query", "")
    if not query:
        return "Error: Missing search query"
    top_k = min(int(args.get("top_k", 5)), 20)
    user_id = context.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context for knowledge base search."})

    try:
        kb_model, error = await _resolve_kb(user_id, args.get("kb_id"), context)
        if error:
            return error

        # A keyword-only KB has no vectors to search. Route rather than fail.
        if kb_model.backend == 'fulltext':
            result = await _keyword_search_impl(kb_model, query, top_k)
            payload = json.loads(result)
            payload["note"] = (
                f"KB '{kb_model.name}' is keyword-indexed; results are exact/prefix "
                f"matches, not semantic ones."
            )
            return json.dumps(payload)

        if kb_model.backend == 'raw':
            return json.dumps({
                "status": "no_search_index",
                "message": (
                    f"KB '{kb_model.name}' stores documents raw with no search index. "
                    f"Call list_documents for kb_id={kb_model.id}, then read_document "
                    f"on what looks relevant."
                ),
            })

        from inference.backends.vector import VectorBackend
        backend = VectorBackend(kb_model)
        results = await backend.search(query, top_k=top_k)
        if not results:
            return json.dumps({"status": "no_results", "message": "No relevant documents found. Try a different query or check that documents are indexed."})

        items = [
            {
                "document_id": r.document_id,
                "score": round(r.score, 4),
                "content": r.content[:2000],
                "metadata": r.metadata,
                "is_image": r.is_image,
            }
            for r in results
        ]
        return json.dumps({"status": "success", "results": items, "count": len(items)})
    except Exception as e:
        return f"Error: Knowledge base search failed: {str(e)}"


@tool({
        "type": "function",
        "function": {
            "name": "keyword_search",
            "description": "Search a knowledge base by EXACT keywords, like grep: case-insensitive whole-word matches plus prefix expansion, ranked by term frequency. Use for IDs, invoice numbers, function/class names, error codes, exact spellings — things semantic search blurs. Supports \"quoted phrases\" for verbatim multi-word matches. Only works on keyword- or hybrid-backend KBs (see list_knowledge_bases); for meaning-based questions use knowledge_base_search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords to find. Multiple words rank chunks matching more of them higher. Wrap words in double quotes for an exact phrase."
                    },
                    "kb_id": {
                        "type": "integer",
                        "description": "ID of the KB to search (from list_knowledge_bases). Omit to search the user's default KB."
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of top results to return (default 5, max 20)."
                    }
                },
                "required": [
                    "query"
                ],
                "additionalProperties": False
            }
        }
    }, parallel=True, effect="read")
async def keyword_search(args: Dict, context: Dict) -> str:
    query = args.get("query", "")
    if not query.strip():
        return "Error: Missing search query"
    top_k = min(int(args.get("top_k", 5)), 20)
    user_id = context.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context for keyword search."})
    try:
        kb_model, error = await _resolve_kb(user_id, args.get("kb_id"), context)
        if error:
            return error

        if kb_model.backend == 'vector':
            return json.dumps({
                "status": "not_keyword_indexed",
                "message": (
                    f"KB '{kb_model.name}' is semantic-only — it has no keyword index. "
                    f"Use knowledge_base_search for it, or ask its content by meaning."
                ),
            })
        if kb_model.backend == 'raw':
            return json.dumps({
                "status": "no_search_index",
                "message": (
                    f"KB '{kb_model.name}' stores documents raw. Call list_documents "
                    f"for kb_id={kb_model.id}, then read_document."
                ),
            })

        return await _keyword_search_impl(kb_model, query, top_k)
    except Exception as e:
        return f"Error: Keyword search failed: {str(e)}"


async def _keyword_search_impl(kb_model, query: str, top_k: int) -> str:
    from inference.backends.fulltext import FullTextBackend
    backend = FullTextBackend(kb_model)
    results = await backend.search(query, top_k=top_k)
    if not results:
        return json.dumps({
            "status": "no_results",
            "message": "No keyword matches. Try fewer or shorter terms — prefix expansion only applies to terms of 3+ characters.",
        })
    items = [
        {
            "document_id": r.document_id,
            "document_name": r.metadata.get("name"),
            "match": r.metadata.get("match"),
            "score": round(r.score, 4),
            "content": r.content[:2000],
        }
        for r in results
    ]
    return json.dumps({"status": "success", "results": items, "count": len(items)})


@tool({
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "List the documents inside one knowledge base: id, name, type, size, status. The entry point for raw KBs (which have no search index) — call this, then read_document on promising titles. Also useful anywhere you need a document's id before reading or citing it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_id": {
                        "type": "integer",
                        "description": "ID of the KB whose documents to list (from list_knowledge_bases). Omit to list the user's default KB."
                    }
                },
                "required": [],
                "additionalProperties": False
            }
        }
    }, parallel=True, effect="read")
async def list_documents(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async
    from inference.models import Document
    user_id = context.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context."})
    try:
        kb_model, error = await _resolve_kb(user_id, args.get("kb_id"), context)
        if error:
            return error

        cap = 50

        def _docs():
            # -id breaks ties: uploads inside one clock tick (coarse on some
            # platforms) must not come back in an arbitrary order.
            docs = Document.objects.filter(knowledge_base=kb_model).order_by('-created_at', '-id')
            rows = list(docs.values('id', 'name', 'file_type', 'file_size', 'status')[:cap])
            return rows, docs.count() > cap

        rows, truncated = await sync_to_async(_docs)()
        return json.dumps({
            "status": "success",
            "kb_id": kb_model.id,
            "documents": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "file_type": r["file_type"],
                    "size_bytes": r["file_size"],
                    "status": r["status"],
                }
                for r in rows
            ],
            "count": len(rows),
            **({"truncated": True, "note": f"Showing the {cap} most recent documents only."} if truncated else {}),
        })
    except Exception as e:
        return f"Error listing documents: {str(e)}"


@tool({
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read a document's full extracted text, one window at a time (~12,000 characters per call). The only way into raw KBs' contents, and the right follow-up after any search hit that looks promising but cut off mid-sentence. Pass the offset from the previous call's footer to keep reading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "integer",
                        "description": "ID of the document to read (from list_documents or a search result)."
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset to start reading from. Omit or 0 to start at the beginning; use the offset named in the previous window's footer to continue."
                    }
                },
                "required": [
                    "document_id"
                ],
                "additionalProperties": False
            }
        }
    }, parallel=True, effect="read")
async def read_document(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async
    from inference.models import Document
    user_id = context.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context."})
    document_id = args.get("document_id")
    if not document_id:
        return "Error: Missing document_id"

    try:
        from django.db.models import Q

        # Owner, or explicitly shared — same visibility rule as the API.
        doc = await sync_to_async(
            lambda: Document.objects.filter(
                Q(id=document_id),
                Q(user_id=user_id) | Q(sharing_mode__in=['shared_read', 'shared_write']),
            ).select_related('knowledge_base').first()
        )()
        if doc is None:
            return json.dumps({
                "error": f"Document {document_id} not found among your readable documents."
            })

        # A document is addressed by id here, not through its KB, so the scope
        # the other four tools enforce would be one `read_document` call away
        # from irrelevant — an agent given one knowledge base could walk the
        # user's whole library by id. A document in no KB at all is out of scope
        # too: files an agent may touch directly are the `fileOps` axis, with
        # its own scope, not this one.
        scope = kb_scope(context)
        if scope and doc.knowledge_base_id not in scope:
            return json.dumps({
                "error": (
                    f"Document {document_id} is not in a knowledge base this "
                    f"agent may read."
                )
            })

        text = doc.content_text or ''
        if not text:
            return json.dumps({
                "error": (
                    f"Document '{doc.name}' has no extracted text (it may be an image "
                    f"or video, or extraction failed)."
                ),
            })

        offset = max(0, int(args.get("offset") or 0))
        if offset >= len(text):
            return (
                f"Offset {offset:,} is past the end of '{doc.name}' "
                f"({len(text):,} characters). Nothing to read."
            )

        window = text[offset:offset + READ_WINDOW_CHARS]
        end = offset + len(window)
        remaining = len(text) - end
        footer = (
            f"\n\n[Document '{doc.name}', characters {offset:,}-{end:,} of {len(text):,}. "
            + (
                f"{remaining:,} remain — call read_document again with offset={end}.]"
                if remaining else "End of document.]"
            )
        )
        return window + footer
    except Exception as e:
        return f"Error reading document: {str(e)}"
