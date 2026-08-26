# Agent Runner（冥想陪伴 agent）实施方案

> 依据 `tmp/agent_harness.md` 的讨论整理。目标：在现有产品之上加一层 **minimal agent harness** ——
> 一个长活容器服务跑 agent loop（LLM tool calling → 工具执行 → 每轮状态落 DynamoDB），
> 对话收敛后把定稿 brief 投给现有 Step Functions 管线。只对 Pro 用户开放，
> 产品叙事是「它记得你、听懂你、带着你走」。
>
> 本文是编码前的设计方案。先给结论，再逐层展开：数据模型、loop、工具集、传输层、
> infra、前端、合规、里程碑与测试。凡与 `CLAUDE.md` 硬约束有出入的地方，第 9 节集中列出需要修订的条款。
>
> **2026-08-25 修订**：agent 侧 **不用 Anthropic 第一方 API，留在 Bedrock**。§3.1 比较了 Bedrock 上的
> 四条路并给出决定；受影响的章节（架构图、secrets、成本、合规、待拍板项）已同步改写。
> 同日再修订：**三层全部自建** —— 第 1 层 LLM API 调用、第 2 层 loop、第 3 层跑 loop 的 harness，
> 都不用现成的库或托管服务（Strands、AgentCore、Bedrock Agents 一个不用），见 §0.5 与 §1。
> 同日拍板（§13）：新建 `plan_pro`；LLM 层先做 Converse + provider 抽象；**零常驻成本** ——
> 第 3 层的算力从 Fargate + ALB 改为 Lambda Function URL（response streaming）+ CloudFront OAC，
> VPC / ALB / ECS 全部取消。§1、§5、§6、§8、§11 已按此改写。
> 再修订：**双引擎**。第 1、2 层各实现两套 —— 自建（`native`，默认）与 LangChain/LangGraph
> （`langgraph`）—— 共用工具、prompt、数据模型与第 3 层 harness，用同一份契约测试保证可互换；
> 目的是覆盖 JD 上的 LangChain/LangGraph 要求并做对比学习。见 §0.5、§3.4。

## 0. 先修正对话里的几处前提

1. **没有「pro」这个 plan。** `api/products.py` 目前只有 `free` 与 `monthly`（20 credits/月）。
   **已拍板：新增目录项 `plan_pro`**（`kind=subscription`, `plan="pro"`, credits 待定价），
   门禁函数只认 `entitlement.plan == "pro"`。plan 的流转不需要新代码：
   `apply_subscription_update` 已经把产品的 `plan` 写到 ENTITLEMENT，取消订阅时回到 `free`，
   agent 入口随之关闭。`monthly` 保持原样、不自动升级。
2. **对话里说「agent 侧直接用 Anthropic API」—— 不采纳，agent 留在 Bedrock。** 理由：
   `CLAUDE.md` 技术栈是 Bedrock 且用户文本留在悉尼；Bedrock 上的 Converse API 与
   Claude Messages API 端点（Mantle）都提供原生 tool calling + streaming，**同样满足**
   「直接操作 LLM API」的简历目标；而且鉴权走 Lambda 执行角色的 SigV4，**整个功能不引入任何新 secret**。
   四条路的对比与决定见 §3.1。
3. **「长活容器」与「每轮落库」互相削弱 —— 拍板后选了后者。** 对话里指出：如果每轮状态都持久化，
   loop 就是可恢复、无状态的，长活容器的必要性消失。零常驻成本的决定把这个推论落到实处：
   **每一轮对话是一次 Lambda 调用**，状态全在 DynamoDB，流式靠 Function URL 的 response streaming。
   代价是没有 ECS/Fargate 上简历；换来的是「durable, resumable agent loop on serverless」这个更纯粹的
   故事，和 ≈ 0 的空闲成本。loop 仍是宿主无关的纯包（§3），Lambda 只是第一个宿主。
4. **对话免费聊、生成才扣 credit。** 这是成本闸门的核心：整个会话不冻结 credit，
   只有终结工具 `finalize_meditation_brief` 被调用那一刻走现有的 `create_job + start_execution`，
   credit 仍由状态机里的 `FreezeCredit` 冻结 —— 约束 1 的三个操作一行不改。
5. **三层全部自建，这是本功能的定义，不是实现细节。** 把 agent 系统分成三层：
   **第 1 层** 直接调 LLM API（tool calling / streaming 的协议细节）；**第 2 层** loop
   （上下文组装、工具分发、收敛、checkpoint）；**第 3 层** 跑 loop 的 harness
   （进程生命周期、鉴权、传输、可恢复、观测）。市面上的东西各占一层：Strands 是第 2 层的库，
   AgentCore 是第 3 层的托管 harness，Bedrock Agents 把 2 和 3 一起藏成黑盒。
   本项目 **三层都自己写**：第 1 层用 `bedrock-runtime` 的裸 `converse_stream`（这是 API，不是框架），
   第 2 层是 `backend/agent/native/`，第 3 层是 `backend/agent_runner/` + Lambda Function URL（§1）。
   **在此之上再加一套 LangChain/LangGraph 引擎**（§3.4）作为第 1、2 层的第二实现，目的是 JD 覆盖与对比学习。
   原则相应改成三条：① 自建引擎是默认引擎，必须完整、独立、不 import 任何 langchain/langgraph；
   ② 框架引擎只能替换第 1、2 层，第 3 层 harness 只有一套且是自建的（LangGraph Platform / LangServe 不用）；
   ③ 两套引擎面对同一份契约测试，产出的 T-item 必须一致 —— 对比才成立。
   Strands、AgentCore、Bedrock Agents 仍然不用：它们没有 JD 价值上的对应，只是替我们写层。

---

## 1. 总体架构

```
PWA (/companion)
  │  POST /agent/sessions/{id}/turns   (fetch + ReadableStream, SSE 事件流; 带 x-amz-content-sha256)
  ▼
CloudFront (站点分发, 新增 /agent/* behavior: 不缓存, 转发 Authorization, OAC 对源 SigV4 签名)
  ▼
Lambda Function URL (AuthType=AWS_IAM, InvokeMode=RESPONSE_STREAM)  ── 只有 CloudFront 能调
  ▼
Agent Lambda (容器镜像, 512 MB, timeout 120 s, reserved concurrency 10)
    backend/agent_runner/   FastAPI (SSE) ← Lambda Web Adapter 把 Function URL 事件桥成 HTTP
    backend/agent/          纯 Python agent 包 (loop / tools / checkpoint / llm)
      │            │                 │
      │            │                 └─ Bedrock Converse API: converse_stream + toolConfig
      │            │                    (Claude au.* profile / Nova, 数据留在澳洲; IAM, 无 secret)
      │            └─ DynamoDB 单表: AGENT#… / MEMORY / AGENTQUOTA#…
      └─ 终结工具: create_job + states:StartExecution ──► 现有生成状态机
```

一次调用 = 一轮对话。没有常驻进程：空闲时成本为零，状态全部在 DynamoDB，
「续跑」不再是异常路径而是每一轮的正常路径 —— 每轮都从 T-item 重建上下文。

三层与三条边界（每层只知道自己下面那层的协议，不知道上面那层的存在）：

