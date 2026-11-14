# 百炼语音识别 API 要点（维护参考）

> 来源：阿里云百炼官方文档（非实时语音识别 / Paraformer / Qwen-Audio-ASR）。
> base_url 形如 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1`。

## 1. paraformer-v2 / fun-asr（异步文件转写）

- 提交：`POST {base}/services/audio/asr/transcription`
  - 请求头：`Authorization: Bearer <key>`、`Content-Type: application/json`、`X-DashScope-Async: enable`
  - 请求体：
    ```json
    {"model":"paraformer-v2",
     "input":{"file_urls":["<公网URL或oss://临时URL>"]},
     "parameters":{"channel_id":[0],"language_hints":["zh"]}}
    ```
  - 返回：`output.task_id`
- **关键限制**：`file_urls` 仅支持公网可访问的 http/https URL，或 OSS 存储的 `oss://` 临时 URL。**不支持 Base64、二进制流、本地文件直传。**
- 轮询：`GET`（失败回退 POST）`{base}/tasks/{task_id}`，仅带 `Authorization` 头。
  - 状态：`PENDING`/`RUNNING` → 继续；`SUCCEEDED` → 取 `output.results[].transcription_url`（24h 有效）；`FAILED`/`CANCELED` → 报错。
- 下载结果 JSON（`transcription_url`）：
  ```json
  {"transcripts":[{"channel_id":0,
    "sentences":[{"begin_time":160,"end_time":6080,"text":"...","sentence_id":1,
                  "words":[{"begin_time":160,"end_time":600,"text":"请把"}]}]}]}
  ```
  - `begin_time`/`end_time` 单位为**毫秒**，相对该音频文件起点。

## 2. qwen-audio-3.1-asr-flash（同步，支持 Base64，零公网依赖）

> **实测（该 MaaS 专属域名）**：响应顶层同时有扁平 `sentence` 与 `output.sentence`，两者内容相同；
> 需兼容两种结构。SSE 流式返回的是**累积全文**（`sentence.text` 包含此前所有句子），
> 逐事件写入会造成重复字幕，因此脚本统一使用同步模式 `X-DashScope-SSE: disable`，
> 一次取得整段词级时间戳，再按句末标点切词成句。

- 提交：`POST {base}/services/aigc/multimodal-generation/generation`
  - 请求头：`Authorization: Bearer <key>`、`Content-Type: application/json`、`X-DashScope-SSE: enable`
  - 请求体：
    ```json
    {"model":"qwen-audio-3.1-asr-flash",
     "input":{"messages":[{"role":"user","content":[
        {"type":"input_audio","input_audio":{"data":"data:audio/mpeg;base64,<B64>"}}]}]},
     "parameters":{"format":"mp3","sample_rate":"16000","language_hints":["zh"]}}
    ```
  - 输入支持音频文件 URL **或 Base64 Data URI**；限制 ≤5 分钟、≤2GB。
- 响应：SSE 流，每个事件含 `output.sentence`（有 `begin_time`/`end_time`(sentence_end 时为 true 才返回)/`text`/`sentence_id`）。累计所有 `sentence` 事件即得到逐句时间戳（毫秒）。
- 若未返回逐句时间戳，按句末标点切分文本并对整段时长均分作为兜底。

## 3. OSS 上传（为 paraformer 取得公网地址）

- `PUT https://{bucket}.{endpoint}/{key}`
  - 头：`Date: <GMT>`、`Content-Type: audio/mpeg`、`x-oss-object-acl: public-read`
  - `Authorization: OSS {AccessKeyId}:{Signature}`
  - 签名串（无额外头时）：`PUT\n\n{audio/mpeg}\n{Date}\n/{bucket}/{key}`，HMAC-SHA1 + Base64。
- 上传后使用公网地址 `https://{bucket}.{endpoint}/{key}` 作为 `file_urls`。
- 处理完毕后 `DELETE` 同 URL 清理对象。

## 4. 时间戳合并

各片段识别出的 `begin_time/end_time` 为相对该片段起点的毫秒值，合并时统一加上片段起始偏移量（秒 × 1000）。
**启用智能静音切分后，偏移量必须是真实切点（如 239.206s），不能写成 `k × segment`。**

## 5. 智能静音切分

```bash
ffmpeg -hide_banner -i audio_16k.mp3 \
  -af "silencedetect=noise=-35dB:d=0.35" -f null -
```

- stderr 中解析 `silence_start: <秒>` / `silence_end: <秒>`，取区间中点作为候选切点。
- 目标切点 = **固定网格** `k×240`（240/480/720/960/1200/1440/1680），在 **±15s** 内选最近静音中点；窗口内无静音则兜底硬切到网格点。固定网格避免逐段累积漂移。
- 每段长度约束 `[60s, 290s]`（290s 为 ≤5 分钟模型上限留余量）；尾部 <20s 不再切。
- 时间戳偏移 = 每段**真实起始时间** `cuts[i]` × 1000，日志逐段打印便于核验。
- 实测 1792s 视频检出 388 处静音，7/8 切点对齐静音、0 个兜底，切点 239.206 / 479.437 / 716.287 / 958.250 / 1200.138 / 1439.909 / 1682.863，偏差 0.09~3.71s。按片段统计时间偏差中位数 +87~+149ms，无系统性漂移。
- 局限：消除「词被腰斩」（实测残片 0），但 ASR 仍会在每段末尾自动补句号，跨段长句可能拆成两条，仍需 `polish.py` 的 merges 兜底。

## 6. 视觉抽帧校对（polish.py --review）

```bash
ffmpeg -y -loglevel error -ss <时间点秒> -i <原视频> -frames:v 1 \
  -vf "scale=1280:-1" -q:v 4 <输出.jpg>
```

- **强制宽 1280px**、**低体积 JPG `-q:v 4`**、**单张 ≤150KB**；若仍超限，`-q:v` 逐级 +2 重编码（宽度不变，最多 6 次）。
- 4K 原片（3838×2160）实测 10 张全部 1280×720、45.7~123.4KB，画面文字（如 `GOOL`）仍清晰可读。
- 扫描字幕中非白名单的英文/数字混合 token，按时间均匀取样（默认最多 12 张），抽帧时间点取该字幕条目的中点。
- 产出 `review.md`（含尺寸/体积列）/ `review_manifest.json`（记录 width、quality、kb）/ `fixes_template.json`。
- **本地脚本不能调用 HY 模型**；截图由 WorkBuddy（HY）读取核对后写入 `fixes.json` 的 `replacements`。
- 抽帧时间点可能已切到别的画面；若画面未出现该词，跳过该项，不要臆测。
