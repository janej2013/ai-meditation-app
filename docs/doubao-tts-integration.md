# 豆包（火山引擎）TTS 语音合成接入文档

> 厂商契约的参考文档：端点、请求头、参数、响应格式，以及若干容易踩错的地方。
> 实现在 [`backend/shared/tts/volcano.py`](../backend/shared/tts/volcano.py)，
> 第六节的默认配置就是那个模块里的常量来源。

## 一、厂商与接口概览

| 项目 | 说明 |
| --- | --- |
| 厂商 | 字节跳动 · 火山引擎（Volcengine）豆包语音 |
| 产品 | 豆包语音合成大模型（Seed-TTS） |
| 接口 | 单向流式 HTTP（V3），`POST https://openspeech.bytedance.com/api/v3/tts/unidirectional` |
| 协议 | HTTPS，请求为 JSON；响应为**逐行 JSON 的 chunked 流**，音频以 base64 分块返回 |
| 计费 | 按合成字符数计费（非 token） |

### 官方文档链接

- **单向流式 HTTP V3 接口文档（此处使用的接口，支持复刻/混音）**：https://www.volcengine.com/docs/6561/1598757
- 豆包语音 API 接口文档总览：https://www.volcengine.com/docs/6561/1096680
- 音色列表（voice_type / speaker 取值）：https://www.volcengine.com/docs/6561/97465
- 火山引擎语音技术控制台（开通服务、获取 App ID / Access Token）：https://console.volcengine.com/speech/app

## 二、认证方式

在火山引擎控制台「语音技术」中创建应用，开通"语音合成大模型"服务后获得：

| 凭证 | 请求头 | 说明 |
| --- | --- | --- |
| App ID | `X-Api-App-Id` | 应用 ID（可选，但建议携带） |
| Access Token | `X-Api-Access-Key` | 鉴权密钥（**必填**） |
| Resource ID | `X-Api-Resource-Id` | 资源/模型 ID，此处使用 `seed-tts-2.0` |
| Cluster | `X-Api-Cluster` | 集群，见下方规则 |

**Cluster 选择规则**：

- 音色 ID 以 `S_` 开头（声音复刻音色）→ `volcano_icl`
- 其他（平台预置大模型音色）→ `volcano_tts`

实现见 `volcano.cluster_for()`。

**本仓库的凭证注入方式**：密钥存在 Secrets Manager，Lambda 只拿到 ARN
（`VOLCANO_SECRET_ARN`），在冷启动时读取一次并缓存。硬约束 4 禁止把密钥放进
Lambda 的明文环境变量，所以**不要**照搬"用环境变量传 Access Token"的做法。密钥
是一个 JSON 文档：

```json
{"api_key": "<Access Token>", "app_id": "<App ID，可选>"}
```

本地调参脚本 `scripts/tts_preview.py` 是唯一的例外——它从 `VOLCANO_API_KEY`
读取，因为它不跑在 AWS 上，也不接触任何用户数据。

## 三、请求格式

```
POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
Content-Type: application/json
X-Api-App-Id: <appId>
X-Api-Access-Key: <accessToken>
X-Api-Resource-Id: seed-tts-2.0
X-Api-Cluster: volcano_tts
```

请求体结构：

