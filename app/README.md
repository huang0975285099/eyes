# 千里眼客户端

Windows Electron 客户端，负责设备信息登记、托盘/状态页和多视频源推流。电脑桌面由同目录的 `screen-helper.exe` 采集；USB 摄像头和支持 RTSP/HTTP 的网络摄像头由 `ffmpeg.exe` 采集并统一编码后推送到 RecordingService 管理的 SRS。

## 运行配置

编辑安装目录下的 `config.json`：

```json
{
    "recordingServiceURL": "http://<服务器地址>:52350",
    "apiKey": "与服务端 CLIENT_API_KEY 完全一致",
    "videoSources": [
        {
            "id": "desktop",
            "type": "screen",
            "displayName": "电脑桌面",
            "enabled": true,
            "fps": 8
        }
    ]
}
```

- `recordingServiceURL` 是 RecordingService 的 HTTP 地址，不要填写 RTMP 地址。
- `apiKey` 必须与服务端 `.env` 中的 `CLIENT_API_KEY` 一致。
- 客户端启动后登记设备，并按固定周期更新设备信息；永久推流地址由服务端统一生成和下发。
- 未配置 `videoSources` 时自动启用一个桌面源，与旧版本行为一致。
- 默认桌面流继续使用纯 MAC 流名；摄像头使用“MAC、来源类型、来源 ID 哈希”组成的安全流名，不在服务器或日志中暴露摄像头地址和密码。

品牌摄像头如果支持主动 RTMP 推流，也可以不经过客户端：在 RecordingService 管理后台的“品牌摄像头直推”中登记摄像头，将生成的永久地址复制到品牌后台即可。该地址不含 token。

## 摄像头配置

USB 摄像头使用 Windows DirectShow。先在客户端机器执行以下命令获取 FFmpeg 识别到的设备名称：

```powershell
.\resources\ffmpeg.exe -hide_banner -list_devices true -f dshow -i dummy
```

然后增加视频源：

```json
{
    "id": "meeting-room-usb",
    "type": "usb_camera",
    "displayName": "会议室 USB 摄像头",
    "deviceName": "Integrated Camera",
    "resolution": "1280x720",
    "fps": 15,
    "bitrateKbps": 1200,
    "maxWidth": 1280,
    "enabled": true
}
```

支持标准 RTSP、RTSPS、HTTP 或 HTTPS 视频输入的网络摄像头配置如下：

```json
{
    "id": "north-gate",
    "type": "ip_camera",
    "displayName": "北门摄像头",
    "url": "rtsp://user:password@192.168.1.20:554/stream1",
    "transport": "tcp",
    "fps": 15,
    "bitrateKbps": 1500,
    "maxWidth": 1280,
    "enabled": true
}
```

多个源可以同时启用。`id` 必须在本机唯一且长期稳定，不要把 URL、用户名或密码放进 `id`。Windows 锁屏时桌面推流会暂停，摄像头推流保持运行。具体品牌到货后，优先使用厂商提供的标准 RTSP 地址；只支持私有 SDK 的品牌可在现有视频源适配层中增加新的输入实现。当前版本只推视频，不采集摄像头麦克风。

配置变更后重启客户端。不要把生产环境的 `config.json` 或 API 密钥提交到公开仓库。

## 开发与构建

在 `app` 目录执行：

```powershell
pnpm install
pnpm run dev          # 开发模式
pnpm run lint         # 代码检查
pnpm run build:win    # 构建 Windows 安装包
```

安装包输出到 `dist/all-seeing-eyes-<版本>-setup.exe`。版本号来自 `package.json` 的 `version`。

## 发布客户端自动更新

先修改 `package.json` 版本号，再执行：

```powershell
pnpm run build:update
```

该命令会生成 `dist/<版本>.zip`，其中包含安装包、`latest.yml` 及可选的 Electron Builder 配置文件。将 ZIP 上传到 RecordingService 管理后台，或调用：

```powershell
curl.exe -X POST `
  -H "X-Update-Key: <UPDATE_ADMIN_KEY>" `
  -F "file=@dist\<版本>.zip" `
  http://<服务器地址>:52350/api/client-updates/upload
```

客户端会从 `/api/client-updates/latest` 检查新版本，并校验安装包 SHA-512；发布时应确保 `UPDATE_ADMIN_KEY` 仅管理员知晓。

## 依赖文件

正式安装包需要将 `resources` 中的 `screen-helper.exe`、`ffmpeg.exe` 及图标一并打包。若采集或推流失败，优先检查这两个可执行文件、`config.json`、摄像头是否被其他程序占用、RTSP 地址、服务端健康状态以及 Windows 防火墙。
