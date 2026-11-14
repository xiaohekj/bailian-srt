#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bailian-srt — 本地视频/音频 -> 中文 SRT 字幕（阿里云百炼云端 ASR）

执行逻辑：
  1. 用本地 ffmpeg 将输入（视频则 -vn 跳过画面）提取/归一化为单声道 16kHz MP3。
  2. 若音频超过 split_threshold（默认 300s），用 silencedetect 扫描静音停顿，
     在每 segment（默认 240s）网格附近的 ±silence_window 内选取最近的静音中点作为真实切点。
     这样每段边界天然落在句末停顿处，避免把一句话硬切成两半。找不到静音时回退等间隔硬切。
  3. 对每个片段并发识别：默认 qwen-audio-3.1-asr-flash，以 Base64 Data URI 本地内存直传，
     无需 OSS / 公网地址。同步模式一次拿到整段词级时间戳，再按句末标点切成句子。
     （高级选项：显式配置 oss + model=paraformer-v2 可走异步文件转写。）
  4. 按每个片段的**真实切点**作为时间偏移量合并所有时间戳，写出 SRT。
  5. 清理临时切片（及可能产生的 OSS 对象）。

中文错别字的修正与语义断句（单行 <=20 汉字）由 HY 模型（本智能体）在脚本跑完后执行，
详见 SKILL.md。本脚本只负责「识别 + 时间戳合并」，不跑任何本地 AI 模型。