| 层 | 目录 | 自己写的是什么 | 依赖什么 | 不知道什么 |
|---|---|---|---|---|
| **第 1 层 · LLM API** | `native/llm/` ｜ `langgraph/model.py` | 自建：`converse_stream` 的请求组装、流式事件解析（分片 toolUse JSON 累加）、错误分类与退避、缓存断点摆放 ｜ 框架：`ChatBedrockConverse` 的配置与 `bind_tools` | boto3 ｜ `langchain-aws` | 工具做什么、会话是什么 |
| **第 2 层 · loop** | `native/loop.py` ｜ `langgraph/graph.py` | 自建：上下文重建、工具并行分发、收敛策略、deadline ｜ 框架：`StateGraph` + `ToolNode` + 条件边 + `astream_events` | 第 1 层 + 共用的 `tools/`、`prompt.py`、`budget.py` | HTTP、FastAPI、Lambda |
| **共用 · 第 2 层的「决定」** | `backend/agent/` 顶层 | 工具的业务语义与输入模型、系统提示、收敛阈值、T-item checkpoint 格式与 fencing token | `shared/db.py` | 用的是哪个引擎 |
| **第 3 层 · harness** | `backend/agent_runner/` + `infra/stacks/agent_stack.py` | 调用生命周期（超时预算、未提交轮的重发语义）、JWT 校验、SSE 传输、并发与配额闸门、指标与日志、Function URL / OAC / IAM / 并发上限 | 第 2 层的 `run_turn()` | LLM 调用细节 |

第 3 层里有一个外来件要点名：**Lambda Web Adapter（LWA）**。Python 托管运行时不原生支持 response
streaming，LWA 是 AWS 提供的一个 Rust 二进制（作为 layer 或 COPY 进镜像），把 Function URL 的调用桥成
对本地 HTTP 服务的请求、把响应以流式写回。它与现有 API Lambda 用的 Mangum 同类 —— 传输适配器，
不写 loop、不管生命周期、不碰状态 —— 所以不违反三层自建。若要做到 100% 自建，第二阶段可以换成
自写的 Runtime API streaming `bootstrap`（`provided.al2023`，约 120 行），本文把它列为可选项而非第一版。

「同一个 loop 跑在别的宿主上」的演示反过来了：第一版就是 Lambda；第二阶段若想讲
「长活进程 vs 按调用」的权衡，再加一个 `backend/agent_runner_fargate/`（同一个 FastAPI 应用，
去掉 LWA 直接 uvicorn）。

---

## 2. 数据模型（单表原地扩展，不加 GSI）

沿用 `PK = USER#<sub>`，新增三类 SK：

| SK | 用途 | 关键字段 |
|---|---|---|
| `AGENT#<session_id>` | 会话头 | `status` (ACTIVE \| FINALIZED \| ABANDONED \| FAILED)、`turn` (int, 已完成轮数)、`engine` (native \| langgraph, 会话内不变)、`model_id`、`created_at`、`updated_at`、`job_id` (FINALIZED 后)、`usage` (累计 input/output/cache_read tokens)、`expires_at` (TTL 30 天) |
| `AGENT#<session_id>#T<0001>` | 每轮 checkpoint | `turn`、`user_text`、`assistant_content` (完整 content blocks，JSON)、`tool_calls` (name + input + result + is_error)、`usage`、`stop_reason`、`expires_at` |
| `MEMORY` | 跨会话记忆 | `insights: [{text, created_at, session_id}]`（上限 20 条，FIFO）、`updated_at` |
| `AGENTQUOTA#<yyyy-mm>` | 月度会话配额 | `sessions` (原子计数)、`expires_at` (TTL 62 天) |

设计要点：

- **每轮一个 item，而不是把整段 transcript 塞进会话头。** DynamoDB 单 item 400 KB 上限；
  per-turn item 让大小天然有界，续跑 = `Query begins_with(SK, "AGENT#<sid>#T")` 后按 `turn` 重建 messages。
- **`turn` 是 fencing token。** 每轮开始时对会话头做条件更新
  `status = ACTIVE AND turn = :expected AND attribute_not_exists(in_flight)` → 置 `in_flight = <ts>`；
  轮末写 T-item 与 `turn = turn + 1` 在同一 `TransactWriteItems`，条件同样带 `turn = :expected`。
  一次超时前没写完的调用、或客户端重发造成的迟到僵尸写入，条件失败即止 ——
  与 `db.py` 现有的 `describe_started_at` attempt-token 是同一个模式，读代码的人一眼认得出。
  `in_flight` 超过 **3 分钟**视为死亡，允许下一次 claim（Lambda 超时 120 s，加余量；
  与 `PICTURE_DESCRIBE_TIMEOUT_SECONDS` 同一套思路）。
- **transcript 与 insights 是用户内容。** 遵守约束 7 的写法：只在 item 上，永不进 INFO 日志、
  永不进状态机 payload。日志只记 `session_id / turn / tool 名 / token 计数 / stop_reason`。
- **配额用独立 item 而不是改 `ENTITLEMENT`。** `AGENTQUOTA#<yyyy-mm>` 上 `ADD sessions :one`，
  条件 `sessions < :cap`（默认 30/月，env `AGENT_SESSIONS_PER_MONTH`）。ENTITLEMENT 不动，
  约束 1 的事务索引布局（index 0 = ENTITLEMENT, index 1 = JOB）不受影响。
- 所有读写都加进 `shared/db.py`（`create_agent_session / claim_turn / commit_turn /
  list_turns / get_memory / append_insight / clear_memory / reserve_agent_session`），
  agent 包只依赖 `EntitlementStore`，不写裸 boto3。

JOB item 的连带改动：新增 `source: "agent"` 与 `agent_session_id` 两个可选字段。
定稿 brief 存在 **`mood_text`** 里（它就是这个 job 「drift from」的文字），
`generate_script` 与 dreamscapes 列表零改动即可工作；`GenerateRequest.mood` 的 500 字上限
只约束 HTTP 入口，工具 schema 自己给 brief 定 1200 字上限。

---

## 3. agent 包：`backend/agent/`

```
backend/agent/
  __init__.py
  contracts.py     AgentEngine 协议: run_turn(TurnInput, *, deadline, emit) -> TurnResult
                   中性消息格式 = Converse 的 content block(text / toolUse / toolResult); 事件 TextDelta/ToolStart/Final
  tools/           两套引擎共用 —— 工具 = Pydantic 输入模型 + handler(ctx, input) + 描述
    registry.py    ToolSpec + ToolContext(user_id, store, sfn) ; to_converse_spec() / to_langchain_tool()
    history.py     get_session_history
    memory.py      save_user_insight
    finalize.py    finalize_meditation_brief (终结工具)
    choices.py     offer_choices (第二阶段, 客户端执行)
  prompt.py        SYSTEM_PROMPT + 记忆块渲染 + 收敛提示文案(共用)
  budget.py        轮数上限、收敛阈值、token 计数(共用)
  checkpoint.py    TurnCheckpoint / SessionState 的 Pydantic 模型 + 与 db.py 的桥(共用; 引擎不直接写库)
  native/          自建引擎(默认). 不 import langchain*
    llm/base.py    LLMProvider 协议
    llm/converse.py BedrockConverseProvider
    llm/mantle.py  BedrockMantleProvider (第二阶段)
    loop.py        run_turn(): 用户消息 → (LLM ↔ 工具)* → TurnResult
  langgraph/       框架引擎. 只在这个包里允许 import langchain_core / langchain_aws / langgraph
    model.py       ChatBedrockConverse 构造(同一个 AGENT_MODEL_ID / cachePoint / guardrail 配置)
    adapters.py    中性消息 ⇄ HumanMessage/AIMessage(tool_calls)/ToolMessage ; ToolSpec → StructuredTool
    graph.py       StateGraph: agent 节点 → tools_condition → ToolNode → agent…; 收敛与 deadline 做成条件边
    engine.py      LangGraphEngine.run_turn(): astream_events(v2) → 同一套 emit 事件 → TurnResult
    saver.py       (第二阶段) BaseCheckpointSaver 的 DynamoDB 实现, 供 interrupt() 跨调用恢复
```

