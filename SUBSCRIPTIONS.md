# 订阅接入设计与使用

订阅账号与 API Key 使用独立的 provider，避免误用按量计费凭据：

| 接入方式 | provider | 凭据 |
| --- | --- | --- |
| ChatGPT 订阅中的 GPT 模型 | `chatgpt` | OpenAI OAuth 登录 |
| Grok 订阅 | `grok` | xAI OAuth 登录 |
| OpenAI API | `openai` | `OPENAI_API_KEY` |
| xAI API | `xai` | `XAI_API_KEY` |

订阅是否允许某个模型、当前额度和使用限制由服务端决定。登录成功并不表示拥有所有 API 模型；优先使用登录后返回的模型列表。订阅失败时不会自动切换到付费 API Key。

## 在 Conda 环境中使用

```bash
conda activate tradingagent
tradingagents login chatgpt
tradingagents login grok
tradingagents auth-status
tradingagents
```

在主菜单选择 **Subscription**，再选择 ChatGPT 或 Grok。根据登录提示在浏览器完成账号授权。`auth-status` 仅查看本机保存的登录状态，不联网刷新，也不证明订阅仍有效或有剩余额度。

无需将订阅令牌填写到 `.env`。账号凭据保存在 `~/.tradingagents/` 的供应商独立文件中，以仅当前用户可读写的权限原子更新。不要把这些文件加入版本管理或台账。

退出登录：

```bash
tradingagents logout chatgpt
tradingagents logout grok
```

命令会说明是否完成服务端撤销，或仅清除了本机凭据。

## 两层使用不同的订阅

在项目 `.env` 配置以下非敏感选项；模型 ID 替换为账号可用列表中的值：

```dotenv
TRADINGAGENTS_LLM_PROVIDER=chatgpt
TRADINGAGENTS_QUICK_THINK_PROVIDER=grok
TRADINGAGENTS_QUICK_THINK_LLM=替换为Grok账号可用模型ID
TRADINGAGENTS_DEEP_THINK_PROVIDER=chatgpt
TRADINGAGENTS_DEEP_THINK_LLM=替换为ChatGPT账号可用模型ID
TRADINGAGENTS_OUTPUT_LANGUAGE=Chinese
```

快模型用于分析师、研究辩论及交易员；深模型用于研究经理和组合经理。两个订阅分别登录、分别刷新，互不共享令牌。订阅 provider 固定官方推理地址，不设置自定义 backend URL。

## 扩展边界

`tradingagents/llm_clients/subscriptions.py` 是订阅元数据注册表，包含显示名称、认证模块、固定推理地址和传输类型。

认证适配器约定：

| 方法 | 职责 |
| --- | --- |
| `sign_in(say=print)` | 完成供应商授权，保存凭据，返回账号显示名称 |
| `sign_out()` | 清除本地凭据；返回 `revoked`、`forgotten` 或 `None` |
| `signed_in_as()` | 本地状态查询，不刷新令牌，不返回令牌 |
| `access_token()` | 获取有效令牌，需要时加锁刷新 |
| `credential()` | 校验登录并返回逐请求获取令牌的 callable |
| `list_models()` | 返回账号可用模型的 `(显示名, ID)` 列表 |

每个适配器提供认证错误和暂时不可达错误两类异常。临时网络问题不应触发重复登录；无效授权应明确要求重新登录。认证状态和模型列表不放入投资分析状态、报告或 checkpoint。

ChatGPT 沿用项目现有的 PKCE 浏览器登录及 Responses API 传输（流式、不保存响应，处理其参数限制）。Grok 使用设备码登录及 Chat Completions 传输，保持现有 xAI 工具调用格式；它没有套用 ChatGPT 的参数过滤规则。两种认证模块都在请求时获取有效令牌，推理层不缓存固定的旧令牌。

新增供应商时：

1. 核实供应商允许的订阅授权流程、公开客户端配置与推理协议。
2. 实现上述认证接口，使用独立凭据文件，补充授权、刷新、撤销和错误处理测试。
3. 在 `SUBSCRIPTIONS` 注册。菜单、登录／退出／状态、模型选择及无 API Key 行为由注册表生成。
4. 在 `openai_client.py` 的订阅传输映射中选择兼容传输；协议不同则实现新的传输类。不要把 OpenAI 的参数限制套用到所有供应商。
5. 验证工具调用、结构化输出、流式响应、混合模型层以及凭据隔离，再用真实账号验收。

当前扩展方式是代码内注册，不加载任意远程插件，也不把任意用户填写的 OAuth URL 视为可信地址。

## 协议依据与验证边界

- [OpenAI 开源客户端订阅授权](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [xAI 官方公布的 OpenCode 订阅接入](https://x.ai/news/grok-opencode)

自动测试使用模拟授权和推理服务响应，不能代替真实账号登录及订阅额度下的一次完整分析。

协议实现参考：[OpenCode xAI OAuth（固定版本）](https://github.com/anomalyco/opencode/blob/e8fea9e63a437fb839fa925a6b63ace31b243471/packages/opencode/src/plugin/xai.ts)、[xAI 推理协议（固定版本）](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/ai/src/providers/xai.ts)。

Grok 适配器使用该公开集成中的客户端配置，不代表 xAI 已单独认证 TradingAgents。供应商变更公开客户端策略、订阅权限或模型接口时，需要更新适配器并重新验收。
