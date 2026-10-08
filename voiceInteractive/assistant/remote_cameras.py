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

    def __init__(self, config: dict[str, Any]) -> None:
        self._enabled = bool(config.get("remote_camera_enabled", False))
        self._api_base = str(config.get("remote_camera_api_base", "")).rstrip("/")
        self._account = str(config.get("remote_camera_account", ""))
        self._password = str(config.get("remote_camera_password", ""))
        self._default_srs_host = str(config.get("remote_camera_srs_host", "10.0.6.226"))
        self._token = ""
        self._streams: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._enabled or not self._api_base:
            print("[remote-cameras] 未启用或未配置 api_base，跳过", flush=True)
            return
        if self._thread is not None:
            return
        self._stop.clear()
        print(f"[remote-cameras] 启动，拉取 {self._api_base} 的活跃流名单...", flush=True)
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
        """启动时拉取一次；失败则每 30 秒重试直到成功。"""
        while not self._stop.is_set():
            try:
                self._refresh()
                return  # 拉取成功，线程退出，不再定期刷新
            except Exception as error:  # noqa: BLE001
                print(f"[remote-cameras] 拉取失败，30秒后重试：{error}", flush=True)
                self._stop.wait(30)

    def _login(self) -> str:
        print(f"[remote-cameras] 登录 go-proxy（账号 {self._account}）...", flush=True)
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
        print("[remote-cameras] 登录成功", flush=True)
        return token

    @staticmethod
    def _parse_streams(raw: bytes) -> list[dict[str, Any]]:
        data = json.loads(raw.decode("utf-8"))
        streams = data.get("data", []) or []
        return streams if isinstance(streams, list) else []

    def _request_streams(self) -> list[dict[str, Any]]:
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
        print(f"[remote-cameras] 已缓存 {len(streams)} 路活跃推流：", flush=True)
        for s in streams:
            name = s.get("user_name") or s.get("account") or s.get("mac") or s.get("stream_name") or "-"
            host = s.get("srs_host") or self._default_srs_host
            print(f"[remote-cameras]   · {s.get('stream_name','')} / {name} / {host}", flush=True)

    def _item(self, stream: dict[str, Any]) -> dict[str, Any]:
        return {
            "stream_name": str(stream.get("stream_name") or ""),
            "srs_host": str(stream.get("srs_host") or self._default_srs_host),
            "user_name": str(stream.get("user_name") or ""),
            "account": str(stream.get("account") or ""),
        }

    def search(self, query: str) -> list[dict[str, Any]]:
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
        matched: list[dict[str, Any]] = []
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


