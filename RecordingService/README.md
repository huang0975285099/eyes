# RecordingService

本项目使用独立的 `eyes` 数据库，不依赖任何外部业务数据库。

```text
Electron -> FFmpeg -> RTMP :21935 -> SRS
                                      |-> HLS/HTTP-FLV :28080
                                      |-> RecordingService -> 录像文件
Electron -> HTTP :52350 -> RecordingService -> MySQL 8.1.0 / eyes
```

## 数据库

Docker Compose 会启动 `mysql:8.1.0`，配置如下：

- 地址：`mysql:3306`（仅 Docker 内网，不开放公网端口）
- 用户：`root`
- 密码：`all_seeing_eyes`
- 数据库：`eyes`
- 数据卷：`mysql_data`

RecordingService 启动时会等待 MySQL 就绪，并自动创建或更新这些表：

`regions`、`areas`、`zones`、`users`、`computers`、`zone_assignments`、`node_settings`、`recording_segments`、`recording_frames`。

健康检查 `GET /api/health` 会同时检查数据库连接；数据库断开时返回 HTTP 503。

## 部署

1. 将 `.env.example` 复制为 `.env`，填写 `CLIENT_API_KEY` 和 `STREAM_TOKEN_SECRET`。
2. 启动：`docker compose up -d --build`。
3. 检查：`docker compose ps` 和 `docker compose logs -f recording-service`。

公网端口：RTMP `21935`、播放 `28080`、API/管理页 `52350`。MySQL 和 SRS API 均只在 Docker 内部网络开放。

## 备份与恢复

备份：

```bash
docker compose exec -T mysql mysqldump -uroot -pall_seeing_eyes --single-transaction eyes > eyes-backup.sql
```

恢复：

```bash
docker compose exec -T mysql mysql -uroot -pall_seeing_eyes eyes < eyes-backup.sql
```

生产环境应限制 `52350` 和 `28080` 的访问来源，并通过 HTTPS 反向代理保护管理接口。
