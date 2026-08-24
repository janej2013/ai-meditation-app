# Dreamscapes（梦境集）功能实施方案

> 依据 `tmp/cache_list.md` 的需求整理。目标：一个「收藏页」，让用户回看并重播过往生成的冥想。
> 本文是编码前的设计方案，重点回答任务里点名的三个问题：
> (a) 单表上的列表查询（不加 GSI 怎么排序）；(b) 列表里点云缩略图的静态化策略；
> (c) DELETE 的 S3 + DynamoDB 顺序与失败处理。

## 0. 先修正任务描述与现状的两处出入

1. **没有 final.mp3。** 任务文件写「final.mp3 不再过期、narration.mp3 保留 90 天过期」，
   这是浏览器混音改版之前的旧说法。现状是：管线只产出旁白，`audio_key = jobs/{job_id}/narration.mp3`
   就是交付物（BGM 在浏览器里实时混），`jobs/{job_id}/script.txt` 才是中间产物。
   因此生命周期规则应改为：**narration.mp3（交付物）永不过期，script.txt（中间产物）保留 90 天过期**。
2. **纯文字任务没有 keywords。** 设计稿的卡片以关键词为题，但 `picture_keywords` 只有带图任务才有。
   列表接口对文字任务额外返回 `mood_excerpt`（mood_text 截断到 ~40 字符，仅返回给本人，不进日志），
   前端卡片标题优先用 keywords，否则用 excerpt。这是对设计的最小补齐，不改管线。

---

## 1. (a) 列表查询：单表、无 GSI 的排序方案

**现状约束**：`PK = USER#<sub>`，`SK = JOB#<uuid4>`。uuid4 的字典序是随机的，
所以 SK 排序天然不是时间序，DynamoDB 层面无法直接「最新在前」。

**方案：整分区取回 + 应用层排序 + 值型游标。** 不加 GSI，理由是分区规模有硬上界：

- 每个 JOB 都消耗一个付费 credit，重度用户也就是几百条量级；
- 用 `ProjectionExpression` 只取列表需要的轻字段
  （`job_id, #status, picture_keywords, mood_text, duration_minutes, picture_key, created_at`），
  单条约 200–400B，几百条也远小于 DynamoDB 单页 1MB —— 通常一次 Query 就取完。

具体做法（新增只读方法 `db.py :: list_done_jobs(user_id)`，不触碰 credit 事务路径）：

1. `Query`：`KeyConditionExpression: PK = USER#<sub> AND begins_with(SK, 'JOB#')`，
   `FilterExpression: #status = DONE`（减少传输量；正确性不依赖它），循环 `LastEvaluatedKey` 取全。
2. Lambda 内按 `created_at` 降序排序（`job_id` 作第二排序键保证全序稳定）。
3. 游标是**值型**的：`base64("{created_at}|{job_id}")`（最后一条已返回项）。下一页 = 重新取全、
   排序、跳到游标之后取 20 条。值型游标在有条目被删除时依然正确（不像 offset 会漂移）。
4. 响应 `{ items: [...], next_cursor: str | null }`，`limit` 固定 20。

**代价与坦白**：翻页时每页都重扫整个分区。在「分区有界（几百条 × 几百字节）」的前提下这是
一次 eventually-consistent Query（≤0.5 RCU/4KB），比引入 GSI（双倍写费用 + 新索引维护）便宜且简单。
如果未来单用户任务数真的失控，再提「新 JOB 改用 UUIDv7 作 job_id（SK 变时间序）」或专门的 GSI —— 
按 CLAUDE.md 的约定，GSI 需要先提案再加，这里明确**不加**。

## 2. (b) 点云缩略图：一次渲染、共享上下文、位图复用

> **实施备注（2026-08-24）**：读取设计稿源文件后发现,原型的卡片缩略图并不是 WebGL
> 渲染,而是 `thumbField()` —— 以种子 LCG 生成 34 个 radial-gradient 的 **CSS
> background-image**。因此本节的 WebGL 工厂方案整体不需要:实现为
> `frontend/src/dreamscapes/thumb.ts` 的确定性纯函数(jobId 哈希做种子/选色),
> 零上下文、零缓存管理,且与设计稿逐像素同源。下文保留作为决策记录。

**问题**：`ParticleCloud`（frontend/src/scene/ParticleCloud.tsx）是 three.js WebGL 组件，
每个实例一个 WebGL 上下文。浏览器对同页 WebGL 上下文有硬上限（约 8–16 个），列表里 N 张卡
各挂一个实例 —— 即使 `paused` —— 会把旧上下文挤掉、并持续占显存。设计要求是「静态、非动画」。

**方案：单例缩略图工厂，产出位图，卡片只挂 `<img>`。**