### 3.1 LLM 层：Bedrock 上的四条路与决定

| 路 | 是什么 | 满足「直接操作 LLM API」？ | 模型 / 区域 | 对本项目的利弊 |
|---|---|---|---|---|
| **A. Converse API**（`bedrock-runtime` `converse_stream` + `toolConfig`） | AWS 的统一消息接口：`toolUse` / `toolResult` content block、`toolChoice`（auto/any/tool）、`cachePoint`、`guardrailConfig`、流式 `contentBlockDelta` | **是**：自己写 loop、自己解析流式 tool 输入、自己回传 toolResult | ap-southeast-2：Nova Lite/Pro 按需在悉尼；Claude ≤ 4.6（Sonnet 4.6 / Haiku 4.5）经 `au.*` profile，数据只在悉尼+墨尔本 | 与管线现有 Bedrock 代码同一套（`raise_for_bedrock_error`、`_bedrock_resources`、IAM 授权形状）；模型无关，一个 env 在 Claude 与 Nova 间切换；Guardrails 原生接入。不支持 strict schema 与 mid-conversation system message |
| **B. Claude Messages API on Bedrock（Mantle）** `https://bedrock-mantle.ap-southeast-2.api.aws/anthropic/v1/messages` | AWS 托管的 Anthropic Messages API 端点，请求体与第一方一致，Python 用 `AnthropicBedrockMantle(aws_region="ap-southeast-2")`，SigV4 鉴权，IAM 动作 `bedrock-mantle:CreateInference` | **是**，且是 Anthropic SDK 的原生形状（`messages.stream`、`tool_use` block、`cache_control`） | Sydney 有 Global 与 **AU** 两种端点；模型 `anthropic.claude-sonnet-5` / `claude-haiku-4-5` / `claude-opus-4-8` 等（4.7+ 只在这条路上） | 简历上直接写「Anthropic Messages API」；能用最新 Claude。只有 Claude；不支持 structured outputs / server-side fallback / mid-conversation system message / Batches；区域定向端点约 +10% 溢价；对本项目是一套**新的**客户端与 IAM 形状 |
| C. Bedrock AgentCore（Runtime + Memory + Gateway + Identity） | **第 3 层的托管 harness**：任意容器跑在按会话隔离的 microVM 里，8 小时会话，流式响应，Cognito JWT 入站授权器，Memory 服务做短/长期记忆；Sydney 已 GA，按用量计费 | 不决定 LLM 调用方式（容器里仍用 A 或 B） | — | **不用**：它替我们写第 3 层 —— 进程生命周期、鉴权、记忆、观测都被接管，正是本功能要亲手做的部分。记录在此只为说明「知道它存在、知道为什么不用」 |
| D. Bedrock Agents（经典托管 agent）/ Strands Agents SDK | Bedrock Agents 把第 2、3 层一起藏成黑盒；Strands 是第 2 层的库 | **否** | — | **不用**，理由同上：loop 必须是自己的 |

**决定：A 为第一版的第 1 层，B 作第二阶段的第二个 provider（仍是第 1 层，只是换端点）；C、D 不用。**

- 选 A 而不是 B 做第一版，是因为它**复用**而不是**新增**：管线的错误分类、`au.` profile 的 IAM ARN 展开、
  Bedrock 的可观测性都已经在仓库里；模型无关意味着「Claude ↔ Nova 一个 env 切换」这个演示零成本；
  Guardrails 是 AWS 原生的安全层（§3.3）。面试里的叙事是「loop 写在 toolUse/toolResult 协议之上，
  与模型无关」。
- B 的价值是「Anthropic SDK 的原生形状」和最新 Claude。因为 `LLMProvider` 协议在，它是第二个 ~150 行的实现，
  不是第二套架构 —— 这正是 provider 抽象存在的理由，也是「multi-provider LLM layer」叙事的闭环。
- C 不用，任何阶段都不用：第 3 层是自建目标的一部分（§0.5）。面试里被问到「为什么不用 AgentCore」，
  答案就是本文 §5 的那张路由表加 §2 的 fencing token —— 那是我们自己写的 harness 在做同样的事。
- 模型默认 **Nova Lite**（`amazon.nova-lite-v1:0`，悉尼按需；env `AGENT_MODEL_ID` 可换成
  `au.` 前缀的 Claude profile，精确 id 以 `aws bedrock list-inference-profiles --region ap-southeast-2` 为准）。
  **2026-08-25 拍板**，依据是 A3 的 evals：Nova Lite 在 20 条用例上 19/20——10 条危机/边界全过
  （固定文案、零工具调用、危机后可回到冥想）、brief 不泄露个人细节、偏好会被记住；唯一未过的
  `no-history-when-memory-exists`（有笔记时仍多查一次历史）是效率偏好而非正确性问题，接受为已知边界。
  每轮 ≈ 1 s、缓存命中 80–97%、全量 evals ≈ US$0.01。Claude 作为升级选项保留，切换只是一个 env。

`BedrockConverseProvider` 的调用形状（boto3，`asyncio.to_thread` 包一层；事件流同步迭代）：

```python
response = bedrock.converse_stream(
    modelId=self.model_id,                                  # au.anthropic.claude-sonnet-4-6-… / amazon.nova-lite-v1:0
    system=[
        {"text": SYSTEM_PROMPT},
        {"cachePoint": {"type": "default"}},                # 静态前缀到此为止
        {"text": memory_block},                             # 每用户不同, 会话内稳定
    ],
    messages=messages,                                      # [{"role","content":[{"text"}|{"toolUse"}|{"toolResult"}]}]
    toolConfig={
        "tools": [{"toolSpec": {"name", "description", "inputSchema": {"json": schema}}} for ...],
        **({"toolChoice": {"tool": {"name": "finalize_meditation_brief"}}} if force_finalize else {}),
    },
    inferenceConfig={"maxTokens": 4096, "temperature": 0.7},
    **({"guardrailConfig": {"guardrailIdentifier": gid, "guardrailVersion": gver,
                            "streamProcessingMode": "async"}} if gid else {}),
)
for event in response["stream"]:
    # contentBlockStart → toolUse {name, toolUseId}; contentBlockDelta → text 或 toolUse.input (partial JSON, 需累加)
    # contentBlockStop → 一个 block 结束(此时把累加的 JSON 解析成 dict);
    # messageStop → stopReason: end_turn | tool_use | max_tokens | guardrail_intervened | content_filtered
    # metadata → usage {inputTokens, outputTokens, cacheReadInputTokens, cacheWriteInputTokens}
```

与第一方 API 相比要知道的差异（都已在 loop 设计里兜住）：

- **没有 `strict`**：工具输入以 Pydantic 校验为准，失败回 `toolResult` `status: "error"` 让模型重试。
- **没有 mid-conversation system message**：收敛提示拼进当轮 user 消息（§3.2）。
- **缓存**：`cachePoint` 放在 system 与 messages 里；Claude 也接受放在 tools 里，Nova 不接受 —— provider
  按模型家族决定放哪。命中数从 `metadata.usage.cacheReadInputTokens` 读，记进 T-item。
- **thinking**：对话轮次不开（Claude 4.6 经 `additionalModelRequestFields` 可开，Nova 无）；不需要。
- **拒答**：`stopReason == "guardrail_intervened" | "content_filtered"` → 本轮以固定安全文案结束，不重试。
- **错误**：`ClientError` 经 `shared/pipeline.raise_for_bedrock_error` 分成 transient / permanent；
  transient 由 provider 自己按 2/4/8 s 退避重试至多 3 次（管线里这件事由 Step Functions 做，
  这里没有状态机，所以 provider 自己做），permanent 直接失败。任何异常都不推进 `turn`。
