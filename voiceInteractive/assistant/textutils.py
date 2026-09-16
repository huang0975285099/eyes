"""文本处理、指令识别与音频工具函数。"""

from __future__ import annotations

import json
import re
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
from vosk import KaldiRecognizer, Model

PUNCTUATION_RE = re.compile(r"[\s，。！？,.!?、：:；;‘’“”\-]+")
SPEECH_BOUNDARY_RE = re.compile(r"[。！？!?；;\n]")


def normalize_text(text: str) -> str:
    return PUNCTUATION_RE.sub("", text).casefold()


def take_speech_segments(text: str, flush: bool = False) -> tuple[list[str], str]:
    """Return complete, speakable pieces while keeping an unfinished suffix."""
    segments: list[str] = []
    remaining = text
    while remaining:
        boundary = SPEECH_BOUNDARY_RE.search(remaining)
        if boundary:
            cut = boundary.end()
        elif len(remaining) >= 42:
            comma = max(remaining.rfind("，", 12, 42), remaining.rfind(",", 12, 42))
            cut = comma + 1 if comma >= 0 else 36
        else:
            break
        segment = remaining[:cut].strip()
        remaining = remaining[cut:]
        if segment:
            segments.append(segment)
    if flush and remaining.strip():
        segments.append(remaining.strip())
        remaining = ""
    return segments, remaining


def contains_wake_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    normalized = normalize_text(text)
    return any(normalize_text(phrase) in normalized for phrase in phrases)


def is_time_command(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in ("现在几点", "几点了", "几点"))


def is_weather_command(text: str) -> bool:
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in (
            "天气",
            "气温",
            "温度",
            "多少度",
            "冷不冷",
            "热不热",
            "下雨",
            "降雨",
            "带伞",
            "会下雨",
            "会不会下雨",
            "天气预报",
        )
    )


def is_rain_question(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in ("下雨", "降雨", "有雨", "带伞"))


def is_desktop_command(text: str) -> bool:
    normalized = normalize_text(text)
    application = any(name in normalized for name in ("计算器", "记事本"))
    action = any(
        verb in normalized for verb in ("打开", "启动", "运行", "写入", "输入", "写")
    )
    return application and action


def is_desktop_follow_up(text: str) -> bool:
    normalized = normalize_text(text)
    return (
        "保存到桌面" in normalized
        or "存到桌面" in normalized
        or "另存到桌面" in normalized
        or "运行代码" in normalized
        or "运行程序" in normalized
        or normalized in {"运行", "打开运行", "执行", "执行代码"}
        or (
            "代码" in normalized
            and any(verb in normalized for verb in ("写", "生成", "输入"))
        )
    )


def strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    match = re.fullmatch(r"```[^\n]*\n([\s\S]*?)\n?```", cleaned)
    return match.group(1).strip() if match else cleaned


def parse_person_presence(text: str) -> bool:
    normalized = normalize_text(text).casefold()
    if normalized in {"person", "有人", "检测到人", "画面有人"}:
        return True
    if normalized in {"empty", "无人", "没有人", "未检测到人", "画面无人"}:
        return False
    raise RuntimeError(f"人物检测返回了无法识别的结果：{text.strip()}")


def is_weather_follow_up(text: str) -> bool:
    normalized = normalize_text(text).removesuffix("呢")
    return normalized in {
        "今天",
        "现在",
        "明天",
        "后天",
        "未来三天",
        "三天",
        "会下雨",
        "会不会下雨",
        "下雨吗",
        "有雨吗",
        "要带伞吗",
        "多少度",
        "冷不冷",
        "热不热",
    }


