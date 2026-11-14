#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
polish.py — 对 transcribe.py 产出的原始 ASR 字幕做「错别字修正 + 语义断句 + 视觉校对清单」。

分工：
  - HY 模型（本智能体）负责语义判断：审阅字幕与视频截图，产出修正配置 JSON
    （错别字替换表、切片边界被截断句子的合并结果）。
  - 本脚本负责确定性执行：可疑专有名词定位、ffmpeg 抽帧、应用替换、合并边界残句、
    按语义断句，并按字符权重在原有时间区间内插值时间戳，保证连续、不重叠、不倒退。

注意：本地脚本无 HY 模型调用能力。--review 只产出「截图 + 校对清单」供 HY 读图核对，
      HY 把画面上看到的正确写法写入 fixes.json 后再跑本脚本生成最终字幕。

个人词库（自动加载）：
  默认读取 <技能根目录>/references/glossary.json，每次运行都会【优先/强制】应用，
  优先级高于 --fixes 的 replacements。新增高频同音错字只需往词库加一行即可。
  --glossary <路径> 换词库；--no-glossary 临时停用。

用法：
  # 第一步：生成待校对清单与截图
  python polish.py <raw.srt> --review --video <原视频.mp4>
  # 第二步：HY 读图核对，填写 fixes.json
  # 第三步：生成最终字幕（词库自动生效）
  python polish.py <raw.srt> --fixes <fixes.json> --output <final.srt>
  # 仅断句 + 词库强制修正（无需 fixes）
  python polish.py <raw.srt> --max-han 20

fixes.json 格式：
  {
    "replacements": {"一次性错字": "修正", ...},
    "merges": [{"ids": [28, 29], "text": "合并后的正确文本"}]
  }
