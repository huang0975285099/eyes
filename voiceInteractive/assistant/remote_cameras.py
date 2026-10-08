"""远程摄像头（go-proxy / SRS WebRTC）名单缓存与查询。

独立模块，便于维护：启动时登录 go-proxy 拉取活跃推流名单并缓存，
后台线程定时刷新，按用户名/账号做容错匹配，供语音"打开XXX的摄像头"命令查询。
asr_server 通过 GET /cameras?q= 暴露给前端。

配置见 config.json 的 remote_camera_* 字段：
- remote_camera_enabled / api_base / account / password / srs_host / refresh_seconds
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any


class RemoteCameraRegistry:
    """go-proxy 活跃推流名单缓存：登录 → 拉流 → 定时刷新 → 容错查询。

    线程安全：后台刷新线程写、HTTP handler 线程读，用锁保护名单。
    """

    def __init__(self, config: dict) -> None:
        self._enabled = bool(config.get("remote_camera_enabled", False))
        self._api_base = str(config.get("remote_camera_api_base", "")).rstrip("/")
        self._account = str(config.get("remote_camera_account", ""))
        self._password = str(config.get("remote_camera_password", ""))
        self._default_srs_host = str(config.get("remote_camera_srs_host", "10.0.6.226"))
        self._refresh_interval = float(config.get("remote_camera_refresh_seconds", 60))
        self._token = ""
        self._streams: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._enabled or not self._api_base:
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="remote-cameras", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh()
            except Exception as error:  # noqa: BLE001
                print(f"[remote-cameras] 刷新失败：{error}", flush=True)
            self._stop.wait(self._refresh_interval)

    def _login(self) -> str:
        payload = json.dumps(
            {"account": self._account, "password": self._password},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            self._api_base + "/api/auth/login",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        token = str((data.get("data") or {}).get("token", ""))
        if not token:
            raise RuntimeError(f"登录未返回 token：{data}")
        return token

    @staticmethod
    def _parse_streams(raw: bytes) -> list[dict]:
        data = json.loads(raw.decode("utf-8"))
        streams = data.get("data", []) or []
        return streams if isinstance(streams, list) else []

    def _request_streams(self) -> list[dict]:
        if not self._token:
            self._token = self._login()
        req = urllib.request.Request(
            self._api_base + "/api/streams?status=live",
            headers={"Authorization": "Bearer " + self._token},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return self._parse_streams(resp.read())
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            # token 失效：重登后重试一次（不再递归处理 401）
            self._token = self._login()
            req2 = urllib.request.Request(
                self._api_base + "/api/streams?status=live",
                headers={"Authorization": "Bearer " + self._token},
            )
            with urllib.request.urlopen(req2, timeout=10) as resp:
                return self._parse_streams(resp.read())

    def _refresh(self) -> None:
        streams = self._request_streams()
        with self._lock:
            self._streams = streams
        print(f"[remote-cameras] 已缓存 {len(streams)} 路活跃推流", flush=True)

    def _item(self, stream: dict) -> dict:
        return {
            "stream_name": str(stream.get("stream_name") or ""),
            "srs_host": str(stream.get("srs_host") or self._default_srs_host),
            "user_name": str(stream.get("user_name") or ""),
            "account": str(stream.get("account") or ""),
        }

    def search(self, query: str) -> list[dict]:
        """按用户名/账号容错匹配（双向包含）；query 为空时返回全部。

        - 名单 user_name 包含 输入（输入"张三" → 命中"张三""张三丰"）
        - 输入 包含 名单 user_name（输入"张三的摄像头"已提取为"张三"，此分支兜底）
        account 同理。命中其一即返回该流。
        """
        with self._lock:
            streams = list(self._streams)
        q = (query or "").strip()
        if not q:
            return [self._item(s) for s in streams]
        matched: list[dict] = []
        for s in streams:
            user = str(s.get("user_name") or "")
            account = str(s.get("account") or "")
            if (user and (user in q or q in user)) or (
                account and (account in q or q in account)
            ):
                matched.append(self._item(s))
        return matched

    def ready(self) -> bool:
        """是否已启用并完成首次拉取。"""
        if not self._enabled:
            return False
        with self._lock:
            return bool(self._streams)

    # ── 远端画面描述：查名单 → 取最新帧 → ai-check（Qwen3-VL）──

    def _go_request(self, method: str, path: str, body: bytes | None = None) -> dict:
        """带 token 请求 go-proxy JSON 接口，401 自动重登重试一次。"""
        url = self._api_base + path
        headers = {"Authorization": "Bearer " + self._token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=200) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            self._token = self._login()
            req2 = urllib.request.Request(
                url, data=body,
                headers={
                    "Authorization": "Bearer " + self._token,
                    **({"Content-Type": "application/json"} if body is not None else {}),
                },
                method=method,
            )
            with urllib.request.urlopen(req2, timeout=200) as resp:
                return json.loads(resp.read().decode("utf-8"))

    def _latest_frame_id(self, stream_name: str) -> int | None:
        from urllib.parse import quote
        path = (
            "/api/recording-frames?stream_name=" + quote(stream_name)
            + "&source=live&page=1&page_size=1"
        )
        data = self._go_request("GET", path)
        items = (data.get("data") or {}).get("items") or []
        if not items:
            return None
        fid = items[0].get("id")
        return int(fid) if fid is not None else None

    def latest_frame(self, name: str) -> dict:
        """按用户名找到流 → 取最新帧 JPEG bytes。返回 {image_bytes, user_name}。

        描述交给调用方用本地模型完成（不使用 go-proxy 的 ai-check）。
        """
        items = self.search(name)
        if not items:
            raise ValueError(f"没找到 {name} 的摄像头")
        stream_name = items[0]["stream_name"]
        user_name = items[0]["user_name"] or items[0]["account"] or name
        frame_id = self._latest_frame_id(stream_name)
        if not frame_id:
            raise ValueError(f"{user_name} 的摄像头暂无截图，请稍后再问")
        # GET /api/recording-frames/:id/image 返回 raw JPEG（非 JSON）
        url = self._api_base + f"/api/recording-frames/{frame_id}/image"
        image_bytes = self._get_bytes(url)
        if not image_bytes:
            raise ValueError("帧图片为空")
        return {"image_bytes": image_bytes, "user_name": user_name}

    def _get_bytes(self, url: str) -> bytes:
        """带 token GET 取 raw bytes，401 自动重登重试一次。"""
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            self._token = self._login()
            req2 = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token})
            with urllib.request.urlopen(req2, timeout=20) as resp:
                return resp.read()
