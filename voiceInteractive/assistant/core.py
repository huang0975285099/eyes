"""语音助手主循环。"""

from __future__ import annotations

import queue
import sys
import threading
import time
from datetime import datetime

import sounddevice as sd
from vosk import KaldiRecognizer, Model

from .config import AudioDevice, Config
from .dashboard import CameraDashboard
from .llm import ModelRouter, OllamaClient, OnlineQwenClient
from .speaker import Speaker
from .textutils import (
    _recognizer,
    _result_text,
    contains_wake_phrase,
    format_time_zh,
    interruption_action,
    is_desktop_command,
    is_end_conversation_command,
    is_exit_command,
    is_person_location_query,
    is_time_command,
    is_vision_command,
    is_vision_follow_up,
    is_weather_command,
    is_weather_follow_up,
    recognition_alternatives,
    select_actionable_recognition,
)
from .tools import DesktopTools, OnlineSearchTools


class VoiceAssistant:
    COMMAND_PHRASES = [
        "现在 几点 了",
        "现在 几点",
        "几点 了",
        "几点",
        "不用 了",
        "没事 了",
        "结束 对话",
        "结束 聊天",
        "休息 吧",
        "再见",
        "退出 助手",
        "关闭 助手",
        "停止 助手",
    ]

    def __init__(
        self,
        config: Config,
        model: Model,
        input_device: AudioDevice,
        speaker: Speaker,
        ollama: OllamaClient | OnlineQwenClient | ModelRouter,
        online_tools: OnlineSearchTools,
        dashboard: CameraDashboard | None,
    ) -> None:
        self.config = config
        self.model = model
        self.input_device = input_device
        self.speaker = speaker
        self.ollama = ollama
        self.online_tools = online_tools
        self.desktop_tools = DesktopTools(
            ollama, run_timeout_seconds=config.code_run_timeout_seconds
        )
        self.dashboard = dashboard
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=80)
        self.interrupt_audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=40)
        self.speaking = threading.Event()
        self.barge_in_enabled = threading.Event()
        self.playback_cancel = threading.Event()
        self._barge_in_stop = threading.Event()
        self._interrupt_lock = threading.Lock()
        self._interrupt_action: str | None = None

    @staticmethod
    def _put_latest(target_queue: queue.Queue[bytes], data: bytes) -> None:
        try:
            target_queue.put_nowait(data)
        except queue.Full:
            try:
                target_queue.get_nowait()
                target_queue.put_nowait(data)
            except queue.Empty:
                pass

    def _audio_callback(self, indata, frames, timing, status) -> None:
        if status:
            print(f"\n音频提示：{status}", file=sys.stderr)
        if self.barge_in_enabled.is_set():
            self._put_latest(self.interrupt_audio_queue, bytes(indata))
            return
        if self.speaking.is_set():
            return
        self._put_latest(self.audio_queue, bytes(indata))

    def _clear_audio(self) -> None:
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                return

    def _clear_interrupt_audio(self) -> None:
        while True:
            try:
                self.interrupt_audio_queue.get_nowait()
            except queue.Empty:
                return

    def _set_interrupt_action(self, action: str) -> None:
        with self._interrupt_lock:
            if self._interrupt_action is None:
                self._interrupt_action = action

    def _consume_interrupt_action(self) -> str | None:
        with self._interrupt_lock:
            action = self._interrupt_action
            self._interrupt_action = None
            return action

    def _barge_in_loop(self, sample_rate: int) -> None:
        # An unrestricted recognizer prevents arbitrary speech or speaker echo from
        # being forced into one of a tiny number of interruption commands.
        recognizer = KaldiRecognizer(self.model, sample_rate)
        while not self._barge_in_stop.is_set():
            try:
                data = self.interrupt_audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if not self.barge_in_enabled.is_set():
                recognizer.Reset()
                continue
            is_final = recognizer.AcceptWaveform(data)
            result = recognizer.Result() if is_final else recognizer.PartialResult()
            text = _result_text(result, "text" if is_final else "partial")
            if not text or text == "[unk]":
                continue
            action = interruption_action(text, self.config.wake_phrases)
            if action is None:
                continue
            print(f"[打断播报] {text}", flush=True)
            self._set_interrupt_action(action)
            self.playback_cancel.set()
            if self.dashboard:
                self.dashboard.store.set_assistant_status(
                    self._continuation_status(action)
                )
            try:
                sd.stop()
            except Exception as error:
                print(f"[停止播放失败] {error}", file=sys.stderr)
            recognizer.Reset()
            self._clear_interrupt_audio()

    def _play(self, action, *args) -> None:
        self.speaking.set()
        self._clear_audio()
        try:
            action(*args)
        finally:
            self._clear_audio()
            self.speaking.clear()

    def _begin_interruptible_playback(self) -> None:
        self._consume_interrupt_action()
        self.playback_cancel.clear()
        self._clear_interrupt_audio()
        # USB 摄像头的麦克风和扬声器距离很近，且没有硬件 AEC。默认在播报时
        # 丢弃麦克风输入，避免把老叶自己的声音识别成唤醒词或停止命令。
        config = getattr(self, "config", None)
        if getattr(config, "barge_in_during_playback", True):
            self.barge_in_enabled.set()
        else:
            self.barge_in_enabled.clear()

    def _end_interruptible_playback(self) -> str | None:
        self.barge_in_enabled.clear()
        self._clear_interrupt_audio()
        return self._consume_interrupt_action()

    def _play_interruptible(self, action, *args) -> str | None:
        self._begin_interruptible_playback()
        try:
            self._play(action, *args)
        finally:
            interrupt_action = self._end_interruptible_playback()
        return interrupt_action

    def _speak_streamed_answer(self, request) -> tuple[str, str | None]:
        speech_queue: queue.Queue[str | None] = queue.Queue()
        prepared_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=2)
        generation_result: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        playback_errors: list[Exception] = []
        displayed_segments: list[str] = []
        self._begin_interruptible_playback()

        def on_segment(segment: str) -> None:
            if self.playback_cancel.is_set():
                return
            displayed_segments.append(segment)
            if self.dashboard:
                self.dashboard.store.set_assistant_status(
                    "正在回答", "".join(displayed_segments)
                )
            speech_queue.put(segment)

        def generation_worker() -> None:
            try:
                generation_result.put(("answer", request(on_segment)))
            except Exception as error:
                generation_result.put(("error", error))
            finally:
                speech_queue.put(None)

        def preparation_worker() -> None:
            try:
                while True:
                    segment = speech_queue.get()
                    if segment is None:
                        break
                    if self.playback_cancel.is_set():
                        continue
                    try:
                        samples = self.speaker.prepare(
                            segment, self.playback_cancel
                        )
                        if samples is not None:
                            prepared_queue.put(("audio", samples))
                    except Exception as error:
                        prepared_queue.put(("error", error))
            finally:
                prepared_queue.put(("done", None))

        worker = threading.Thread(
            target=generation_worker, name="streaming-model", daemon=True
        )
        preparation = threading.Thread(
            target=preparation_worker, name="streaming-tts", daemon=True
        )
        worker.start()
        preparation.start()
        playback_failed = False
        try:
            while True:
                item_kind, item_value = prepared_queue.get()
                if item_kind == "done":
                    break
                if item_kind == "error":
                    if isinstance(item_value, Exception):
                        playback_errors.append(item_value)
                    else:
                        playback_errors.append(RuntimeError(str(item_value)))
                    playback_failed = True
                    try:
                        self._play(self.speaker.chime, False)
                    except Exception:
                        pass
                    continue
                if playback_failed or self.playback_cancel.is_set():
                    discard = getattr(self.speaker, "discard_prepared", None)
                    if discard is not None:
                        discard(item_value)
                    continue
                try:
                    # Synthesis is prefetched in streaming-tts, but WASAPI playback
                    # stays on the main voice thread for reliable Windows output.
                    self._play(
                        self.speaker.play_prepared,
                        item_value,
                        self.playback_cancel,
                    )
                except Exception as error:
                    playback_errors.append(error)
                    playback_failed = True
                    try:
                        self._play(self.speaker.chime, False)
                    except Exception:
                        pass
        finally:
            worker.join()
            preparation.join()
            interrupt_action = self._end_interruptible_playback()
        result_kind, result_value = generation_result.get()
        if result_kind == "error":
            if isinstance(result_value, Exception):
                raise result_value
            raise RuntimeError(str(result_value))
        if playback_errors:
            print(f"[语音合成失败] {playback_errors[0]}", file=sys.stderr)
        return str(result_value), interrupt_action

    @staticmethod
    def _continuation_status(interrupt_action: str | None) -> str:
        if interrupt_action == "wake":
            return "回答已打断，请直接说新问题"
        if interrupt_action == "stop":
            return "已停止播报，可以继续提问"
        return "可以继续提问，无需再次唤醒"

    def _person_location_response(
        self, primary_text: str, recognition_candidates: list[str]
    ) -> tuple[str, str] | None:
        if self.dashboard is None:
            return None
        seen: set[str] = set()
        for candidate in [primary_text, *recognition_candidates]:
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            if not is_person_location_query(candidate):
                continue
            answer = self.dashboard.face_service.answer_location(candidate)
            if answer is not None:
                return candidate, answer
        return None

    def _broadcast_pending_scene(self) -> bool:
        if not self.dashboard:
            return False
        pending = self.dashboard.store.consume_scene_broadcast()
        if pending is None:
            return False
        request_id, frame = pending
        try:
            description = self.ollama.describe_scene(frame)
        except Exception as error:
            print(f"[动态画面分析失败] {error}", file=sys.stderr)
            self.dashboard.store.fail_scene_broadcast(request_id, str(error))
            return False
        if not self.dashboard.store.finish_scene_broadcast(
            request_id, frame, description
        ):
            return False
        print(f"[动态画面播报] {description}")
        self.dashboard.store.set_assistant_status("动态画面播报", description)
        try:
            interrupt_action = self._play_interruptible(
                self.speaker.say, description, self.playback_cancel
            )
        except Exception as error:
            print(f"[动态画面播报失败] {error}", file=sys.stderr)
            self._play(self.speaker.chime, False)
            return True
        if interrupt_action is not None:
            self.dashboard.store.set_assistant_status(
                self._continuation_status(interrupt_action)
            )
        return True

    def run(self) -> None:
        sample_rate = self.input_device.sample_rate
        wake_recognizer = _recognizer(
            self.model, sample_rate, list(self.config.wake_phrases)
        )
        command_recognizer = KaldiRecognizer(self.model, sample_rate)
        if self.config.asr_accent_enhancement_enabled:
            command_recognizer.SetMaxAlternatives(
                self.config.asr_max_alternatives
            )
        self._barge_in_stop.clear()
        barge_in_thread = threading.Thread(
            target=self._barge_in_loop,
            args=(sample_rate,),
            name="barge-in-recognizer",
            daemon=True,
        )
        barge_in_thread.start()
        state = "waiting"
        command_deadline = 0.0
        last_partial = ""
        final_recognition_candidates: list[str] = []
        vision_context_active = False
        weather_context_active = False
        desktop_context_active = False

        print("\n已启动。请说：老叶老叶")
        print("听到“我在”后开始提问（按 Ctrl+C 退出）\n")
        if self.dashboard:
            self.dashboard.store.set_assistant_status("等待“老叶老叶”唤醒")

        with sd.RawInputStream(
            samplerate=sample_rate,
            blocksize=4000,
            device=self.input_device.index,
            dtype="int16",
            channels=1,
            callback=self._audio_callback,
        ):
            while not (
                self.dashboard and self.dashboard.store.shutdown_event.is_set()
            ):
                if self.dashboard:
                    scene_broadcasted = self._broadcast_pending_scene()
                    if scene_broadcasted and state == "command":
                        command_deadline = (
                            time.monotonic()
                            + self.config.command_timeout_seconds
                        )
                    presence_event_id = self.dashboard.store.consume_presence_alert()
                    if presence_event_id is not None:
                        alert = "检测到有人进入画面。"
                        print(f"[动态监测提醒] 事件 {presence_event_id} / {alert}")
                        if not self.dashboard.store.scene_broadcast_enabled():
                            self.dashboard.store.set_assistant_status(
                                "动态监测提醒", alert
                            )
                        if (
                            self.config.person_alert_voice
                            and not self.dashboard.store.scene_broadcast_enabled()
                        ):
                            try:
                                self._play(self.speaker.say, alert)
                            except Exception as error:
                                print(f"[动态提醒播报失败] {error}", file=sys.stderr)
                                self._play(self.speaker.chime, False)
                        if state == "command":
                            command_deadline = (
                                time.monotonic()
                                + self.config.command_timeout_seconds
                            )
                if state == "command" and time.monotonic() > command_deadline:
                    print("[会话结束] 一段时间没有继续提问，重新等待唤醒。")
                    self.ollama.end_conversation()
                    self.online_tools.start_conversation()
                    self.desktop_tools.start_conversation()
                    vision_context_active = False
                    weather_context_active = False
                    desktop_context_active = False
                    state = "waiting"
                    wake_recognizer.Reset()
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("等待“老叶老叶”唤醒")

                try:
                    data = self.audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                recognizer = wake_recognizer if state == "waiting" else command_recognizer
                is_final = recognizer.AcceptWaveform(data)
                if is_final:
                    result_payload = recognizer.Result()
                    final_recognition_candidates = recognition_alternatives(
                        result_payload
                    )
                    text = select_actionable_recognition(
                        final_recognition_candidates,
                        self.config.wake_phrases,
                        state,
                    )
                    if (
                        final_recognition_candidates
                        and text != final_recognition_candidates[0]
                    ):
                        print(
                            "[口音纠错] "
                            f"{final_recognition_candidates[0]} -> {text}"
                        )
                    if (
                        self.config.asr_accent_enhancement_enabled
                        and len(final_recognition_candidates) > 1
                    ):
                        print(
                            "[识别候选] "
                            + " / ".join(final_recognition_candidates)
                        )
                else:
                    final_recognition_candidates = []
                    text = _result_text(recognizer.PartialResult(), "partial")
                    if text == last_partial:
                        continue
                    last_partial = text

                if not text:
                    continue
                if text == "[unk]":
                    continue
                print(f"[{state}] {text}")

                if state == "command":
                    command_deadline = max(command_deadline, time.monotonic() + 3.0)

                if state == "waiting" and contains_wake_phrase(
                    text, self.config.wake_phrases
                ):
                    print("[已唤醒] 正在听……")
                    print("[回答] 我在")
                    self.ollama.start_conversation()
                    self.online_tools.start_conversation()
                    self.desktop_tools.start_conversation()
                    vision_context_active = False
                    weather_context_active = False
                    desktop_context_active = False
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("我在，请开始提问", "我在")
                    try:
                        self._play(self.speaker.say, "我在。")
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, True)
                    state = "command"
                    command_deadline = time.monotonic() + self.config.command_timeout_seconds
                    command_recognizer.Reset()
                    last_partial = ""
                    continue

                if (
                    state == "command"
                    and is_final
                    and (
                        is_desktop_command(text)
                        or (
                            desktop_context_active
                            and self.desktop_tools.can_handle_follow_up(text)
                        )
                    )
                ):
                    vision_context_active = False
                    weather_context_active = False
                    print(f"[电脑操作] {text}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("正在执行电脑操作")
                    try:
                        response = self.desktop_tools.execute(text)
                        action_succeeded = True
                        desktop_context_active = self.desktop_tools.context_active
                    except Exception as error:
                        print(f"[电脑操作失败] {error}", file=sys.stderr)
                        reason = str(error).strip().rstrip("。")
                        response = f"电脑操作没有成功，{reason or '请稍后再试'}。"
                        action_succeeded = False
                    self.ollama.remember(text, response)
                    print(f"[电脑操作回答] {response}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            "电脑操作完成" if action_succeeded else "电脑操作失败",
                            response,
                        )
                    try:
                        interrupt_action = self._play_interruptible(
                            self.speaker.say, response, self.playback_cancel
                        )
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, False)
                        interrupt_action = None
                    state = "command"
                    command_deadline = (
                        time.monotonic() + self.config.command_timeout_seconds
                    )
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            self._continuation_status(interrupt_action), response
                        )
                elif (
                    state == "command"
                    and is_final
                    and (
                        location_response := self._person_location_response(
                            text, final_recognition_candidates
                        )
                    )
                    is not None
                ):
                    resolved_text, answer = location_response
                    vision_context_active = False
                    weather_context_active = False
                    desktop_context_active = False
                    self.ollama.remember(resolved_text, answer)
                    if resolved_text != text:
                        print(f"[人员姓名纠错] {text} -> {resolved_text}")
                    print(f"[人员位置回答] {answer}")
                    self.dashboard.store.set_assistant_status("人员位置查询完成", answer)
                    try:
                        interrupt_action = self._play_interruptible(
                            self.speaker.say, answer, self.playback_cancel
                        )
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, False)
                        interrupt_action = None
                    state = "command"
                    command_deadline = time.monotonic() + self.config.command_timeout_seconds
                    command_recognizer.Reset()
                    last_partial = ""
                    self.dashboard.store.set_assistant_status(
                        self._continuation_status(interrupt_action), answer
                    )
                elif state == "command" and is_time_command(text):
                    vision_context_active = False
                    weather_context_active = False
                    response = format_time_zh(datetime.now().astimezone())
                    self.ollama.remember(text, response)
                    print(f"[回答] {response}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("回答完成", response)
                    try:
                        interrupt_action = self._play_interruptible(
                            self.speaker.say, response, self.playback_cancel
                        )
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, False)
                        interrupt_action = None
                    state = "command"
                    command_deadline = (
                        time.monotonic() + self.config.command_timeout_seconds
                    )
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            self._continuation_status(interrupt_action), response
                        )
                elif (
                    state == "command"
                    and is_final
                    and (
                        is_weather_command(text)
                        or (
                            weather_context_active
                            and is_weather_follow_up(text)
                        )
                    )
                ):
                    vision_context_active = False
                    print(f"[天气查询] {text}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("正在查询天气")
                    try:
                        answer = self.online_tools.search_weather(text)
                        weather_context_active = True
                        weather_succeeded = True
                    except Exception as error:
                        print(f"[天气查询失败] {error}", file=sys.stderr)
                        if "配置中关闭" in str(error):
                            answer = "天气查询功能暂时关闭。"
                        elif any(
                            phrase in str(error)
                            for phrase in ("没有找到地点", "城市不存在", "未找到")
                        ):
                            answer = "没有找到这个城市，请重新说城市名，比如成都天气。"
                        else:
                            answer = "天气服务暂时连接失败，请稍后再试。"
                        weather_context_active = False
                        weather_succeeded = False
                    self.ollama.remember(text, answer)
                    print(f"[天气回答] {answer}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            "天气查询完成" if weather_succeeded else "天气查询失败",
                            answer,
                        )
                    try:
                        interrupt_action = self._play_interruptible(
                            self.speaker.say, answer, self.playback_cancel
                        )
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, False)
                        interrupt_action = None
                    state = "command"
                    command_deadline = (
                        time.monotonic() + self.config.command_timeout_seconds
                    )
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            self._continuation_status(interrupt_action), answer
                        )
                elif state == "command" and is_final and is_exit_command(text):
                    self.ollama.end_conversation()
                    self.online_tools.start_conversation()
                    self.desktop_tools.start_conversation()
                    self._barge_in_stop.set()
                    vision_context_active = False
                    weather_context_active = False
                    desktop_context_active = False
                    print("再见。")
                    try:
                        self._play(self.speaker.say, "再见。")
                    except Exception:
                        self._play(self.speaker.chime, True)
                    return
                elif (
                    state == "command"
                    and is_final
                    and is_end_conversation_command(text)
                ):
                    answer = "好的，需要时再叫我。"
                    print(f"[会话结束] {answer}")
                    self.ollama.end_conversation()
                    self.online_tools.start_conversation()
                    self.desktop_tools.start_conversation()
                    vision_context_active = False
                    weather_context_active = False
                    desktop_context_active = False
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            "等待“老叶老叶”唤醒", answer
                        )
                    try:
                        self._play(self.speaker.say, answer)
                    except Exception as error:
                        print(f"[语音合成失败] {error}", file=sys.stderr)
                        self._play(self.speaker.chime, True)
                    state = "waiting"
                    wake_recognizer.Reset()
                    command_recognizer.Reset()
                    last_partial = ""
                elif state == "command" and (
                    is_vision_command(text)
                    or (
                        is_final
                        and vision_context_active
                        and is_vision_follow_up(text)
                    )
                ):
                    weather_context_active = False
                    answer_was_streamed = False
                    interrupt_action = None
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            "正在拍摄本次分析快照"
                        )
                        image_bytes = self.dashboard.store.request_snapshot(
                            self.config.camera_snapshot_timeout_seconds
                        )
                    else:
                        image_bytes = None
                    if image_bytes is None:
                        answer = "没有拍到同步画面，请确认摄像头网页已打开并允许权限。"
                        self.ollama.remember(text, answer)
                        vision_context_active = False
                    else:
                        print("[视觉] 正在分析当前画面……")
                        if self.dashboard:
                            self.dashboard.store.set_assistant_status("正在分析当前画面")
                        try:
                            answer, interrupt_action = self._speak_streamed_answer(
                                lambda on_segment: self.ollama.ask_vision(
                                    text,
                                    image_bytes,
                                    on_segment,
                                    self.playback_cancel,
                                )
                            )
                            answer_was_streamed = True
                            vision_context_active = True
                        except Exception as error:
                            print(f"[视觉分析失败] {error}", file=sys.stderr)
                            answer = "当前画面分析失败，请确认奥拉马视觉模型可以使用。"
                            vision_context_active = False
                    print(f"[视觉回答] {answer}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("视觉回答完成", answer)
                    if not answer_was_streamed:
                        try:
                            interrupt_action = self._play_interruptible(
                                self.speaker.say, answer, self.playback_cancel
                            )
                        except Exception as error:
                            print(f"[语音合成失败] {error}", file=sys.stderr)
                            self._play(self.speaker.chime, False)
                    state = "command"
                    command_deadline = (
                        time.monotonic() + self.config.command_timeout_seconds
                    )
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            self._continuation_status(interrupt_action), answer
                        )
                elif state == "command" and is_final:
                    vision_context_active = False
                    weather_context_active = False
                    print(f"[问题] {text}")
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status("正在生成回答")
                    try:
                        answer, interrupt_action = self._speak_streamed_answer(
                            lambda on_segment: self.ollama.ask(
                                text,
                                on_segment,
                                self.playback_cancel,
                                (
                                    final_recognition_candidates
                                    if self.config.asr_accent_enhancement_enabled
                                    else None
                                ),
                            )
                        )
                        print(f"[模型回答] {answer}")
                        if self.dashboard:
                            self.dashboard.store.set_assistant_status("回答完成", answer)
                    except Exception as error:
                        interrupt_action = None
                        print(f"[模型调用失败] {error}", file=sys.stderr)
                        try:
                            self._play(
                                self.speaker.say,
                                "模型服务暂时无法回答，请稍后再试。",
                            )
                        except Exception:
                            self._play(self.speaker.chime, False)
                    state = "command"
                    command_deadline = (
                        time.monotonic() + self.config.command_timeout_seconds
                    )
                    command_recognizer.Reset()
                    last_partial = ""
                    if self.dashboard:
                        self.dashboard.store.set_assistant_status(
                            self._continuation_status(interrupt_action)
                        )
        self._barge_in_stop.set()
        print("已收到关闭请求，语音助手安全退出。")