"""
import argparse
import json
import os
import re
import subprocess

END_PUNCT = "。！？!?；;"
SPLIT_RE = re.compile(r"([^，。！？；：、,!?;:]+[，。！？；：、,!?;:]*)")
ASCII_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._+\-]*$")
GLOSSARY_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "references", "glossary.json")

# 常见且不易误识的词，不列入视觉校对清单；其余英文/数字混合词视为「拿不准」
SAFE_TERMS = {
    "ip", "ai", "ui", "cpu", "gpu", "vpn", "dns", "udp", "tcp", "http", "https",
    "tls", "wifi", "pc", "id", "ok", "app", "log", "bug", "logo", "usb", "gb",
    "mb", "kb", "ms", "kg", "km", "s", "m", "the", "and", "in", "on", "of",
    "github", "google", "youtube", "windows", "android", "ios",
}
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+\-]*|\d+\.\d+")


def parse_srt(path):
    txt = open(path, encoding="utf-8-sig").read().replace("\r\n", "\n")
    out = []
    for b in re.split(r"\n\s*\n", txt.strip()):
        lines = b.strip().split("\n")
        if len(lines) < 3:
            continue
        m = re.match(r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*"
                     r"(\d\d):(\d\d):(\d\d),(\d\d\d)", lines[1])
        if not m:
            continue
        g = m.groups()
        begin = (int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2])) * 1000 + int(g[3])
        end = (int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6])) * 1000 + int(g[7])
        out.append({"id": int(lines[0]), "b": begin, "e": end,
                    "t": "\n".join(lines[2:]).strip()})
    return out


def fmt(ms):
    ms = int(round(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def han_count(s):
    return len(re.findall(r"[\u4e00-\u9fff]", s))


def weight(s):
    """显示权重：汉字 1，其他可见字符 0.5。用于断句与时间插值。"""
    vis = re.sub(r"\s", "", s)
    return han_count(vis) + 0.5 * (len(vis) - han_count(vis))


def hard_split(seg, max_han):
    """无标点超长片段：按字数切，尽量落在空格/英文数字边界。"""
    res, cur = [], ""
    for ch in seg:
        cur += ch
        if han_count(cur) >= max_han or weight(cur) >= max_han * 1.4:
            # 尝试在最后一个空白/中英交界处断开
            cut = len(cur)
            for i in range(len(cur) - 1, max(0, len(cur) - 6), -1):
                if cur[i].isspace():
                    cut = i
                    break
            piece, rest = cur[:cut].strip(), cur[cut:].strip()
            if piece:
                res.append(piece)
            cur = rest
    if cur.strip():
        res.append(cur.strip())
    return res


def break_line(text, max_han):
    """把一条长字幕按语义切成多行，每行汉字数 <= max_han。"""
    text = re.sub(r"\s+", " ", text).strip()
    if han_count(text) <= max_han and weight(text) <= max_han * 1.4:
        return [text]

    frags = [f for f in SPLIT_RE.findall(text) if f.strip()]
    parts = []
    for f in frags:
        if han_count(f) > max_han or weight(f) > max_han * 1.4:
            parts.extend(hard_split(f, max_han))
        else:
            parts.append(f.strip())

    # 贪心合并相邻短片段，尽量贴近但不超上限
    lines, cur = [], ""
    for p in parts:
        cand = (cur + p).strip() if cur else p
        if cur and (han_count(cand) > max_han or weight(cand) > max_han * 1.4):
            lines.append(cur.strip())
            cur = p
        else:
            cur = cand
    if cur.strip():
        lines.append(cur.strip())
    return [l for l in lines if l]


# --------------------------------------------------------------------------- #
# 个人词库（默认自动加载，优先级最高）
# --------------------------------------------------------------------------- #
def load_glossary(path, enabled=True):
    """读取个人词库；返回 (replacements dict, 说明字符串)。失败时优雅降级为空。"""
    if not enabled:
        return {}, "已通过 --no-glossary 停用"
    if not path or not os.path.exists(path):
        return {}, f"词库不存在，跳过：{path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        reps = data.get("replacements", {})
        if not isinstance(reps, dict):
            raise ValueError("replacements 必须是对象")
        clean = {str(k): str(v) for k, v in reps.items()
                 if str(k) and not str(k).startswith("_")}
        return clean, f"加载 {len(clean)} 条：{os.path.abspath(path)}"
    except Exception as e:
        print(f"[glossary] 警告：词库解析失败（{e}），本次跳过：{path}")
        return {}, "解析失败，已跳过"


def merge_replacements(fixes_reps, glossary_reps):
    """词库优先：同名键以词库为准，并提示被覆盖的 fixes 条目。"""
    merged = dict(fixes_reps or {})
    overridden = [k for k in merged if k in glossary_reps
                  and merged[k] != glossary_reps[k]]
    for k in overridden:
        print(f"[glossary] 覆盖 fixes 条目：{k!r} "
              f"{merged[k]!r} -> {glossary_reps[k]!r}")
    merged.update(glossary_reps)
    return merged, overridden


CJK = r"\u4e00-\u9fff"


def normalize_cjk_latin_spacing(text):
    """中文与英文字母/数字相邻时补一个空格。

    ASR 原始输出本身就是「这个 IP」「100 多兆」的风格；词库把中文错字替换成
    英文术语后（如 沃普前置 -> Warp前置）容易粘连，这里统一规整。
    仅处理中文紧邻字母/数字，不动标点、不动已有空格。
    """
    text = re.sub(f"([{CJK}])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(f"([A-Za-z0-9])([{CJK}])", r"\1 \2", text)
    return text


def apply_replacements(items, reps):
    """确定性替换：长词优先；纯英文键按整词边界匹配，其余按子串替换。

    返回实际命中的替换次数，便于验证词库是否真的生效。
    """
    hits = 0
    for key in sorted(reps, key=len, reverse=True):
        val = reps[key]
        if ASCII_KEY_RE.match(key):
            # 整词边界：前后不能是字母/数字/下划线，避免 Goo 误伤 Google
            pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(key) +
                             r"(?![A-Za-z0-9_])")
            for it in items:
                new, n = pat.subn(val, it["t"])
                if n:
                    hits += n
                    it["t"] = new
        else:
            for it in items:
                n = it["t"].count(key)
                if n:
                    hits += n
                    it["t"] = it["t"].replace(key, val)
    return hits


# --------------------------------------------------------------------------- #
# 视觉抽帧校对清单
# --------------------------------------------------------------------------- #
def collect_suspicious(items):
    """找出字幕中「拿不准」的英文/数字专有名词，记录首次出现时间与上下文。"""
    found = {}
    for it in items:
        text = it["t"]
        for m in TOKEN_RE.finditer(text):
            tok = m.group(0).strip("._+-")
            low = tok.lower()
            if len(tok) < 2 or low in SAFE_TERMS or tok.isdigit():
                continue
            # 纯小数点数字（如 2.0）不校对；含字母或形如 1.16 的版本号才保留
            if not re.search(r"[A-Za-z]", tok) and "." not in tok:
                continue
            rec = found.setdefault(tok, {"token": tok, "count": 0,
                                         "time_ms": (it["b"] + it["e"]) // 2,
                                         "context": text})
            rec["count"] += 1
    return sorted(found.values(), key=lambda r: r["time_ms"])


FRAME_WIDTH = 1280        # 强制缩放宽度
FRAME_QUALITY = 4         # JPG 质量（ffmpeg -q:v，越小越好）
FRAME_MAX_KB = 150        # 单张体积上限


def grab_frame(video, time_ms, out_path, width=FRAME_WIDTH,
               quality=FRAME_QUALITY, max_kb=FRAME_MAX_KB):
    """抽一帧：强制 scale=<width>:-1、低体积 JPG(-q:v 4)，并保证 <= max_kb。

    若某帧画面复杂导致超限，按 q 逐级 +2 降质重编码，宽度始终保持 width。
    返回 (是否成功, 错误信息, 元信息 dict)。
    """
    t = max(0.0, time_ms / 1000.0)
    q = quality
    last_err = ""
    for _ in range(6):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}",
               "-i", video, "-frames:v", "1",
               "-vf", f"scale={width}:-1", "-q:v", str(q), out_path]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0 or not os.path.exists(out_path):
            last_err = r.stderr.decode("utf-8", "replace")[:200]
            return False, last_err, {}
        kb = os.path.getsize(out_path) / 1024.0
        if kb <= max_kb:
            return True, "", {"width": width, "quality": q,
                              "kb": round(kb, 1), "retries": q - quality}
        q += 2
    # 极端情况仍超限：最后一次结果保留，但在清单中标记
    return True, "", {"width": width, "quality": q - 2,
                      "kb": round(kb, 1), "retries": q - 2 - quality,
                      "over_limit": True}


def build_review(items, video, frames_dir, max_frames, srt_path):
    """生成截图 + review_manifest.json + 供 HY 阅读的 review.md + fixes 模板。"""
    os.makedirs(frames_dir, exist_ok=True)
    suspects = collect_suspicious(items)
    # 按时间均匀取样，避免整份清单只覆盖片头
    if len(suspects) > max_frames:
        step = len(suspects) / max_frames
        suspects = [suspects[min(len(suspects) - 1, int(i * step))]
                    for i in range(max_frames)]

    entries = []
    for i, rec in enumerate(suspects, 1):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", rec["token"])[:24]
        fname = f"{i:02d}_{safe}.jpg"
        fpath = os.path.join(frames_dir, fname)
        ok, err, meta = grab_frame(video, rec["time_ms"], fpath)
        entries.append({**rec, "time": fmt(rec["time_ms"]),
                        "frame": fname if ok else None, "error": err or None,
                        **meta})

    manifest = {
        "video": os.path.abspath(video),
        "frames_dir": os.path.abspath(frames_dir),
        "srt": os.path.abspath(srt_path),
        "items": entries,
    }
    with open(os.path.join(frames_dir, "review_manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    lines = [
        "# 视觉校对清单",
        "",
        "HY 模型请逐行读取 `frame` 列的截图，核对画面上实际的英文/专有名词写法，",
        "把「字幕里的写法 -> 画面上的正确写法」填入 fixes.json 的 `replacements`。",
        "",
        "| # | 字幕写法 | 时间点 | 截图 | 尺寸/体积 | 出现次数 | 上下文 |",
        "|---|---------|--------|------|-----------|---------|--------|",
    ]
    for i, e in enumerate(entries, 1):
        ctx = e["context"].replace("|", "/")[:60]
        frame = f"`{e['frame']}`" if e["frame"] else "抽帧失败"
        if e.get("kb") is not None:
            flag = " ⚠超限" if e.get("over_limit") else ""
            size = f"{e['width']}px / {e['kb']}KB{flag}"
        else:
            size = "-"
        lines.append(f"| {i} | `{e['token']}` | {e['time']} | {frame} | "
                     f"{size} | {e['count']} | {ctx} |")
    lines += ["", "核对完成后执行：", "",
              "```bash", "python polish.py <raw.srt> --fixes <fixes.json> --output <final.srt>",
              "```", ""]
    with open(os.path.join(frames_dir, "review.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    template = {"replacements": {}, "merges": []}
    with open(os.path.join(frames_dir, "fixes_template.json"), "w",
              encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)

    ok_count = sum(1 for e in entries if e["frame"])
    sizes = [e["kb"] for e in entries if e.get("kb") is not None]
    over = sum(1 for e in entries if e.get("over_limit"))
    print(f"[review] 可疑专有名词 {len(collect_suspicious(items))} 个，"
          f"取样 {len(entries)} 个，成功抽帧 {ok_count} 张")
    if sizes:
        print(f"[review] 截图规格：宽 {FRAME_WIDTH}px / q:v {FRAME_QUALITY}；"
              f"体积 {min(sizes):.1f}~{max(sizes):.1f}KB "
              f"（上限 {FRAME_MAX_KB}KB，超限 {over} 张）")
    print(f"[review] 截图与清单：{os.path.abspath(frames_dir)}")
    print("[review] 下一步：HY 读取截图核对画面文字 -> 填写 fixes_template.json "
          "-> 作为 --fixes 传入本脚本")


def main():
    ap = argparse.ArgumentParser(description="HY 修正后的断句与时间戳插值")
    ap.add_argument("input", help="原始 ASR .srt")
    ap.add_argument("--fixes", help="模型审定的修正配置 JSON")
    ap.add_argument("--output", help="输出最终 .srt（默认覆盖输入）")
    ap.add_argument("--max-han", type=int, default=20, help="单行最多汉字数，默认 20")
    ap.add_argument("--review", action="store_true",
                    help="生成视觉校对清单：定位可疑专有名词并按时间戳抽帧")
    ap.add_argument("--video", help="--review 用的原视频路径（需含画面）")
    ap.add_argument("--frames-dir", help="截图输出目录（默认 <srt 同级>/<名称>_frames）")
    ap.add_argument("--max-frames", type=int, default=12,
                    help="最多抽取几张截图，默认 12")
    ap.add_argument("--review-only", action="store_true",
                    help="仅生成校对清单，不生成字幕")
    ap.add_argument("--glossary", default=GLOSSARY_DEFAULT,
                    help=f"个人词库路径（默认 {GLOSSARY_DEFAULT}）")
    ap.add_argument("--no-glossary", action="store_true",
                    help="临时停用个人词库（用于 A/B 对比或排查误替换）")
    args = ap.parse_args()

    items = parse_srt(args.input)
    if args.review:
        if not args.video:
            ap.error("--review 需要同时提供 --video <原视频路径>")
        if not os.path.exists(args.video):
            ap.error(f"视频不存在：{args.video}")
        fd = args.frames_dir or (os.path.splitext(args.input)[0] + "_frames")
        build_review(items, args.video, fd, args.max_frames, args.input)
        if args.review_only:
            return

    fixes = {"replacements": {}, "merges": []}
    if args.fixes:
        fixes.update(json.load(open(args.fixes, encoding="utf-8")))

    # 词库自动加载（优先级高于 fixes），无论是否传 --fixes 都会生效
    glossary_reps, ginfo = load_glossary(args.glossary, not args.no_glossary)
    print(f"[glossary] {ginfo}")
    reps, overridden = merge_replacements(
        fixes.get("replacements", {}), glossary_reps)

    by_id = {it["id"]: it for it in items}

    # 1) 合并切片边界被截断的句子（由 HY 模型给出正确文本）
    merged_ids = set()
    for mg in fixes.get("merges", []):
        ids, text = mg["ids"], mg["text"]
        first, last = by_id.get(ids[0]), by_id.get(ids[-1])
        if not first or not last:
            continue
        first["t"] = text
        first["e"] = last["e"]
        for i in ids[1:]:
            merged_ids.add(i)
    items = [it for it in items if it["id"] not in merged_ids]

    # 2) 错别字替换（词库优先 + 长词优先 + 英文整词边界）
    hits = apply_replacements(items, reps)
    print(f"[glossary] 共 {len(reps)} 条替换规则（词库 {len(glossary_reps)} 条，"
          f"覆盖 fixes {len(overridden)} 条），实际命中 {hits} 处")
    # 清理标点前后多余空格 + 中英文之间补空格
    for it in items:
        it["t"] = normalize_cjk_latin_spacing(it["t"])
        it["t"] = re.sub(r"\s+([，。！？；、,!?;])", r"\1", it["t"])
        it["t"] = re.sub(r"([，。！？；、,!?;:])\s+", r"\1", it["t"])
        it["t"] = re.sub(r"\s+", " ", it["t"]).strip()

    # 3) 语义断句 + 按权重插值时间戳
    final = []
    for it in items:
        b, e = it["b"], max(it["e"], it["b"] + 200)
        lines = break_line(it["t"], args.max_han)
        ws = [weight(l) for l in lines]
        total = sum(ws) or 1
        t = b
        for i, (line, w) in enumerate(zip(lines, ws)):
            nb = t
            ne = e if i == len(lines) - 1 else t + (e - b) * w / total
            final.append((nb, ne, line))
            t = ne

    out = args.output or args.input
    with open(out, "w", encoding="utf-8") as f:
        for i, (b, e, t) in enumerate(final, 1):
            f.write(f"{i}\n{fmt(b)} --> {fmt(e)}\n{t}\n\n")
    print(f"[polish] {len(items)} 条原始字幕 -> {len(final)} 条，"
          f"最长行 {max(han_count(x[2]) for x in final)} 汉字，输出 {out}")


if __name__ == "__main__":
    main()
