# 千里眼客户端

Electron 只负责生命周期、状态页和托盘；`screen-helper.exe` 采集 Windows 桌面并调用同目录的 `ffmpeg.exe`，以 RTMP 推到 RecordingService 节点的 SRS。

## 配置

编辑 `config.json`：

- `recordingServiceURL`：RecordingService 管理 API，例如 `http://112.18.238.6:52350`。
- `apiKey`：客户端共享密钥，需匹配 RecordingService 的 `CLIENT_API_KEY`。

客户端通过 RecordingService 自动获取带短期 token 的完整 RTMP 地址，不保存公网 RTMP 主机配置。

安装后每 5 分钟向 RecordingService 更新一次设备信息。流名称是去掉分隔符的小写 MAC，例如 `d85ed39f2a17`。

## 构建

```powershell
pnpm install
pnpm run lint
pnpm run build:win
```

安装包输出到 `dist/all-seeing-eyes-1.0.0-setup.exe`。
