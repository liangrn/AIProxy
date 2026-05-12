import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .adapters import chat_completion_to_response, responses_to_chat_payload
from .config import activate_profile, get_settings, normalize_models, public_settings, save_profile, save_runtime_config, update_active_profile_models
from .sse import chat_stream_to_responses_sse
from scripts.codex_config import install as install_codex_config
from scripts.codex_config import restore as restore_codex_config
from scripts.codex_config import status as codex_config_status


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
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


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
    app = FastAPI(title="Codex Responses Proxy", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    async def admin_page():
        return HTMLResponse(ADMIN_HTML)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/admin/config")
    async def admin_config():
        return {"config": public_settings()}

    @app.post("/admin/config")
    async def update_admin_config(request: Request):
        data = await request.json()
        settings = save_runtime_config(data)
        return {"ok": True, "config": public_settings(settings)}

    @app.post("/admin/profiles/{profile_id}")
    async def update_profile(profile_id: str, request: Request):
        data = await request.json()
        profile = save_profile(profile_id, data)
        return {"ok": True, "profile_id": profile_id, "profile": profile, "config": public_settings()}

    @app.post("/admin/profiles/{profile_id}/activate")
    async def set_active_profile(profile_id: str):
        profile = activate_profile(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Profile not found: {profile_id}")
        return {"ok": True, "profile_id": profile_id, "profile": profile, "config": public_settings()}

    @app.post("/admin/models/refresh")
    async def refresh_models():
        settings = get_settings()
        if not settings.upstream_api_key:
            raise HTTPException(status_code=400, detail="API key is required to refresh models")

        headers = {
            "Authorization": f"Bearer {settings.upstream_api_key}",
            "Accept": "application/json",
            "User-Agent": settings.upstream_user_agent,
        }
        timeout = httpx.Timeout(settings.request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
            response = await client.get(f"{settings.upstream_base_url}/models", headers=headers)
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail=response.text)

        body = response.json()
        models = normalize_models([item.get("id", "") for item in body.get("data", []) if isinstance(item, dict)])
        if not models:
            raise HTTPException(status_code=502, detail="No models found in upstream response")
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
                "wire_api": "responses",
                "responses_url": f"{codex_base_url}/responses",
                "models_url": f"{codex_base_url}/models",
            },
            "upstream": {
                "provider": settings.upstream_provider_name,
                "base_url": settings.upstream_base_url,
                "models_url": f"{settings.upstream_base_url}/models",
                "chat_completions_url": f"{settings.upstream_base_url}/chat/completions",
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
        try:
            async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
                response = await client.get(f"{settings.upstream_base_url}/models", headers=headers)
        except httpx.HTTPError as exc:
            result["error"] = str(exc)
            return JSONResponse(result, status_code=502)

        if response.status_code >= 400:
            result["error"] = response.text
            return JSONResponse(result, status_code=502)

        body = response.json()
        models = normalize_models([item.get("id", "") for item in body.get("data", []) if isinstance(item, dict)])
        result["upstream"]["models_ok"] = bool(models)
        result["upstream"]["models_count"] = len(models)
        result["ok"] = bool(models)
        if not models:
            result["error"] = "No models found in upstream response"
            return JSONResponse(result, status_code=502)
        return result

    @app.get("/admin/codex/status")
    async def admin_codex_status():
        config_path = codex_config_path()
        backups = sorted(config_path.parent.glob(f"{config_path.name}.codexproxy-backup-*"))
        return {
            "status": codex_config_status(config_path),
            "config_path": str(config_path),
            "backup_available": bool(backups),
            "latest_backup": str(backups[-1]) if backups else None,
        }

    @app.post("/admin/codex/install")
    async def admin_codex_install(request: Request):
        data = await request.json()
        settings = get_settings()
        model = settings.upstream_model
        proxy_url = str(data.get("proxy_url") or f"http://{settings.listen_host}:{settings.listen_port}")
        backup_path = install_codex_config(codex_config_path(), model=model, proxy_url=proxy_url)
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
        chat_payload = responses_to_chat_payload(payload, settings.upstream_model, stream=stream)
        model = chat_payload["model"]

        if stream:
            chunks = stream_chat_completion(chat_payload)
            return StreamingResponse(
                chat_stream_to_responses_sse(chunks, model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        chat = await create_chat_completion(chat_payload, stream=False)
        return JSONResponse(chat_completion_to_response(chat, model))

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
  <title>CodexProxy 管理</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #202124; background: #f7f8fa; }
    body { margin: 0; }
    main { max-width: 960px; margin: 0 auto; padding: 28px 20px 48px; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    h2 { margin: 0 0 16px; font-size: 18px; }
    p { line-height: 1.55; color: #4b5563; }
    section { background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 20px; margin-top: 18px; }
    label { display: block; font-size: 13px; font-weight: 600; margin: 14px 0 6px; }
    input, textarea, select { width: 100%; box-sizing: border-box; border: 1px solid #c7ced9; border-radius: 6px; padding: 10px 12px; font: inherit; background: #fff; }
    textarea { min-height: 84px; resize: vertical; }
    button { border: 0; border-radius: 6px; padding: 10px 14px; font: inherit; font-weight: 650; cursor: pointer; background: #1f6feb; color: #fff; }
    button.secondary { background: #44546a; }
    button.danger { background: #b42318; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
    .status { min-height: 22px; margin-top: 12px; font-size: 14px; color: #255e2e; }
    .muted { color: #697386; font-size: 13px; }
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
    <h1>CodexProxy 管理</h1>
    <p>在这里配置 OpenAI-compatible 中转平台、模型列表，以及写入或恢复 Codex Desktop 配置。中转配置保存后立即影响新的代理请求；Codex Desktop 配置修改后需要重启 Codex Desktop。</p>

    <section>
      <h2>中转平台配置</h2>
      <label for="profileSelect">当前配置</label><select id="profileSelect"></select>
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
        <button id="saveConfig">保存并设为当前生效</button>
        <button id="newProfile" class="secondary">新建配置</button>
        <button id="checkProtocol" class="secondary">验证配置</button>
      </div>
      <div id="configStatus" class="status"></div>
    </section>

    <section>
      <h2>Codex Desktop 配置</h2>
      <p class="muted">当前状态：<span id="codexStatus">读取中</span>；配置文件：<code id="codexPath"></code></p>
      <p class="muted">Codex Desktop 模型会自动使用当前配置的默认模型：<code id="codexModelText"></code></p>
      <label for="proxyUrl">代理地址</label><input id="proxyUrl">
      <div class="actions">
        <button id="installCodex">应用到 Codex Desktop</button>
        <button id="restoreCodex" class="danger">恢复默认配置</button>
      </div>
      <div id="codexMessage" class="status"></div>
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
        option.textContent = profileId === config.active_profile ? `${profile.name}（当前）` : profile.name;
        $('profileSelect').appendChild(option);
      }
      $('profileSelect').value = config.active_profile;
      fillProfile(config.active_profile);
      $('proxyUrl').value = `http://${config.listen_host}:${config.listen_port}`;

      const codexRes = await fetch('/admin/codex/status');
      const codex = await codexRes.json();
      $('codexStatus').textContent = codex.status + (codex.backup_available ? '，有备份可恢复' : '，暂无备份');
      $('codexPath').textContent = codex.config_path;
      $('restoreCodex').disabled = !codex.backup_available;
    }

    $('profileSelect').onchange = async () => {
      const profileId = $('profileSelect').value;
      const res = await fetch(`/admin/profiles/${encodeURIComponent(profileId)}/activate`, { method: 'POST' });
      $('configStatus').textContent = res.ok ? '已切换当前生效配置。' : '切换失败';
      await refresh();
    };

    $('newProfile').onclick = () => {
      $('provider').value = '';
      $('baseUrl').value = '';
      $('apiKey').value = '';
      $('defaultModel').value = '';
      currentModels = [];
      renderModelMenu();
      $('timeout').value = 300;
      $('userAgent').value = 'curl/8.7.1';
      $('configStatus').textContent = '填写后保存即可创建新配置。';
    };

    $('saveConfig').onclick = async () => {
      const profileId = profileIdFromName($('provider').value);
      const payload = {
        name: $('provider').value,
        base_url: $('baseUrl').value,
        api_key: $('apiKey').value,
        default_model: $('defaultModel').value,
        models: currentModels,
        user_agent: $('userAgent').value || 'curl/8.7.1',
        timeout_seconds: Number($('timeout').value || 300)
      };
      const res = await fetch(`/admin/profiles/${encodeURIComponent(profileId)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      $('configStatus').textContent = res.ok ? '已保存，新的代理请求会使用这组配置。' : '保存失败';
      $('apiKey').value = '';
      await refresh();
    };

    $('refreshModels').onclick = async () => {
      const res = await fetch('/admin/models/refresh', { method: 'POST' });
      const body = await res.json();
      $('configStatus').textContent = res.ok ? '模型列表已刷新。' : `刷新失败：${body.detail}`;
      if (res.ok) {
        currentModels = body.profile.models || [];
        $('defaultModel').value = body.profile.default_model;
        renderModelMenu();
        $('modelMenu').classList.add('open');
      }
      await refresh();
    };

    $('checkProtocol').onclick = async () => {
      const res = await fetch('/admin/protocol/check', { method: 'POST' });
      const body = await res.json();
      if (res.ok && body.ok) {
        $('configStatus').textContent = `验证通过：Codex Desktop 使用 Responses，本地地址 ${body.codex_desktop.base_url}；上游模型接口返回 ${body.upstream.models_count} 个模型。`;
      } else {
        $('configStatus').textContent = `验证失败：${body.error || '上游模型接口未返回模型'}`;
      }
    };

    $('installCodex').onclick = async () => {
      const res = await fetch('/admin/codex/install', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ proxy_url: $('proxyUrl').value }) });
      const body = await res.json();
      $('codexMessage').textContent = res.ok ? `已写入，备份：${body.backup}。请重启 Codex Desktop。` : body.detail;
      await refresh();
    };

    $('restoreCodex').onclick = async () => {
      const res = await fetch('/admin/codex/restore', { method: 'POST' });
      const body = await res.json();
      $('codexMessage').textContent = res.ok ? `已恢复自：${body.restored_from}。请重启 Codex Desktop。` : body.detail;
      await refresh();
    };

    refresh().catch((error) => {
      $('configStatus').textContent = error.message;
    });
  </script>
</body>
</html>
"""
