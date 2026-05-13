# AIProxy

AIProxy 是给 Codex Desktop 和 Claude Desktop 使用的本地中转代理。

它做两件事：

- 对 Codex Desktop 暴露 `/v1/responses`。上游支持 `v1/responses` 就直连；只支持 `v1/chat/completions` 时会自动转换。
- 对 Claude Desktop 暴露 `/anthropic/v1/messages`。上游支持 `v1/messages` 就直连；不支持时可自动回退到 `v1/chat/completions`。

## 你会得到什么

- 一个本地管理页：添加平台、刷新模型、切换当前平台。
- Codex 和 Claude 共用平台列表，但各自独立选择当前平台和上游协议。
- Codex 默认 `Auto`：优先 `v1/responses`，不支持时回退 `v1/chat/completions`。
- Claude 默认 `Auto`：优先 `v1/messages`，不支持时回退 `v1/chat/completions`。
- Codex Desktop 配置一键安装、恢复原始配置。
- 所有本地运行时配置保存在 `config.local.json`，不会提交到仓库。

## 快速开始

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 准备环境变量：

```bash
cp .env.example .env
```

3. 启动服务：

```bash
./start.sh
```

4. 打开管理页：

```text
http://127.0.0.1:8383/
```

普通用户建议直接走管理页，不必手改 `config.local.json`。

## 配置

第一次启动前，只需要准备 `.env`：

```bash
cp .env.example .env
```

最小可用示例：

```bash
UPSTREAM_BASE_URL=https://ark.cn-beijing.volces.com/api/coding
UPSTREAM_PROVIDER_NAME=字节跳动
UPSTREAM_API_KEY=你的平台-key
UPSTREAM_MODEL=glm-5.1
UPSTREAM_MODELS=glm-5.1,glm-4.7
UPSTREAM_USER_AGENT=curl/8.7.1
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8383
REQUEST_TIMEOUT_SECONDS=300
```

常用字段只有这些：

```bash
UPSTREAM_PROVIDER_NAME=你的平台名称
UPSTREAM_BASE_URL=https://你的平台域名/v1
UPSTREAM_API_KEY=你的平台-key
UPSTREAM_MODEL=默认模型ID
UPSTREAM_MODELS=模型A,模型B,模型C
```

管理页里的“平台地址”可以只填根地址，例如：

```text
https://ark.cn-beijing.volces.com/api/coding
```

程序会自动按 OpenAI-compatible API 约定补成：

```text
https://ark.cn-beijing.volces.com/api/coding/v1
```

如果平台要走 `v1/chat/completions`，上游必须支持 streaming：

```text
POST /v1/chat/completions
stream: true
```

管理页保存的配置会写入本地 `config.local.json`。该文件优先级高于 `.env`，保存后新的代理请求会立即使用当前生效配置。

如果要自定义运行时配置文件路径，可以设置 `AIPROXY_CONFIG_PATH`。旧的 `CODEXPROXY_CONFIG_PATH` 仍然兼容，但新配置建议使用 `AIPROXY_CONFIG_PATH`。

不要提交 `.env`。管理页保存的 `config.local.json` 同样会被 `.gitignore` 忽略。

## 管理页怎么配

管理页支持多个共享中转平台。Codex 和 Claude 共用同一组平台列表，但各自选择当前生效的平台，互不影响。

### 上游协议

- `v1/chat/completions`：固定走 `{平台地址}/v1/chat/completions`
- `v1/responses`：固定走 `{平台地址}/v1/responses`
- `Auto`：Codex 优先尝试 `v1/responses`，Claude 优先尝试 `v1/messages`，不支持时回退到 `v1/chat/completions`
- `v1/messages`：Claude 固定透传 `{平台地址}/messages`

协议验证结果会明确告诉你：

- 当前实际用了哪个协议
- 如果优先协议不支持，是否已自动回退
- 当前验证命中的上游 URL

### 模型和映射

- 先添加平台，再点“刷新模型”。
- Codex 的“默认模型”从当前平台模型列表里选，选择后立即生效。
- Claude 不维护默认模型，只维护“Claude 模型名 -> 上游真实模型名”映射。
- Claude 的映射右侧可以手输，也可以从刷新后的模型列表里选。
- 如果 Codex 和 Claude 使用同一个平台，一侧刷新模型后，另一侧候选列表也会同步更新。

