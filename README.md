# bailian-srt

本地视频 / 音频 → 中文 SRT 字幕。调用阿里云百炼（DashScope）云端 ASR，本地只跑 ffmpeg，不运行任何本地 AI 模型。

实测：30 分钟 / 1.2GB 视频，与人工稿文本相似度 0.9336；8 段并发识别几十秒完成；输出单行 ≤20 汉字、时间戳零重叠零倒退。

## 安全须知（务必先读）

- **不要提交 `config.json`**：它含明文 API Key 和 MaaS 工作空间 ID，已被 `.gitignore` 排除。
- 本仓库只提供 `config.example.json`（`api_key` 为空、`base_url` 为占位符）。
- 推荐做法：设置环境变量 `BAILIAN_API_KEY`，它会覆盖配置文件里的值，密钥完全不落盘。
  ```bash
  # Windows PowerShell
  setx BAILIAN_API_KEY "你的新Key"
  ```
- 如果曾把真实 Key 提交进任何仓库，**立即去百炼后台作废并轮换**，删历史提交也无济于事（Git 历史可恢复）。
- 建议始终使用**私有仓库**。GitHub 公开仓库会自动扫描密钥；私有仓库的推送保护（push protection）属于付费的 Secret Protection，免费版不覆盖。

## 安装

1. 依赖：`ffmpeg` / `ffprobe` 在 PATH 中；Python 3（仅标准库，无需 pip 安装）。
2. 把整个 `bailian-srt/` 目录放到 WorkBuddy 用户级技能目录：
   ```
   C:\Users\<用户名>\.workbuddy-ai\skills\bailian-srt\
   ```
   （macOS / Linux 为 `~/.workbuddy-ai/skills/bailian-srt/`）
3. 复制 `config.example.json` 为 `config.json`，填入自己的 `base_url`；`api_key` 留空并改用环境变量，或自行填入。
4. 重启 WorkBuddy，确认技能列表中出现 `bailian-srt`。

## 使用

对话中直接说：

> 用 `bailian-srt` 给 `E:\fire\你的视频.mp4` 生成字幕

会自动完成：ffmpeg 提取 16k 单声道 → ±15s 静音点切分 → 云端识别 → 词库强制修正 → 语义断句 → 输出同名 `.srt`，临时切片自动删除。

命令行也可单独运行：

```bash
python scripts/transcribe.py <视频路径>          # 生成原始 ASR 字幕
python scripts/polish.py <raw.srt> --review --review-only --video <视频>   # 视觉校对清单
python scripts/polish.py <raw.srt> --output <最终.srt>                      # 词库修正 + 断句
```

## 目录说明

| 文件 | 作用 |
|---|---|
| `SKILL.md` | 技能说明、触发规则、完整工作流 |
| `scripts/transcribe.py` | 音频提取、智能静音切分、云端识别、时间戳合并 |
| `scripts/polish.py` | 词库强制修正、语义断句、时间戳插值、视觉抽帧清单 |
| `references/glossary.json` | **个人高频词库**，新增错字只需在 `replacements` 加一行 |
| `references/api_notes.md` | 百炼接口要点与实测结论 |
| `references/fixes_example.json` | 一次性修正配置示例 |
| `config.example.json` | 脱敏配置模板 |

## 词库维护

编辑 `references/glossary.json`：

```json
"replacements": {
  "新错字": "正确写法"
}
```

纯英文键按整词边界匹配（`Goo`→`GOOL` 不会误伤 `Google`），含中文的键走子串替换，长词优先。词库每次运行自动加载，优先级高于 `--fixes`。

## 说明

SRT 是纯文本、不含样式；上传到 YouTube Studio 等平台后的字幕外观由播放器渲染。
