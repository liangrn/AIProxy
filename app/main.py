import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .adapters import (
    anthropic_messages_to_chat_payload,
    chat_completion_to_anthropic_message,
    chat_completion_to_response,
    responses_to_chat_payload,
)
from .config import (
    activate_profile,
    get_claude_settings,
    get_settings,
    normalize_models,
    normalize_profile,
    public_settings,
    resolve_claude_model,
    save_claude_config,
    save_codex_config,
    save_profile,
    update_active_profile_models,
)
from .sse import chat_stream_to_responses_sse
from scripts.codex_config import install as install_codex_config
from scripts.codex_config import original_backup_path as codex_original_backup_path
from scripts.codex_config import restore as restore_codex_config
from scripts.codex_config import status as codex_config_status


class UpstreamProtocolUnsupported(Exception):
    pass


_RESOLVED_API_STYLE_CACHE: dict[tuple[str, str, str, str], str] = {}


def api_style_cache_key(settings) -> tuple[str, str, str, str]:
    return (
        settings.active_profile_id,
        settings.upstream_base_url,
        settings.upstream_api_key,
        settings.upstream_api_style,
    )


def cached_resolved_api_style(settings) -> str | None:
    return _RESOLVED_API_STYLE_CACHE.get(api_style_cache_key(settings))


def cache_resolved_api_style(settings, resolved_style: str) -> None:
    _RESOLVED_API_STYLE_CACHE[api_style_cache_key(settings)] = resolved_style


def summarize_upstream_error(response: httpx.Response, url: str) -> str:
    content_type = response.headers.get("content-type", "").split(";")[0].strip() or "unknown"
    body_preview = response.text.strip()
    if body_preview:
        body_preview = " ".join(body_preview.split())
        if body_preview.startswith("<!DOCTYPE html") or body_preview.startswith("<html"):
            body_preview = "HTML page returned"
        elif len(body_preview) > 180:
            body_preview = body_preview[:180] + "..."
    else:
        body_preview = "empty body"
    return f"Upstream request failed: GET {url} -> {response.status_code} {response.reason_phrase} ({content_type}). {body_preview}"


def parse_upstream_json_response(response: httpx.Response, url: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=summarize_upstream_error(response, url))
    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream returned non-JSON response: GET {url} -> {response.status_code} {response.reason_phrase} ({response.headers.get('content-type', 'unknown')}).",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail=f"Upstream returned unexpected JSON type for GET {url}")
    return body


async def fetch_profile_models(profile: dict[str, Any]) -> list[str]:
    if not profile["api_key"]:
        raise HTTPException(status_code=400, detail="API key is required to refresh models")
    headers = {
        "Authorization": f"Bearer {profile['api_key']}",
        "Accept": "application/json",
        "User-Agent": profile["user_agent"],
    }
    timeout = httpx.Timeout(float(profile["timeout_seconds"]))
    models_url = f"{profile['base_url']}/models"
    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        response = await client.get(models_url, headers=headers)
    body = parse_upstream_json_response(response, models_url)
    models = normalize_models([item.get("id", "") for item in body.get("data", []) if isinstance(item, dict)])
    if not models:
        raise HTTPException(status_code=502, detail=f"No models found in upstream response for GET {models_url}")
    return models


async def check_profile_contract(profile: dict[str, Any], listen_host: str, listen_port: int) -> dict[str, Any]:
    models = await fetch_profile_models(profile)
    codex_base_url = f"http://{listen_host}:{listen_port}/v1"
    return {
        "ok": True,
        "codex_desktop": {
            "base_url": codex_base_url,
            "models_url": f"{codex_base_url}/models",
        },
        "upstream": {
            "provider": profile["name"],
            "base_url": profile["base_url"],
            "models_url": f"{profile['base_url']}/models",
            "models_ok": True,
            "models_count": len(models),
        },
    }


def is_unsupported_responses_endpoint(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if response.status_code in {404, 405, 415, 501}:
        return True
    if content_type == "text/html":
        return True
    return False


def claude_upstream_headers(request: Request, stream: bool) -> dict[str, str]:
    settings = get_claude_settings()
    headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
        "User-Agent": settings.upstream_user_agent,
        "Accept": "text/event-stream" if stream else request.headers.get("accept", "application/json"),
    }
    return headers


def claude_messages_headers(request: Request, stream: bool) -> dict[str, str]:
    headers = claude_upstream_headers(request, stream=stream)
    for header_name in ("anthropic-version", "anthropic-beta"):
        value = request.headers.get(header_name)
        if value:
            headers[header_name] = value
    return headers


def mapped_claude_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    settings = get_claude_settings()
    claude_model = str(payload.get("model") or "").strip()
    upstream_model = resolve_claude_model(claude_model, settings.model_mappings)
    if not upstream_model:
        raise HTTPException(status_code=400, detail=f"Claude model mapping is not configured for {claude_model or '<empty>'}")
    upstream_payload = dict(payload)
    upstream_payload["model"] = upstream_model
    return upstream_payload, claude_model, upstream_model


async def create_claude_message(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_claude_settings()
    if not settings.upstream_api_key:
        raise HTTPException(status_code=500, detail="Claude upstream API key is not configured")

    upstream_payload, claude_model, upstream_model = mapped_claude_payload(payload)
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        if settings.upstream_api_style in {"anthropic", "auto"}:
            url = f"{settings.upstream_base_url}/messages"
            response = await client.post(url, headers=claude_messages_headers(request, stream=False), json=upstream_payload)
            if response.status_code < 400:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise HTTPException(status_code=502, detail=f"Upstream returned non-JSON response: POST {url}") from exc
                if not isinstance(body, dict):
                    raise HTTPException(status_code=502, detail=f"Upstream returned unexpected JSON type for POST {url}")
                body["model"] = claude_model
                return body
            if settings.upstream_api_style == "anthropic" or not is_unsupported_claude_messages_endpoint(response):
                raise HTTPException(status_code=response.status_code, detail=response.text)

        chat_payload = anthropic_messages_to_chat_payload(payload, upstream_model, stream=False)
        url = f"{settings.upstream_base_url}/chat/completions"
        response = await client.post(url, headers=claude_upstream_headers(request, stream=False), json=chat_payload)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=summarize_upstream_error(response, url))
    body = parse_upstream_json_response(response, url)
    return chat_completion_to_anthropic_message(body, claude_model)