- **IAM**：`bedrock:InvokeModel` **与** `bedrock:InvokeModelWithResponseStream`，资源用
  `pipeline_stack._bedrock_resources` 的展开规则（把它移到 `infra/stacks/bedrock.py` 供两栈共用）。

`BedrockMantleProvider`（第二阶段）：`AnthropicBedrockMantle(aws_region="ap-southeast-2")`，
`client.messages.stream(model="anthropic.claude-sonnet-5", ...)`，`cache_control` 打在 system 块上；
IAM 动作 `bedrock-mantle:CreateInference`。区域定向（AU）端点与 Global 端点的取舍：Global 无溢价但可能
路由出境，本项目只能用 AU。

### 3.2 loop

```text
# 伪代码
# 第 3 层 (agent_runner): 落库与闸门在这里, 引擎不碰数据库
async def handle_turn(user_id, session_id, user_text, *, engine, store, emit, deadline):
    session = store.claim_turn(user_id, session_id)            # 条件: ACTIVE ∧ 无在途 ∧ engine 匹配; 失败 → 409
    history = rebuild_messages(store.list_turns(user_id, session_id))
    result = await engine.run_turn(TurnInput(history, user_text, session.turn), deadline=deadline, emit=emit)
    store.commit_turn(user_id, session_id, expected_turn=session.turn,
                      checkpoint=TurnCheckpoint.from_result(result))   # T-item + turn+1, 同一事务
    emit(Done(turn=session.turn + 1, job_id=result.finalized and result.finalized.job_id))

# 第 2 层 (agent/native/loop.py): 纯计算, 输入历史、输出本轮结果
async def run_turn(inp: TurnInput, *, deadline, emit) -> TurnResult:
    messages = inp.history + [user(inp.user_text, hint=converge_hint(inp.turn))]
    tool_choice = converge_policy(inp.turn)                    # None / 强制 finalize
    tool_log, finalized = [], None
    for _ in range(MAX_TOOL_ITERATIONS_PER_TURN):              # 4
        if deadline.exhausted(): tool_choice = NO_MORE_TOOLS   # 让模型直接作答
        final = await stream_and_emit(llm, messages, tools, tool_choice, emit)
        if final.stop_reason != "tool_use":
            break
        results = await execute_all(final.tool_use_blocks, tools)   # 并行执行; 一条 user 消息回传全部 toolResult
        messages += [assistant(final.content), user(results)]
        tool_log += results
        if (f := results.finalized()):                         # 终结工具成功 => 本轮即最后一轮
            finalized = f
            break
    return TurnResult(content=final.content, tool_log=tool_log, usage=..., stop_reason=..., finalized=finalized)
```

- **并行工具调用**：一条 assistant 消息可能带多个 `tool_use`，全部执行后放进**同一条** user 消息回传；
  工具异常 → `tool_result` 带 `is_error: true` 而不是丢弃（否则模型会停止并行调用）。
- **收敛策略** (`budget.py`)：`MAX_TURNS = 12`。第 9 轮起把一段固定的收敛提示
  （「请在两轮内把对话收敛成 brief」）拼在当轮 user 消息末尾 —— 放在尾部，缓存前缀不受影响；
  第 12 轮直接 `toolChoice={"tool": {"name": "finalize_meditation_brief"}}` 强制终结
  （Claude 与 Nova 都支持 `tool` 模式）。
- **工具输入一律经 Pydantic 校验**（流式 `toolUse.input` 是分片 JSON，累加到 `contentBlockStop` 再解析，
  不做字符串匹配）。Converse 没有 `strict`，所以 Pydantic 就是契约：校验失败回
  `toolResult` `status: "error"` 附字段级原因，让模型重试一次；`finalize` 的 brief 由此约束成型。
- **checkpoint 只在轮末写一次。** 轮内工具调用的中间态不落库 —— 一轮是原子的，
  超时就整轮重来（工具本身幂等，见 §4），这是比「每个工具调用都落库」简单得多、且足够的粒度。
- **时间预算**：`run_turn` 接收一个 `deadline`（宿主从 Lambda 剩余时间减 10 s 得到）；
  每次 LLM 往返前检查，不够就不再发起新的工具迭代，改为让模型直接作答（`toolChoice: auto` +
  一句「不要再调用工具」拼进 user 消息）。轮内 ≤ 4 次工具迭代 × 单次 ≤ 20 s，在 120 s 内绰绰有余。

### 3.3 系统提示（`prompt.py`）

固定不变的部分（可缓存）：

- 身份：冥想引导助手，不是心理咨询；用温和、简短的口吻，每次回复 ≤ 3 句，一次只问一个问题。
- **危机策略写死**：出现自伤/伤人/危机信号时，用固定文案引导求助专业资源
  （澳洲：Lifeline 13 11 14、Beyond Blue、000），**不展开「治疗性」对话**，不调用任何工具，
  不 finalize。
- 隐私：不复述用户的姓名、地点、人物、事件；brief 里只写「感受与需要」，与 `generate_script`
  现有 SYSTEM_PROMPT 的规则一致。
- 工具使用规则：开场先 `get_session_history`（如果记忆块为空）；只有在用户明确表达了偏好时才
  `save_user_insight`；用户确认后再 `finalize`。

每用户变化的部分（记忆块）：`MEMORY.insights` 渲染成一段「你之前记下的」；为空时是一句固定占位，
保证 system 的第二块也稳定。

**Bedrock Guardrails 作为第二层**（env `AGENT_GUARDRAIL_ID` 未设则关闭）：CDK 建一个 guardrail，
denied topic 只配「医疗/心理**诊断与治疗方案**」并作用于**输出**，拦下模型越界给建议的情况；
危机应答**不**交给 guardrail —— 用一段固定文案替代模型回复会把正在求助的人挡在门外，
prompt 层的策略（引导资源、不展开）才是对的粒度。价格 $0.15 / 1000 text units（一个 text unit ≤ 1000 字符），
只开 denied topics 一项，按每会话 ~15k 字符算 ≈ $0.002/会话，可忽略。IAM：`bedrock:ApplyGuardrail`。

---

### 3.4 双引擎：自建 vs LangGraph，共用什么、对比什么

**切分原则**：凡是「决定」（工具的业务语义、系统提示、收敛阈值、落库格式、fencing token）只有一份，
在 `backend/agent/` 顶层；凡是「机制」（怎么调模型、怎么解析流、怎么分发工具、怎么组织 loop）有两份，
在 `native/` 与 `langgraph/`。harness（第 3 层）只认 `AgentEngine` 协议，按 env `AGENT_ENGINE=native|langgraph`
选一个。两套引擎接收同样的 `TurnInput`（重建好的中性历史 + 用户文本 + 轮号），返回同样的 `TurnResult`
（本轮 assistant content、工具调用日志、usage、stop_reason、finalized），**由 harness 写 T-item** ——
引擎不碰数据库，所以两套引擎产出的落库记录天然同构，可以逐字段比对。

概念对照（也是将来 `docs/agent-engines-compared.md` 的骨架）：

