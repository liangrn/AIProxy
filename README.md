# CodexProxy

CodexProxy 是给 Codex Desktop 使用的本地中转代理。

它对 Codex Desktop 暴露 `/v1/responses`，内部转发到任意 OpenAI-compatible 中转平台的 `/v1/chat/completions`，并把 Chat Completions 的流式输出转换成 Codex 需要的 Responses SSE 事件，确保流式响应以 `response.completed` 正常结束。

## 功能

- 支持配置任意 OpenAI-compatible 中转平台。
- 默认示例支持 `gpt-5.5` 和 `gpt-5.4`。
- 支持非流式 `/v1/responses`。
- 支持流式 `/v1/responses`，尾部会补齐 `response.completed` 和 `[DONE]`。
- 提供 `/healthz` 健康检查。
- 提供本地管理页，可动态设置多个中转平台、刷新模型并切换当前生效配置。
- 提供 Codex Desktop 配置安装和恢复脚本。
- 密钥通过 `.env` 或进程环境变量传入，不写入代码。

## 环境

运行环境使用你的 conda 环境：

```bash
conda activate iai
```

如果环境里缺依赖：

```bash
cd /Users/liangrn/Documents/Codes/CodexProxy
pip install -r requirements.txt
```

## 配置

创建本地环境配置：

```bash
cd /Users/liangrn/Documents/Codes/CodexProxy
cp .env.example .env
```

编辑 `.env`：

```bash
UPSTREAM_BASE_URL=https://www.uocode.com/v1
UPSTREAM_PROVIDER_NAME=uocode
UPSTREAM_API_KEY=你的平台-key
UPSTREAM_MODEL=gpt-5.5
UPSTREAM_MODELS=gpt-5.5,gpt-5.4,gpt-5.4-mini,gpt-5.3-codex
UPSTREAM_USER_AGENT=curl/8.7.1
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8383
REQUEST_TIMEOUT_SECONDS=300
```

不要提交 `.env`，里面有密钥。管理页保存的 `config.local.json` 同样会被 `.gitignore` 忽略。

如果要换成其他中转平台，只需要改这些字段：

```bash
UPSTREAM_PROVIDER_NAME=你的平台名称
UPSTREAM_BASE_URL=https://你的平台域名/v1
UPSTREAM_API_KEY=你的平台-key
UPSTREAM_MODEL=默认模型ID
UPSTREAM_MODELS=模型A,模型B,模型C
```

管理页里的“平台地址”可以只填根地址，例如：

```text
https://www.uocode.com
```

程序会自动按 OpenAI-compatible API 约定补成：

```text
https://www.uocode.com/v1
```

要求是上游平台必须兼容 OpenAI Chat Completions streaming，也就是支持：

```text
POST /v1/chat/completions
stream: true
```

也可以启动服务后打开本地管理页配置：

```text
http://127.0.0.1:8383/
```

管理页保存的配置会写入本地 `config.local.json`。该文件优先级高于 `.env`，保存后新的代理请求会立即使用当前生效配置。

管理页支持多个中转配置，例如 UoCode 和 AiCoeGo。当前生效配置决定 `/v1/responses` 的上游平台、`/v1/models` 的模型列表，以及应用到 Codex Desktop 时写入的默认模型。

管理页的模型流程：

- 填写平台名称、平台地址和 API Key。
- 点击“刷新模型”，CodexProxy 会请求当前平台的 `/v1/models`。
- 刷新成功后，默认模型输入框会变成可输入过滤的模型选择框。
- 选择默认模型并保存后，新请求会立即使用这个模型。

管理页提供“验证配置”按钮，用来检查两件事：

- Codex Desktop 侧使用本地 `http://127.0.0.1:8383/v1`，协议是 `wire_api = "responses"`。
- 上游平台侧使用 `{平台地址}/v1/models` 和 `{平台地址}/v1/chat/completions`，并能从 `/models` 读取模型。

API Key 会直接显示在管理页中，方便本机维护配置。不要把管理页监听到公网或局域网。

`LISTEN_HOST` 和 `LISTEN_PORT` 是服务启动参数，不支持运行时动态生效；修改后需要重启代理。