async def stream_claude_message(request: Request, payload: dict[str, Any]):
    settings = get_claude_settings()
    if not settings.upstream_api_key:
        raise HTTPException(status_code=500, detail="Claude upstream API key is not configured")

    upstream_payload, _, upstream_model = mapped_claude_payload(payload)
    if settings.upstream_api_style in {"anthropic", "auto"}:
        url = f"{settings.upstream_base_url}/messages"
        headers = claude_messages_headers(request, stream=True)
        timeout = httpx.Timeout(settings.request_timeout_seconds)
        client = httpx.AsyncClient(timeout=timeout, http2=True)
        stream_context = client.stream("POST", url, headers=headers, json=upstream_payload)
        try:
            response = await stream_context.__aenter__()
        except Exception:
            await client.aclose()
            raise
        if response.status_code < 400:
            async def iterator():
                try:
                    async for chunk in response.aiter_raw():
                        if chunk:
                            yield chunk
                finally:
                    await stream_context.__aexit__(None, None, None)
                    await client.aclose()

            return iterator()

        body = await response.aread()
        await stream_context.__aexit__(None, None, None)
        await client.aclose()
        error_detail = body.decode("utf-8", "replace")
        if settings.upstream_api_style == "anthropic" or not is_unsupported_claude_messages_endpoint_body(response, error_detail):
            raise HTTPException(status_code=response.status_code, detail=error_detail)

    raise HTTPException(status_code=501, detail="Claude Chat Completions streaming is not supported yet")


