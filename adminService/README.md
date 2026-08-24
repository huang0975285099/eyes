# AdminService

Windows原生C++监控管理客户端，用于替代浏览器承担集中观看和日常录制管理。客户端通过
MediaService HTTP API读取在线流、录像、抽帧和录制设置，使用随程序分发的FFmpeg原生
解码H.264/H.265，不依赖浏览器插件。

## 系统要求

- Windows 10（含）以上64位系统，支持Windows 10、Windows 11和后续兼容版本。
- 不支持Windows 7/8、Linux或macOS。
- 当前为免安装便携版，必须保持`Viewer.exe`和`ffmpeg.exe`位于同一目录。

## 当前功能

- 在线流列表：显示视频源、编码、分辨率、MAC和流名，双击播放。
- H.264/H.265实时播放：从SRS RTMP地址读取，由FFmpeg解码后在Win32窗口中渲染。
- 录像列表和播放。
- 抽帧列表和图片查看。
- 开启/关闭服务器录像、设置录像保留天数。
- 创建品牌摄像头直推视频源，自动复制固定RTMP地址。
- 显示在线流、录像数量、磁盘占用和AI Worker数量。
- 播放断开后可重新播放；同一时间只解码一个画面。

MediaService不使用token/API Key，因此客户端只需配置服务地址。生产环境仍应通过防火墙或
VPN限制`22222`、`1935`端口来源。

## 构建

需要MSYS2 UCRT64 GCC，当前开发机已安装在`C:\msys64\ucrt64\bin`。在PowerShell执行：

```powershell
cd D:\project\eyes\adminService
.\build.ps1
```

输出：

```text
dist/Viewer.exe
dist/ffmpeg.exe
dist/adminService.ini
```

`ffmpeg.exe`来自`app/resources/ffmpeg.exe`，该版本支持H.264、H.265和Enhanced RTMP。

## 运行配置

编辑EXE同目录的`adminService.ini`：

```ini
[server]
mediaServiceURL=http://10.0.20.219:22222
```

MediaService的`GET /api/streams`会返回每路视频的编码、分辨率和`rtmp_url`，客户端无需自行
拼接SRS地址。