### 验证配置

- 平台弹窗里的“验证配置”只检查 `{平台地址}/v1/models` 是否可用。
- Codex 和 Claude 区块里的“验证”检查的是当前协议链路是否可用。

API Key 会直接显示在管理页中，方便本机维护配置。不要把管理页监听到公网或局域网。

`LISTEN_HOST` 和 `LISTEN_PORT` 是服务启动参数，不支持运行时动态生效；修改后需要重启代理。

## 启动、重启和关停

前台启动，适合调试：

```bash
./start.sh
```

默认监听：

```text
http://127.0.0.1:8383
```

后台启动，适合日常使用：

```bash
nohup ./start.sh > aiproxy.log 2>&1 &
```

关停服务：

```bash
./stop.sh
```

重启服务：

```bash
./restart.sh
```

如果只是修改中转平台、API Key、默认模型、协议或 Claude 模型映射，不需要重启，管理页保存后会立即生效。如果修改了 `.env` 里的 `LISTEN_HOST` 或 `LISTEN_PORT`，需要重启。

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
  -d '{"model":"glm-5.1","input":"say hi","stream":false}'
```

流式验证：

```bash
curl -N http://127.0.0.1:8383/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm-5.1","input":"count 1 to 3","stream":true}'
```

流式输出最后应该包含：

```text
event: response.completed
data: [DONE]
```

验证 `glm-4.7`：

```bash
curl -N http://127.0.0.1:8383/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm-4.7","input":"count 1 to 2","stream":true}'
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
python scripts/codex_config.py install --model glm-5.1 --proxy-url http://127.0.0.1:8383
```

如果要切换到其他模型，例如 `glm-4.7`：

```bash
python scripts/codex_config.py install --model glm-4.7 --proxy-url http://127.0.0.1:8383
```

如果要启用“保留官方登录态代理”：

```bash
python scripts/codex_config.py install --mode auth-proxy --model glm-5.1 --proxy-url http://127.0.0.1:8383
```

旧的 `openai-compatible` 覆盖内置 `openai` provider 方案不可用。Codex Desktop 会拒绝覆盖内置 `openai` provider，错误形式是 `model_providers contains reserved built-in provider IDs: openai`。

查看当前是否已经安装 AIProxy 配置：

```bash
python scripts/codex_config.py status
```

脚本会写入类似配置：

```toml
model_provider = "codex_proxy"
model = "glm-5.1"

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

Claude 和 Codex 共享中转平台列表，但当前生效平台互相独立。可以让 Codex 和 Claude 使用不同平台，也可以共用同一个“字节跳动”配置。

管理页的 ClaudeProxy Tab 支持配置模型映射，例如：

```text
claude-opus-4.6   -> glm-5.1
claude-sonnet-4.6 -> glm-5.1
claude-haiku-4.6  -> glm-5.1
```

ClaudeProxy 页面的映射交互规则：

- 左侧 `claude-opus-4.6` 这列是 Claude 请求模型名，保持手工输入。
- 右侧 `glm-5.1` 这列是上游真实模型名，支持直接输入，也支持从刷新后的模型列表中下拉选择。
- 点击“刷新模型”只会更新右侧候选列表，不会自动改写已经填写的映射值。
- 如果某个已填的上游模型不在新拉取的候选列表中，原值会保留，仍然可以继续保存。

请求进入本地代理后，会先把 Claude 请求模型名映射成上游真实模型名，再按当前协议转发。

- 如果当前协议是 `v1/messages`，就直接转到上游 `/messages`
- 如果当前协议是 `v1/chat/completions`，就转成 Chat Completions 请求
- 如果当前协议是 `Auto`，优先试 `v1/messages`，不支持时自动回退到 `v1/chat/completions`

当前版本重点覆盖文本对话链路。多模态和工具调用的深度转换不在这版目标里。

## 恢复默认配置

恢复到第一次安装 AIProxy 前的原始配置：

```bash
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
