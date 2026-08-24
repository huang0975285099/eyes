# 千里眼客户端

Windows Electron 客户端，负责托盘/状态页和多视频源推流。电脑桌面由同目录的 `screen-helper.exe` 采集并编码为H.264（硬件编码保持原始分辨率；宽度超过1280且只能使用`libx264`软编码时缩放到1280）；USB摄像头由`ffmpeg.exe`编码为H.264；RTSP/NVR网络摄像头保留原始H.264/H.265码流，只重封装后推送到SRS 6。

## 运行配置

编辑安装目录下的 `config.json`：

```json
{
    "mediaServiceURL": "http://<服务器地址>:22222",
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

- `mediaServiceURL`是MediaService的HTTP地址，不要填写RTMP地址。
- 客户端不登记设备信息；只使用MAC和视频源信息获取稳定推流地址。
- 状态页的“当前用户”作为点位负责人保存在本机，并随推流同步到 MediaService；本人可在“本机信息”中修改，超级管理员也可在 AIService 中修改。客户端每分钟同步一次管理员设置，不会中断当前推流。
- 为便于识别点位，客户端还会同步主机名、内网 IP 和 MAC；CPU、内存、磁盘序列号只在本机展示，不上传。
- 未配置 `videoSources` 时自动启用一个桌面源。
- 所有视频源都使用“MAC、来源类型、来源 ID 哈希”组成的安全流名，不在服务器或日志中暴露摄像头地址和密码。

品牌摄像头如果支持主动RTMP推流，可以直接填写
`rtmp://<服务器>:1935/live/<唯一流名>`，不需要客户端、登记、token或API Key。
`adminService/Viewer.exe`的“品牌摄像头直推”功能仅用于生成固定流名和保存设备名称。

## 摄像头配置

### NVR / RTSP图形化配置

安装客户端后，在Windows托盘中右键“千里眼”，选择“NVR / RTSP 转推配置”。页面支持新增、编辑、启停和删除多个RTSP通道；填写通道名称、RTSP地址和TCP/UDP传输方式，点击“保存并应用”后立即生效，不需要重启客户端。

每个启用通道运行一个后台FFmpeg进程。H.264和H.265均使用原码流copy，只完成RTSP到Enhanced RTMP的重封装，不进行解码、缩放或重新编码。通道ID由客户端首次保存时自动生成并保持稳定。

图形化配置保存在当前Windows用户的Electron用户数据目录中。包含密码的RTSP地址不会写入日志，但会以明文保存在本机配置文件中，因此应限制Windows账户和配置目录的访问权限。

### 手动配置

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

多个源可以同时启用。`id` 必须在本机唯一且长期稳定，不要把 URL、用户名或密码放进 `id`。Windows 锁屏时桌面推流会暂停，摄像头推流保持运行。网络摄像头的`fps`、`bitrateKbps`和`maxWidth`在原码直通模式下不生效；如果需要改变分辨率或码率，必须显式启用转码。具体品牌到货后，优先使用厂商提供的标准RTSP地址。当前版本只推视频，不采集摄像头麦克风。

直接修改安装目录`config.json`后需要重启客户端；通过NVR配置页面保存则会自动应用。不要把包含生产RTSP密码的配置文件提交到公开仓库。

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

该命令会生成`dist/<版本>.zip`，其中包含安装包、`latest.yml`及可选的Electron Builder配置文件。客户端更新ZIP通过MediaService API上传：

```powershell
curl.exe -X POST `
  -H "X-Update-Key: <UPDATE_ADMIN_KEY>" `
  -F "file=@dist\<版本>.zip" `
  http://<服务器地址>:22222/api/client-updates/upload
```

客户端会从 `/api/client-updates/latest` 检查新版本，并校验安装包 SHA-512；发布时应确保 `UPDATE_ADMIN_KEY` 仅管理员知晓。

## 依赖文件

正式安装包需要将 `resources` 中的 `screen-helper.exe`、`ffmpeg.exe` 及图标一并打包。若采集或推流失败，优先检查这两个可执行文件、`config.json`、摄像头是否被其他程序占用、RTSP 地址、服务端健康状态以及 Windows 防火墙。
