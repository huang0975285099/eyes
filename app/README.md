# 千里眼客户端

Windows Electron 客户端，负责设备信息登记、托盘/状态页和屏幕推流。屏幕由同目录的 `screen-helper.exe` 采集，使用 `ffmpeg.exe` 推送到 RecordingService 管理的 SRS。

## 运行配置

编辑安装目录下的 `config.json`：

```json
{
  "recordingServiceURL": "http://<服务器地址>:52350",
  "apiKey": "与服务端 CLIENT_API_KEY 完全一致"
}
```

- `recordingServiceURL` 是 RecordingService 的 HTTP 地址，不要填写 RTMP 地址。
- `apiKey` 必须与服务端 `.env` 中的 `CLIENT_API_KEY` 一致。
- 客户端启动后登记设备，并按固定周期更新设备信息；推流地址和短期 token 由服务端下发。
- 流名称为去掉冒号等分隔符并转为小写的 MAC，例如 `d8:5e:d3:9f:2a:17` 变为 `d85ed39f2a17`。

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

正式安装包需要将 `resources` 中的 `screen-helper.exe`、`ffmpeg.exe` 及图标一并打包。若采集或推流失败，优先检查这两个可执行文件、`config.json`、服务端健康状态以及 Windows 防火墙。