```json
{
  "user": { "uid": "your-user-id" },
  "req_params": {
    "text": "要合成的文本",
    "speaker": "zh_male_ruyayichen_saturn_bigtts",
    "audio_params": {
      "format": "mp3",
      "sample_rate": 24000,
      "speech_rate": -25,
      "volume": 100,
      "emotion": "ASMR",
      "emotion_scale": 4
    },
    "additions": "{\"context_texts\":[\"...\"],\"cache_config\":{\"text_type\":1,\"use_cache\":true}}"
  }
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user.uid` | string | 是 | 业务侧用户标识，任意字符串 |
| `req_params.text` | string | 是 | 待合成文本 |
| `req_params.speaker` | string | 是 | 音色 ID，见[音色列表](https://www.volcengine.com/docs/6561/97465) |
| `audio_params.format` | string | 否 | 音频格式，`mp3` / `pcm` / `ogg_opus` 等 |
| `audio_params.sample_rate` | number | 否 | 采样率，如 24000 |
| `audio_params.speech_rate` | number | 否 | 语速，范围约 [-50, 100]，0 为正常，负数减速 |
| `audio_params.volume` | number | 否 | 音量 |
| `audio_params.emotion` | string | 否 | 情感，如 `ASMR`（需音色支持） |
| `audio_params.emotion_scale` | number | 否 | 情感强度 |
| `req_params.additions` | string | 否 | **JSON 字符串**（注意需 `json.dumps`，不是嵌套对象），扩展参数 |
| `req_params.mix_speaker` | object | 否 | 混合音色配置（见官方文档） |

`additions` 内用到的扩展参数：

- `context_texts: string[]` — 上下文提示词，用自然语言描述期望的语气/风格（Seed-TTS 2.0 特性）
- `cache_config: { text_type: 1, use_cache: true }` — 开启文本缓存，相同文本命中缓存可加速并省费

## 四、响应格式与解析

响应为 chunked 流，**每行一个 JSON 对象**，需按行拆分解析：

```jsonc
{"code": 0, "data": "<base64 音频分块>"}   // 音频数据行，可能有多行
{"code": 20000000, "message": "OK", ...}   // 结束标记行
```

解析规则（实现见 `VolcanoProvider._read_stream`）：

1. 按 `\n` 分行，过滤空行，逐行解析 JSON；
2. `code == 20000000` → 合成结束，停止解析；
3. `code` 非 0 且非 20000000 → 错误，`message` 为错误信息；
4. 其余行取 `data` 字段（base64 字符串），**按顺序拼接**即为完整音频的 base64；
5. base64 解码后即得到 `format` 指定格式的音频二进制。

## 五、参考实现

Python 实现见 [`backend/shared/tts/volcano.py`](../backend/shared/tts/volcano.py)：

| 关注点 | 位置 |
| --- | --- |
| 构造请求体（含 `additions` 的字符串化） | `VolcanoProvider._build_payload` |
| 请求头与集群选择 | `VolcanoProvider._headers` / `cluster_for` |
| 逐行解析响应、区分结束标记与错误 | `VolcanoProvider._read_stream` |
| 长文本分块与拼接 | `VolcanoProvider.synthesize` / `chunk_script` |

本地试听与调参用 `scripts/tts_preview.py`，它直接驱动上面这些代码，
只是把音色、语速、情感、提示词做成了命令行参数。

## 六、默认配置

英文一列即 `volcano.py` 中 `DEFAULT_VOICE` / `DEFAULT_SPEECH_RATE` /
`DEFAULT_EMOTION` / `DEFAULT_EMOTION_SCALE` / `DEFAULT_CONTEXT_TEXTS` 的取值。
音色 ID 的 `zh_male_` 前缀反映的是原始录音人，不是输出语言——英文一列那个确实
是英文预设。

| 配置项 | 中文 | 英文 |
| --- | --- | --- |
| 模型（Resource ID） | `seed-tts-2.0` | `seed-tts-2.0` |
| 音色 | `zh_male_ruyayichen_saturn_bigtts`（儒雅逸辰） | `zh_male_m191_uranus_bigtts` |
| 语速 `speech_rate` | -25 | -25 |
| 格式 / 采样率 | mp3 / 24000 | mp3 / 24000 |
| 情感 | 不设置 | `emotion: 'ASMR'`，`emotion_scale: 4` |
| context_texts | 冥想引导风格提示词（平静、空灵、慢速、留白） | 同左英文版 |

中文默认 `context_texts` 提示词：

> 用极其平静、克制且空灵的语气说。语调要低沉宽厚，语速极慢，每一句话之间都要有明显的留白。不要带有任何情绪起伏，像是在深山旷野中回荡的自然之声，让声音听起来有一种托举感和慈悲感。

## 七、注意事项

1. **`additions` 必须是 JSON 字符串**，不是对象——直接传对象会被服务端忽略或报错。
2. **连接复用**：火山服务端 keep-alive 为 1 分钟，长文本分块合成时应复用 TCP 连接——`VolcanoProvider` 持有一个 `urllib3.PoolManager` 来做这件事。
3. **超时**：长文本合成较慢，建议超时设为 60s。
4. **文本长度**：单次请求文本不宜过长；超长文本请用官方[异步长文本接口](https://www.volcengine.com/docs/6561/1829010)（支持 10 万字符）。
5. **错误处理**：HTTP 200 不代表成功，必须逐行检查 `code`；常见错误如鉴权失败、音色不存在、资源未开通等都通过流内 JSON 的 `code`/`message` 返回。
6. **声音复刻音色**（`S_` 开头）需将 cluster 切为 `volcano_icl`，且账号需开通声音复刻服务。
7. **缓存**：`cache_config.use_cache = true` 时相同文本+音色命中缓存，可显著降低延迟。
8. **密钥**：不写进代码，也不放进 Lambda 明文环境变量（硬约束 4）；存 Secrets Manager，只把 ARN 作为环境变量下发。
