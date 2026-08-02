# RecordingService

RecordingService 为千里眼客户端提供设备登记、短期 RTMP 推流地址、实时流查看、录像录制和客户端更新服务。数据保存在独立的 `eyes` MySQL 数据库中，不依赖其他业务数据库。

```text
Electron -> HTTP :52350 -> RecordingService -> MySQL 8.1.0 / eyes
Electron -> FFmpeg -> RTMP :1935 -> SRS
                                      |-> HTTP-FLV/HLS :8090
                                      |-> RecordingService -> 录像文件
```

## 配置

```bash
cp .env.example .env
```

生产环境至少修改以下配置：

- `CLIENT_API_KEY`：客户端登记和推流配置接口的共享密钥，必须与 `app/config.json` 的 `apiKey` 一致。
- `STREAM_TOKEN_SECRET`：签发/校验短期推流 token 的密钥；不要复用公开或弱密钥。
- `PUBLIC_RTMP_HOST`：客户端可访问的 RTMP 地址，例如 `example.com:1935`。
- `RECORDING_SRS_HTTP_HOST`：客户端/管理页可访问的 SRS HTTP-FLV/HLS 地址，例如 `example.com:8090`。
- `UPDATE_ADMIN_KEY`：客户端更新 ZIP 上传密钥；为空时禁用上传。
- `RECORDING_DIR`：宿主机录像目录，默认 `/home/test/recordings`，应改为实际磁盘挂载点。

录像默认分段 600 秒；Compose 当前默认录像保留 2 天、截图保留 30 天。管理页保存的录制开关和录像保留天数会写入数据库，并覆盖环境变量、立即生效。

## Docker 部署

需要 Docker、Docker Compose Plugin，以及可用的录像存储目录：

```bash
docker compose config -q
docker compose up -d --build
docker compose ps
docker compose logs -f recording-service
```

也可以使用离线部署脚本（脚本会加载镜像 tar 并执行健康检查）：

```bash
chmod +x deploy.sh
./deploy.sh                 # 使用默认 latest
./deploy.sh --tag <标签>    # 使用指定 recording-service 镜像标签
```

对外端口：

- `52350`：管理后台和 API
- `1935`：SRS RTMP 推流
- `8090`：SRS HTTP-FLV/HLS 播放

SRS API `1985`、MySQL `3306` 仅在 Docker 内部网络开放。生产环境应限制 `52350`、`8090` 的来源，并通过 HTTPS 反向代理保护管理接口。

## 常用接口

- `GET /api/health`：服务和数据库健康检查，数据库不可用时返回 HTTP 503。
- `POST /api/clients/register`：客户端登记/更新设备信息。
- `GET /api/streams/publish-config`：获取带短期 token 的 RTMP 推流配置。
- `GET /api/streams`、`GET /api/stats`：实时流和系统统计。
- `GET /api/segments`、`GET /api/frames`：录像片段和截图列表；媒体分别通过 `/segments/{id}/video`、`/frames/{id}/image` 获取。
- `GET/PUT /api/recording-settings`：查看或修改录制开关、录像保留天数。
- `GET /api/client-updates/latest`：客户端检查更新。
- `POST /api/client-updates/upload`：上传更新 ZIP，必须提供 `X-Update-Key: <UPDATE_ADMIN_KEY>`。

管理后台地址为 `http://<服务器地址>:52350`。更新 ZIP 必须包含 `latest.yml`、对应的 `*-setup.exe`，并且其中的版本、路径和 SHA-512 必须匹配；客户端侧 ZIP 可由 `app` 目录的 `pnpm run build:update` 生成。

## 备份与恢复

```bash
docker compose exec -T mysql mysqldump -uroot -pall_seeing_eyes \
  --single-transaction eyes > eyes-backup.sql

docker compose exec -T mysql mysql -uroot -pall_seeing_eyes \
  eyes < eyes-backup.sql
```

数据库表由服务启动时自动创建或更新，包括 `users`、`computers`、`recording_settings`、`recording_segments` 和 `recording_frames`。备份时还应保留宿主机 `RECORDING_DIR` 中的录像文件；数据库记录和文件需要同时恢复才能正常播放。