## 启动、重启和关停

前台启动，适合调试：

```bash
cd /Users/liangrn/Documents/Codes/CodexProxy
conda activate iai
./run.sh
```

默认监听：

```text
http://127.0.0.1:8383
```

后台启动，适合日常使用：

```bash
cd /Users/liangrn/Documents/Codes/CodexProxy
conda activate iai
nohup ./run.sh > codexproxy.log 2>&1 &
```

关停服务：

```bash
cd /Users/liangrn/Documents/Codes/CodexProxy
./stop.sh
```

重启服务：

```bash
cd /Users/liangrn/Documents/Codes/CodexProxy
conda activate iai
./restart.sh
```

如果只是修改中转平台、API Key 或默认模型，不需要重启，管理页保存后会立即生效。如果修改了 `.env` 里的 `LISTEN_HOST` 或 `LISTEN_PORT`，需要重启。

## 验证代理

健康检查：

```bash
curl -sS http://127.0.0.1:8383/healthz
```

预期：

```json
{"ok":true}
```

非流式验证：

```bash
curl -sS http://127.0.0.1:8383/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","input":"say hi","stream":false}'
```

流式验证：

```bash
curl -N http://127.0.0.1:8383/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","input":"count 1 to 3","stream":true}'
```

流式输出最后应该包含：

```text
event: response.completed
data: [DONE]
```

验证 `gpt-5.4`：

```bash
curl -N http://127.0.0.1:8383/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.4","input":"count 1 to 2","stream":true}'
```

## 配置 Codex Desktop

项目提供脚本自动修改 `~/.codex/config.toml`，安装前会生成备份。

也可以在管理页中点击“应用到 Codex Desktop”。页面操作和脚本一样会生成备份，并自动使用当前生效配置的默认模型。

切换 Codex Desktop 到本地代理：

```bash
cd /Users/liangrn/Documents/Codes/CodexProxy
conda activate iai
python scripts/codex_config.py install --model gpt-5.5 --proxy-url http://127.0.0.1:8383
```

如果要切换到 `gpt-5.4`：

```bash
python scripts/codex_config.py install --model gpt-5.4 --proxy-url http://127.0.0.1:8383
```

查看当前是否已经安装 CodexProxy 配置：

```bash
python scripts/codex_config.py status
```

脚本会写入类似配置：

```toml
model_provider = "codex_proxy"
model = "gpt-5.5"

[model_providers.codex_proxy]
name = "Codex Proxy"
base_url = "http://127.0.0.1:8383/v1"
wire_api = "responses"
requires_openai_auth = false
stream_max_retries = 0
```

修改配置后，退出并重新打开 Codex Desktop。

## 恢复默认配置

恢复到安装 CodexProxy 前的最近一次备份：

```bash
cd /Users/liangrn/Documents/Codes/CodexProxy
conda activate iai
python scripts/codex_config.py restore
```

恢复后同样需要重启 Codex Desktop。

也可以在管理页中点击“恢复默认配置”。如果没有可用备份，按钮会禁用。

备份文件会保存在 `~/.codex/` 目录，文件名类似：

```text
config.toml.codexproxy-backup-20260512-104500
```

## 测试

```bash
cd /Users/liangrn/Documents/Codes/CodexProxy
conda activate iai
pytest -q
```

## 文件说明

- `app/main.py`：FastAPI 入口，上游请求和路由。
- `app/adapters.py`：Responses 请求和 Chat Completions 请求之间的结构转换。
- `app/sse.py`：Chat streaming 到 Responses SSE 的事件转换。
- `app/config.py`：环境变量和本地动态配置。
- `scripts/codex_config.py`：安装和恢复 Codex Desktop 配置。
- `tests/`：代理行为和配置脚本测试。

## 注意事项

- Codex Desktop 当前应使用 `wire_api = "responses"`。
- 不要直接把 Codex Desktop 配到中转平台的 `/v1/chat/completions`；Codex 需要 Responses 协议。
- 如果端口 `8383` 被占用，可以改 `.env` 里的 `LISTEN_PORT`，同时重新运行 `scripts/codex_config.py install --proxy-url http://127.0.0.1:新端口`。