| 概念 | 自建 `native/` | 框架 `langgraph/` | 对比时看什么 |
|---|---|---|---|
| 模型调用 | `BedrockConverseProvider.stream_turn()`：手写请求、手解析 `contentBlockDelta` | `ChatBedrockConverse(model_id, …)` + `.bind_tools(tools, tool_choice=…)`；流式由 `astream` 给出 `AIMessageChunk` | 框架替你合并了分片 toolUse JSON；代价是 `cachePoint` / `guardrailConfig` 要经 `additional_model_request_fields` 之类的旁路传，能否精确落位要验证 |
| 工具定义 | `ToolSpec` → `toolSpec.inputSchema.json`（Pydantic `model_json_schema()`） | `ToolSpec.to_langchain_tool()` → `StructuredTool.from_function(args_schema=Model)`；上下文用闭包绑定 | 同一份 Pydantic 模型两处复用，schema 必须逐字节一致（契约测试断言） |
| loop | `for _ in range(MAX_TOOL_ITERATIONS)` 显式循环 | `StateGraph(MessagesState)`：`agent` 节点 → `tools_condition` → `ToolNode(tools)` → 回到 `agent` | 显式循环 vs 图；图的 `recursion_limit` 与我们的迭代上限如何对应（`2 × 4 + 1`） |
| 并行工具 | `asyncio.gather` 后一条 user 消息回传全部 toolResult | `ToolNode` 默认并行执行同一条 AIMessage 里的多个 tool_calls | 行为一致；观察 ToolMessage 的顺序 |
| 收敛 / 强制终结 | `budget.converge_policy(turn)` → 拼提示 / `toolChoice: {tool: …}` | 同一个 `converge_policy`；实现为 `agent` 节点里按轮号换 `tool_choice`，第 12 轮 `bind_tools(..., tool_choice="finalize_meditation_brief")` | 「决定」共用、机制不同 —— 这正是切分原则的体现 |
| deadline | 每次 LLM 往返前检查 | 条件边 `should_continue` 里检查，超时则路由到 `END` | 图的可视化（`get_graph().draw_mermaid()`）在这里第一次真正有用 |
| 流式事件 | provider 事件流直接 `emit` | `graph.astream_events(version="v2")`：`on_chat_model_stream` → `delta`，`on_tool_start` → `tool` | 事件粒度与延迟；LangGraph 的事件里含大量无关节点事件，要过滤 |
| checkpoint | T-item 就是恢复状态 | 第一版 `MemorySaver`（调用内），跨调用恢复靠 harness 从 T-item 重建；第二阶段自写 `BaseCheckpointSaver`（`saver.py`，存 `AGENT#<sid>#LG#<checkpoint_id>`） | 两种粒度：按轮（我们）vs 按超步（LangGraph）；item 大小、写放大、能否支持 `interrupt()` |
| human-in-the-loop | `offer_choices` = 客户端工具，结果作为 toolResult 回传（第二阶段） | `interrupt()` + `Command(resume=…)`，需要持久 saver（第二阶段） | 这是 LangGraph 最有卖点的能力，也是我们自建版本最能说明「为什么不需要框架」的地方 |
| 错误分类 / 重试 | `raise_for_bedrock_error` + provider 自己退避 | `ChatBedrockConverse` 走 boto3 默认重试；节点级 `RetryPolicy` 可加 | 谁在重试、重试几次、是否可观测 |

**边界纪律**（用一个 `tests/test_engine_isolation.py` 守着）：`backend/agent/native/` 与 `backend/agent/` 顶层
任何文件 import `langchain*` / `langgraph*` 即失败；反向不限。依赖放在 `pyproject.toml` 的
`agent-langgraph` extra（`langchain-core`、`langchain-aws`、`langgraph`），只装进 agent 镜像，不进 shared layer。

**部署形态**：同一镜像起两个 Lambda（`AgentFunction` env `native`，`AgentFunctionLangGraph` env `langgraph`），
站点分发两条 behavior `/agent/*` 与 `/agent-lg/*`；零常驻，所以两个函数的空闲成本仍是 0。
会话头记录 `engine`，一个会话从头到尾用同一个引擎；T-item 与 EMF 指标都带 `Engine` 维度，
**对比数据（轮延迟、token、缓存命中、工具错误）从上线第一天起自动积累**。PWA 默认 `native`，
`/companion?engine=langgraph` 切换（写进会话），仅此一处感知引擎。

**对比学习的产出**：`docs/agent-engines-compared.md`（里程碑 L3），按上表逐项写「同一场景两套实现的代码
片段 + 观察到的差异 + 各自的坑」；这份文档本身就是面试材料。

## 4. 工具集：真实语义、幂等、最小

| 工具 | 输入 (strict) | 做什么 | 幂等性 |
|---|---|---|---|
| `get_session_history` | `{limit: 1..10}` | `store.list_done_jobs` → 取最近 N 条的 `created_at / duration_minutes / picture_keywords / mood 摘要(≤60 字)` | 只读 |
| `save_user_insight` | `{insight: str ≤ 120}` | `store.append_insight`，同文本去重，上限 20 条 FIFO | 同文本重复调用无副作用 |
| `finalize_meditation_brief` | `brief: str`（40..1200）、`duration_minutes: int`（3..30） | **提议，不花钱**（A4b 改）：① `generation_gate`：`NO_CREDIT` / `JOB_IN_FLIGHT` → error 结果，让模型据此和用户说话；② `store.set_pending_brief()` 把 brief 与时长放到会话头（覆盖旧提议）；③ 返回 `{"status": "awaiting_confirmation", "duration_minutes"}` + `proposal`，loop 发 `ProposalReady` 事件，轮次**继续**——模型再说一句「准备好了，你可以开始或修改」。真正的启动在 `POST /agent/sessions/{id}/confirm`（`agent_runner.turns.confirm_session`）：同一把 claim 锁、同一个 gate、同一个 `start_generation()`，`job_id = uuid5(namespace, session_id)`，会话 FINALIZED 但 `turn` 不推进 | 提议幂等（覆盖）；确认幂等（同 job_id、`ExecutionAlreadyExists` 吞掉；已 FINALIZED 直接返回 job_id） |
| `offer_choices`（第二阶段） | `{question, options: 2..4}` | **客户端执行的工具**：SSE 把选项推给 PWA 渲染成 chips，用户点选后作为 `tool_result` 回传。这是「带着你走」的载体，也是 harness 里最有讲头的模式（tool result 来自人） | — |

对话里提的 `preview_scene_options / estimate_duration` 是纯函数、没有真实副作用，
放进工具集会稀释含金量，**不做**。`recall_insights` 也不需要：记忆在会话开始时已注入 system。

**没有任何工具会花钱**（A4b）：模型只能提议；credit 只在用户在 app 里点了确认、`confirm` 路由调用 `start_generation()`
之后才由状态机冻结。门禁在提议时与确认时各查一次，都与 `POST /generate` 一致（`available >= 1`、`frozen == 0`）。
这是 §0.5「花钱的决定在我们的代码里」的直接体现：A4 冒烟里 Nova Lite 曾在用户未同意时直接终结并扣费，两段式之后
模型的判断不再能触发扣费。

---

## 5. 宿主：`backend/agent_runner/`（FastAPI + SSE，跑在 Lambda 上）

