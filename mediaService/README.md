# MediaService

MediaService 为千里眼客户端和品牌摄像头提供视频源管理、永久 RTMP 推流地址、实时流查看、录像录制、AI控制面和客户端更新服务。数据保存在独立的 `eyes` MySQL 数据库中，不依赖其他业务数据库。

```text
Electron -> HTTP :18888 -> MediaService -> MySQL 8.1.0 / eyes
Electron desktop/USB/IP camera -> FFmpeg -> RTMP :1935 -> SRS 6
                                      |-> Native AdminService viewer
                                      |-> MediaService -> 录像文件
                                      |-> AIService -> 抽帧/后续实时AI
```

## 配置

```bash
cp .env.example .env
```

生产环境至少修改以下配置：

- `PUBLIC_RTMP_HOST`：客户端可访问的 RTMP 地址，例如 `example.com:1935`。
- `MEDIA_SRS_HTTP_HOST`：客户端自预览可访问的SRS HTTP地址，例如`example.com:18889`（容器内部仍使用8080）。
- `UPDATE_ADMIN_KEY`：客户端更新 ZIP 上传密钥；为空时禁用上传。
- `RECORDING_DIR`：宿主机录像目录，默认 `/home/test/recordings`，应改为实际磁盘挂载点。

客户端不需要人工设备登记。为便于客户和管理员识别点位，客户端推流时会同步点位负责人、主机名、内网 IP 和 MAC；CPU、内存、磁盘序列号不会上传。品牌摄像头无需登记，可直接配置 RTMP 地址。请通过防火墙限制 `22222` 管理端口和
`1935` 推流端口的访问范围。

录像默认分段600秒。录像不再使用全局开关：客户在AIService后台对自己名下的每个视频源分别设置是否录像及保留1～87600小时，规则写入`video_recording_rules`并立即生效。`RECORDING_RETAIN_HOURS`只作为尚未保存规则时的默认小时数；截图保留时间仍由服务端环境变量控制。系统每10分钟检查一次过期录像，保留期从片段结束时间开始计算。每台电脑可以包含多个独立视频源，当前内置来源类型为`screen`、`usb_camera`和`ip_camera`。
服务启动迁移会删除已废弃的`recording_settings`旧表，不读取或继承旧的全局录像配置。

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
`MEDIA_SRS_HTTP_HOST`设为`10.0.20.219:18889`；公网服务器继续使用公网地址。

对外端口：

- `18888`：公网 MediaService HTTP API，供APP调用；容器内部端口仍为`22222`
- `18887`：公网 AIService Web管理后台、健康状态和模块列表；容器内部端口仍为`11111`
- `1935`：SRS RTMP 推流
- `8080`：SRS HTTP-FLV/HLS，仅保留给电脑推流APP自预览和兼容工具

SRS API `1985`、MySQL `3306` 仅在 Docker 内部网络开放。生产环境应限制 `18888`、`18887`、`18889` 的来源，并通过 HTTPS 反向代理保护管理接口。

MediaService按规则和SRS在线状态创建持久化实时抽帧任务；AIService领取任务后直接
拉取SRS实时流、执行FFmpeg并回报图片。这个过程不读取录像片段，也不依赖该视频源的录像开关。
两个容器只为持久化AI图片而共享`recordings`卷。

两台目标服务器没有GPU不影响当前`frame_sampler`：抽帧由CPU上的FFmpeg完成。
`.env`已经预留`DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`和`QWEN_VL_MODEL`并传入
AIService容器，但当前代码尚未调用Qwen。后续Qwen视觉模型适合作为低频图片/视频片段
复核与事件描述，不应直接代替多路实时检测、跟踪和时序模型。API Key、Base URL和模型
必须属于同一百炼地域；密钥只能保存在服务器`.env`中。

## 常用接口

- `GET /api/health`：服务和数据库健康检查，数据库不可用时返回 HTTP 503。
- `POST /api/streams/publish-config`：APP 按设备和视频源获取永久 RTMP 推流配置，必须提交 `mac`、`source_type`、`source_id` 和 `display_name`。
- `GET /api/streams`、`GET /api/stats`：实时流和系统统计。
- `GET /api/video-sources`：查看已登记的视频源、在线状态及品牌摄像头直推地址。
- `POST /api/video-sources`：登记一个独立品牌摄像头并生成永久 RTMP 地址，请求字段为 `source_id`、`display_name` 和可选的 `brand`。
- `GET /api/segments`、`GET /segments/{id}/video`：供可信内网AdminService查询和播放全部录像。
- `/api/portal/auth/*`：平台初始化、登录、当前账号、修改密码和退出登录。
- `GET/PUT /api/portal/sources`：按登录客户返回视频源，并保存每路录像、保留小时数以及“每N分钟抽M帧”的实时抽帧规则。
- `GET/POST/PUT /api/portal/customers`、`PUT /api/portal/source-owner`：仅平台管理员创建、启停、重置客户账号和分配视频源。
- `GET /api/portal/frames`、`GET /api/portal/frames/{id}/image`：按客户隔离的抽帧结果。
- `GET /api/ai/algorithms`：AI能力目录；当前抽帧已启用，打架、安全帽和火灾为后续模块。
- `GET /api/ai/jobs/stats`：AI任务状态和Worker心跳概览。
- `/api/internal/ai/*`：AIService任务领取、结果上报和心跳接口，仅供Docker内部网络或可信局域网调用。
- `GET /api/client-updates/latest`：客户端检查更新。
- `POST /api/client-updates/upload`：上传更新 ZIP，必须提供 `X-Update-Key: <UPDATE_ADMIN_KEY>`。

集中观看和H.264/H.265录像回放使用`adminService/Viewer.exe`；客户逐路服务配置使用
`http://<AIService>:18887/customer/`，超级管理员使用AIService根页面。MediaService不再
提供浏览器管理页，`18888`仅提供HTTP API。更新ZIP必须包含`latest.yml`、对应的
`*-setup.exe`，并且版本、路径和SHA-512匹配。

RTMP 发布不使用 token、API Key、数据库登记或回调校验。任何能够访问1935端口的设备都可以向 `live` 应用推流；品牌摄像头后台可填写形如 `rtmp://<服务器>:1935/live/<自定义流名>` 的地址。不同设备必须使用不同流名，避免互相覆盖。生产环境应限制1935端口来源IP，管理端口18888也应仅允许可信来源访问。

部分摄像头后台提供一个“推流地址”输入框，可直接填写
`rtmp://example.com:1935/live/camera-001`；部分后台将其拆成两个输入框，则分别填写
`rtmp_server`（例如 `rtmp://example.com:1935/live`）和唯一的`stream_key`。如需在管理
后台保存摄像头名称和固定地址，也可以选择通过接口登记：

```bash
curl -X POST http://<服务器地址>:18888/api/video-sources \
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

数据库表由服务启动时自动创建或更新，包括`customers`、`users`、`user_sessions`、
`video_sources`、`video_recording_rules`、录像、抽帧以及`ai_algorithms`、
`video_analysis_rules`、`ai_jobs`、`ai_workers`、`ai_events`等平台表。
备份时还应保留宿主机 `RECORDING_DIR` 中的录像和AI证据文件；数据库记录和文件需要
同时恢复才能正常播放。