依赖：ffmpeg/ffprobe 在 PATH 中；Python 3 标准库即可，无需 pip 安装。
"""

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(SKILL_ROOT, "config.json")

DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://dashscope.aliyuncs.com/api/v1",
    "model": "qwen-audio-3.1-asr-flash",
    "split_threshold": 300,
    "segment": 240,
    # 智能静音切分：在 segment 网格附近寻找最近的静音停顿作为真实切点
    "smart_split": True,
    "silence_window": 15,          # 固定 240s 网格前后各找 ±15s 内的静音
    "silence_noise_db": -35,       # 低于该阈值视为静音
    "silence_min_duration": 0.35,  # 静音最短持续秒数
    "max_segment_seconds": 290,    # 单段硬上限，留余量满足模型 <=5 分钟限制
    "max_workers": 4,
    "oss": {
        "endpoint": "",
        "bucket": "",
        "access_key_id": "",
        "access_key_secret": "",
    },
    "language_hints": ["zh"],
}

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm",
             ".m4v", ".ts", ".mpeg", ".mpg", ".3gp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
             ".opus", ".wma", ".amr"}


def log(msg):
    print(f"[bailian-srt] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            log(f"警告：读取 config.json 失败：{e}")
    env_key = os.environ.get("BAILIAN_API_KEY")
    if env_key:
        cfg["api_key"] = env_key
    return cfg


# --------------------------------------------------------------------------- #
# ffmpeg / ffprobe
# --------------------------------------------------------------------------- #
def run(cmd):
    log("运行: " + " ".join(cmd))
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg/ffprobe 失败: " +
                           r.stderr.decode("utf-8", "replace")[:600])
    return r


def probe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        return float(r.stdout.decode().strip())
    except Exception:
        return 0.0


def extract_audio(input_path, out_path, is_video):
    if is_video:
        cmd = ["ffmpeg", "-y", "-i", input_path, "-vn",
               "-ac", "1", "-ar", "16000", "-b:a", "64k", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", input_path,
               "-ac", "1", "-ar", "16000", "-b:a", "64k", out_path]
    run(cmd)


def detect_silence_midpoints(audio_path, cfg):
    """用 ffmpeg silencedetect 找出静音停顿，返回每个静音区间的中点时刻（秒）。"""
    af = (f"silencedetect=noise={cfg['silence_noise_db']}dB:"
          f"d={cfg['silence_min_duration']}")
    cmd = ["ffmpeg", "-hide_banner", "-i", audio_path,
           "-af", af, "-f", "null", "-"]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = r.stderr.decode("utf-8", "replace")
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", err)]
    mids = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else s + cfg["silence_min_duration"]
        if e > s:
            mids.append((s + e) / 2.0)
    return mids


def compute_cut_points(duration, cfg, silences):
    """生成真实切点（含 0 与 duration）。

    以**固定网格** k*segment（240/480/720 ...）为目标，在 ±silence_window 内
    选最近的静音中点作为该段的真实起始时间；窗口内无静音则兜底为网格硬切。
    每段的时间戳偏移量严格使用该段的真实起始时间 cuts[i]（不是 i*segment），
    因此不会出现累计漂移。

    约束：切点严格递增、每段长度落在 [60s, max_segment_seconds]、
    尾部不留 <20s 的碎片。返回 (cuts, snapped, nseg, fallback)。
    """
    segment = cfg["segment"]
    window = cfg["silence_window"]
    max_seg = cfg["max_segment_seconds"]
    cuts = [0.0]
    snapped = 0
    fallback = 0
    k = 1
    while True:
        target = k * segment
        if target >= duration - 20:
            break
        cands = [s for s in silences if abs(s - target) <= window]
        if cands:
            c = min(cands, key=lambda s: abs(s - target))
            snapped += 1
        else:
            c = float(target)  # 窗口内无静音 -> 兜底硬切
            fallback += 1
        # 保证严格递增与段长上限（窗口偏移不会突破，这里是防御性约束）
        c = max(c, cuts[-1] + 60.0)
        c = min(c, cuts[-1] + max_seg)
        if duration - c < 20:
            break
        cuts.append(round(c, 3))
        k += 1
    cuts.append(round(duration, 3))
    return cuts, snapped, len(cuts) - 1, fallback


def split_audio(audio_path, out_dir, cuts):
    """按真实切点切分；返回 [(片段路径, 该片段起始秒数)]，起始秒数即时间戳偏移量。"""
    segs = []
    for i in range(len(cuts) - 1):
        start, end = cuts[i], cuts[i + 1]
        seg_path = os.path.join(out_dir, f"seg_{i:03d}.mp3")
        cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", audio_path,
               "-t", f"{end - start:.3f}", "-ac", "1", "-ar", "16000",
               "-b:a", "64k", seg_path, "-avoid_negative_ts", "make_zero"]
        run(cmd)
        segs.append((seg_path, start))
    return segs


# --------------------------------------------------------------------------- #
# SRT 工具
# --------------------------------------------------------------------------- #
def ms_to_srt(ms):
    ms = int(round(ms))
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fix_end_times(items):
    items = sorted(items, key=lambda x: x[0])
    for i, it in enumerate(items):
        b, e, t = it
        if e <= b:
            nxt = items[i + 1][0] if i + 1 < len(items) else b + 1500
            e = max(b + 200, nxt)
        items[i] = (b, e, t)
    return items


def write_srt(path, items):
    items = [it for it in items if it[2].strip()]
    items = fix_end_times(items)
    items.sort(key=lambda x: x[0])
    with open(path, "w", encoding="utf-8") as f:
        for i, (b, e, t) in enumerate(items, 1):
            f.write(f"{i}\n{ms_to_srt(b)} --> {ms_to_srt(e)}\n{t.strip()}\n\n")


# --------------------------------------------------------------------------- #
# HTTP 工具
# --------------------------------------------------------------------------- #
def _retryable(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def http_post_json(url, headers, body, timeout=600, retries=4):
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if not _retryable(e) or attempt == retries - 1:
                raise
            last = e
            wait = 2 ** attempt * 2
            log(f"  请求失败({type(e).__name__})，{wait}s 后重试 [{attempt+1}/{retries}]")
            time.sleep(wait)
    raise last


def http_get_json(url, headers, timeout=600, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if not _retryable(e) or attempt == retries - 1:
                raise
            last = e
            time.sleep(2 ** attempt * 2)
    raise last


# --------------------------------------------------------------------------- #
# OSS 上传 / 删除（用于 paraformer/fun-asr 取得公网地址）
# --------------------------------------------------------------------------- #
def _oss_signature(verb, content_type, date, bucket, key, ak_secret):
    canonical = f"{verb}\n\n{content_type}\n{date}\n/{bucket}/{key}"
    return base64.b64encode(
        hmac.new(ak_secret.encode(), canonical.encode(), hashlib.sha1).digest()
    ).decode()


def oss_upload(local_path, cfg, key):
    oss = cfg["oss"]
    endpoint, bucket = oss["endpoint"], oss["bucket"]
    ak, sk = oss["access_key_id"], oss["access_key_secret"]
    url = f"https://{bucket}.{endpoint}/{key}"
    data = open(local_path, "rb").read()
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    ctype = "audio/mpeg"
    sig = _oss_signature("PUT", ctype, date, bucket, key, sk)
    headers = {
        "Date": date,
        "Content-Type": ctype,
        "Authorization": f"OSS {ak}:{sig}",
        "x-oss-object-acl": "public-read",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    urllib.request.urlopen(req, timeout=180).read()
    return url


def oss_delete(key, cfg):
    oss = cfg["oss"]
    endpoint, bucket = oss["endpoint"], oss["bucket"]
    ak, sk = oss["access_key_id"], oss["access_key_secret"]
    url = f"https://{bucket}.{endpoint}/{key}"
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    sig = _oss_signature("DELETE", "", date, bucket, key, sk)
    headers = {"Date": date, "Authorization": f"OSS {ak}:{sig}"}
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:
        log(f"  警告：删除 OSS 对象 {key} 失败：{e}")


# --------------------------------------------------------------------------- #
# 识别后端
# --------------------------------------------------------------------------- #
def asr_paraformer(seg_path, offset_ms, cfg, oss_keys):
    base = cfg["base_url"].rstrip("/")
    api_key = cfg["api_key"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    key = f"bailian-srt/{int(time.time()*1000)}_{os.path.basename(seg_path)}"
    url = oss_upload(seg_path, cfg, key)
    oss_keys.append(key)

    body = {
        "model": cfg["model"],
        "input": {"file_urls": [url]},
        "parameters": {
            "channel_id": [0],
            "language_hints": cfg.get("language_hints", ["zh"]),
        },
    }
    task = http_post_json(base + "/services/audio/asr/transcription", headers, body)
    task_id = task["output"]["task_id"]
    log(f"  任务 {task_id} 已提交，轮询中...")

    result = None
    h = {"Authorization": f"Bearer {api_key}"}
    for _ in range(600):
        try:
            res = http_get_json(base + "/tasks/" + task_id, h)
        except urllib.error.HTTPError:
            res = http_post_json(base + "/tasks/" + task_id, h, {})
        status = res.get("output", {}).get("task_status")
        if status == "SUCCEEDED":
            result = res
            break
        if status in ("FAILED", "CANCELED"):
            raise RuntimeError("ASR 任务失败: " +
                               json.dumps(res, ensure_ascii=False)[:600])
        time.sleep(3)
    if not result:
        raise RuntimeError("ASR 任务轮询超时")

    items = []
    for item in result["output"]["results"]:
        if item.get("subtask_status") != "SUCCEEDED":
            continue
        tj = http_get_json(item["transcription_url"], {})
        for tr in tj.get("transcripts", []):
            for s in tr.get("sentences", []):
                b = s.get("begin_time", 0) + offset_ms
                e = s.get("end_time", 0) + offset_ms
                items.append((b, e, s.get("text", "")))
    return items


def _sentences_from_words(words, offset_ms, text_fallback=""):
    """把词级时间戳按句末标点切成句子级字幕条目。

    该端点返回的是「累积全文 + 词级时间戳」，不能把每个 sentence 事件当成一句，
    否则会产生大量重复。词级 words 才是可靠的时间戳来源。
    """
    if not words:
        if text_fallback:
            return [(0 + offset_ms, 1000 + offset_ms, text_fallback)]
        return []
    END = "。！？!?；;"
    items = []
    cur, b, e = [], None, None
    for w in words:
        wb = w.get("begin_time")
        we = w.get("end_time")
        if b is None:
            b = wb
        e = we if we is not None else e
        cur.append(w.get("text", "") + (w.get("punctuation") or ""))
        if any(ch in (w.get("punctuation") or "") for ch in END):
            items.append((b + offset_ms, (e or b) + offset_ms, "".join(cur).strip()))
            cur, b, e = [], None, None
    if cur and b is not None:
        items.append((b + offset_ms, (e or b) + offset_ms, "".join(cur).strip()))
    return items


def asr_qwen(seg_path, offset_ms, cfg):
    """qwen-audio-3.1-asr-flash 同步（SSE=disable）：一次拿到整段词级时间戳。"""
    base = cfg["base_url"].rstrip("/")
    api_key = cfg["api_key"]
    b64 = base64.b64encode(open(seg_path, "rb").read()).decode()
    data_uri = f"data:audio/mpeg;base64,{b64}"
    body = {
        "model": "qwen-audio-3.1-asr-flash",
        "input": {
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "input_audio",
                    "input_audio": {"data": data_uri},
                }],
            }]
        },
        "parameters": {
            "format": "mp3",
            "sample_rate": "16000",
            "language_hints": cfg.get("language_hints", ["zh"]),
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "disable",
    }
    url = base + "/services/aigc/multimodal-generation/generation"
    ev = http_post_json(url, headers, body, timeout=900)

    # 兼容两种响应结构：官方 output.sentence 与该端点额外提供的扁平 sentence
    out = ev.get("output") or {}
    sent = (out.get("sentence") if isinstance(out, dict) else None) or ev.get("sentence") or {}
    words = sent.get("words") or []
    text = sent.get("text") or out.get("text") or ev.get("text") or ""

    # 少数情况下会返回 sentences 列表，按 sentence_id 去重后逐一取词
    more = (out.get("sentences") if isinstance(out, dict) else None) or ev.get("sentences")
    if not words and isinstance(more, list):
        for s in more:
            if s.get("words"):
                words = s["words"]
                text = s.get("text", text)
                break

    items = _sentences_from_words(words, offset_ms, text)
    log(f"  片段 offset={offset_ms}ms: {len(words)} 词 -> {len(items)} 句")
    return items


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="bailian-srt: 本地视频/音频 -> 中文 SRT 字幕 (百炼云端 ASR)")
    ap.add_argument("input", help="本地视频或音频文件路径")
    ap.add_argument("--output", help="输出 .srt 路径（默认：与输入同目录同名 .srt）")
    ap.add_argument("--model",
                    help="覆盖模型，如 paraformer-v2 / fun-asr / qwen-audio-3.1-asr-flash")
    ap.add_argument("--keep-temp", action="store_true", help="保留临时文件（调试用）")
    ap.add_argument("--max-segments", type=int, default=0,
                    help="仅处理前 N 个片段（调试/冒烟测试用，0=全部）")
    ap.add_argument("--no-smart-split", action="store_true",
                    help="关闭静音点切分，回退为固定 240s 硬切（用于对比）")
    args = ap.parse_args()

    cfg = load_config()
    if args.model:
        cfg["model"] = args.model
    if args.no_smart_split:
        cfg["smart_split"] = False
    if not cfg.get("api_key"):
        sys.exit("错误：未配置 API Key。请在 config.json 或环境变量 BAILIAN_API_KEY 中设置。")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("错误：未检测到 ffmpeg/ffprobe，请先安装并加入 PATH。")

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        sys.exit(f"错误：输入文件不存在：{input_path}")
    ext = os.path.splitext(input_path)[1].lower()
    is_video = ext in VIDEO_EXT
    if not is_video and ext not in AUDIO_EXT:
        log(f"警告：未知扩展名 {ext}，将尝试作为音频处理。")

    out_path = args.output or (os.path.splitext(input_path)[0] + ".srt")
    out_path = os.path.abspath(out_path)

    tmpdir = os.path.join(
        os.path.dirname(out_path),
        "." + os.path.splitext(os.path.basename(out_path))[0] + "_bailian_tmp")
    os.makedirs(tmpdir, exist_ok=True)
    oss_keys = []
    try:
        audio_path = os.path.join(tmpdir, "audio_16k.mp3")
        log(f"步骤1：{'视频提取' if is_video else '音频归一化'}为 16k 单声道 MP3 ...")
        extract_audio(input_path, audio_path, is_video)
        duration = probe_duration(audio_path)
        log(f"  音频时长：{duration:.1f}s")

        model = cfg["model"]
        use_paraformer = model in ("paraformer-v2", "paraformer-8k-v2", "fun-asr")
        oss_ok = all(cfg.get("oss", {}).get(k)
                     for k in ("endpoint", "bucket", "access_key_id", "access_key_secret"))
        if use_paraformer and not oss_ok:
            log("提示：paraformer/fun-asr 只接受公网 URL，但未配置 OSS，自动回退 qwen-audio-3.1-asr-flash。")
            use_paraformer = False

        if duration > cfg["split_threshold"]:
            if cfg.get("smart_split", True):
                log(f"步骤2：智能静音切分（目标每 {cfg['segment']}s，"
                    f"±{cfg['silence_window']}s 内找静音停顿）...")
                silences = detect_silence_midpoints(audio_path, cfg)
                log(f"  检测到 {len(silences)} 处静音停顿")
                cuts, snapped, nseg, fallback = compute_cut_points(
                    duration, cfg, silences)
                if snapped == 0:
                    log("  警告：所有窗口内都没有可用静音，已全部回退为等间隔硬切")
                log(f"  {snapped}/{nseg} 个切点对齐静音，"
                    f"{fallback} 个窗口无静音已硬切兜底")
                log(f"  固定网格目标(s)={', '.join(str(i*cfg['segment']) for i in range(1, nseg))}")
                log("  真实切点(s)=" + ", ".join(f"{c:.3f}" for c in cuts))
                log("  各段时长(s)=" +
                    ", ".join(f"{cuts[i+1]-cuts[i]:.1f}" for i in range(len(cuts)-1)))
            else:
                log(f"步骤2：固定每 {cfg['segment']}s 硬切（已关闭智能切分）...")
                n = max(1, math.ceil(duration / cfg["segment"]))
                cuts = [min(i * cfg["segment"], duration) for i in range(n)] + [duration]
                cuts = sorted(set(round(c, 3) for c in cuts))
            # 调试模式：先截断切点再切分，避免生成用不到的片段文件
            if args.max_segments and len(cuts) - 1 > args.max_segments:
                log(f"  [调试] 仅切分并处理前 {args.max_segments} 个片段")
                cuts = cuts[:args.max_segments + 1]
            segs = split_audio(audio_path, tmpdir, cuts)
        else:
            log("步骤2：音频未超过阈值，无需切分，单段处理。")
            segs = [(audio_path, 0)]
        log(f"  共 {len(segs)} 个片段")

        log(f"步骤3：并发识别（最大并发 {cfg['max_workers']}）...")
        oss_keys_local = []

        def worker(seg):
            seg_path, start = seg
            # 严格使用每段真实起始时间做偏移累加（不是 i*segment）
            offset = int(round(start * 1000))
            log(f"  片段 {os.path.basename(seg_path)} 真实起始={start:.3f}s "
                f"-> 时间戳偏移={offset}ms")
            if use_paraformer:
                return asr_paraformer(seg_path, offset, cfg, oss_keys_local)
            return asr_qwen(seg_path, offset, cfg)

        with ThreadPoolExecutor(max_workers=cfg["max_workers"]) as ex:
            results = list(ex.map(worker, segs))
        oss_keys.extend(oss_keys_local)

        all_items = []
        for r in results:
            all_items.extend(r)
        all_items = fix_end_times(all_items)
        all_items.sort(key=lambda x: x[0])

        log(f"步骤4：合并得到 {len(all_items)} 条字幕，写入 {out_path}")
        write_srt(out_path, all_items)
        log("完成：原始 ASR 字幕已生成。后续由 HY 模型做错别字修正与 <=20 字断句。")
    finally:
        if not args.keep_temp:
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
        for key in oss_keys:
            try:
                oss_delete(key, cfg)
            except Exception:
                pass


if __name__ == "__main__":
    main()
