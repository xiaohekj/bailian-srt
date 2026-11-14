---
name: bailian-srt
description: 调用阿里云百炼（DashScope）云端语音识别，将本地视频/音频生成中文 SRT 字幕。当用户需要为本地 mp4/mkv/mov/mp3/wav 等文件生成字幕、做语音转写、给视频加字幕、长音频转写，或提到百炼 / paraformer / 通义听悟 / 语音识别生成字幕时使用。全程云端 API，不在本地运行任何 AI 模型。
agent_created: true
---

# bailian-srt

将本地视频或音频文件转为中文 SRT 字幕。全程使用阿里云百炼（DashScope）云端 ASR；本地仅用 ffmpeg 做音频提取、静音点切分与抽帧，不运行任何本地 AI 模型。中文错别字修正、语义断句与画面文字校对由本智能体（HY 模型）执行。

默认方案开箱即用：**Base64 本地内存直传，不需要 OSS、不需要公网地址、不产生对象存储费用**。

## 工作流程

### 第 1 步：识别并生成原始 SRT

```bash
python "C:/Users/hezha/.workbuddy-ai/skills/bailian-srt/scripts/transcribe.py" "<本地视频或音频路径>"
```

脚本内部完成：视频 `-vn` 提取 16k 单声道 MP3 → `silencedetect` 智能静音点切分 → 并发云端识别 → 按**真实切点**合并时间戳 → 输出同名 `.srt` → 自动删除临时切片。

常用可选参数：`--output <路径>`、`--max-segments N`（冒烟测试）、`--no-smart-split`（对比硬切）、`--keep-temp`（保留临时文件）。

### 第 2 步：备份原始稿 + 生成视觉校对清单

```bash
cp "<原名>.srt" "<原名>.raw.srt"
python "C:/Users/hezha/.workbuddy-ai/skills/bailian-srt/scripts/polish.py" "<原名>.raw.srt" \
  --review --review-only --video "<原视频.mp4>"
```

`--review` 会自动扫描字幕中拿不准的英文/数字专有名词，按时间戳用 ffmpeg 抽帧，输出到一个 `<名称>_frames/` 目录：
`review.md`（人/模型可读清单，含尺寸/体积列）、`review_manifest.json`、若干 `.jpg` 截图、`fixes_template.json`。

截图统一 **强制缩放宽 1280px**（`scale=1280:-1`）、**低体积 JPG**（`-q:v 4`）、**单张 ≤150KB**；若某帧仍超限会自动逐级降质重编码（宽度不变）。实测 10 张全部 1280×720、45.7~123.4KB，且画面文字（如 `GOOL`）依旧清晰可读。这样能明显加快把截图发给 HY 识图的速度。

**关键约束**：本地脚本没有调用 HY 模型的能力。截图只是证据，必须**由你（HY 模型）逐张读图**核对画面上的英文/专有名词写法，再把「字幕写法 → 画面正确写法」填进 `fixes.json` 的 `replacements`。画面上没出现该词的截图应跳过，不得凭猜测改写。

### 第 3 步：生成最终字幕

**个人词库 `references/glossary.json` 会自动加载并强制应用**，无需在 `fixes.json` 里重复写高频错字。把「一次性错字」+「切片边界合并」写进 `fixes.json`（格式见 `references/fixes_example.json`），然后：

```bash
python "C:/Users/hezha/.workbuddy-ai/skills/bailian-srt/scripts/polish.py" "<原名>.raw.srt" \
  --fixes "<fixes.json>" --output "<原名>.srt"
```

若只需断句 + 词库修正，省略 `--fixes` 即可（词库仍然生效）。

## 个人词库（glossary.json）

`references/glossary.json` 是**高频专属词库**，`polish.py` 每次运行自动加载，**优先级高于 `fixes.json`**。以后遇到新的同音错字或专有名词误识，**只需往 `replacements` 里加一行**，不用改代码：

```json
"replacements": {
  "错字": "正确写法",
  "新错字": "新正确写法"
}
```

- **纯英文键按整词边界匹配**（`Goo`→`GOOL` 不会误伤 `Google`，`Mask` 不会误伤 `masking`）；含中文或空格的键走普通子串替换。
- **长词优先**：`Mask in Mask` 会先于 `Mask` 应用。
- 优先级：`glossary.json` > `fixes.json`，同名键以词库为准并在日志提示；`fixes.json` 独有条目不受影响。
- 词库解析失败会优雅降级（打印警告、跳过），不会中断生成。
- `--glossary <路径>` 换词库；`--no-glossary` 临时停用（A/B 对比、排查误替换）。
- 建议每条在 `evidence` 写一句依据（画面截图 / 语义 / 官方拼写）。**不要收录纯写法差异**（如 `100`/`一百`、大小写偏好），只收录确定是错的词；已识别正确的词也不必收录。
- 当前词库含：`加宽→家宽`、`密室→密钥`、`Goo→GOOL`、`BPP→BPB`、`Mask→MASQUE`、`沃普→Warp`、`MiHome/米 home→mihomo` 等 17 条，依据见文件内 `evidence`。

## 智能静音切分（避免硬切断句）

