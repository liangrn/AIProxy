import json
import time
from typing import Any

from .adapters import content_to_text, make_message_id, make_response_id, responses_usage


def encode_sse(event: str, data: dict[str, Any] | str) -> str:
    if isinstance(data, str):
        payload = data
    else:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def response_created(response_id: str, model: str, sequence: int) -> str:
    response = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "model": model,
        "output": [],
    }
    return encode_sse("response.created", {"type": "response.created", "response": response, "sequence_number": sequence})


def message_started(message_id: str, sequence_start: int) -> list[str]:
    return [
        encode_sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
                "sequence_number": sequence_start,
            },
        ),
        encode_sse(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": [], "logprobs": []},
                "sequence_number": sequence_start + 1,
            },
        ),
    ]


def text_delta(message_id: str, delta: str, sequence: int) -> str:
    return encode_sse(
        "response.output_text.delta",
        {
            "type": "response.output_text.delta",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "delta": delta,
            "logprobs": [],
            "sequence_number": sequence,
        },
    )


def response_finished(response_id: str, message_id: str, model: str, text: str, usage: Any, sequence_start: int) -> list[str]:
    created = int(time.time())
    message = {
        "id": message_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}],
    }
    response = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "completed_at": created,
        "error": None,
        "model": model,
        "output": [message],
        "usage": responses_usage(usage),
    }
    return [
        encode_sse(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "text": text,
                "logprobs": [],
                "sequence_number": sequence_start,
            },
        ),
        encode_sse(
            "response.content_part.done",
            {
                "type": "response.content_part.done",
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": [], "logprobs": []},
                "sequence_number": sequence_start + 1,
            },
        ),
        encode_sse(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": message,
                "sequence_number": sequence_start + 2,
            },
        ),
        encode_sse(
            "response.completed",
            {
                "type": "response.completed",
                "response": response,
                "sequence_number": sequence_start + 3,
            },
        ),
        "data: [DONE]\n\n",
    ]


async def chat_stream_to_responses_sse(chunks: Any, model: str):
    response_id = make_response_id()
    message_id = make_message_id()
    sequence = 0
    text_parts: list[str] = []
    usage = None

    yield response_created(response_id, model, sequence)
    sequence += 1
    for event in message_started(message_id, sequence):
        yield event
    sequence += 2

    async for chunk in chunks:
        if chunk == "[DONE]":
            continue
        if not isinstance(chunk, dict):
            continue

        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            content = delta.get("content") or delta.get("reasoning_content")
            if content:
                content = content_to_text(content)
                text_parts.append(content)
                yield text_delta(message_id, content, sequence)
                sequence += 1
        if chunk.get("usage"):
            usage = chunk["usage"]

    text = "".join(text_parts)
    for event in response_finished(response_id, message_id, model, text, usage, sequence):
        yield event