def parse_weather_query(text: str, default_location: str) -> tuple[str, list[int]]:
    normalized = normalize_text(text)
    if "未来三天" in normalized or "三天天气" in normalized:
        day_indexes = [0, 1, 2]
    elif "后天" in normalized:
        day_indexes = [2]
    elif "明天" in normalized:
        day_indexes = [1]
    else:
        day_indexes = [0]

    # Vosk commonly inserts spaces between Chinese words or even characters.
    # Use normalized text for extraction so "查 询 成 都 天 气" becomes "成都".
    location = normalized
    removable = (
        "帮我",
        "给我",
        "我要",
        "我想",
        "想知道",
        "麻烦",
        "请问",
        "请",
        "查询",
        "查一下",
        "查查",
        "搜索",
        "搜一下",
        "看一下",
        "看看",
        "用",
        "一下",
        "未来三天",
        "三天",
        "今天",
        "明天",
        "后天",
        "现在",
        "当地",
        "天气预报",
        "天气",
        "气温",
        "温度",
        "多少度",
        "冷不冷",
        "热不热",
        "会不会下雨",
        "会下雨吗",
        "下雨吗",
        "降雨",
        "有雨吗",
        "有雨",
        "要不要带伞",
        "要带伞吗",
        "带伞",
        "怎么样",
        "如何",
        "情况",
        "预报",
        "的",
        "吗",
        "呢",
    )
    for phrase in sorted(removable, key=len, reverse=True):
        location = location.replace(phrase, "")
    location = re.sub(r"[\s，。！？,.!?、：:；;]+", " ", location).strip()
    return location or default_location, day_indexes


def is_exit_command(text: str) -> bool:
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in (
            "退出助手",
            "关闭助手",
            "停止助手",
            "退出老叶",
            "关闭老叶",
            "停止老叶",
        )
    )


def is_end_conversation_command(text: str) -> bool:
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in (
            "不用了",
            "没事了",
            "结束对话",
            "结束聊天",
            "休息吧",
            "再见",
        )
    )