| 路由 | 行为 |
|---|---|
| `GET /health` | 无鉴权；CloudFront 不暴露它（behavior 只放行 `/agent/*`），留给本地与 smoke |
| `POST /agent/sessions` | 门禁：`plan == "pro"`（否则 403 `plan_required`）；配额：`reserve_agent_session` (`sessions < cap`，否则 429)；读 MEMORY；返回 `{session_id, turn: 0, insights_count}` |
| `POST /agent/sessions/{id}/turns` | body `{text ≤ 1000}`；返回 `text/event-stream`。事件：`delta {text}` / `tool {name}`（只有名字，不带参数 —— 参数是用户内容）/ `proposal {duration_minutes}`（模型提议了一份 brief，等用户确认）/ `done {turn, job_id, awaiting_confirmation}` / `error {code, retryable}`。每 15 s 发一条 `: ping` 注释行保活。一条新消息会撤回上一轮的提议。409 = 已有在途轮或会话非 ACTIVE（`busy_or_closed`）/ 轮数用尽（`session_exhausted`）；credit 不足是工具结果，不是 HTTP 错误 |
| `POST /agent/sessions/{id}/confirm` | **唯一会花钱的请求**：读会话头的 `pending_brief` → 同一把 claim 锁 → `generation_gate` → `start_generation()` → 会话 FINALIZED。200 `{job_id}`（重复确认返回同一个 job_id）；409 `nothing_to_confirm` / `busy_or_closed` / `job_in_flight`；402 `no_credit`；503 `start_failed`（claim 已释放，可重试） |
| `GET /agent/sessions/{id}` | 重建 transcript（刷新页面用）：`{status, turn, job_id, pending: {brief, duration_minutes} \| null, turns: [{turn, user_text, assistant_text, tools}]}`；不存在/过期 → 404 |
| `POST /agent/sessions/{id}/abandon` | 状态 → ABANDONED（幂等） |
| `GET /agent/memory` / `DELETE /agent/memory` | 查看 / 一键清除。清除后下一轮重建 system 即生效 |

- **harness 只认 `AgentEngine` 协议**：启动时按 `AGENT_ENGINE` 构造 `NativeEngine` 或 `LangGraphEngine`，
  之后所有路由、落库、指标代码都不知道引擎是哪个；会话头的 `engine` 与函数的 env 不一致时 409（防止一个
  会话被两套引擎交替处理）。
- **一次调用 = 一轮。** 应用本身仍是普通的 FastAPI（本地 `uvicorn` 直接跑、测试用 `httpx.AsyncClient` 打），
  Lambda 上由 LWA 把 Function URL 事件桥成 HTTP、把 SSE 以 response streaming 写回
  （`AWS_LWA_INVOKE_MODE=response_stream`）。没有 SIGTERM、没有优雅退出：一轮要么在 120 s 内 commit，
  要么超时 —— 超时不推进 `turn`，`in_flight` 3 分钟后过期，客户端重发同一条消息即可。
- **JWT 校验在函数内做**：Function URL 的 IAM 鉴权只证明「请求来自 CloudFront」，用户身份仍要验。
  用 `PyJWT[crypto]` 按 Cognito JWKS（冷启动时拉取、按 `kid` 缓存于模块级）校验签名、`iss`、`aud`、`exp`，
  再套用与 `api/deps.py` 完全相同的 claims 规则（`token_use == "id"`、必须有 `sub`）。
  单独放 `agent_runner/auth.py`，测试用本地生成的 RSA 密钥对。
- **ID token 走 `X-Id-Token` 头**（A5 发现）：CloudFront OAC 签名会覆盖 viewer 的 `Authorization`，bearer token 永远到不了函数；`Authorization: Bearer` 仅作本机 uvicorn 的回退。前端（§8）的 `agent.ts` 用这个头。
- **不用 `EventSource`**：它不能带 Authorization 头。前端用 `fetch` + `ReadableStream` 手工解析 SSE（§8）。
- **CORS**：走站点分发的同源 behavior（§6），函数本身不需要 CORS；本地 dev 由 vite 代理 `/agent` 到 `localhost:8080`。
- **冷启动**：容器镜像 + Python ≈ 1–2 s，落在第一条 `delta` 之前；前端在收到首个事件前显示「正在想」。
  不买 provisioned concurrency —— 那是常驻成本。JWKS 与 boto3 客户端都在模块级懒初始化，热调用不重做。
- DynamoDB 仍用同步 `boto3`（复用 `EntitlementStore`），在 `asyncio.to_thread` 里调。
- **观测是 harness 的职责**：结构化 JSON 日志（`session_id / turn / tool / stop_reason / latency_ms`，
  永不含用户内容）；CloudWatch EMF 指标 `AgentTurns`、`ToolErrors`、`LLMTransientRetries`、
  `InputTokens/OutputTokens/CacheReadTokens`、`TurnLatency`；Lambda `Errors`、`Throttles`（并发上限撞顶）
  与 `ToolErrors` 各一条告警。X-Ray 不接 —— 一轮之内没有跨服务链路，日志里的 `turn` 就够了。

## 6. infra：`infra/stacks/agent_stack.py`

新栈 `Meditation-<env>-Agent`，在 Pipeline 之后、Frontend 之前创建（Frontend 要引用它的 Function URL）。
**没有 VPC、没有 ALB、没有 ECS、没有 secret。**

| 资源 | 决定 | 理由 / 代价 |
|---|---|---|
| Lambda ×2 | `DockerImageFunction`，`file="agent_runner/Dockerfile"`（基于 `public.ecr.aws/lambda/python:3.12`，装 `agent-langgraph` extra，`COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:<pinned> /lambda-adapter /opt/extensions/`），512 MB，timeout 120 s，`reserved_concurrent_executions=10`。**同一镜像两个函数**：`AgentFunction`（`AGENT_ENGINE=native`）与 `AgentFunctionLangGraph`（`langgraph`），各自独立的 Function URL 与 LogGroup | 与 API Lambda 同一套镜像构建；并发上限是**成本天花板**（10 × 120 s × 512 MB 最坏 ≈ 每分钟几美分），也是对 Function URL 被滥用的兜底。两个函数空闲成本仍为 0。 |
| Function URL | `auth_type=AWS_IAM`，`invoke_mode=RESPONSE_STREAM` | IAM 鉴权 + OAC ⇒ 只有本账户的 CloudFront 分发能调；`RESPONSE_STREAM` 是 SSE 的前提（API Gateway 做不到：HTTP API 30 s 且不流式，所以这里不走现有 HTTP API）。 |
| CloudFront | 在 **frontend_stack 的站点分发**上新增两条 behavior：`/agent/*` → native 的 Function URL，`/agent-lg/*` → langgraph 的 Function URL（各自 `FunctionUrlOrigin` + `OriginAccessControl`，`lambda` 签名类型）：`CachePolicy.CACHING_DISABLED`、origin request policy 转发 Authorization 与所有 header/query、`origin read timeout 60 s`、允许 `POST/GET/DELETE` | 同源 ⇒ 无 CORS、PWA 直接 `fetch('/agent/...')`。SSE 靠 15 s 心跳不触发 read timeout。OAC 对 POST 要求 viewer 自带 `x-amz-content-sha256`（§8）。 |
| IAM（函数角色） | 表：`GetItem/PutItem/UpdateItem/Query`；`states:StartExecution` 仅生成状态机；`bedrock:InvokeModel` + `bedrock:InvokeModelWithResponseStream` 于 `au.` profile 与其底层模型 ARN（复用 `_bedrock_resources`，移到 `infra/stacks/bedrock.py` 两栈共用）；`bedrock:ApplyGuardrail` 于 guardrail ARN；**无 S3** | 与管线 Lambda 同粒度、同 helper。 |
| Guardrail | `bedrock.CfnGuardrail`（denied topics 一项，输出侧），版本随栈发布；env 未设即关闭 | 见 §3.3；dev 可以不建。 |
| 日志 | 显式 LogGroup，1 个月保留 | 与其他栈一致。 |
| 输出 | `AgentFunctionUrl`（仅 smoke / 排障用；生产流量只经 CloudFront） | |

**成本估算（Sydney，US$/月）**：常驻 **0**。按量：Lambda 512 MB × 平均 15 s/轮 × 12 轮 × 30 会话 ≈
5,400 GB-s/用户/月 ≈ $0.09；Function URL 与 response streaming 无额外单价；CloudFront 请求费忽略不计。
LLM 费用见 §7。—— 「zero idle cost」的叙事得以保留，代价是每轮 1–2 s 的冷启动与 15 分钟的硬上限
（一轮远用不到）。

