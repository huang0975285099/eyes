"""联网搜索工具与桌面操作工具。"""

from __future__ import annotations

import ast
import ctypes
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
import urllib.request

from .config import Config
from .platform_utils import build_proxy_opener
from .textutils import (
    is_desktop_follow_up,
    is_rain_question,
    normalize_text,
    parse_weather_query,
    strip_code_fence,
)


class OnlineSearchTools:
    WEATHER_URL = "https://uapis.cn/api/v1/misc/weather"
    WEATHER_CACHE_SECONDS = 5 * 60
    WEATHER_STALE_SECONDS = 30 * 60

    def __init__(self, config: Config) -> None:
        self.config = config
        self.last_weather_location = ""
        self._forecast_cache: dict[str, tuple[float, dict]] = {}
        self._opener = build_proxy_opener(getattr(config, "network_proxy", ""))

    def start_conversation(self) -> None:
        self.last_weather_location = ""

    def _get_json(self, url: str, parameters: dict) -> dict:
        request = urllib.request.Request(
            f"{url}?{urlencode(parameters)}",
            headers={
                "User-Agent": "LaoyeVoiceAssistant/1.0",
                "Connection": "close",
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.config.internet_retry_count + 1):
            try:
                with self._opener.open(
                    request, timeout=self.config.internet_timeout_seconds
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as error:
                last_error = error
                if attempt < self.config.internet_retry_count:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"请求失败：{last_error}") from last_error

    @staticmethod
    def _number(value) -> int:
        return int(round(float(value)))

    def _get_forecast(self, location: str) -> tuple[dict, bool]:
        cache_key = normalize_text(location)
        cached = self._forecast_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] <= self.WEATHER_CACHE_SECONDS:
            return cached[1], False
        try:
            forecast = self._get_json(
                self.WEATHER_URL,
                {
                    "city": location,
                    "forecast": "true",
                    "extended": "true",
                    "hourly": "true",
                    "lang": "zh",
                },
            )
        except Exception:
            if cached and now - cached[0] <= self.WEATHER_STALE_SECONDS:
                return cached[1], True
            raise
        cache_entry = (now, forecast)
        self._forecast_cache[cache_key] = cache_entry
        canonical_city = normalize_text(str(forecast.get("city", "")))
        if canonical_city:
            self._forecast_cache[canonical_city] = cache_entry
        return forecast, False

    def search_weather(self, question: str) -> str:
        if not self.config.internet_tools_enabled:
            raise RuntimeError("工具已在配置中关闭")
        location_query, day_indexes = parse_weather_query(
            question,
            self.last_weather_location or self.config.weather_default_location,
        )
        if not location_query:
            return "请告诉我需要查询哪个城市的天气。"

        forecast, used_stale_cache = self._get_forecast(location_query)
        city = str(forecast.get("city", location_query)).strip()
        if not city or "weather" not in forecast:
            message = str(forecast.get("message") or forecast.get("error") or "")
            raise RuntimeError(message or f"没有找到地点“{location_query}”")
        self.last_weather_location = city
        future_days = forecast.get("forecast", [])
        day_names = ("今天", "明天", "后天")
        descriptions: list[str] = []
        rain_question = is_rain_question(question)
        for day_index in day_indexes:
            daily = None
            if day_index < len(future_days):
                daily = future_days[day_index]
            if rain_question:
                descriptions.append(
                    self._describe_rain(forecast, daily, day_index, day_names[day_index])
                )
                continue
            if day_index == 0:
                try:
                    condition = str(forecast["weather"])
                    temperature = self._number(forecast["temperature"])
                    apparent = self._number(forecast["feels_like"])
                    high = self._number(forecast["temp_max"])
                    low = self._number(forecast["temp_min"])
                    humidity = self._number(forecast["humidity"])
                except (KeyError, TypeError, ValueError) as error:
                    raise RuntimeError("国内天气服务没有返回完整实况") from error
                wind = (
                    f"{forecast.get('wind_direction', '')}"
                    f"{forecast.get('wind_power', '')}"
                ).strip()
                air_quality = str(forecast.get("aqi_category", "")).strip()
                descriptions.append(
                    f"今天{condition}，当前{temperature}度，体感{apparent}度，"
                    f"最高{high}度，最低{low}度，湿度{humidity}%"
                    + (f"，{wind}" if wind else "")
                    + (f"，空气质量{air_quality}" if air_quality else "")
                )
            else:
                try:
                    if daily is None:
                        raise IndexError
                    high = self._number(daily["temp_max"])
                    low = self._number(daily["temp_min"])
                    rain_probability = self._number(daily["pop"])
                    day_weather = str(daily["weather_day"])
                    night_weather = str(daily["weather_night"])
                except (IndexError, KeyError, TypeError, ValueError) as error:
                    raise RuntimeError("国内天气服务返回的预报不完整") from error
                condition = (
                    day_weather
                    if day_weather == night_weather
                    else f"{day_weather}转{night_weather}"
                )
                descriptions.append(
                    f"{day_names[day_index]}{condition}，最高{high}度，"
                    f"最低{low}度，降雨概率{rain_probability}%"
                )

        province = str(forecast.get("province", "")).strip()
        location_name = city
        if province and province not in city:
            location_name = f"{city}，{province}"
        answer = f"{location_name}：" + "；".join(descriptions) + "。"
        if used_stale_cache:
            answer += "天气服务刚刚连接不稳，这是最近一次成功查询的数据。"
        return answer

    @staticmethod
    def _contains_rain(value) -> bool:
        return "雨" in str(value)

    def _describe_rain(
        self, forecast: dict, daily: dict | None, day_index: int, day_name: str
    ) -> str:
        daily = daily or {}
        probabilities: list[int] = []
        try:
            probabilities.append(self._number(daily.get("pop", 0)))
        except (TypeError, ValueError):
            pass

        conditions = [
            str(daily.get("weather_day", "")),
            str(daily.get("weather_night", "")),
        ]
        try:
            precipitation = float(daily.get("precip", 0) or 0)
        except (TypeError, ValueError):
            precipitation = 0.0

        rain_hours: list[tuple[int, str]] = []
        if day_index == 0:
            target_date = str(daily.get("date", ""))
            for hour in forecast.get("hourly_forecast", []) or []:
                time_text = str(hour.get("time", ""))
                if target_date and not time_text.startswith(target_date):
                    continue
                try:
                    probability = self._number(hour.get("pop", 0) or 0)
                except (TypeError, ValueError):
                    probability = 0
                probabilities.append(probability)
                try:
                    hourly_precipitation = float(hour.get("precip", 0) or 0)
                except (TypeError, ValueError):
                    hourly_precipitation = 0.0
                condition = str(hour.get("weather", ""))
                if (
                    probability >= 30
                    or hourly_precipitation > 0
                    or self._contains_rain(condition)
                ):
                    match = re.search(r"\s(\d{1,2}):", time_text)
                    if match:
                        rain_hours.append((int(match.group(1)), condition))

        max_probability = max(probabilities, default=0)
        has_rain = (
            bool(rain_hours)
            or precipitation > 0
            or any(self._contains_rain(condition) for condition in conditions)
            or max_probability >= 30
        )
        if not has_rain:
            probability_text = (
                f"，最高降雨概率{max_probability}%" if probabilities else ""
            )
            return f"{day_name}大概率不会下雨{probability_text}，通常不用带伞"

        time_text = ""
        if rain_hours:
            first_hour = rain_hours[0][0]
            last_hour = rain_hours[-1][0]
            period = (
                f"{first_hour}点前后"
                if first_hour == last_hour
                else f"{first_hour}点到{last_hour}点"
            )
            hourly_conditions = [item[1] for item in rain_hours if item[1]]
            rain_type = next(
                (item for item in hourly_conditions if self._contains_rain(item)), "降雨"
            )
            time_text = f"，预计{period}有{rain_type}"
        elif conditions[0] and conditions[1]:
            if conditions[0] == conditions[1]:
                time_text = f"，预计有{conditions[0]}"
            else:
                time_text = f"，白天{conditions[0]}、夜间{conditions[1]}"

        probability_text = (
            f"，最高降雨概率{max_probability}%" if probabilities else ""
        )
        return f"{day_name}会下雨{time_text}{probability_text}，建议带伞"


