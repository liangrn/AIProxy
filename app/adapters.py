import time
import uuid
from typing import Any


def make_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def make_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def responses_input_to_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    if "messages" in payload and isinstance(payload["messages"], list):
        return payload["messages"]

    value = payload.get("input", "")
    if isinstance(value, str):
        return [{"role": "user", "content": value}]

    if isinstance(value, list):
        messages: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue

            if not isinstance(item, dict):
                continue

            role = item.get("role", "user")
            content = item.get("content", "")
            messages.append({"role": role, "content": content_to_text(content)})
        return messages

    return [{"role": "user", "content": str(value)}]


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)

    return "" if content is None else str(content)


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if content:
        return content_to_text(content)
    reasoning_content = message.get("reasoning_content")
    if reasoning_content:
        return content_to_text(reasoning_content)
    return ""


def anthropic_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text"):
                parts.append(str(part["text"]))
        return "\n".join(parts)
    return "" if content is None else str(content)


def anthropic_messages_to_chat_payload(payload: dict[str, Any], upstream_model: str, stream: bool) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    system = payload.get("system")
    system_text = anthropic_content_to_text(system)
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        messages.append(
            {
                "role": str(message.get("role") or "user"),
                "content": anthropic_content_to_text(message.get("content")),
            }
        )

    chat_payload: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "stream": stream,
    }
    if "max_tokens" in payload:
        chat_payload["max_tokens"] = payload["max_tokens"]
    for key in ("temperature", "top_p", "stop"):
        if key in payload:
            chat_payload[key] = payload[key]
    return chat_payload


def chat_finish_reason_to_claude_stop_reason(reason: Any) -> str | None:
    if reason == "stop":
        return "end_turn"
    if reason == "length":
        return "max_tokens"
    if reason == "tool_calls":
        return "tool_use"
    return None


def chat_completion_to_anthropic_message(chat: dict[str, Any], requested_model: str) -> dict[str, Any]:
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = assistant_message_text(message)
    usage = responses_usage(chat.get("usage"))
    return {
        "id": chat.get("id") or make_message_id(),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": chat_finish_reason_to_claude_stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        },
    }


def responses_to_chat_payload(payload: dict[str, Any], default_model: str, stream: bool) -> dict[str, Any]:
    chat_payload: dict[str, Any] = {
        "model": payload.get("model") or default_model,
        "messages": responses_input_to_messages(payload),
        "stream": stream,
    }

    passthrough_keys = [
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "frequency_penalty",
        "stop",
        "tools",
        "tool_choice",
    ]
    for key in passthrough_keys:
        if key in payload:
            chat_payload[key] = payload[key]

    if "max_output_tokens" in payload and "max_tokens" not in chat_payload:
        chat_payload["max_tokens"] = payload["max_output_tokens"]

    instructions = payload.get("instructions")
    if instructions:
        chat_payload["messages"] = [{"role": "system", "content": str(instructions)}] + chat_payload["messages"]

    if stream:
        stream_options = dict(payload.get("stream_options") or {})
        stream_options.setdefault("include_usage", True)
        chat_payload["stream_options"] = stream_options

    return chat_payload


def responses_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def chat_completion_to_response(chat: dict[str, Any], model: str) -> dict[str, Any]:
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = assistant_message_text(message)
    created = int(time.time())

    return {
        "id": chat.get("id") or make_response_id(),
        "object": "response",
        "created_at": chat.get("created") or created,
        "status": "completed",
        "completed_at": created,
        "error": None,
        "model": chat.get("model") or model,
        "output": [
            {
                "id": make_message_id(),
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        ],
        "usage": responses_usage(chat.get("usage")),
    }