**infra 测试**（`infra/tests/test_agent_stack.py`）：模板里没有 `AWS::EC2::VPC`、`AWS::ECS::*`、
`AWS::ElasticLoadBalancingV2::*`；函数无 `Secrets` 且环境变量中无 `ANTHROPIC_*`；Function URL `AuthType == AWS_IAM`
且 `InvokeMode == RESPONSE_STREAM`；`ReservedConcurrentExecutions` 存在；函数角色无 `s3:*`；
Bedrock 授权同时含 `InvokeModel` 与 `InvokeModelWithResponseStream` 且资源只在 `ap-southeast-2` / `ap-southeast-4`；
`StartExecution` 只指向生成状态机；站点分发含 `/agent/*` 与 `/agent-lg/*` 两条 behavior、缓存禁用、源为各自的
Function URL 且带 OAC；两个函数只有 `AGENT_ENGINE` 一处不同（其余环境变量、角色策略逐项相等）；
`test_cost_hygiene` 新增断言：没有 provisioned concurrency。

## 7. 模型与成本

| 项 | 第一版决定 |
|---|---|
| 模型 | **Nova Lite**（`amazon.nova-lite-v1:0`，拍板见 §3.1）；env `AGENT_MODEL_ID` 可换 `au.` Claude profile |
| 每会话上限 | 12 轮、每轮 ≤ 4 次工具往返、`maxTokens=4096` |
| 每月上限 | 30 会话 / Pro 用户 |
| 缓存 | system 打 `cachePoint`；期望 `cacheReadInputTokens` 占输入 70% 以上 |

粗算一个「满 12 轮」会话：输入累计 ≈ 40k tokens（~70% 缓存命中，缓存读按 0.1×）、输出 ≈ 4k。

| 模型（Bedrock 按需价，AU profile 可能另有 ~10% 溢价） | 每会话 | 30 会话/月 |
|---|---|---|
| Claude Sonnet 4.6（$3 / $15） | ≈ $0.10–0.12 | ≈ $3–4 |
| Claude Haiku 4.5（$1 / $5） | ≈ $0.035 | ≈ $1 |
| **Nova Lite（$0.06 / $0.24，默认）** | ≈ $0.004 | ≈ $0.1 |

Pro 定价必须覆盖这一块加 20 次生成的 TTS/LLM 成本 —— 这是定价输入，不是工程决定；
`usage` 落在 T-item 上，上线后用真实数据校准。

## 8. 前端（`frontend/src/companion/`）

- 路由 `/companion`（`CompanionPage`）：Pro 用户在 HomePage 多一个入口；非 Pro 点进去看到 Plans 引导。
  `?engine=langgraph` 时 `agent.ts` 的 base path 换成 `/agent-lg`，并把 `engine` 写进 `createSession()`；
  这是前端唯一感知引擎的地方，UI 不变。
- `api/agent.ts`：`createSession()`、`sendTurn(sessionId, text, {onDelta, onTool, signal})`（fetch + ReadableStream，
  手工解析 `event:`/`data:` 行）、`getSession()`、`abandon()`、`getMemory()`/`clearMemory()`。
  与 `client.ts` 共享 `ApiError` / `NotSignedInError` 与取 ID token 的逻辑。
  **每个带 body 的请求附 `x-amz-content-sha256`**（`crypto.subtle.digest('SHA-256', body)` 的 hex）——
  CloudFront OAC 对 Lambda 源的 POST 要求 viewer 提供 payload 哈希，否则源返回 403；封装在 `agent.ts` 的
  `request()` 里，页面代码不感知。
- 会话状态放 `useCompanion()` hook：本地乐观追加用户消息；`delta` 逐字渲染；`tool` 事件渲染成一行轻提示
  （「翻看你之前的冥想…」「记下了」）；`done` 带 `job_id` 时 `navigate('/generating/' + job_id)` ——
  接进现有的 GeneratingPage → PlayerPage 流程，一行不改。
- `error` / 断流：保留输入框内容，提示重发；因为 `turn` 未推进，重发是安全的。
- AccountPage 增加「它记得的」列表与「全部忘掉」按钮（调 `DELETE /agent/memory`，二次确认）。
- 第二阶段 `offer_choices`：`tool` 事件带 `options` 时渲染 chips，点选后调 `POST .../turns` 并以
  `{tool_result: {tool_use_id, choice}}` 体回传（runner 把它作为 `tool_result` 续跑本轮）。
- 测试：vitest 覆盖 SSE 解析器与 hook 的状态机；Playwright 一条端到端（mock runner）。

---

## 9. 合规、隐私与 `CLAUDE.md` 修订

必须做的产品/合规事项：

1. **边界**：系统提示里的危机策略（§3.3）+ 一组固定的回归 eval（§10）。
2. **记忆的可见与可删**：`GET/DELETE /agent/memory` + AccountPage 入口；这是信任卖点，也是 Privacy Act 下的必要项。
3. **隐私政策新增章节**：陪伴对话内容与「记忆」的用途、保留期（会话 30 天 TTL，记忆直到用户删除）、
   如何清除。处理地点不变（Bedrock，`au.` profile：悉尼 + 墨尔本），**不需要**境外处理披露。
4. **会话 TTL 30 天**：transcript 是最敏感的用户内容，不需要留更久。

`CLAUDE.md` 需要修订的条款（实施时随代码一起改）：

| 条款 | 现状 | 改为 |
|---|---|---|
| 技术栈 · LLM | 单次 `converse` 调用 | 加：「陪伴 agent 用 Bedrock Converse 的 `converse_stream` + `toolConfig`（Claude `au.` profile 默认、Nova 可切），loop 与 provider 在 `backend/agent/`；永不使用 Global 或 APAC profile」 |
| 约束 2 | 只有 API Lambda 可启动执行 | 加一句：「agent runner 是第二个被允许的启动方，且走同一个 `start_generation()`；credit 仍只在状态机内冻结」 |
| 约束 7 | prompt 与日志不含 PII | 加：「记忆（MEMORY.insights）与 transcript 只注入 agent 会话的 prompt，永不进日志、永不进状态机 payload，存储加密，用户可一键删除」 |
| 单表约定 | 列出 SK | 加 `AGENT#…`、`AGENT#…#T…`、`MEMORY`、`AGENTQUOTA#…` |
| 布局 | — | 加 `backend/agent/`、`backend/agent_runner/`、`infra/stacks/agent_stack.py` |
| 约束 4 秘密清单 | Volcano、Stripe、CloudFront key | 不变 —— agent 不引入新 secret，这一点值得写进 README |
| 硬约束新增 | — | 「陪伴会话不冻结 credit；唯一的花钱动作是 `finalize_meditation_brief`，其门禁与 `POST /generate` 相同」 |

---

## 10. 测试策略

