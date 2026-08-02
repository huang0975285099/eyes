# RecordingService

数据链路：

```text
Electron -> screen-helper -> FFmpeg -> RTMP :21935 -> SRS
                                                     |-> HLS/HTTP-FLV :28080
                                                     |-> RecordingService -> FFmpeg 分片 -> /var/recordings
Electron -> HTTP :52350 /api/clients/register -> RecordingService -> MySQL computers
```

RecordingService 通过 Docker 内部地址 `srs:1985` 发现流，通过 `srs:1935` 拉流录制。SRS 的公网端口只供客户端推流和管理页面播放。

## 部署

1. 复制 `.env.example` 为 `.env` 并填写数据库和密钥。
2. 确保 MySQL 的 `recorder` 用户至少拥有：

```sql
GRANT SELECT ON ukeysystem.regions TO 'recorder'@'%';
GRANT SELECT ON ukeysystem.areas TO 'recorder'@'%';
GRANT SELECT ON ukeysystem.zones TO 'recorder'@'%';
GRANT SELECT ON ukeysystem.users TO 'recorder'@'%';
GRANT SELECT, INSERT, UPDATE ON ukeysystem.computers TO 'recorder'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON ukeysystem.recording_segments TO 'recorder'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON ukeysystem.recording_frames TO 'recorder'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON ukeysystem.zone_assignments TO 'recorder'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON ukeysystem.node_settings TO 'recorder'@'%';
FLUSH PRIVILEGES;
```

3. 启动：`docker compose up -d --build`。

公网端口：RTMP `21935`，播放 `28080`，RecordingService API/管理页 `52350`。SRS API `1985` 仅在 Docker 内部网络开放。

生产环境应通过防火墙限制 `52350` 和 `28080` 的来源，并为管理 API 配置 HTTPS 反向代理。