def is_unsupported_claude_messages_endpoint(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    text = response.text.lower()
    if response.status_code in {404, 405, 415, 501}:
        return True
    if content_type == "text/html":
        return True
    return "/v1/messages dispatch" in text


def is_unsupported_claude_messages_endpoint_body(response: httpx.Response, body_text: str) -> bool:
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if response.status_code in {404, 405, 415, 501}:
        return True
    if content_type == "text/html":
        return True
    return "/v1/messages dispatch" in body_text.lower()


async def create_chat_completion(payload: dict[str, Any], stream: bool) -> dict[str, Any]:
    settings = get_settings()
    if not settings.upstream_api_key:
        raise HTTPException(status_code=500, detail="UPSTREAM_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
        "User-Agent": settings.upstream_user_agent,
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        response = await client.post(f"{settings.upstream_base_url}/chat/completions", headers=headers, json=payload)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=summarize_upstream_error(response, f"{settings.upstream_base_url}/chat/completions"))
    return parse_upstream_json_response(response, f"{settings.upstream_base_url}/chat/completions")


async def request_upstream_response(payload: dict[str, Any], stream: bool):
    settings = get_settings()
    if not settings.upstream_api_key:
        raise HTTPException(status_code=500, detail="UPSTREAM_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
        "User-Agent": settings.upstream_user_agent,
        "Accept": "text/event-stream" if stream else "application/json",
    }
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    url = f"{settings.upstream_base_url}/responses"
    client = httpx.AsyncClient(timeout=timeout, http2=True)
    if not stream:
        try:
            response = await client.post(url, headers=headers, json=payload)
        finally:
            await client.aclose()
        if response.status_code >= 400:
            if is_unsupported_responses_endpoint(response):
                raise UpstreamProtocolUnsupported(summarize_upstream_error(response, url))
            raise HTTPException(status_code=response.status_code, detail=summarize_upstream_error(response, url))
        try:
            return parse_upstream_json_response(response, url)
        except HTTPException as exc:
            if is_unsupported_responses_endpoint(response):
                raise UpstreamProtocolUnsupported(exc.detail) from exc
            raise

    stream_context = client.stream("POST", url, headers=headers, json=payload)
    try:
        response = await stream_context.__aenter__()
    except Exception:
        await client.aclose()
        raise
    if response.status_code >= 400:
        body = await response.aread()
        await stream_context.__aexit__(None, None, None)
        await client.aclose()
        if is_unsupported_responses_endpoint(response):
            raise UpstreamProtocolUnsupported(body.decode("utf-8", "replace"))
        raise HTTPException(status_code=response.status_code, detail=body.decode("utf-8", "replace"))
    return client, stream_context, response


async def create_upstream_response(payload: dict[str, Any]) -> dict[str, Any]:
    body = await request_upstream_response(payload, stream=False)
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Upstream responses endpoint returned unexpected payload")
    return body


async def stream_upstream_response(payload: dict[str, Any]):
    client, stream_context, response = await request_upstream_response(payload, stream=True)
    try:
        async for chunk in response.aiter_raw():
            if chunk:
                yield chunk
    finally:
        await stream_context.__aexit__(None, None, None)
        await client.aclose()


def configured_or_cached_api_style(settings) -> str:
    if settings.upstream_api_style != "auto":
        return settings.upstream_api_style
    return cached_resolved_api_style(settings) or "auto"


async def check_codex_upstream_protocol() -> dict[str, Any]:
    settings = get_settings()
    codex_base_url = f"http://{settings.listen_host}:{settings.listen_port}/v1"
    result: dict[str, Any] = {
        "ok": False,
        "codex_desktop": {
            "base_url": codex_base_url,
            "responses_url": f"{codex_base_url}/responses",
        },
        "upstream": {
            "provider": settings.upstream_provider_name,
            "base_url": settings.upstream_base_url,
            "configured_api_style": settings.upstream_api_style,
            "resolved_api_style": None,
            "protocol_url": None,
            "model": settings.upstream_model,
        },
    }

    responses_payload = {"model": settings.upstream_model, "input": "ping", "max_output_tokens": 1, "stream": False}
    if settings.upstream_api_style in {"responses", "auto"}:
        try:
            await create_upstream_response(responses_payload)
            cache_resolved_api_style(settings, "responses")
            result["ok"] = True
            result["upstream"]["resolved_api_style"] = "responses"
            result["upstream"]["protocol_url"] = f"{settings.upstream_base_url}/responses"
            return result
        except UpstreamProtocolUnsupported as exc:
            if settings.upstream_api_style == "responses":
                result["error"] = str(exc) or "Configured upstream responses endpoint is not supported"
                return JSONResponse(result, status_code=502)
        except HTTPException as exc:
            result["error"] = exc.detail
            return JSONResponse(result, status_code=exc.status_code)

    chat_payload = responses_to_chat_payload(responses_payload, settings.upstream_model, stream=False)
    try:
        await create_chat_completion(chat_payload, stream=False)
    except HTTPException as exc:
        result["error"] = exc.detail
        return JSONResponse(result, status_code=exc.status_code)
    cache_resolved_api_style(settings, "chat")
    result["ok"] = True
    result["upstream"]["resolved_api_style"] = "chat"
    result["upstream"]["protocol_url"] = f"{settings.upstream_base_url}/chat/completions"
    return result


async def stream_chat_completion(payload: dict[str, Any]):
    settings = get_settings()
    if not settings.upstream_api_key:
        raise HTTPException(status_code=500, detail="UPSTREAM_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
        "User-Agent": settings.upstream_user_agent,
        "Accept": "text/event-stream",
    }
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        async with client.stream("POST", f"{settings.upstream_base_url}/chat/completions", headers=headers, json=payload) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise HTTPException(status_code=response.status_code, detail=body.decode("utf-8", "replace"))

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if not data:
                    continue
                if data == "[DONE]":
                    yield "[DONE]"
                    continue
                yield json.loads(data)


def create_app() -> FastAPI:
    app = FastAPI(title="AIProxy", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    async def admin_page():
        return HTMLResponse(ADMIN_HTML)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/admin/config")
    async def admin_config():
        settings = get_settings()
        return {"config": public_settings(settings), "resolved_api_style": cached_resolved_api_style(settings)}

    @app.post("/admin/codex/config")
    async def update_admin_codex_config(request: Request):
        data = await request.json()
        try:
            save_codex_config(data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Profile not found: {exc.args[0]}") from exc
        return {"ok": True, "config": public_settings()}

    @app.get("/admin/claude/config")
    async def admin_claude_config():
        return {"config": public_settings()["claude"]}

    @app.post("/admin/claude/config")
    async def update_admin_claude_config(request: Request):
        data = await request.json()
        try:
            claude_config = save_claude_config(data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Profile not found: {exc.args[0]}") from exc
        return {"ok": True, "config": claude_config, "public_config": public_settings()}

    @app.post("/admin/profiles/{profile_id}")
    async def update_profile(profile_id: str, request: Request):
        data = await request.json()
        namespace = str(request.query_params.get("namespace") or "codex")
        profile = save_profile(profile_id, data, namespace=namespace)
        return {"ok": True, "profile_id": profile_id, "profile": profile, "config": public_settings()}

    @app.post("/admin/profiles/draft/models/refresh")
    async def refresh_draft_models(request: Request):
        data = await request.json()
        profile = normalize_profile(data)
        models = await fetch_profile_models(profile)
        return {"ok": True, "models": models}

    @app.post("/admin/profiles/draft/protocol/check")
    async def draft_protocol_check(request: Request):
        data = await request.json()
        profile = normalize_profile(data)
        settings = get_settings()
        try:
            return await check_profile_contract(profile, settings.listen_host, settings.listen_port)
        except HTTPException as exc:
            return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)

    @app.post("/admin/profiles/{profile_id}/activate")
    async def set_active_profile(profile_id: str):
        profile = activate_profile(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Profile not found: {profile_id}")
        return {"ok": True, "profile_id": profile_id, "profile": profile, "config": public_settings()}

    @app.post("/admin/claude/protocol/check")
    async def claude_protocol_check(request: Request):
        settings = get_claude_settings()
        model = str((await request.json()).get("model") or settings.model_mappings[0]["claude_model"])
        payload = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
        endpoint = f"{settings.upstream_base_url}/messages"
        if settings.upstream_api_style == "chat":
            endpoint = f"{settings.upstream_base_url}/chat/completions"
        result: dict[str, Any] = {
            "ok": False,
            "claude_desktop": {
                "base_url": f"http://{settings.listen_host}:{settings.listen_port}/anthropic",
                "messages_url": f"http://{settings.listen_host}:{settings.listen_port}/anthropic/v1/messages",
                "models_url": f"http://{settings.listen_host}:{settings.listen_port}/anthropic/v1/models",
            },
            "upstream": {
                "provider": settings.upstream_provider_name,
                "base_url": settings.upstream_base_url,
                "messages_url": endpoint,
                "model": resolve_claude_model(model, settings.model_mappings),
            },
        }
        try:
            body = await create_claude_message(request, payload)
        except HTTPException as exc:
            result["error"] = exc.detail
            return JSONResponse(result, status_code=exc.status_code)
        result["ok"] = True
        result["response_model"] = body.get("model")
        return result

    @app.post("/admin/codex/protocol/check")
    async def codex_protocol_check():
        return await check_codex_upstream_protocol()

    @app.post("/admin/models/refresh")
    async def refresh_models():
        settings = get_settings()
        profile = {
            "name": settings.upstream_provider_name,
            "base_url": settings.upstream_base_url,
            "api_key": settings.upstream_api_key,
            "default_model": settings.upstream_model,
            "models": settings.upstream_models,
            "user_agent": settings.upstream_user_agent,
            "timeout_seconds": settings.request_timeout_seconds,
        }
        models = await fetch_profile_models(profile)
        profile = update_active_profile_models(models)
        return {"ok": True, "profile": profile}

    @app.post("/admin/protocol/check")
    async def protocol_check():
        settings = get_settings()
        codex_base_url = f"http://{settings.listen_host}:{settings.listen_port}/v1"
        result: dict[str, Any] = {
            "ok": False,
            "codex_desktop": {
                "base_url": codex_base_url,
                "models_url": f"{codex_base_url}/models",
            },
            "upstream": {
                "provider": settings.upstream_provider_name,
                "base_url": settings.upstream_base_url,
                "models_url": f"{settings.upstream_base_url}/models",
                "models_ok": False,
                "models_count": 0,
            },
        }
        if not settings.upstream_api_key:
            result["error"] = "API key is required"
            return JSONResponse(result, status_code=400)

        headers = {
            "Authorization": f"Bearer {settings.upstream_api_key}",
            "Accept": "application/json",
            "User-Agent": settings.upstream_user_agent,
        }
        timeout = httpx.Timeout(settings.request_timeout_seconds)
        models_url = f"{settings.upstream_base_url}/models"
        try:
            async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
                response = await client.get(models_url, headers=headers)
        except httpx.HTTPError as exc:
            result["error"] = str(exc)
            return JSONResponse(result, status_code=502)

        try:
            body = parse_upstream_json_response(response, models_url)
        except HTTPException as exc:
            result["error"] = exc.detail
            return JSONResponse(result, status_code=502)

        models = normalize_models([item.get("id", "") for item in body.get("data", []) if isinstance(item, dict)])
        result["upstream"]["models_ok"] = bool(models)
        result["upstream"]["models_count"] = len(models)
        result["ok"] = bool(models)
        if not models:
            result["error"] = f"No models found in upstream response for GET {models_url}"
            return JSONResponse(result, status_code=502)
        return result

    @app.get("/admin/codex/status")
    async def admin_codex_status():
        config_path = codex_config_path()
        original_backup = codex_original_backup_path(config_path)
        return {
            "status": codex_config_status(config_path),
            "config_path": str(config_path),
            "backup_available": original_backup.exists(),
            "latest_backup": str(original_backup) if original_backup.exists() else None,
        }

    @app.post("/admin/codex/install")
    async def admin_codex_install(request: Request):
        data = await request.json()
        settings = get_settings()
        model = settings.upstream_model
        proxy_url = str(data.get("proxy_url") or f"http://{settings.listen_host}:{settings.listen_port}")
        mode = str(data.get("mode") or "auth-proxy")
        try:
            backup_path = install_codex_config(codex_config_path(), model=model, proxy_url=proxy_url, mode=mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "status": "installed", "backup": str(backup_path), "restart_required": True}

    @app.post("/admin/codex/restore")
    async def admin_codex_restore():
        try:
            restored_from = restore_codex_config(codex_config_path())
        except SystemExit as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "status": "restored", "restored_from": str(restored_from), "restart_required": True}

    @app.post("/v1/responses")
    async def responses(request: Request):
        payload = await request.json()
        settings = get_settings()
        stream = bool(payload.get("stream", False))
        resolved_api_style = configured_or_cached_api_style(settings)

        if resolved_api_style in {"responses", "auto"}:
            try:
                if stream:
                    cache_resolved_api_style(settings, "responses")
                    return StreamingResponse(
                        stream_upstream_response(payload),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )

                response_body = await create_upstream_response(payload)
                cache_resolved_api_style(settings, "responses")
                return JSONResponse(response_body)
            except UpstreamProtocolUnsupported:
                if settings.upstream_api_style == "responses":
                    raise HTTPException(status_code=502, detail="Configured upstream responses endpoint is not supported")
                cache_resolved_api_style(settings, "chat")

        chat_payload = responses_to_chat_payload(payload, settings.upstream_model, stream=stream)
        model = chat_payload["model"]
        if settings.upstream_api_style == "auto":
            cache_resolved_api_style(settings, "chat")

        if stream:
            chunks = stream_chat_completion(chat_payload)
            return StreamingResponse(
                chat_stream_to_responses_sse(chunks, model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        chat = await create_chat_completion(chat_payload, stream=False)
        return JSONResponse(chat_completion_to_response(chat, model))

    async def claude_messages_handler(request: Request):
        payload = await request.json()
        stream = bool(payload.get("stream", False))
        if stream:
            return StreamingResponse(
                await stream_claude_message(request, payload),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return JSONResponse(await create_claude_message(request, payload))

    app.add_api_route("/anthropic/v1/messages", claude_messages_handler, methods=["POST"])
    app.add_api_route("/v1/messages", claude_messages_handler, methods=["POST"])

    @app.get("/anthropic/v1/models")
    async def claude_models():
        settings = get_claude_settings()
        return {
            "data": [
                {"id": mapping["claude_model"], "type": "model", "display_name": mapping["claude_model"]}
                for mapping in settings.model_mappings
            ]
        }

    @app.get("/v1/models")
    async def models():
        settings = get_settings()
        return {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "owned_by": settings.upstream_provider_name}
                for model in settings.upstream_models
            ],
        }

    return app


app = create_app()


def codex_config_path() -> Path:
    return Path(os.getenv("CODEX_CONFIG_PATH", str(Path.home() / ".codex" / "config.toml"))).expanduser()


ADMIN_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Proxy管理</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #202124; background: #f7f8fa; }
    body { margin: 0; }
    main { max-width: 960px; margin: 0 auto; padding: 28px 20px 48px; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    h2 { margin: 0 0 16px; font-size: 18px; }
    p { line-height: 1.55; color: #4b5563; }
    section { background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 20px; margin-top: 18px; }
    label { display: block; font-size: 13px; font-weight: 600; margin: 14px 0 6px; }
    input, textarea, select { width: 100%; height: 42px; box-sizing: border-box; border: 1px solid #c7ced9; border-radius: 6px; padding: 10px 12px; font: inherit; line-height: 20px; background: #fff; }
    textarea { min-height: 84px; height: auto; resize: vertical; }
    button { height: 42px; border: 0; border-radius: 6px; padding: 10px 14px; font: inherit; line-height: 20px; font-weight: 650; cursor: pointer; background: #1f6feb; color: #fff; }
    .inline-control { height: 42px; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: stretch; }
    .inline-control select, .inline-control button { height: 42px; }
    .inline-control button { margin: 0; white-space: nowrap; }
    button.secondary { background: #44546a; }
    button.danger { background: #b42318; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .status-row { margin-top: 12px; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
    .status { font-size: 14px; color: #255e2e; }
    .status:not(:empty) { margin-top: 12px; }
    .status:empty { display: none; }
    .muted { color: #697386; font-size: 13px; }
    .tabs { display: flex; gap: 8px; margin-top: 18px; border-bottom: 1px solid #d9dee7; }
    .tab-button { background: transparent; color: #44546a; border-radius: 6px 6px 0 0; }
    .tab-button.active { background: #1f6feb; color: #fff; }
    .panel-hidden { display: none; }
    .mapping-row { display: grid; grid-template-columns: 1fr 1fr auto; gap: 10px; align-items: end; margin-top: 10px; }
    .title-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
    dialog { width: min(920px, calc(100vw - 32px)); border: 1px solid #d9dee7; border-radius: 8px; padding: 0; box-shadow: 0 24px 80px rgba(15, 23, 42, .28); }
    dialog::backdrop { background: rgba(15, 23, 42, .32); }
    .dialog-body { padding: 20px; }
    .dialog-title { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 10px; }
    details { margin-top: 12px; }
    summary { cursor: pointer; color: #44546a; font-weight: 650; }
    .combo { position: relative; }
    .model-menu { position: absolute; z-index: 5; left: 0; right: 0; top: calc(100% + 4px); max-height: 220px; overflow: auto; background: #fff; border: 1px solid #c7ced9; border-radius: 6px; box-shadow: 0 12px 28px rgba(15, 23, 42, .16); display: none; }
    .model-menu.open { display: block; }
    .model-option { width: 100%; display: block; text-align: left; background: transparent; color: #202124; border-radius: 0; padding: 9px 12px; font-weight: 500; }
    .model-option:hover, .model-option.active { background: #eef4ff; }
    .model-empty { padding: 10px 12px; color: #697386; font-size: 13px; }
    code { background: #eef1f5; padding: 2px 5px; border-radius: 4px; }
    @media (max-width: 720px) { .row { grid-template-columns: 1fr; } main { padding: 20px 12px 36px; } }
  </style>
</head>
<body>
  <main>
    <div class="title-row">
      <div>
        <h1>AI Proxy管理</h1>
        <p>在这里配置本地 CodexProxy 和 ClaudeProxy。两者共享中转平台列表，但当前生效平台互相独立。</p>
      </div>
      <div class="actions" style="margin-top:0;">
        <button id="addProfile" type="button">添加中转平台</button>
        <button id="editProfile" class="secondary" type="button">修改当前平台</button>
      </div>
    </div>

    <div class="tabs">
      <button id="codexTabButton" class="tab-button active" type="button">CodexProxy</button>
      <button id="claudeTabButton" class="tab-button" type="button">ClaudeProxy</button>
    </div>

    <dialog id="profileDialog">
      <div class="dialog-body">
        <div class="dialog-title">
          <h2 id="profileDialogTitle">中转平台配置</h2>
          <button id="closeProfileDialog" class="secondary" type="button">关闭</button>
        </div>
      <div class="row">
        <div><label for="provider">平台名称</label><input id="provider"></div>
        <div><label for="baseUrl">平台地址 <span class="muted">程序会自动补 `/v1`</span></label><input id="baseUrl" placeholder="https://www.uocode.com"></div>
      </div>
      <label for="apiKey">API Key</label>
      <input id="apiKey" placeholder="sk-...">
      <div class="row">
        <div>
          <label for="defaultModel">默认模型</label>
          <div class="combo">
            <input id="defaultModel" autocomplete="off" placeholder="输入关键字过滤模型">
            <div id="modelMenu" class="model-menu"></div>
          </div>
        </div>
        <div><label>&nbsp;</label><button id="refreshModels" class="secondary">刷新模型</button></div>
      </div>
      <details>
        <summary>高级设置</summary>
        <div class="row">
          <div><label for="userAgent">User-Agent</label><input id="userAgent" placeholder="curl/8.7.1"></div>
          <div><label for="timeout">请求超时秒数</label><input id="timeout" type="number" min="1" step="1" placeholder="300"></div>
        </div>
      </details>
      <div class="actions">
        <button id="saveConfig">保存并生效</button>
        <button id="checkProtocol" class="secondary">验证配置</button>
      </div>
      <div id="configStatus" class="status"></div>
      </div>
    </dialog>

    <section class="codex-panel">
      <div class="row">
        <div>
          <label for="profileSelect">选择中转平台</label>
          <select id="profileSelect"></select>
        </div>
        <div>
          <label for="codexApiStyle">上游协议</label>
          <div class="inline-control">
            <select id="codexApiStyle">
              <option value="auto">Auto</option>
              <option value="chat">v1/chat/completions</option>
              <option value="responses">v1/responses</option>
            </select>
            <button id="checkCodexProtocol" class="secondary" type="button">验证</button>
          </div>
        </div>
      </div>
      <p class="muted">选择后自动生效。</p>
      <div class="status-row">
        <div id="codexProtocolStatus" class="status"></div>
      </div>
    </section>

    <section class="codex-panel">
      <h2>Codex Desktop 配置</h2>
      <p class="muted">当前状态：<span id="codexStatus">读取中</span>；配置文件：<code id="codexPath"></code></p>
      <p class="muted">Codex Desktop 模型会自动使用当前配置的默认模型：<code id="codexModelText"></code></p>
      <label for="codexMode">安装模式</label>
      <select id="codexMode">
        <option value="auth-proxy" selected>保留官方登录态代理</option>
        <option value="proxy">普通第三方代理</option>
      </select>
      <p class="muted">这个模式使用自定义 <code>codex_proxy</code> provider，并要求 Codex 携带官方登录态。它可保留登录态，不保证官方云端历史视图。</p>
      <label for="proxyUrl">代理地址</label><input id="proxyUrl">
      <div class="actions">
        <button id="installCodex">修改Codex配置</button>
        <button id="restoreCodex" class="danger">恢复默认配置</button>
      </div>
      <div id="codexMessage" class="status"></div>
    </section>

    <section class="claude-panel panel-hidden">
      <div class="row">
        <div>
          <label for="claudeProfileSelect">选择中转平台</label>
          <select id="claudeProfileSelect"></select>
        </div>
        <div>
          <label for="claudeApiStyle">上游协议</label>
          <div class="inline-control">
            <select id="claudeApiStyle">
              <option value="anthropic">v1/messages</option>
              <option value="chat">v1/chat/completions</option>
              <option value="auto">Auto</option>
            </select>
            <button id="checkClaudeProtocol" class="secondary" type="button">验证</button>
          </div>
        </div>
      </div>
      <div class="status-row">
        <div id="claudeProtocolStatus" class="status"></div>
      </div>
      <p class="muted">选择后自动生效。</p>
      <p class="muted">Claude Desktop Developer Mode 的 Gateway URL填：<code id="claudeGatewayUrl"></code>。API Key可填任意非空值。</p>
      <label>模型映射</label>
      <div id="mappingRows"></div>
      <div class="actions">
        <button id="addMapping" class="secondary" type="button">添加映射</button>
        <button id="saveClaudeConfig" type="button">保存映射</button>
      </div>
      <div class="row">
        <div>
          <label for="claudeTestModel">验证模型名</label>
          <div class="inline-control">
            <input id="claudeTestModel" placeholder="claude-opus-4.6">
            <button id="checkClaudeModel" class="secondary" type="button">验证模型</button>
          </div>
        </div>
      </div>
      <div class="status-row">
        <div id="claudeModelStatus" class="status"></div>
      </div>
      <div id="claudeStatus" class="status"></div>
    </section>

    <section>
      <h2>说明</h2>
      <p>默认代理地址是 <code>http://127.0.0.1:8383</code>。请只在本机访问这个管理页，不要把管理端口开放到公网或局域网。API key 保存在本地 <code>config.local.json</code>，不要提交到代码仓库。</p>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let currentConfig = null;
    let currentModels = [];
    let currentMappings = [];
    let activeTab = 'codex';
    let editingProfileId = null;

    function showTab(tab) {
      activeTab = tab;
      const isClaude = tab === 'claude';
      document.querySelectorAll('.codex-panel').forEach((el) => el.classList.toggle('panel-hidden', isClaude));
      document.querySelectorAll('.claude-panel').forEach((el) => el.classList.toggle('panel-hidden', !isClaude));
      $('codexTabButton').classList.toggle('active', !isClaude);
      $('claudeTabButton').classList.toggle('active', isClaude);
    }

    function setButtonLoading(button, isLoading, loadingText, normalText) {
      button.disabled = isLoading;
      button.textContent = isLoading ? loadingText : normalText;
    }

    function activeProfileIdForTab() {
      return activeTab === 'claude' ? currentConfig.claude.active_profile : currentConfig.codex.active_profile;
    }

    function profilePayloadFromDialog() {
      return {
        name: $('provider').value,
        base_url: $('baseUrl').value,
        api_key: $('apiKey').value,
        default_model: $('defaultModel').value,
        models: currentModels,
        user_agent: $('userAgent').value || 'curl/8.7.1',
        timeout_seconds: Number($('timeout').value || 300)
      };
    }

    function openProfileDialog(profileId) {
      editingProfileId = profileId;
      if (profileId) {
        fillProfile(profileId);
        $('profileDialogTitle').textContent = '修改中转平台';
      } else {
        $('provider').value = '';
        $('baseUrl').value = '';
        $('apiKey').value = '';
        $('defaultModel').value = '';
        currentModels = [];
        renderModelMenu();
        $('timeout').value = 300;
        $('userAgent').value = 'curl/8.7.1';
        $('profileDialogTitle').textContent = '添加中转平台';
        $('configStatus').textContent = '填写后保存即可创建新配置。';
      }
      $('profileDialog').showModal();
    }

    function profileIdFromName(name) {
      return (name || 'profile').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'profile';
    }

    function fillProfile(profileId) {
      const profile = currentConfig.profiles[profileId];
      $('provider').value = profile.name;
      $('baseUrl').value = profile.base_url;
      $('apiKey').value = profile.api_key || '';
      $('defaultModel').value = profile.default_model;
      currentModels = profile.models || [];
      renderModelMenu();
      $('timeout').value = profile.timeout_seconds || 300;
      $('userAgent').value = profile.user_agent || 'curl/8.7.1';
      $('codexModelText').textContent = profile.default_model;
    }

    function renderClaudeProfiles() {
      $('claudeProfileSelect').innerHTML = '';
      for (const [profileId, profile] of Object.entries(currentConfig.profiles)) {
        const option = document.createElement('option');
        option.value = profileId;
        option.textContent = profileId === currentConfig.claude.active_profile ? `${profile.name}（当前）` : profile.name;
        $('claudeProfileSelect').appendChild(option);
      }
      $('claudeProfileSelect').value = currentConfig.claude.active_profile;
      $('claudeApiStyle').value = currentConfig.claude.api_style || 'auto';
      $('claudeGatewayUrl').textContent = currentConfig.claude.base_url;
      currentMappings = (currentConfig.claude.model_mappings || []).map((mapping) => ({ ...mapping }));
      if (currentMappings.length && !$('claudeTestModel').value) {
        $('claudeTestModel').value = currentMappings[0].claude_model;
      }
      renderMappings();
    }

    function renderMappings() {
      $('mappingRows').innerHTML = '';
      currentMappings.forEach((mapping, index) => {
        const row = document.createElement('div');
        row.className = 'mapping-row';
        row.innerHTML = `
          <div><input data-mapping-index="${index}" data-mapping-field="claude_model" placeholder="claude-opus-4.6"></div>
          <div><input data-mapping-index="${index}" data-mapping-field="upstream_model" placeholder="glm-5.1"></div>
          <button class="danger" type="button" data-remove-mapping="${index}">删除</button>
        `;
        $('mappingRows').appendChild(row);
        row.querySelector('[data-mapping-field="claude_model"]').value = mapping.claude_model || '';
        row.querySelector('[data-mapping-field="upstream_model"]').value = mapping.upstream_model || '';
      });
      document.querySelectorAll('[data-mapping-field]').forEach((input) => {
        input.oninput = () => {
          const index = Number(input.dataset.mappingIndex);
          currentMappings[index][input.dataset.mappingField] = input.value;
        };
      });
      document.querySelectorAll('[data-remove-mapping]').forEach((button) => {
        button.onclick = () => {
          currentMappings.splice(Number(button.dataset.removeMapping), 1);
          renderMappings();
        };
      });
    }

    function renderModelMenu() {
      const query = $('defaultModel').value.trim().toLowerCase();
      const matches = currentModels.filter((model) => model.toLowerCase().includes(query));
      $('modelMenu').innerHTML = '';
      if (!matches.length) {
        const empty = document.createElement('div');
        empty.className = 'model-empty';
        empty.textContent = currentModels.length ? '没有匹配的模型' : '先刷新模型';
        $('modelMenu').appendChild(empty);
        return;
      }
      for (const model of matches) {
        const option = document.createElement('button');
        option.type = 'button';
        option.className = 'model-option' + (model === $('defaultModel').value ? ' active' : '');
        option.textContent = model;
        option.onclick = () => {
          $('defaultModel').value = model;
          $('modelMenu').classList.remove('open');
          renderModelMenu();
        };
        $('modelMenu').appendChild(option);
      }
    }

    function showAllModels() {
      $('defaultModel').value = '';
      renderModelMenu();
      $('modelMenu').classList.add('open');
    }

    function setRefreshLoading(isLoading) {
      $('refreshModels').disabled = isLoading;
      $('refreshModels').textContent = isLoading ? '刷新中...' : '刷新模型';
      if (isLoading) {
        $('configStatus').textContent = '正在刷新模型，请稍候。';
      }
    }

    $('defaultModel').addEventListener('focus', () => {
      renderModelMenu();
      $('modelMenu').classList.add('open');
    });

    $('defaultModel').addEventListener('input', () => {
      renderModelMenu();
      $('modelMenu').classList.add('open');
    });

    document.addEventListener('click', (event) => {
      if (!$('modelMenu').contains(event.target) && event.target !== $('defaultModel')) {
        $('modelMenu').classList.remove('open');
      }
    });

    async function refresh() {
      const configRes = await fetch('/admin/config');
      const { config } = await configRes.json();
      currentConfig = config;
      $('profileSelect').innerHTML = '';
      for (const [profileId, profile] of Object.entries(config.profiles)) {
        const option = document.createElement('option');
        option.value = profileId;
        option.textContent = profileId === config.codex.active_profile ? `${profile.name}（当前）` : profile.name;
        $('profileSelect').appendChild(option);
      }
      $('profileSelect').value = config.codex.active_profile;
      $('codexApiStyle').value = config.codex.api_style || 'auto';
      fillProfile(config.codex.active_profile);
      renderClaudeProfiles();
      $('proxyUrl').value = `http://${config.listen_host}:${config.listen_port}`;

      const codexRes = await fetch('/admin/codex/status');
      const codex = await codexRes.json();
      $('codexStatus').textContent = codex.status + (codex.backup_available ? '，有备份可恢复' : '，暂无备份');
      $('codexPath').textContent = codex.config_path;
      $('restoreCodex').disabled = !codex.backup_available;
    }

    async function saveCodexConfig(statusText) {
      $('codexProtocolStatus').textContent = statusText;
      const payload = {
        active_profile: $('profileSelect').value,
        api_style: $('codexApiStyle').value
      };
      const res = await fetch('/admin/codex/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      const body = await res.json();
      $('codexProtocolStatus').textContent = res.ok ? 'Codex 当前配置已生效。' : `切换失败：${body.detail}`;
      await refresh();
    }

    async function saveClaudeSelection(statusText) {
      $('claudeStatus').textContent = statusText;
      const payload = {
        active_profile: $('claudeProfileSelect').value,
        api_style: $('claudeApiStyle').value,
        model_mappings: currentMappings
      };
      const res = await fetch('/admin/claude/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      const body = await res.json();
      $('claudeStatus').textContent = res.ok ? 'ClaudeProxy 当前配置已生效。' : `切换失败：${body.detail}`;
      await refresh();
    }

    $('profileSelect').onchange = async () => {
      await saveCodexConfig('正在切换 Codex 当前配置，请稍候。');
    };

    $('codexApiStyle').onchange = async () => {
      await saveCodexConfig('正在切换 Codex 上游协议，请稍候。');
    };

    $('claudeProfileSelect').onchange = async () => {
      await saveClaudeSelection('正在切换 ClaudeProxy 当前配置，请稍候。');
    };

    $('claudeApiStyle').onchange = async () => {
      await saveClaudeSelection('正在切换 ClaudeProxy 上游协议，请稍候。');
    };

    $('codexTabButton').onclick = () => showTab('codex');
    $('claudeTabButton').onclick = () => showTab('claude');
    $('addProfile').onclick = () => openProfileDialog(null);
    $('editProfile').onclick = () => openProfileDialog(activeProfileIdForTab());
    $('closeProfileDialog').onclick = () => $('profileDialog').close();

    $('saveConfig').onclick = async () => {
      const button = $('saveConfig');
      setButtonLoading(button, true, '保存中...', '保存并生效');
      $('configStatus').textContent = '正在保存配置，请稍候。';
      try {
        const profileId = editingProfileId || profileIdFromName($('provider').value);
        const res = await fetch(`/admin/profiles/${encodeURIComponent(profileId)}?namespace=${encodeURIComponent(activeTab)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(profilePayloadFromDialog()) });
        const body = await res.json();
        $('configStatus').textContent = res.ok ? '保存成功，当前页面配置已生效。' : `保存失败：${body.detail || '请求失败'}`;
        if (res.ok) {
          $('apiKey').value = '';
          await refresh();
          $('profileDialog').close();
        }
      } catch (error) {
        $('configStatus').textContent = `保存失败：${error.message}`;
      } finally {
        setButtonLoading(button, false, '保存中...', '保存并生效');
      }
    };

    $('refreshModels').onclick = async () => {
      setRefreshLoading(true);
      try {
        const res = await fetch('/admin/profiles/draft/models/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(profilePayloadFromDialog()) });
        const body = await res.json();
        $('configStatus').textContent = res.ok ? '模型列表已刷新。' : `刷新失败：${body.detail}`;
        if (res.ok) {
          currentModels = body.models || [];
          showAllModels();
          return;
        }
      } catch (error) {
        $('configStatus').textContent = `刷新失败：${error.message}`;
      } finally {
        setRefreshLoading(false);
      }
    };

    $('checkProtocol').onclick = async () => {
      const button = $('checkProtocol');
      setButtonLoading(button, true, '验证中...', '验证配置');
      $('configStatus').textContent = '正在验证配置，请稍候。';
      try {
        const res = await fetch('/admin/profiles/draft/protocol/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(profilePayloadFromDialog()) });
        const body = await res.json();
        if (res.ok && body.ok) {
          $('configStatus').textContent = `验证通过：本地地址 ${body.codex_desktop.base_url}；上游模型接口返回 ${body.upstream.models_count} 个模型。`;
        } else {
          $('configStatus').textContent = `验证失败：${body.error || '上游模型接口未返回模型'}`;
        }
      } catch (error) {
        $('configStatus').textContent = `验证失败：${error.message}`;
      } finally {
        setButtonLoading(button, false, '验证中...', '验证配置');
      }
    };

    $('checkCodexProtocol').onclick = async () => {
      const button = $('checkCodexProtocol');
      setButtonLoading(button, true, '验证中...', '验证');
      $('codexProtocolStatus').textContent = '正在验证 Codex 上游协议，请稍候。';
      try {
        const res = await fetch('/admin/codex/protocol/check', { method: 'POST' });
        const body = await res.json();
        if (res.ok && body.ok) {
          $('codexProtocolStatus').textContent = `验证通过：${body.upstream.provider} 使用 ${body.upstream.resolved_api_style}；上游 ${body.upstream.protocol_url}；模型 ${body.upstream.model}。`;
        } else {
          $('codexProtocolStatus').textContent = `验证失败：${body.error || '请求失败'}`;
        }
      } catch (error) {
        $('codexProtocolStatus').textContent = `验证失败：${error.message}`;
      } finally {
        setButtonLoading(button, false, '验证中...', '验证');
      }
    };

    $('installCodex').onclick = async () => {
      const button = $('installCodex');
      setButtonLoading(button, true, '修改中...', '修改Codex配置');
      $('codexMessage').textContent = '正在修改 Codex 配置，请稍候。';
      try {
        const res = await fetch('/admin/codex/install', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: $('codexMode').value, proxy_url: $('proxyUrl').value }) });
        const body = await res.json();
        $('codexMessage').textContent = res.ok ? `修改成功，模式：${$('codexMode').selectedOptions[0].textContent}；备份：${body.backup}。请重启 Codex Desktop。` : body.detail;
        await refresh();
      } catch (error) {
        $('codexMessage').textContent = `修改失败：${error.message}`;
      } finally {
        setButtonLoading(button, false, '修改中...', '修改Codex配置');
      }
    };

    $('restoreCodex').onclick = async () => {
      const button = $('restoreCodex');
      setButtonLoading(button, true, '恢复中...', '恢复默认配置');
      $('codexMessage').textContent = '正在恢复默认配置，请稍候。';
      try {
        const res = await fetch('/admin/codex/restore', { method: 'POST' });
        const body = await res.json();
        $('codexMessage').textContent = res.ok ? `恢复成功，来源：${body.restored_from}。请重启 Codex Desktop。` : body.detail;
        await refresh();
      } catch (error) {
        $('codexMessage').textContent = `恢复失败：${error.message}`;
      } finally {
        setButtonLoading(button, false, '恢复中...', '恢复默认配置');
      }
    };

    $('addMapping').onclick = () => {
      currentMappings.push({ claude_model: '', upstream_model: '' });
      renderMappings();
    };

    $('saveClaudeConfig').onclick = async () => {
      const button = $('saveClaudeConfig');
      setButtonLoading(button, true, '保存中...', '保存映射');
      $('claudeStatus').textContent = '正在保存 ClaudeProxy 模型映射，请稍候。';
      try {
        const payload = {
          active_profile: $('claudeProfileSelect').value,
          api_style: $('claudeApiStyle').value,
          model_mappings: currentMappings
        };
        const res = await fetch('/admin/claude/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const body = await res.json();
        $('claudeStatus').textContent = res.ok ? '保存成功，ClaudeProxy 模型映射已生效。' : `保存失败：${body.detail}`;
        await refresh();
      } catch (error) {
        $('claudeStatus').textContent = `保存失败：${error.message}`;
      } finally {
        setButtonLoading(button, false, '保存中...', '保存映射');
      }
    };

    $('checkClaudeProtocol').onclick = async () => {
      const button = $('checkClaudeProtocol');
      setButtonLoading(button, true, '验证中...', '验证');
      $('claudeProtocolStatus').textContent = '正在验证 Claude 上游协议，请稍候。';
      try {
        const res = await fetch('/admin/claude/protocol/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
        const body = await res.json();
        if (res.ok && body.ok) {
          $('claudeProtocolStatus').textContent = `验证通过：Gateway ${body.claude_desktop.base_url}；上游 ${body.upstream.messages_url}。`;
        } else {
          $('claudeProtocolStatus').textContent = `验证失败：${body.error || '请求失败'}`;
        }
      } catch (error) {
        $('claudeProtocolStatus').textContent = `验证失败：${error.message}`;
      } finally {
        setButtonLoading(button, false, '验证中...', '验证');
      }
    };

    $('checkClaudeModel').onclick = async () => {
      const button = $('checkClaudeModel');
      setButtonLoading(button, true, '验证中...', '验证模型');
      $('claudeModelStatus').textContent = '正在验证 Claude 模型，请稍候。';
      try {
        const res = await fetch('/admin/claude/protocol/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: $('claudeTestModel').value }) });
        const body = await res.json();
        if (res.ok && body.ok) {
          $('claudeModelStatus').textContent = `验证通过：Gateway ${body.claude_desktop.base_url}；上游 ${body.upstream.messages_url}；模型映射到 ${body.upstream.model}。`;
        } else {
          $('claudeModelStatus').textContent = `验证失败：${body.error || '请求失败'}`;
        }
      } catch (error) {
        $('claudeModelStatus').textContent = `验证失败：${error.message}`;
      } finally {
        setButtonLoading(button, false, '验证中...', '验证模型');
      }
    };

    refresh().catch((error) => {
      $('configStatus').textContent = error.message;
    });
  </script>
</body>
</html>
"""