def is_stop_speaking_command(text: str) -> bool:
    normalized = normalize_text(text)
    for prefix in ("麻烦你", "麻烦", "请你", "请"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    for suffix in ("可以吗", "好吗", "谢谢", "吧"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized in {
        "停",
        "停下",
        "停下来",
        "停一下",
        "别说了",
        "别讲了",
        "别播了",
        "别说话了",
        "不要说了",
        "不要讲了",
        "停止回答",
        "停止播报",
        "安静",
    }


def interruption_action(text: str, wake_phrases: tuple[str, ...]) -> str | None:
    normalized = normalize_text(text)
    for prefix in ("那个", "喂", "哎"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    for phrase in wake_phrases:
        wake_phrase = normalize_text(phrase)
        if normalized == wake_phrase:
            return "wake"
        if normalized.startswith(wake_phrase):
            remainder = normalized[len(wake_phrase) :]
            if is_stop_speaking_command(remainder):
                return "stop"
            # Calling the wake word during playback always stops the current
            # answer and returns to command listening.
            return "wake"
    if is_stop_speaking_command(text):
        return "stop"
    return None


def is_vision_command(text: str) -> bool:
    normalized = normalize_text(text)
    return ("看到" in normalized and "什么" in normalized) or any(
        phrase in normalized
        for phrase in (
            "你看到了什么",
            "你看到什么",
            "你看见了什么",
            "你看见什么",
            "画面里有什么",
            "摄像头里有什么",
            "看看前面",
            "看一下前面",
            "你能看到什么",
        )
    )


def is_vision_follow_up(text: str) -> bool:
    """Recognize short references that usually point at the last camera answer."""
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in (
            "这个",
            "那个",
            "这位",
            "那位",
            "画面",
            "镜头",
            "左边",
            "右边",
            "前面",
            "后面",
            "穿着",
            "戴着",
            "什么颜色",
            "几个人",
            "多少人",
            "男的",
            "女的",
            "老人",
            "年轻人",
            "小孩",
            "孩子",
            "他是",
            "她是",
            "他们",
            "她们",
        )
    )


def is_person_location_query(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in ("在哪", "在哪里", "哪边", "什么位置"))


def select_actionable_recognition(
    alternatives: list[str], wake_phrases: tuple[str, ...], state: str
) -> str:
    if not alternatives:
        return ""

    def is_actionable(candidate: str) -> bool:
        if state == "waiting":
            return contains_wake_phrase(candidate, wake_phrases)
        return any(
            checker(candidate)
            for checker in (
                is_desktop_command,
                is_time_command,
                is_weather_command,
                is_exit_command,
                is_end_conversation_command,
                is_vision_command,
                is_person_location_query,
            )
        )

    if is_actionable(alternatives[0]):
        return alternatives[0]
    return next(
        (candidate for candidate in alternatives[1:] if is_actionable(candidate)),
        alternatives[0],
    )


def build_accent_aware_question(question: str, alternatives: list[str]) -> str:
    unique_alternatives: list[str] = []
    seen = {normalize_text(question)}
    for alternative in alternatives:
        normalized = normalize_text(alternative)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_alternatives.append(alternative.strip())
    if not unique_alternatives:
        return question
    candidates = "；".join(unique_alternatives[:2])
    return (
        f"用户使用带四川口音的普通话。首选识别为：{question}。"
        f"其他识别候选为：{candidates}。"
        "请结合中文语义和对话上下文判断真实问题并直接回答，不要提及识别过程。"
    )


def chinese_number(value: int) -> str:
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    if value < 20:
        return "十" + (digits[value % 10] if value % 10 else "")
    return digits[value // 10] + "十" + (digits[value % 10] if value % 10 else "")


def format_time_zh(now: datetime) -> str:
    if now.hour < 5:
        period = "凌晨"
    elif now.hour < 9:
        period = "早上"
    elif now.hour < 12:
        period = "上午"
    elif now.hour < 14:
        period = "中午"
    elif now.hour < 18:
        period = "下午"
    else:
        period = "晚上"

    hour = now.hour % 12 or 12
    if now.minute == 0:
        minute_text = "整"
    elif now.minute < 10:
        minute_text = f"零{chinese_number(now.minute)}分"
    else:
        minute_text = f"{chinese_number(now.minute)}分"
    return f"现在是{period}{chinese_number(hour)}点{minute_text}。"


def _safe_extract_zip(archive: zipfile.ZipFile, target: Path) -> None:
    target_resolved = target.resolve()
    for member in archive.infolist():
        destination = (target / member.filename).resolve()
        if target_resolved not in destination.parents and destination != target_resolved:
            raise RuntimeError(f"模型压缩包包含不安全路径：{member.filename}")
    archive.extractall(target)


def ensure_model(model_path: Path, model_url: str) -> Path:
    if model_path.is_dir() and (model_path / "conf" / "model.conf").exists():
        return model_path

    model_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path = model_path.parent / f"{model_path.name}.zip.part"
    print(f"首次运行：正在下载离线中文识别模型（约 42 MB）\n{model_url}")

    last_percent = -1

    def show_progress(blocks: int, block_size: int, total: int) -> None:
        nonlocal last_percent
        if total > 0:
            percent = min(100, blocks * block_size * 100 // total)
            if percent != last_percent:
                print(f"\r下载进度：{percent:3d}%", end="", flush=True)
                last_percent = percent

    try:
        urllib.request.urlretrieve(model_url, archive_path, show_progress)
        print("\n正在解压模型……")
        with zipfile.ZipFile(archive_path) as archive:
            _safe_extract_zip(archive, model_path.parent)
    finally:
        archive_path.unlink(missing_ok=True)

    if not (model_path / "conf" / "model.conf").exists():
        raise RuntimeError(f"模型解压后未出现在预期目录：{model_path}")
    return model_path


def _recognizer(model: Model, sample_rate: int, phrases: list[str]) -> KaldiRecognizer:
    grammar = json.dumps([*phrases, "[unk]"], ensure_ascii=False)
    return KaldiRecognizer(model, sample_rate, grammar)


def _result_text(payload: str, key: str) -> str:
    try:
        return str(json.loads(payload).get(key, ""))
    except json.JSONDecodeError:
        return ""


def recognition_alternatives(payload: str) -> list[str]:
    try:
        result = json.loads(payload)
    except json.JSONDecodeError:
        return []
    raw_alternatives = result.get("alternatives", [])
    candidates: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_alternatives, list):
        for item in raw_alternatives:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            normalized = normalize_text(text)
            if text and normalized not in seen:
                seen.add(normalized)
                candidates.append(text)
    direct_text = str(result.get("text", "")).strip()
    if direct_text and normalize_text(direct_text) not in seen:
        candidates.insert(0, direct_text)
    return candidates


def resample_pcm(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or samples.size == 0:
        return samples
    frames = samples.shape[0]
    target_frames = max(1, round(frames * target_rate / source_rate))
    source_x = np.arange(frames, dtype=np.float64)
    target_x = np.linspace(0, frames - 1, target_frames)
    if samples.ndim == 1:
        return np.interp(target_x, source_x, samples).astype(samples.dtype)
    channels = [
        np.interp(target_x, source_x, samples[:, channel])
        for channel in range(samples.shape[1])
    ]
    return np.column_stack(channels).astype(samples.dtype)
