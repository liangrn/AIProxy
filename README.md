# AIProxy

AIProxy 是给 Codex Desktop 和 Claude Desktop 使用的本地中转代理。

它对 Codex Desktop 暴露 `/v1/responses`。上游平台如果原生支持 Responses 协议，就直接透传；如果只支持 OpenAI-compatible 的 `/v1/chat/completions`，就自动或按配置走协议转换，把 Chat Completions 的流式输出转换成 Codex 需要的 Responses SSE 事件，确保流式响应以 `response.completed` 正常结束。

## 功能

- 支持配置任意 OpenAI-compatible 中转平台。
- 默认示例支持 `gpt-5.5` 和 `gpt-5.4`。
- 支持非流式 `/v1/responses`。
- 支持流式 `/v1/responses`，尾部会补齐 `response.completed` 和 `[DONE]`。
- 支持在 Codex 和 Claude 页面分别配置各自的上游协议。
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
cd /Users/liangrn/Documents/Codes/AIProxy
pip install -r requirements.txt
```

## 配置

创建本地环境配置：

```bash
cd /Users/liangrn/Documents/Codes/AIProxy
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

如果平台走 `chat` 协议，要求上游兼容 OpenAI Chat Completions streaming，也就是支持：

```text
POST /v1/chat/completions
stream: true
```

也可以启动服务后打开本地管理页配置：

```text
http://127.0.0.1:8383/
```

管理页保存的配置会写入本地 `config.local.json`。该文件优先级高于 `.env`，保存后新的代理请求会立即使用当前生效配置。

如果要自定义运行时配置文件路径，可以设置 `AIPROXY_CONFIG_PATH`。旧的 `CODEXPROXY_CONFIG_PATH` 仍然兼容，但新配置建议使用 `AIPROXY_CONFIG_PATH`。

管理页支持多个共享中转配置，例如 UoCode、AiCoeGo 和字节跳动。Codex 和 Claude 会分别选择自己的当前配置，互不影响。

Codex 页和 Claude 页会分别配置各自的“上游协议”：

- `v1/chat/completions`：固定走 `{平台地址}/v1/chat/completions`
- `v1/responses`：固定走 `{平台地址}/v1/responses`
- `Auto`：Codex 优先尝试原生 `responses`，Claude 优先尝试 `messages`，不支持时回退到 `chat/completions`
- `v1/messages`：Claude 固定透传 `{平台地址}/messages`

管理页的模型流程：

- 填写平台名称、平台地址和 API Key。
- 点击“刷新模型”，AIProxy 会请求当前平台的 `/v1/models`。
- 刷新成功后，默认模型输入框会变成可输入过滤的模型选择框。
- 选择默认模型并保存后，新请求会立即使用这个模型。

管理页提供“验证配置”按钮，用来检查平台模型接口是否可用：

- 本地代理地址会显示为 `http://127.0.0.1:8383/v1`。
- 上游平台侧固定使用 `{平台地址}/v1/models` 读取模型并统计模型数量。

API Key 会直接显示在管理页中，方便本机维护配置。不要把管理页监听到公网或局域网。

`LISTEN_HOST` 和 `LISTEN_PORT` 是服务启动参数，不支持运行时动态生效；修改后需要重启代理。

## 启动、重启和关停

前台启动，适合调试：

```bash
cd /Users/liangrn/Documents/Codes/AIProxy
conda activate iai
./start.sh
```

默认监听：

```text
http://127.0.0.1:8383
```

后台启动，适合日常使用：

```bash
cd /Users/liangrn/Documents/Codes/AIProxy
conda activate iai
nohup ./start.sh > aiproxy.log 2>&1 &
```

关停服务：

```bash
cd /Users/liangrn/Documents/Codes/AIProxy
./stop.sh
```

重启服务：