1. 新增 `frontend/src/scene/cloudThumbnail.ts`：模块级单例，惰性创建**一个**隐藏的
   ParticleCloud 渲染管线（复用现组件抽出的 boot/几何逻辑，不复制 shader 代码 —— 
   把「建场景 + 渲一帧」从组件里提炼成可复用函数，组件自身改为调用它，保证不出现第二份实现）。
2. `renderCloudThumbnail(jobId, { tint, size }): Promise<string>`：
   以 `jobId` 的哈希为确定性种子摆点（同一张卡每次进来长一样），渲**恰好一帧**，
   `readPixels`/`toDataURL` 导出成 dataURL 后立即释放该帧资源；请求串行排队，
   同一时刻只有一个 GL 上下文存活。
3. 结果按 `job_id` 记忆化：内存 Map + `sessionStorage`（dataURL 很小，128×128 足够）。
   列表滚动、翻页、返回都不重渲。
4. 卡片渲染 `<img src={dataURL}>`，色调 tint 用现有 theme token 以 CSS 叠加实现
   （设计稿的 tinted 效果放 CSS 层，不进 shader，便于随主题变化）。
5. 兜底：WebGL 不可用（jsdom 测试、低端 WebView）时返回 `null`，卡片退化为 token 渐变底 —— 
   与 ParticleCloud 现有的「boot 失败静默空层」行为一致。

明确**不做**的：不给列表挂 N 个 `paused` 的 ParticleCloud；不引入离屏 worker（OffscreenCanvas
兼容性不值得为 128px 缩略图买单）。

## 3. (c) DELETE 流程：DynamoDB 先行，S3 随后，重试自愈

`DELETE /dreamscapes/{job_id}`，语义是软删（状态 DELETED）+ 物理清理音频对象。

**顺序选择：先 DynamoDB、后 S3。** 两种顺序的失败形态：

- S3 先删、DDB 更新失败 → 卡片还在列表里但音频已没了，用户点开播放 404 —— **可见的坏状态**；
- DDB 先标 DELETED、S3 删失败 → 用户视角一切正常（卡片立刻消失、列表排除、播放入口 404），
  只剩**不可见的孤儿对象**（纯存储成本问题，且签名 URL 只签给 DONE 任务，孤儿对象无法再被访问）。

后者的失败形态严格更好，且单条条件更新的可靠性远高于 list+batch-delete。

**处理器步骤**：

1. `get_job(user.sub, job_id)`（天然限定在调用者分区）：不存在或属别人 → 404（无存在性泄露，
   与现有 GET /jobs 同一套路）；状态既非 DONE 也非 DELETED（在途任务）→ 404（在途任务不是 dreamscape）。
2. 新增 `db.py :: mark_job_deleted(user_id, job_id)`：条件更新
   `#status IN (DONE, DELETED)` → `status = DELETED`。已是 DELETED 时照常成功 —— 这就是幂等的锚点。
   （这是任务状态更新、不动 credit 计数器，走 `_update_job` 一类的单条更新即可，不违反约束 1。）
3. S3 清理：`list_objects_v2(prefix=f"jobs/{job_id}/")` → `delete_objects`。
   删除不存在的 key 本身就成功，天然幂等。**即使第 1 步发现状态已是 DELETED 也照跑这一步** —— 
   这样上次 S3 失败后的重试会把清理补完（自愈）。
4. 成功返回 204。S3 步骤失败 → 500，前端回滚乐观删除并可重试；重试命中第 3 条的自愈路径。
   残余风险（客户端永不重试 → 孤儿对象）接受，不为它建 sweeper。
5. **绝不动 `pictures/`**：约束 9 —— 图片只由生命周期规则过期，任何 Lambda 都不持有对它的
   `s3:DeleteObject`。IAM 上把删除权限精确授到 `jobs/*`（见下节），从权限层面把这条锁死。

连带调整：`GET /jobs/{job_id}` 对 DELETED 任务返回 404（不再发签名 URL）；
`JobStatus` 枚举加 `DELETED`；列表查询只认 DONE，自动排除。

---

## 4. 后端 API 变更清单

新路由文件 `backend/api/routers/dreamscapes.py`：

| 路由 | 行为 |
|---|---|
| `GET /dreamscapes?cursor=` | 见第 1 节。每项：`job_id, keywords, mood_excerpt, duration_minutes, source_type, created_at`。`source_type` 由 `picture_key` 是否存在推得（`picture`/`text`）。**不含音频 URL**。 |
| `DELETE /dreamscapes/{job_id}` | 见第 3 节。204 / 404 / 500。 |

`GET /jobs/{job_id}`（routers/generate.py）保持播放 URL 的唯一出口：它本来就在每次调用时
现签 CloudFront URL（`_audio_url` 无缓存），旧梦重访天然拿到新鲜 URL —— 这条只需回归测试确认，
外加「DELETED → 404」一处改动。

