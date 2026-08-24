# MediaService

MediaService 为千里眼客户端和品牌摄像头提供设备登记、永久 RTMP 推流地址、实时流查看、录像录制、AI控制面和客户端更新服务。数据保存在独立的 `eyes` MySQL 数据库中，不依赖其他业务数据库。

```text
Electron -> HTTP :22222 -> MediaService -> MySQL 8.1.0 / eyes
Electron desktop/USB/IP camera -> FFmpeg -> RTMP :1935 -> SRS
                                      |-> HTTP-FLV/HLS :8090
                                      |-> MediaService -> 录像文件
                                      |-> AIService -> 抽帧/后续实时AI
```

## 配置

```bash
cp .env.example .env
```

生产环境至少修改以下配置：

- `CLIENT_API_KEY`：客户端登记和推流配置接口的共享密钥，必须与 `app/config.json` 的 `apiKey` 一致。
- `PUBLIC_RTMP_HOST`：客户端可访问的 RTMP 地址，例如 `example.com:1935`。
- `MEDIA_SRS_HTTP_HOST`：客户端/管理页可访问的SRS HTTP-FLV/HLS地址，例如`example.com:8090`。
- `UPDATE_ADMIN_KEY`：客户端更新 ZIP 上传密钥；为空时禁用上传。
- `RECORDING_DIR`：宿主机录像目录，默认 `/home/test/recordings`，应改为实际磁盘挂载点。

录像默认分段 600 秒；Compose 当前默认录像保留 2 天、截图保留 30 天。管理页保存的录制开关和录像保留天数会写入数据库，并覆盖环境变量、立即生效。每台电脑可以登记多个独立视频源，当前内置来源类型为 `screen`、`usb_camera` 和 `ip_camera`；符合小写字母、数字和下划线命名规则的新适配器类型也可直接登记，无需再次修改服务端流名协议。

## Docker 部署

需要 Docker、Docker Compose Plugin，以及可用的录像存储目录：

```bash
docker compose config -q
docker compose up -d --build --remove-orphans
docker compose restart srs
docker compose ps
docker compose logs -f media-service ai-service
```

这是全新的 MediaService 部署，Compose 使用 `eyes` 项目名和新的数据卷。
首次启动使用 `--remove-orphans` 清理同一目录下的无效容器。

也可以使用离线部署脚本（脚本会加载镜像 tar 并执行健康检查）：

```bash
chmod +x deploy.sh
./deploy.sh                 # 使用默认 latest
./deploy.sh --tag <标签>    # 使用指定 media-service 镜像标签
```

日常发布统一从`mediaService/build.sh`执行。脚本会同时构建并部署MediaService和
AIService；不传`--target`时会要求输入部署目标：

```bash
./build.sh                         # 交互选择：1=公网，2=内网
./build.sh --target 1              # 公网 test@112.18.238.6:2202
./build.sh --target 2              # 内网 administrator@10.0.20.219:22
./build.sh --target 2 --tag v1.0.0 # 指定镜像标签
```

如需临时使用其他SSH端口，可覆盖端口：

```bash
INTRANET_REMOTE_PORT=2202 ./build.sh --target 2
```

也可以将`INTRANET_REMOTE_PORT`写入执行环境。

公网部署目录为`/home/test/eyes/`，内网部署目录为
`/home/administrator/eyes/`，两者各自保留独立的`.env`。构建脚本只上传`.env.example`，
不会覆盖生产密钥。内网服务器首次部署前应在该目录
创建`.env`，并将`PUBLIC_RTMP_HOST`设为`10.0.20.219:1935`、
`MEDIA_SRS_HTTP_HOST`设为`10.0.20.219:8090`；公网服务器继续使用公网地址。

对外端口：

- `22222`：MediaService管理后台和API
- `11111`：AIService健康状态和模块列表
- `1935`：SRS RTMP 推流
- `8090`：SRS HTTP-FLV/HLS 播放

SRS API `1985`、MySQL `3306` 仅在 Docker 内部网络开放。生产环境应限制 `22222`、`11111`、`8090` 的来源，并通过 HTTPS 反向代理保护管理接口。