| 层 | 方式 |
|---|---|
| **引擎契约测试**（`tests/agent/test_engine_contract.py`，`parametrize(engine=["native","langgraph"])`） | 同一组脚本化场景（文本 / 单工具 / 并行工具 / 工具报错 / 拒答 / 第 9 轮收敛提示 / 第 12 轮强制 finalize / deadline 逼近），native 用 **FakeProvider**，langgraph 用 `langchain_core` 的 `GenericFakeChatModel`（回放同样的 tool_calls）。断言两套引擎的 `TurnResult` **逐字段相等**（assistant content、工具调用顺序与结果、finalized、stop_reason），以及工具 schema 逐字节一致 |
| `native/loop.py` | moto DynamoDB 下的 harness 集成：checkpoint 写入内容与 `turn` 推进、并行 tool_result 在同一条 user 消息、`is_error` 路径、claim 冲突 409、僵尸写入被条件拒绝 |
| `langgraph/graph.py` | 图结构快照（`get_graph().draw_mermaid()` 存为 fixture，防止无意改图）；`recursion_limit` 与迭代上限对应；`astream_events` 过滤后的事件序列等于 native 的事件序列 |
| 隔离 | `test_engine_isolation.py`：`native/` 与 `agent/` 顶层不得 import `langchain*` / `langgraph*` |
| 工具 | 每个工具的 schema 是 `strict`：用 jsonschema 校验示例输入；`finalize` 的幂等（同会话两次 → 同 job_id）；credit 不足 → `is_error` |
| `BedrockConverseProvider` | `botocore.stub.Stubber` 回放录制的 `converse_stream` 事件序列（文本 / 分片 toolUse JSON / guardrail_intervened / ThrottlingException）；断言请求形状（cachePoint 位置随模型家族、toolChoice、guardrailConfig）与 transient 重试次数；**不在 CI 里打真 Bedrock** |
| runner | `httpx.AsyncClient` 打 FastAPI：JWT 校验用本地 RSA 密钥对；SSE 事件序列；deadline 逼近时不再发起工具迭代；超时（模拟）后 `turn` 未推进且重发同一消息成功 |
| evals（手工） | `backend/tests/agent/evals/`：10 条危机/边界对话 + 10 条正常收敛对话，用 dev 凭证真跑，**两套引擎各跑一遍**，输出「是否调用工具 / 是否 finalize / 回复是否含固定文案 / 轮延迟 / token」的对照表；每次改 prompt 必跑，结果贴 PR |
| infra | §6 列出的断言 |
| 现有 | `ruff`、`pytest`、`cdk synth` 全绿是每个 PR 的门槛（`make check`） |

---

## 11. 里程碑（按序，每一步都可独立合并、独立演示）

1. **A1 · agent 包骨架**（无网络、无 infra）：`llm/base.py`、FakeProvider、`loop.py`、`checkpoint.py`、
   `db.py` 新方法与 moto 测试。完成标准：pytest 全绿，loop 能在 Fake 上跑完一轮并续跑。
2. **A2 · 工具与终结**：三个工具 + `shared/jobs.py::start_generation()` 抽取（`routers/generate.py` 改为调用它）。
   完成标准：本地用 Fake 跑一段脚本化对话后，dev 表里出现 JOB 且状态机被启动（此步需要人工跑，因为会花钱）。
3. **A3 · BedrockConverseProvider + prompt**：真实流式调用、`cachePoint`、Claude/Nova 切换、evals 目录。
   完成标准：`python -m agent.cli` 在本机终端里能和它聊并 finalize（本机用 dev 的 AWS 凭证，
   这一步会花 Bedrock 费用，人工跑）。
4. **A4 · runner 宿主**：FastAPI、JWT、SSE、deadline 预算、Dockerfile（含 LWA）。完成标准：本机
   `uvicorn` 直接跑通；再用 Lambda 镜像本地起容器（LWA 在本地退化为普通 HTTP），用 dev Cognito 的
   ID token curl 出 SSE 流。
5. **A5 · agent_stack + 站点分发 behavior**：Function URL（IAM + RESPONSE_STREAM）、OAC、并发上限；
   synth/diff 通过，infra 测试通过；人工 deploy 到 dev，smoke 一段对话到 GeneratingPage，
   并确认直接 curl Function URL 得到 403（只有 CloudFront 能调）。
6. **A6 · 前端** `/companion` + 记忆管理 + Pro 门禁 + `plan_pro` 产品。
7. **A7 · 文档与合规**：README 架构节、`CLAUDE.md` 修订、隐私政策章节、成本记录到 `docs/deployment.md`。
   —— 到此自建路径端到端可用，是默认引擎。以下是框架路径，**在自建路径合并之后开始**，
   这样契约测试有一个已验证的参照物。
8. **L1 · LangGraph 引擎**：`langgraph/` 四个文件 + `agent-langgraph` extra + 隔离测试；契约测试对两套引擎全绿。
   完成标准：`python -m agent.cli --engine langgraph` 本机聊通并 finalize，T-item 与 native 同构。
9. **L2 · 第二个函数与 behavior**：`AgentFunctionLangGraph`、`/agent-lg/*`、PWA `?engine=` 切换、
   `Engine` 指标维度。完成标准：dev 上两条路径各 smoke 一段对话；CloudWatch 里两条曲线。
10. **L3 · 对比文档** `docs/agent-engines-compared.md`：按 §3.4 的表逐项写代码片段、观察到的差异与坑，
    附 dev 上一周的 `Engine` 维度指标截图。
11. **第二阶段**：`offer_choices` —— 自建版做客户端工具、LangGraph 版做 `interrupt()` + 自写 DynamoDB
    `BaseCheckpointSaver`（两者是对比文档里最重的一节）；`BedrockMantleProvider`；自写 Runtime API
    streaming `bootstrap` 取代 LWA；可选的 `agent_runner_fargate/` 长活变体。

---

## 12. 明确不做的事

- 不做 WebSocket / API Gateway WebSocket API：SSE 已满足单向流式；HTTP API 的 30 s 集成超时且不流式，
  是它不能承载 runner、必须走 Function URL 的原因。
- 不做 ECS / Fargate / ALB / VPC：零常驻成本是拍板项；provisioned concurrency 同理不买。
- 不把 Function URL 设成 `AuthType=NONE`：只有 CloudFront（OAC）能调，加 reserved concurrency 兜底。
- 不用 Strands（第 2 层库）、AgentCore（第 3 层托管 harness）、Bedrock Agents（2+3 层黑盒）、
  Anthropic Tool Runner / Managed Agents、LangGraph Platform / LangServe / LangSmith（第 3 层）：
  自建引擎是默认且完整的（§0.5）。LangChain/LangGraph **只**以第二引擎的身份出现在 `langgraph/` 包内，
  不替代 harness，不渗入 native。boto3 不在此列 —— 它是 API 客户端，`converse_stream` 就是裸 API。
- 不做「一个引擎、两种模式」的折中（比如 native loop 里调 LangChain 的模型类）：那样两边都不完整，
  对比也不成立。
- 不用 Anthropic 第一方 API，也不用 Global / APAC inference profile：数据只在澳洲。
- 不做 server-side compaction：12 轮上限使上下文远小于窗口；真到那一步先加轮数上限而不是加 compaction。
- 不做 GSI；不改 ENTITLEMENT 结构；不在 API Lambda 里加任何 agent 路由（runner 独占 `/agent/*`，边界清晰）。
- 不给 agent 任何 S3 权限：它从不碰音频或图片。

## 13. 已拍板（2026-08-25）

| 问题 | 决定 | 落到哪里 |
|---|---|---|
| Pro 的定义 | 新建 `plan_pro`（订阅，`plan="pro"`，credits 数待定价）；`monthly` 不动 | `api/products.py`；门禁 `plan == "pro"`（§5） |
| LLM 层 | 先做 Converse + `LLMProvider` 抽象；Mantle 是第二阶段的第二个实现 | §3.1 |
| 常驻成本 | **零**：Lambda Function URL（response streaming）+ CloudFront OAC；不做 ECS / ALB / VPC | §1、§5、§6 |
| 双引擎 | 自建为默认；LangChain/LangGraph 作第 1、2 层的第二实现，共用工具/prompt/数据模型/harness，契约测试保证同构 | §0.5、§3.4、§11 L1–L3 |

下一步：里程碑 A1 —— agent 包骨架、FakeProvider、`db.py` 新方法与 moto 测试（§11）。