class DesktopTools:
    """Small, explicit allowlist of local desktop actions."""

    BLOCKED_PYTHON_MODULES = {
        "ctypes",
        "os",
        "pathlib",
        "shutil",
        "socket",
        "subprocess",
        "winreg",
    }
    BLOCKED_PYTHON_CALLS = {"compile", "eval", "exec", "open", "__import__"}

    def __init__(
        self,
        model_router=None,
        notes_dir: Path | None = None,
        desktop_dir: Path | None = None,
        run_timeout_seconds: float = 10.0,
    ) -> None:
        self.model_router = model_router
        self.notes_dir = notes_dir or Path(tempfile.gettempdir()) / "LaoyeVoiceAssistant"
        self.desktop_dir = desktop_dir
        self.run_timeout_seconds = max(1.0, float(run_timeout_seconds))
        self.current_document: Path | None = None
        self.current_code_suffix = ""
        self.current_code_request = ""
        self.last_run_error = ""
        self.pending_fix_confirmation = False
        self.pending_run_confirmation = False

    @property
    def context_active(self) -> bool:
        return self.current_document is not None

    def start_conversation(self) -> None:
        self.current_document = None
        self.current_code_suffix = ""
        self.current_code_request = ""
        self.last_run_error = ""
        self.pending_fix_confirmation = False
        self.pending_run_confirmation = False

    def can_handle_follow_up(self, text: str) -> bool:
        normalized = normalize_text(text)
        confirmation = normalized in {
            "好",
            "好的",
            "可以",
            "确认",
            "是",
            "是的",
            "修改吧",
            "确认修改",
            "运行吧",
            "确认运行",
        }
        cancellation = normalized in {"不用", "不要", "取消", "不用了", "先不修改"}
        return is_desktop_follow_up(text) or (
            (self.pending_fix_confirmation or self.pending_run_confirmation)
            and (confirmation or cancellation)
        )

    @staticmethod
    def _wants_code(text: str) -> bool:
        normalized = normalize_text(text)
        return "代码" in normalized and any(
            verb in normalized for verb in ("写", "生成", "输入")
        )

    @staticmethod
    def _literal_text(text: str) -> str:
        for pattern in (r"写\s*入", r"输\s*入", r"写\s*上", r"记\s*下"):
            match = re.search(pattern, text)
            if match:
                return text[match.end() :].strip(" ，。！？,.!?、：:；;")
        normalized = normalize_text(text)
        if "写" in normalized and "代码" not in normalized:
            return normalized.split("写", 1)[1].strip()
        return ""

    @staticmethod
    def _code_suffix(text: str) -> str:
        normalized = normalize_text(text).casefold()
        if "sql" in normalized or "数据库" in normalized:
            return ".sql"
        if "javascript" in normalized or "js代码" in normalized:
            return ".js"
        if "java代码" in normalized:
            return ".java"
        return ".py"

    @staticmethod
    def _code_request(text: str) -> str:
        request = re.sub(
            r"请?\s*(?:打开|启动|运行)?\s*(?:在\s*)?记\s*事\s*本(?:里|中)?"
            r"[\s，,、。；;]*(?:然后)?",
            "",
            text,
            count=1,
        ).strip(" ，。！？,.!?、：:；;")
        generic_requests = {
            "写代码",
            "写几行代码",
            "写一段代码",
            "生成代码",
            "输入代码",
        }
        if normalize_text(request) in generic_requests or not request:
            return "使用Python写一个简短的问候程序，并打印当前时间"
        return request

    def _new_document(self, text: str = "") -> Path:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        note_path = self.notes_dir / f"laoye_note_{timestamp}.txt"
        note_path.write_text(text, encoding="utf-8")
        self.current_document = note_path
        self.current_code_suffix = ""
        self.current_code_request = ""
        self.last_run_error = ""
        self.pending_fix_confirmation = False
        self.pending_run_confirmation = False
        return note_path

    @staticmethod
    def _open_notepad(note_path: Path) -> None:
        subprocess.Popen(["notepad.exe", str(note_path)])

    def _write_current(self, text: str, code_suffix: str = "") -> Path:
        note_path = self.current_document or self._new_document()
        note_path.write_text(text, encoding="utf-8")
        if code_suffix:
            self.current_code_suffix = code_suffix
        self.last_run_error = ""
        self.pending_fix_confirmation = False
        self.pending_run_confirmation = False
        self._open_notepad(note_path)
        return note_path

    def _desktop_directory(self) -> Path:
        if self.desktop_dir is not None:
            return self.desktop_dir
        buffer = ctypes.create_unicode_buffer(260)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer)
        if result != 0 or not buffer.value:
            raise RuntimeError("无法找到桌面目录")
        return Path(buffer.value)

    def _requested_filename(self, command: str, suffix: str) -> str:
        match = re.search(
            r"(?:文件\s*名(?:叫|为|是)?|命名为)\s*([^，。！？,.!?、；;]+)",
            command,
        )
        if not match:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return f"老叶代码_{timestamp}{suffix}"
        filename = match.group(1).strip().replace("点py", ".py").replace("点txt", ".txt")
        filename = re.sub(r'[<>:"/\\|?*]', "_", Path(filename).name).rstrip(". ")
        if not filename:
            raise RuntimeError("没有识别到有效文件名")
        if not Path(filename).suffix:
            filename += suffix
        return filename

    def _save_to_desktop(self, command: str) -> str:
        if self.current_document is None or not self.current_document.exists():
            raise RuntimeError("当前没有可保存的记事本内容")
        desktop = self._desktop_directory()
        desktop.mkdir(parents=True, exist_ok=True)
        suffix = self.current_code_suffix or self.current_document.suffix or ".txt"
        requested_name = self._requested_filename(command, suffix)
        destination = desktop / requested_name
        stem = destination.stem
        destination_suffix = destination.suffix
        counter = 2
        while destination.exists():
            destination = desktop / f"{stem}_{counter}{destination_suffix}"
            counter += 1
        shutil.copy2(self.current_document, destination)
        self.current_document = destination
        self._open_notepad(destination)
        return f"已保存到桌面，文件名是{destination.name}。"

    @classmethod
    def _validate_python_for_run(cls, code: str) -> None:
        try:
            tree = ast.parse(code)
        except SyntaxError as error:
            raise RuntimeError(f"Python代码存在语法错误：{error.msg}") from error
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = (
                    [alias.name.split(".", 1)[0] for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [str(node.module or "").split(".", 1)[0]]
                )
                if any(module in cls.BLOCKED_PYTHON_MODULES for module in modules):
                    raise RuntimeError("代码包含文件、网络或系统操作，已阻止运行")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in cls.BLOCKED_PYTHON_CALLS:
                    raise RuntimeError("代码包含动态执行或文件操作，已阻止运行")

    @staticmethod
    def _short_output(value: str, limit: int = 240) -> str:
        cleaned = re.sub(r"\s+", " ", value).strip()
        return cleaned if len(cleaned) <= limit else cleaned[:limit].rstrip() + "，后面省略"

    def _run_current(self) -> str:
        if self.current_document is None or not self.current_document.exists():
            raise RuntimeError("当前没有可以运行的代码")
        suffix = self.current_code_suffix or self.current_document.suffix.casefold()
        if suffix != ".py":
            raise RuntimeError("目前只支持直接运行Python代码")
        code = self.current_document.read_text(encoding="utf-8")
        self._validate_python_for_run(code)
        environment = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(self.current_document)],
                cwd=str(self.current_document.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.run_timeout_seconds,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            self.last_run_error = f"运行超过{self.run_timeout_seconds:g}秒，已停止"
            self.pending_fix_confirmation = True
            self.pending_run_confirmation = False
            return f"代码运行超时，已停止。要我分析并修改吗？"

        output = self._short_output(result.stdout)
        error_output = self._short_output(result.stderr)
        if result.returncode == 0:
            self.last_run_error = ""
            self.pending_fix_confirmation = False
            self.pending_run_confirmation = False
            return f"代码运行成功，输出是：{output or '没有输出'}。"

        self.last_run_error = error_output or output or f"退出代码{result.returncode}"
        self.pending_fix_confirmation = True
        self.pending_run_confirmation = False
        return f"代码运行失败：{self.last_run_error}。要我分析并修改吗？"

    def _fix_current(self) -> str:
        if self.model_router is None:
            raise RuntimeError("代码生成模型未连接")
        if self.current_document is None or not self.current_document.exists():
            raise RuntimeError("当前没有可以修改的代码")
        current_code = self.current_document.read_text(encoding="utf-8")
        fixed_code = strip_code_fence(
            self.model_router.fix_code(
                self.current_code_request,
                current_code,
                self.last_run_error,
            )
        )
        if not fixed_code:
            raise RuntimeError("模型没有返回修复后的代码")
        self._write_current(fixed_code, self.current_code_suffix or ".py")
        self.pending_run_confirmation = True
        return "代码已经修改并写回记事本。要重新运行吗？"

    def execute(self, command: str) -> str:
        normalized = normalize_text(command)
        confirmations = {
            "好",
            "好的",
            "可以",
            "确认",
            "是",
            "是的",
            "修改吧",
            "确认修改",
            "运行吧",
            "确认运行",
        }
        cancellations = {"不用", "不要", "取消", "不用了", "先不修改"}
        if normalized in cancellations and (
            self.pending_fix_confirmation or self.pending_run_confirmation
        ):
            self.pending_fix_confirmation = False
            self.pending_run_confirmation = False
            return "好的，已取消。"
        if self.pending_fix_confirmation and normalized in confirmations:
            return self._fix_current()
        if self.pending_run_confirmation and normalized in confirmations:
            self.pending_run_confirmation = False
            return self._run_current()
        if any(
            phrase in normalized for phrase in ("保存到桌面", "存到桌面", "另存到桌面")
        ):
            return self._save_to_desktop(command)
        if (
            "运行代码" in normalized
            or "运行程序" in normalized
            or normalized in {"运行", "打开运行", "执行", "执行代码"}
        ):
            return self._run_current()
        if "计算器" in normalized:
            subprocess.Popen(["calc.exe"])
            return "计算器已打开。"

        if self._wants_code(command):
            if self.model_router is None:
                raise RuntimeError("代码生成模型未连接")
            code_request = self._code_request(command)
            generated = strip_code_fence(self.model_router.generate_code(code_request))
            if not generated:
                raise RuntimeError("没有生成可写入的代码")
            self.current_code_request = code_request
            self._write_current(generated, self._code_suffix(command))
            self.current_code_request = code_request
            return "记事本已打开，代码已经写好了。"

        literal_text = self._literal_text(command)
        if literal_text:
            self._write_current(literal_text)
            return "记事本已打开，内容已经写好了。"

        if "记事本" in normalized:
            note_path = self._new_document()
            self._open_notepad(note_path)
            return "记事本已打开。"
        raise ValueError("不支持的电脑操作")