MediaService在录像片段入库后创建持久化抽帧任务，AIService领取任务、执行FFmpeg并
回报图片；两个容器共享 `recordings` 卷。

两台目标服务器没有GPU不影响当前`frame_sampler`：抽帧由CPU上的FFmpeg完成。
`.env`已经预留`DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`和`QWEN_VL_MODEL`并传入
AIService容器，但当前代码尚未调用Qwen。后续Qwen视觉模型适合作为低频图片/视频片段
复核与事件描述，不应直接代替多路实时检测、跟踪和时序模型。API Key、Base URL和模型
必须属于同一百炼地域；密钥只能保存在服务器`.env`中。

## 常用接口

- `GET /api/health`：服务和数据库健康检查，数据库不可用时返回 HTTP 503。
- `POST /api/clients/register`：客户端登记/更新设备信息。
- `POST /api/streams/publish-config`：APP 按设备和视频源获取永久 RTMP 推流配置，必须提交 `mac`、`source_type`、`source_id` 和 `display_name`。
- `GET /api/streams`、`GET /api/stats`：实时流和系统统计。
- `GET /api/video-sources`：查看已登记的视频源、在线状态及品牌摄像头直推地址。
- `POST /api/video-sources`：登记一个独立品牌摄像头并生成永久 RTMP 地址，请求字段为 `source_id`、`display_name` 和可选的 `brand`。
- `GET /api/segments`、`GET /api/frames`：录像片段和截图列表，支持 `mac`、`source_type` 过滤；媒体分别通过 `/segments/{id}/video`、`/frames/{id}/image` 获取。
- `GET/PUT /api/recording-settings`：查看或修改录制开关、录像保留天数。
- `GET /api/ai/algorithms`：AI能力目录；当前抽帧已启用，打架、安全帽和火灾为后续模块。
- `GET /api/ai/jobs/stats`：AI任务状态和Worker心跳概览。
- `/api/internal/ai/*`：AIService任务领取、结果上报和心跳接口，仅供Docker内部网络或可信局域网调用。
- `GET /api/client-updates/latest`：客户端检查更新。
- `POST /api/client-updates/upload`：上传更新 ZIP，必须提供 `X-Update-Key: <UPDATE_ADMIN_KEY>`。

管理后台地址为 `http://<服务器地址>:22222`。更新 ZIP 必须包含 `latest.yml`、对应的 `*-setup.exe`，并且其中的版本、路径和 SHA-512 必须匹配；客户端侧 ZIP 可由 `app` 目录的 `pnpm run build:update` 生成。

RTMP 发布不使用 token。SRS 发布回调只接受 `video_sources` 中已登记且启用的视频源。品牌摄像头后台填写的地址形如 `rtmp://<服务器>:1935/live/camera--<固定标识>`，配置一次即可长期使用。因为地址本身不再提供身份认证，生产环境应限制1935端口来源IP，管理端口22222也应仅允许可信内网访问。

部分摄像头后台提供一个“推流地址”输入框，直接填写接口返回的 `rtmp_url`；部分后台将其拆成两个输入框，则分别填写 `rtmp_server`（例如 `rtmp://example.com:1935/live`）和 `stream_key`。也可以通过接口登记：

```bash
curl -X POST http://<服务器地址>:22222/api/video-sources \
  -H 'Content-Type: application/json' \
  -d '{"source_id":"north-gate","display_name":"北门摄像头","brand":"camera-brand"}'
```

## 备份与恢复

```bash
docker compose exec -T mysql mysqldump -uroot -pall_seeing_eyes \
  --single-transaction eyes > eyes-backup.sql

docker compose exec -T mysql mysql -uroot -pall_seeing_eyes \
  eyes < eyes-backup.sql
```

数据库表由服务启动时自动创建或更新，包括设备、录像、抽帧以及
`ai_algorithms`、`video_analysis_rules`、`ai_jobs`、`ai_workers`、`ai_events` 等AI平台表。
备份时还应保留宿主机 `RECORDING_DIR` 中的录像和AI证据文件；数据库记录和文件需要
同时恢复才能正常播放。