```bash
cd /Users/liangrn/Documents/Codes/AIProxy
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

也可以在管理页中点击“修改Codex配置”。页面操作和脚本一样会生成备份，并自动使用当前生效配置的默认模型。

支持两种安装模式：

- `保留官方登录态代理`：写入自定义 `codex_proxy` provider，但设置 `requires_openai_auth = true`，让 Codex 继续携带官方登录态请求本地 AIProxy。
- `普通第三方代理`：直接切到 `codex_proxy` provider，适合纯代理使用。

第一种模式已经验证会把官方 Bearer auth 和 `ChatGPT-Account-Id` 发送到本地 AIProxy，同时请求出口仍可转到第三方平台。它能保留官方登录态，但官方云端历史视图是否显示这些会话仍取决于 Codex Desktop 内部逻辑；请求不经过官方模型后端时，不能保证官方云端历史完整记录。

切换 Codex Desktop 到本地代理：

```bash
cd /Users/liangrn/Documents/Codes/AIProxy
conda activate iai
python scripts/codex_config.py install --model gpt-5.5 --proxy-url http://127.0.0.1:8383
```

如果要切换到 `gpt-5.4`：

```bash
python scripts/codex_config.py install --model gpt-5.4 --proxy-url http://127.0.0.1:8383
```

如果要启用“保留官方登录态代理”：

```bash
python scripts/codex_config.py install --mode auth-proxy --model gpt-5.4 --proxy-url http://127.0.0.1:8383
```

旧的 `openai-compatible` 覆盖内置 `openai` provider 方案不可用。Codex Desktop 会拒绝覆盖内置 `openai` provider，错误形式是 `model_providers contains reserved built-in provider IDs: openai`。

查看当前是否已经安装 AIProxy 配置：

```bash
python scripts/codex_config.py status
```

脚本会写入类似配置：

```toml
model_provider = "codex_proxy"
model = "gpt-5.5"

[model_providers.codex_proxy]
name = "AI Proxy"
base_url = "http://127.0.0.1:8383/v1"
wire_api = "responses"
requires_openai_auth = false
stream_max_retries = 0
```

修改配置后，退出并重新打开 Codex Desktop。

## 配置 Claude Desktop Developer Mode Gateway

AIProxy 同时提供 ClaudeProxy，本地地址为：

```text
http://127.0.0.1:8383/anthropic
```

Claude Desktop Developer Mode 的 third-party gateway 中手动填写：

```text
Gateway URL: http://127.0.0.1:8383/anthropic
API Key: 任意非空值
Model: claude-opus-4.6
```

ClaudeProxy 不会修改 Claude Desktop 的本地配置文件；它只在管理页里显示应填写的 Gateway URL，并提供连通性验证。

Claude 和 Codex 共享中转平台列表，但当前生效平台互相独立。可以让 Codex 使用 UoCode，同时让 Claude 使用“字节跳动”。

管理页的 ClaudeProxy Tab 支持配置模型映射，例如：

```text
claude-opus-4.6   -> glm-5.1
claude-sonnet-4.6 -> glm-5.1
claude-haiku-4.6  -> glm-5.1
```

请求进入本地代理后，只会把 Anthropic Messages 请求体中的 `model` 改写为映射后的真实模型，其他字段尽量原样透传到上游 `{平台地址}/messages`。上游响应里的 `model` 会在非流式响应中改回 Claude Desktop 请求的模型名。

ClaudeProxy v1 默认使用 Anthropic Messages 透传模式，已经适合支持 `/messages` 的中转平台，例如当前“字节跳动”配置。Chat Completions 协议转换、多模态和工具调用的深度转换不作为 v1 目标。

## 恢复默认配置

恢复到第一次安装 AIProxy 前的原始配置：

```bash
cd /Users/liangrn/Documents/Codes/AIProxy
conda activate iai
python scripts/codex_config.py restore
```

恢复后同样需要重启 Codex Desktop。

也可以在管理页中点击“恢复默认配置”。如果没有可用备份，按钮会禁用。

备份文件会保存在 `~/.codex/` 目录。

首次安装前的原始配置会保存在：

```text
config.toml.codexproxy-original-backup
```

## 测试

```bash
cd /Users/liangrn/Documents/Codes/AIProxy
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