- 用 `ffmpeg silencedetect` 扫描全片静音区间，以**固定网格** `k×segment`（240/480/720…）为目标，在 **±15s** (`silence_window`) 内选最近的**静音中点**作为真实切点；窗口内无静音则兜底硬切到网格点。固定网格避免切点逐段累积漂移。
- 时间戳偏移量**严格使用每段真实起始时间** `cuts[i]`（如 239206ms、479437ms），**不能用 `i×segment`**，否则全片时间戳会整体错位。日志会逐段打印真实起始与偏移便于核验。
- 约束：每段长度落在 `[60s, max_segment_seconds]`（默认上限 290s，留余量满足模型 ≤5 分钟限制）；尾部不留 <20s 碎片。
- **实测**：30 分钟视频检出 388 处静音，7/8 切点对齐静音、0 个需硬切兜底，切点偏差 0.09~3.71s 均 ≤15s，最大段长 243.0s。按片段统计与参考稿的时间偏差中位数 +87~+149ms 且**无系统性漂移**，证明偏移累加正确。
- **效果与边界**：消除了「直接做。」+「了内置」这类**词被腰斩**的错误（实测残片数 0）。但分片识别时 ASR 仍会在段落末尾自动补句号，跨段长句仍可能被拆成两条（如「我用这个客户端注册 Muse。」/「都是直接秒过的…」）。因此 `merges` 仍须保留作安全网，第 3 步前务必检查各真实切点附近是否有需要合并的残句。

## 识别模型与依赖

- 默认 `qwen-audio-3.1-asr-flash`：Base64 Data URI 本地内存直传，≤5 分钟/段，无需 OSS / 公网 URL。实测准确率 93.36%，`core`、`Warp` 等技术专有名词一次识别正确。
- 高级可选：`--model paraformer-v2`（或 `fun-asr`）走异步文件转写，但它们**只接受公网 URL**，需在 `config.json` 的 `oss` 中填写 OSS 信息；日常不需要。
- 依赖：本机 `ffmpeg` / `ffprobe` 在 PATH 中；Python 3 标准库即可，无需 pip 安装。

## 配置（config.json，位于技能根目录）

- `api_key`：百炼 API Key。**安全建议**：改用环境变量 `BAILIAN_API_KEY` 提供（会覆盖文件值），避免密钥明文落盘；轮换 Key 后把新值写入此处或环境变量。
- `base_url`：DashScope/MaaS 接口地址，即用户的 MaaS 专属域名。
- `model`：默认 `qwen-audio-3.1-asr-flash`。
- `split_threshold` / `segment`：超过 300s 按目标每 240s 切分。
- `smart_split` / `silence_window` / `silence_noise_db` / `silence_min_duration` / `max_segment_seconds`：静音切分参数。
- `max_workers`：并发片段数，默认 4。
- `oss`：仅显式使用 paraformer/fun-asr 时才需要，默认方案完全不用填。

## HY 模型修正规则（必须执行）

1. **错别字修正**：修正确定的错字与标点误识，结合上下文还原用字。数字写法差异（`100` vs `一百`）属正常差异，不要强行统一。
   - **语义错别字要特别留意**：如「加宽」应为「家宽」（家庭宽带／住宅 IP），全片 8 处。这类错误**参考稿可能同样全错**（本例两份 ASR 稿都写成「加宽」），只能靠上下文语义判断，绝不能盲目照抄参考稿。
   - **高频错字一律进词库**：判定为高频/专属的错字，加进 `references/glossary.json` 而不是 `fixes.json`，这样以后所有视频自动生效。
   - **画面是术语拼写的最终依据**：本次靠截图确认 `GOOL`、`MASQUE`、`mihomo`（全小写）、`BPB`。特别注意 `mihomo` 是文件名，不是 `MiHome`——早前凭语感改成 `MiHome 内核` 是错的，已纠正。
2. **视觉校对**：读 `--review` 生成的截图，核对专有名词。实测抓出过 `Goo`→`GOOL`（画面下拉框显示 GOOL）、`BPP`→`BPB` 等 ASR 错误。画面无该词则跳过。
3. **语义断句**：按语义与自然停顿断句，**单行不超过 20 个汉字**。长句拆分、过碎短句合并。
4. **时间戳处理**：合并多句取首句 begin 与末句 end；拆分按字数比例插值，确保连续、不重叠、不倒退。`polish.py` 已确定性实现。
5. **保持顺序**与编号（1, 2, 3 …）正确，输出标准 SRT；不增删原话信息，不翻译。

## 输出

最终在同目录生成 `<原名>.srt`（修正后）与 `<原名>.raw.srt`（原始 ASR 备份）。SRT 为纯文本，不含样式；上传 YouTube Studio 后由播放器渲染默认字幕外观。

## 实测要点（务必遵守）

- 该 MaaS 端点的 qwen-audio 响应同时提供**扁平 `sentence`** 与 `output.sentence`，脚本已兼容；只认 `output.sentence` 会取到空结果。
- **不要用 SSE 流式结果做字幕**：该端点 SSE 返回「累积全文」，逐事件写入会产生大量重复条目。脚本用 `X-DashScope-SSE: disable` 同步模式，一次取整段**词级时间戳**（`words` 全为 `fixed:true`），再按句末标点切词成句。
- 4 分钟片段实测稳定返回 300~550 词 → 24~35 句；30 分钟视频 8 段并发约 200+ 条原始字幕，几十秒完成。
- 中文识别准确率很高，参考稿本身也可能是 ASR 产物（曾出现 `core`→「靠」、`Warp`→「卧铺」），**不要盲目照抄参考稿**。

## 故障排查

- `未检测到 ffmpeg`：安装 ffmpeg 并加入 PATH。
- 识别结果为空：检查音频是否过短/无语音，或 API Key 与 base_url 是否正确。
- 静音切分报警「没有可用静音」：背景音乐连续无停顿时属正常，会自动回退硬切；可放宽 `silence_noise_db` 或 `silence_window`。
- 抽帧失败：确认 `--video` 指向含画面的原视频（纯 MP3 输入无法抽帧）。
- 冒烟测试：`--max-segments 1` 只跑第一段，确认 API 可用后再跑全量。
- 更完整的接口细节见 `references/api_notes.md`。
