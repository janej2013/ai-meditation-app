# "Drift from a picture" — 服务端图片理解方案（`describe_picture` step）

> 状态：已实现（2026-08-24，分支 `feat/nova-lite-default`），待 dev 真机验证。决策日期 2026-08-24（同日修订：图片长期保留）。
> 决定：新增独立的 `describe_picture` 状态机步骤；图片上传到 S3 后**保留 1 年**（lifecycle 规则到期删除，
> handler 不删）；前端去掉所有隐私承诺文案。
> 背景：后续要做"随时重听已生成的冥想，不再消耗 credit"，用户内容（图片、脚本、音频）都需要长期留存。

## 一、背景与现状

- 6b 里 "Drift from a picture" 是纯客户端功能：文件变成 object URL 采样进粒子云，然后走普通 mood
  面板生成。README "Known gaps" 记录了 keywords 屏（"In your picture, we found…"）要等一个 vision 步骤，
  **不能伪造关键词**。
- 现有状态机：`FreezeCredit → GenerateScript → Synthesize → CommitCredit`，每个 task 都 `Catch → RollbackCredit`。
  `mood_text` 存在 `JOB#` item 上而不是 payload 里（constraint 7），`generate_script` 从 item 读回。
- 现有 audio bucket：全私有、无 CORS、只有一条 `jobs/` 前缀的 lifecycle 规则
  （[data_stack.py:72](../infra/stacks/data_stack.py#L72)）。
- 前端 [HomePage.tsx:317](../frontend/src/pages/HomePage.tsx#L317) 明文承诺图片不离开设备——**这条承诺必须先删，功能才能上**。
- 规划中的"重听"功能要求 `jobs/` 下的脚本与音频长期保留；目前 `AUDIO_RETENTION_DAYS = 90`
  （[data_stack.py:22](../infra/stacks/data_stack.py#L22)），落地重听时需一并放长，本方案先把图片对齐到 1 年。

## 二、模型选择

| 模型 | 输入 / 输出（每百万 token，Bedrock 参考价） | ap-southeast-2 | 结论 |
| --- | --- | --- | --- |
| **Amazon Nova Lite** `amazon.nova-lite-v1:0` | ~$0.06 / $0.24 | 有，bare model id | **选用**。多模态，已是 `generate_script` 的默认模型，用户数据留在 Sydney |
| Amazon Nova Pro | ~$0.80 / $3.20 | 有 | 质量更好但对"提 3–5 个氛围关键词"是浪费 |
| Claude Haiku 4.5 | ~$1 / $5 | 需 `au.` 跨区 profile | 贵 15 倍，且跨区 profile 会踩用例表单/区域开通坑 |
| Amazon Nova Micro | 最便宜 | 有 | 纯文本，不支持图片，排除 |

一张 ≤1568px 的图片在 Nova 上约 1000–1500 输入 token，加 prompt 与 ~100 token 输出，**单次约 $0.0001–0.0002**，
相对 TTS 成本可忽略。价格以 [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) 为准。

Nova Converse API 的图片约束（实现时以官方文档复核）：格式 JPEG / PNG / WebP / GIF；单张 ≤ 3.75 MB（bytes 直传）；
不支持 HEIC——iPhone 原图必须在浏览器转码（见第七节）。

## 三、端到端流程

```
浏览器                      API Lambda                 S3 (audio bucket)          Step Functions
  │ 选图 → canvas 缩放/转 JPEG   │                            │                          │
  │ (≤1568px, 剥离 EXIF)         │                            │                          │
  │── POST /pictures/upload ───▶│ 生成 presigned POST         │                          │
  │◀─ {picture_id, url, fields}─│ key=pictures/<sub>/<uuid>.jpg│                         │
  │── POST <url> (multipart) ─────────────────────────────────▶│ 落盘                      │
  │── POST /generate {mood, duration_minutes, picture_id} ─▶│                            │                          │
  │                              │ 校验 picture_id 属于本人；  │                          │
  │                              │ JOB item 写 picture_key     │                          │
  │                              │ start_execution(has_picture=true) ────────────────────▶│
  │◀─ 202 {job_id} ─────────────│                            │                          │ FreezeCredit
  │                              │                            │                          │ HasPicture? ──no──▶ GenerateScript
  │                              │                            │◀─ GetObject ─────────────│ DescribePicture (Nova Lite)
  │                              │                            │   (对象保留，lifecycle 1 年) │   写 keywords 到 JOB item
  │── GET /jobs/{id} 轮询 ──────▶│ 返回 status + picture_keywords │                       │ GenerateScript（prompt 带 keywords）
  │   keywords 屏展示             │                            │                          │ Synthesize → CommitCredit
```

### 3.1 上传：presigned POST 而非 presigned PUT

用 `generate_presigned_post` 而不是 PUT，因为只有 POST policy 能在服务端**强制**：

- `content-length-range: [1, 4_000_000]`（PUT 无法限制大小，用户可传 100 MB 把 Lambda 撑爆）；
- `Content-Type` 精确等于 `image/jpeg`（浏览器统一转 JPEG 后只放行这一种）；
- key 精确等于 `pictures/<cognito_sub>/<uuid4>.jpg`（用户无法写到别人的前缀）。

URL 有效期 5 分钟。API Lambda 只需要 `s3:PutObject` on `pictures/*`（presign 用调用者的身份签名）。

### 3.2 `POST /pictures/upload`（新路由，JWT 保护）

```python
class UploadPictureResponse(BaseModel):
    picture_id: str  # uuid4，不含路径；客户端随后传给 /generate
    url: str
    fields: dict[str, str]  # presigned POST 的表单字段，原样放进 multipart
    expires_in: int
```

不写 DynamoDB：picture_id 与 key 的映射是确定性的（`pictures/{sub}/{picture_id}.jpg`），`/generate`
只需校验 `picture_id` 是合法 uuid4，然后用**当前用户的 sub** 拼 key——不存在引用他人对象的可能。

### 3.3 `POST /generate` 扩展

```python
class GenerateRequest(BaseModel):
    mood: str = Field(min_length=1, max_length=500)
    duration_minutes: int = Field(ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)
    picture_id: UUID | None = None  # 新增，可选
```

- `store.create_job(...)` 新增 `picture_key` 字段写入 JOB item（与 `mood_text` 同理，**不进 execution 输入**）。
- 状态机输入只多一个布尔：`{"user_id", "job_id", "duration_minutes", "has_picture": true}`。
- 不在 API 层 `head_object` 校验对象存在：上传和生成之间有竞态，且校验会给 API Lambda 多一次 S3 往返；
  对象不存在由 `describe_picture` 当作永久失败处理并回滚 credit（用户看到失败、credit 不扣，可重试）。

### 3.4 状态机改动（`pipeline_stack.py`）

```
FreezeCreditTask
  └─▶ HasPicture (Choice: $.has_picture == true)
        ├─ true  ─▶ DescribePictureTask ─▶ GenerateScriptTask
        └─ false ─▶ GenerateScriptTask
GenerateScriptTask ─▶ SynthesizeTask ─▶ CommitCreditTask ─▶ Succeeded
```

- `DescribePictureTask`：`Retry` on `[BEDROCK_TRANSIENT, *LAMBDA_SERVICE_ERRORS]`（2s，×2，3 次），
  `Catch States.ALL → RollbackCreditTask`（constraint 3）。task timeout 60s。
- `Choice` 判空要稳：`sfn.Condition.boolean_equals("$.has_picture", True)`，缺字段走默认分支。
- `PipelineState` 新增 `has_picture: bool = False`。不加 `picture_key`、不加 keywords——它们是用户内容派生物，
  和 `mood_text` 一样只走 JOB item。

### 3.5 `describe_picture` handler（`backend/functions/describe_picture/`）

```python
def lambda_handler(event, _context):
    state = PipelineState.model_validate(event)
    job = store.get_job(state.user_id, state.job_id)
    if not job.picture_key:  # 状态机说有图但 item 没 key：数据不一致，永久失败
        raise PictureDescriptionError("job has no picture_key")
    image = _fetch(job.picture_key)  # get_object；NoSuchKey / 超限 → 永久失败
    description = _describe(image)  # Nova Lite converse，见 3.6
    store.set_job_picture_description(state.user_id, state.job_id, description)
    logger.info("picture described job_id=%s keywords=%d", state.job_id, len(description.keywords))
    return state.model_dump()
```

要点：

- **handler 不删对象。** 图片由 lifecycle 规则在 1 年后过期（见 4.2）。这让 Step Functions 的 Retry 真正有效：
  Bedrock 瞬时错误重试时图仍在桶里；同一 job 幂等重跑也只是覆盖同样的 keywords 字段。
- 取图前 `head_object` 看 `ContentLength`，>4 MB 直接永久失败（防御 presigned policy 被绕过的情况）。
- `_fetch` 把 bytes 交给 `converse` 的 `{"image": {"format": "jpeg", "source": {"bytes": ...}}}`；不落磁盘。
- 日志只有 `job_id` 和关键词**个数**，不打关键词、不打摘要（它们由用户图片派生，按 constraint 7 处理）。
- Lambda 内存 512 MB、超时 60 s；环境变量复用 `BEDROCK_MODEL_ID`（同一个 Nova Lite）。

### 3.6 Prompt 与输出契约

```python
class PictureDescription(BaseModel):
    keywords: list[str] = Field(min_length=3, max_length=5)  # 每个 ≤ 24 字符
    summary: str = Field(min_length=10, max_length=240)  # 一句氛围描述，第二人称、现在时
```

System prompt 要点（英文，实现时放 `describe_picture/prompt.py`）：

- 只描述**氛围、光线、色彩、自然元素、空间感**，用于引导一段冥想；
- **不得**识别或描述任何人（年龄、性别、外貌、身份）、不得转写图中文字、车牌、门牌、屏幕内容——
  这是 constraint 7 在视觉侧的对应；
- 输出严格 JSON：`{"keywords": [...], "summary": "..."}`，不加解释；
- `temperature 0.3`，`maxTokens 300`。

解析：先 `json.loads`，失败则尝试截取首尾大括号再解析；仍失败 → `PictureDescriptionError`（永久，回滚）。
Pydantic 校验失败同样永久失败——不静默降级到"无图"模式，否则用户付了 credit 却没拿到 picture 体验且不知情。

### 3.7 `generate_script` 读取关键词

`build_user_message(mood_text, duration_minutes, picture=None)` 增加可选段落：

```
The listener chose a picture. It felt like: <summary>
Weave these images through the meditation: <kw1>, <kw2>, <kw3>.
Do not mention that a picture was used.
```

`generate_script` 从 JOB item 读 `picture_summary` / `picture_keywords`（与 `mood_text` 同一次 `get_job`），有则拼入。

### 3.8 `GET /jobs/{job_id}` 扩展

`JobResponse` 新增 `picture_keywords: list[str] | None`。前端 GENERATING 轮询时一旦拿到非空数组就切到
keywords 屏；DONE 时仍然带着，Player 可复用。`summary` 不返回前端（只服务 prompt）。

## 四、数据与存储

### 4.1 JOB item 新增字段

| 字段 | 类型 | 写入者 | 说明 |
| --- | --- | --- | --- |
| `picture_key` | S | API `/generate` | `pictures/<sub>/<uuid>.jpg`；重听功能可据此在 Player 复现粒子云采样 |
| `picture_keywords` | L\<S\> | `describe_picture` | 3–5 个 |
| `picture_summary` | S | `describe_picture` | ≤240 字符 |

`db.py` 新增 `set_job_picture_description(user_id, job_id, description)`，一次 `update_item`。
`Job` 模型加三个可选字段。不需要 GSI。

### 4.2 S3：`pictures/` 前缀 + 1 年 lifecycle

在 `data_stack.py` 的 audio bucket 上新增一条规则，常量 `PICTURE_RETENTION_DAYS = 365`：

```python
s3.LifecycleRule(
    id="ExpireUploadedPictures",
    enabled=True,
    # Pictures back the planned replay feature (re-listen without spending a
    # credit), so they live as long as the job they belong to. Nothing in the
    # pipeline deletes them; this rule is the only reaper.
    prefix="pictures/",
    expiration=Duration.days(PICTURE_RETENTION_DAYS),
    abort_incomplete_multipart_upload_after=Duration.days(1),
)
```

- 上传了但从未点生成的"孤儿"图片同样留 1 年——一张 ≤4 MB 的 JPEG 存一年不到 $0.001，不值得为它加清理逻辑。
- **与重听功能对齐**：重听落地时把 `AUDIO_RETENTION_DAYS` 也改成 365（或统一成一个 `JOB_RETENTION_DAYS`），
  否则会出现图片还在、音频已过期的 job。
- `pictures/` 暂不走 CloudFront：现阶段只被 Lambda 读。重听功能若要在 Player 里重新显示原图，届时给
  `pictures/` 也走签名 URL（与 `jobs/` 同一 OAC 授权即可，constraint 6 不变）。

### 4.3 Bucket CORS

浏览器直传 S3 需要 bucket 级 CORS（[data_stack.py:63](../infra/stacks/data_stack.py#L63) 那句"needs no CORS
configuration"的注释要更新）：

```python
cors = [
    s3.CorsRule(
        allowed_methods=[s3.HttpMethods.POST],
        allowed_origins=[site_origin],  # https://<domain>；dev 另加 http://localhost:5173
        allowed_headers=["content-type"],
        max_age=300,
    )
]
```

只放行 POST——GET 仍然走 CloudFront 签名 URL，constraint 6 不受影响。`site_origin` 通过 stack 参数传入，
prod 不允许 `*`。

## 五、IAM（最小权限）

| 主体 | 新增权限 | 资源 |
| --- | --- | --- |
| API Lambda | `s3:PutObject` | `arn:aws:s3:::<bucket>/pictures/*` |
| `describe_picture` | `s3:GetObject` | `arn:aws:s3:::<bucket>/pictures/*` |
| `describe_picture` | `bedrock:InvokeModel` | 与 `generate_script` 相同的 `_bedrock_resources(bedrock_model_id)` |
| `describe_picture` | `dynamodb:GetItem`, `UpdateItem` | 主表 |

API Lambda 不给 `GetObject`；`describe_picture` 不给 `PutObject` / `DeleteObject`——没有任何代码路径删图。

## 六、失败矩阵

| 情形 | 归类 | 结果 |
| --- | --- | --- |
| Bedrock Throttling / ServiceUnavailable / ModelTimeout | 瞬时 | Retry ×3（图仍在桶里，重试有效）；耗尽后回滚 |
| `NoSuchKey`（用户没传完就点了生成 / 上传失败） | 永久 | 回滚，前端提示"picture didn't arrive, try again" |
| 对象 > 4 MB / Content-Type 不是 jpeg | 永久 | 回滚 |
| Nova 输出非 JSON / 校验失败 | 永久 | 回滚（不静默降级） |
| Nova 拒答（内容审核） | 永久 | 回滚；前端提示换一张图 |

回滚 credit 逻辑不变，仍然只经 `db.py`（constraint 1）。

## 七、前端改动

1. **删除隐私承诺**（`HomePage.tsx:317`）。不再有任何"never leaves your device / deleted after" 之类的措辞；
   替换为纯功能性提示，例如：
   > We'll read the mood of your picture and weave it into this meditation.

   `HomePage.tsx` 顶部注释（第 10 行 "It is never uploaded…"）与第 231 行注释一并删除。
2. **客户端预处理**（上传前，canvas）：
   - 长边缩到 ≤1568 px，`toBlob('image/jpeg', 0.85)`；
   - canvas 重编码天然剥离 EXIF（含 GPS）。这不是承诺，是顺带的好处：服务端也用不到这些元数据；
   - HEIC/HEIF 由浏览器解码后统一变成 JPEG（Safari 原生支持；Chrome on Android 大多支持）；解码失败给出
     "please choose a JPEG or PNG" 提示；
   - 现有的粒子云采样继续用同一个 canvas，不重复解码。
3. **上传时机**：用户选图后立即请求 `/pictures/upload` 并后台上传；用户填 mood 期间上传通常已完成。
   点生成时若上传仍在进行则等待（带 spinner），失败则提示重试，不发起 `/generate`。
4. **keywords 屏**：`GeneratingPage` 轮询到 `picture_keywords` 非空时播放 "In your picture, we found…"，
   读 README 里被记录为 deferred 的那段原型逻辑。
5. `HomePage.test.tsx` 中"never leaves the browser"的注释与断言同步删除/更新。

## 八、文档与约束同步

- `CLAUDE.md`：
  - 项目概述加一句 picture 流程；
  - 仓库布局 `functions/` 列表加 `describe_picture`；
  - constraint 7 补一句：图片派生的关键词/摘要与用户文本同等对待，不进 payload、不进 INFO 日志；
  - 新增 constraint：上传图片只允许写到 `pictures/<sub>/`，由 lifecycle 规则统一过期，业务代码不删除用户对象。
- `README.md`：架构图加 DescribePicture；"Known gaps" 删除 keywords 屏条目；数据留存章节写明图片保留 1 年、
  以及重听功能要求音频留存对齐。
- 记忆文件 `milestone-6b-design-revision` 中"client-side only / never uploaded"的记录在实现落地后更新。

## 九、测试

- `backend/tests/test_describe_picture.py`：
  - 成功路径：mock Bedrock 返回合法 JSON → item 写入；S3 client 上**从未**调用 `delete_object`；
  - Bedrock 抛 `ThrottlingException` → 抛 `BedrockTransientError`；
  - `NoSuchKey` / 超限 / 非 JSON / Pydantic 失败 → `PictureDescriptionError`。
- `backend/tests/test_api_generate.py`：`picture_id` 非法 uuid → 422；合法 → item 有 `picture_key`、
  execution 输入含 `has_picture: true` 且**不含** key。
- `backend/tests/test_api_pictures.py`：presigned POST 的 key 前缀为当前 sub、policy 含 content-length-range。
- `infra/tests`：
  - bucket 有两条 lifecycle 规则，`pictures/` 那条 expiration 365 天；
  - CORS 只允许 POST；
  - 状态机定义含 `Choice` 与 `DescribePictureTask`，后者有 Retry 和指向 Rollback 的 Catch；
  - `describe_picture` 角色无 `s3:PutObject` / `s3:DeleteObject`，API 角色无 `s3:GetObject` on `pictures/*`。
- 完成标准照旧：`ruff check . && ruff format --check . && pytest && cdk synth` 全绿。

## 十、实施顺序

1. `shared`：`PipelineState.has_picture`、`Job` 三字段、`db.set_job_picture_description`、`PictureDescription` 模型。
2. `describe_picture` handler + prompt + 单测。
3. `generate_script` prompt 接入关键词。
4. `data_stack`：lifecycle + CORS；`pipeline_stack`：Lambda、IAM、Choice；`api_stack`：`PutObject` 授权与 `AUDIO_BUCKET` 环境变量。
5. API：`/pictures/upload` 路由、`/generate` 与 `/jobs/{id}` 扩展。
6. 前端：预处理、上传、删除隐私文案、keywords 屏。
7. 文档同步；dev 部署（人工）跑一次真机：确认 `describe_picture` 成功、JOB item 有 keywords、
   `aws s3 ls s3://<bucket>/pictures/<sub>/` 能看到对象。

## 十一、未决 / 后续

- 重听功能：需要 `AUDIO_RETENTION_DAYS` 放长到与图片一致、`GET /jobs` 列表接口、Player 直接播已有 `audio_key`
  的签名 URL；这些不在本方案范围内，但本方案的字段设计（`picture_key` 留在 JOB item）已为其预留。
- Nova Lite 对抽象/艺术类图片的关键词质量若不理想，可只对 picture 步骤切 Nova Pro（单独 env var `PICTURE_MODEL_ID`），
  成本仍在每次 $0.002 以内。
- 用户删除账号时，`pictures/<sub>/` 与 `jobs/` 下的对象是否随之清理——重听功能落地时和 DynamoDB 用户分区一起设计。
