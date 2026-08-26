"""Imagine agent flow: classify -> hitl_gate -> dispatch -> respond.

Implemented as a linear function rather than a LangGraph because the only branch is the
HITL pause, which is naturally persisted on the conversation row and resumed via a separate
HTTP call. Adding StateGraph + checkpointer here would be ceremony with no behavior gain.
"""
import logging
from typing import Any, Dict, Optional

from django.conf import settings
from django.db import transaction

from ..models import Generation, ImagineConversation, ImagineMessage
from ..services.dispatcher import run_generation
from .intent import classify
from .hitl import broadcast_hitl_request, needs_hitl

logger = logging.getLogger(__name__)


def _intent_to_generation_kwargs(intent: Dict[str, Any], user) -> Dict[str, Any]:
    p = intent.get("params") or {}
    return {
        "user": user,
        "type": intent["type"],
        "prompt": intent.get("prompt") or "",
        "model": intent.get("model") or "",
        "negative_prompt": p.get("negative_prompt"),
        "resolution": str(p["resolution"]) if p.get("resolution") else None,
        "aspect_ratio": p.get("aspect_ratio"),
        "duration": str(p["duration"]) if p.get("duration") else None,
        "quality": p.get("quality"),
        "voice": p.get("voice"),
        "speed": float(p["speed"]) if p.get("speed") else None,
        "metadata": {"agent": True, "reasoning": intent.get("reasoning", "")},
    }


def _serialize_generation(g: Optional[Generation]) -> Optional[Dict[str, Any]]:
    if not g:
        return None
    return {
        "id": g.id,
        "type": g.type,
        "prompt": g.prompt,
        "model": g.model,
        "status": g.status,
        "output_url": g.output_url,
        "error_message": g.error_message,
    }


def _conversation_history(conv: ImagineConversation) -> list:
    out = []
    for m in conv.messages.order_by('created_at')[:20]:
        out.append({"role": m.role, "content": m.content[:500]})
    return out


def _dispatch_intent(conv: ImagineConversation, intent: Dict[str, Any]) -> Generation:
    kwargs = _intent_to_generation_kwargs(intent, conv.user)
    if not kwargs["model"]:
        raise ValueError("Cannot dispatch: no model selected")
    generation = Generation.objects.create(**kwargs)
    run_generation(generation)
    return generation


def run_turn(
    conversation: ImagineConversation,
    user_message: str,
    preferred_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Handle one user message. Returns API response payload.

    `preferred_model` carries the picker selection from the chat composer, so
    Agent mode is not limited to whatever the router happens to choose.
    """
    with transaction.atomic():
        ImagineMessage.objects.create(
            conversation=conversation, role='user', content=user_message,
        )

    try:
        intent = classify(
            user_message,
            user=conversation.user,
            # The message just persisted above is the tail of this history;
            # pass it without the current message, or the classifier sees the
            # request twice — once as history, once in the user block.
            history=_conversation_history(conversation)[:-1],
            preferred_model=preferred_model,
        )
    except Exception as e:
        logger.exception("intent classification failed")
        msg = ImagineMessage.objects.create(
            conversation=conversation,
            role='assistant',
            content=f"Generation failed: {e}",
        )
        conversation.status = 'idle'
        conversation.save(update_fields=['status', 'updated_at'])
        return {
            "conversation_id": conversation.id,
            "message_id": msg.id,
            "assistant_message": f"Generation failed: {e}",
            "requires_hitl": False,
            "generation": None,
            "error": str(e),
        }
    threshold = float(getattr(settings, "IMAGINE_HITL_COST_THRESHOLD", 0.10))

    if needs_hitl(intent, threshold):
        conversation.status = 'awaiting_hitl'
        conversation.pending_intent = intent
        conversation.save(update_fields=['status', 'pending_intent', 'updated_at'])
        assistant_text = (
            intent.get("clarifying_question")
            or f"Here's what I understood — confirm to generate ({intent['type']}, model: {intent.get('model')})."
        )
        msg = ImagineMessage.objects.create(
            conversation=conversation,
            role='assistant',
            content=assistant_text,
            intent=intent,
            requires_hitl=True,
        )
        broadcast_hitl_request(conversation, intent)
        return {
            "conversation_id": conversation.id,
            "message_id": msg.id,
            "assistant_message": assistant_text,
            "intent_preview": intent,
            "requires_hitl": True,
            "generation": None,
        }

    return _dispatch_and_record(conversation, intent)


def resume_turn(conversation: ImagineConversation, decision: str, overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Resume after HITL approve/edit/cancel."""
    intent = conversation.pending_intent or {}
    if decision == 'cancel':
        conversation.status = 'idle'
        conversation.pending_intent = None
        conversation.save(update_fields=['status', 'pending_intent', 'updated_at'])
        msg = ImagineMessage.objects.create(
            conversation=conversation, role='assistant', content='Cancelled.',
        )
        return {
            "conversation_id": conversation.id,
            "message_id": msg.id,
            "assistant_message": 'Cancelled.',
            "requires_hitl": False,
            "generation": None,
        }

    if decision == 'edit' and overrides:
        merged_params = {**(intent.get("params") or {}), **(overrides.get("params") or {})}
        for key in ("type", "model", "prompt"):
            if key in overrides and overrides[key]:
                intent[key] = overrides[key]
        intent["params"] = merged_params

    return _dispatch_and_record(conversation, intent)


def _dispatch_and_record(conversation: ImagineConversation, intent: Dict[str, Any]) -> Dict[str, Any]:
    conversation.status = 'generating'
    conversation.pending_intent = None
    conversation.save(update_fields=['status', 'pending_intent', 'updated_at'])

    try:
        generation = _dispatch_intent(conversation, intent)
    except Exception as e:
        logger.exception("dispatch failed")
        msg = ImagineMessage.objects.create(
            conversation=conversation,
            role='assistant',
            content=f"Generation failed: {e}",
            intent=intent,
        )
        conversation.status = 'idle'
        conversation.save(update_fields=['status', 'updated_at'])
        return {
            "conversation_id": conversation.id,
            "message_id": msg.id,
            "assistant_message": f"Generation failed: {e}",
            "requires_hitl": False,
            "generation": None,
            "error": str(e),
        }

    if generation.status == 'pending':
        text = f"Submitted {generation.type} job — I'll update you when it's ready."
    elif generation.status == 'failed':
        text = f"Generation failed: {generation.error_message}"
    else:
        text = f"Done. Here's your {generation.type}."

    msg = ImagineMessage.objects.create(
        conversation=conversation,
        role='assistant',
        content=text,
        intent=intent,
        generation=generation,
    )
    conversation.status = 'idle'
    conversation.save(update_fields=['status', 'updated_at'])

    return {
        "conversation_id": conversation.id,
        "message_id": msg.id,
        "assistant_message": text,
        "intent_preview": intent,
        "requires_hitl": False,
        "generation": _serialize_generation(generation),
    }