日志约束照旧（约束 7）：只记 `job_id`、状态、数量；keywords / mood_excerpt 是用户内容，
可以进响应体，不进 INFO 日志。

## 5. infra 变更（data_stack + api_stack）

**data_stack —— 生命周期规则重构**（对应第 0 节的修正）：

- 现有 `ExpireGeneratedAudio`（prefix `jobs/`，90 天全删）拆成两条：
  1. `AbortJobUploads`：prefix `jobs/`，仅保留 `abort_incomplete_multipart_upload_after=7d`，**无 expiration**；
  2. `ExpireJobIntermediates`：过期规则改为**按对象标签过滤**（`prefix jobs/` + tag `transient=true`），
     90 天过期 —— `generate_script` 上传 `script.txt` 时带 `Tagging="transient=true"`。
     narration.mp3 不打标签，从此不过期。
- 选标签而不是挪前缀（如 `tmp/`）的原因：不动 key 约定，`get_job`、签名 URL、
  管线各步的读写授权全都不用改；存量对象（旧 narration）自动获益于「无标签 = 不过期」。
  代价是存量 `script.txt` 无标签、不再过期 —— 纯文本几 KB，接受。
- `pictures/` 规则原样不动。

**api_stack —— API Lambda 新增权限**：

- `s3:DeleteObject` 于 `arn:...:bucket/jobs/*`（**不含** `pictures/*`，呼应约束 9）；
- `s3:ListBucket` 于 bucket，`Condition: {"StringLike": {"s3:prefix": "jobs/*"}}`。

**pipeline_stack**：`generate_script` 的 `put_object` 加 Tagging（沿用已有的 `s3:PutObject`
授权即可，内联 Tagging 不需要额外 action）。

## 6. 前端方案

设计稿唯一事实来源：Claude Design 项目 `Meditation PWA Prototype.dc.html`（经 claude_design MCP 导入，
需先 `/design-login`），只取三个增量：Home 入口行、Dreamscapes 页、Player 重访头。
点云、Player、theme tokens 全部复用已移植的实现，不二次导入。

1. **API 客户端**（`src/api/client.ts`）：`listDreamscapes(cursor?)`、`deleteDreamscape(jobId)`。
2. **数据 hook**（新增 `src/dreamscapes/useDreamscapes.ts`）：
   - 游标分页（`items` 累积 + `loadMore`）；
   - 删除 = 乐观移除 → 调 DELETE → 失败回滚 + toast；
   - 首页计数从第一页结果派生并模块级缓存 —— Home 上**加载中不渲染任何东西**（无 spinner），
     拿到数才淡入那一行。
3. **HomePage**：主句下方安静的「N dreamscapes collected」入口行，文案、间距、渐入随设计稿。
4. **DreamscapesPage**（新增 `src/pages/DreamscapesPage.tsx`）：
   - 关键词标题卡片 + 第 2 节的静态缩略图；
   - `wovenAgo(date)` 相对时间格式化器（独立纯函数，便于测试）；
   - 左滑删除 + 软确认（Pointer Events 实现，阈值/回弹动效按设计稿）；
   - 空态、免费层 footer 行（**纯展示**，「只保留最近 3 条」的保留策略本期不实现）。
5. **PlayerPage**：重访头变体（keywords + woven-ago 行）。经路由 state 传入
   `{ from: 'dreamscapes', keywords, created_at }`；打开时调 `GET /jobs/{id}` 取当次签名 URL。

## 7. 测试

后端（pytest + moto）：
- 列表：分页游标（>20 条跨页、末页 `next_cursor=null`）、只出 DONE（PENDING/FAILED/DELETED 全排除）、
  按 created_at 降序；
- 删除：幂等（连打两次都 204、第二次仍执行 S3 清理）、跨用户 404、在途任务 404、
  moto 验证 `jobs/{id}/` 下对象确实被删而 `pictures/` 原样；
- `GET /jobs`：DELETED → 404；DONE 每次调用签发新 URL；
- infra 测试：synth 后断言生命周期规则形态（jobs/ 无 expiration、tag 规则 90 天、pictures/ 不变）。

前端（vitest）：列表 hook（分页、乐观删除与失败回滚）、`wovenAgo` 格式化器、
缩略图工厂的记忆化（WebGL 不可用路径返回 null）。

## 8. 实施顺序与完成标准

顺序：① db.py（DELETED 状态、list_done_jobs、mark_job_deleted）→ ② dreamscapes 路由 + GET /jobs 调整
→ ③ infra（生命周期 + IAM + tagging）→ ④ 后端测试 → ⑤ 前端 client/hook → ⑥ 三个界面增量
→ ⑦ 前端测试。

完成标准（照任务原文）：`ruff` 干净、`pytest` 全过、`cdk synth` 通过、`npm run build` + vitest 通过、
dev 后端上流程可走通。**部署（cdk deploy）由人执行**（约束 8）。
