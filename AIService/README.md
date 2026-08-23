# AIService

统一的视频AI分析平台。当前第一个可用模块是 `frame_sampler`：从
RecordingService 已完成的录像片段中抽取代表性图片，供现有管理后台人工查看。

## 服务边界

- RecordingService 管理视频源、录像、AI算法目录、持久化任务、Worker状态和分析结果。
- AIService 领取任务并执行算法，不直接访问 MySQL。
- 两个服务共享 `/var/recordings` 卷；视频和图片写共享卷，数据库只保存索引。
- SRS 仍然是实时视频的唯一入口。未来打架、安全帽和火灾模块由 AIService 从
  SRS 拉流，不能复用当前低频录像抽帧作为实时模型输入。

## 当前模块

| code | 状态 | 输入 | 输出 |
|---|---|---|---|
| `frame_sampler` | 已实现、默认启用 | 完成的 MP4 录像片段 | JPEG 图片 |
| `fight` | 已登记、未启用 | 连续视频帧 | 异常事件 |
| `helmet` | 已登记、未启用 | 视频帧 | 异常事件 |
| `fire` | 已登记、未启用 | 连续视频帧 | 异常事件 |

抽帧规则保持与原系统一致：不足30秒的片段不抽；普通片段抽2张；达到10分钟后，
每5分钟对应1张。已存在的图片会直接复用并重新登记，不会重复运行 FFmpeg。

## 运行

生产部署使用 `RecordingService/docker-compose.yml`，该 Compose 已加入
`ai-service` 并共享 `recordings` 卷：

```bash
cd RecordingService
docker compose up -d --build
```

检查状态：

```bash
docker compose ps
docker compose logs -f ai-service
curl http://127.0.0.1:52350/api/ai/algorithms
curl http://127.0.0.1:52350/api/ai/jobs/stats
```

AIService 的52351端口仅在Compose内部提供 `/health` 和 `/api/modules`，默认不映射
到宿主机。AI任务领取、结果和心跳接口也应仅允许Docker网络或可信局域网访问。

## 扩展一个算法

1. 在 `ai_service/modules` 中实现 `Analyzer`。
2. 为模块指定稳定的 `code`。
3. 在 `__main__.py` 注册模块。
4. 在 RecordingService 算法目录启用对应能力并配置视频源规则。
5. 图片和视频证据写共享存储，只把路径、时间、置信度和模型版本上报。

实时算法下一阶段会增加共享拉流/解码和帧环形缓存。不要让每一个算法模块独立
拉取同一路视频，否则会重复消耗网络、解码器和GPU资源。

## 本地测试

当前代码仅使用Python标准库：

```bash
python -m unittest discover -s tests -v
python -m compileall -q ai_service
```
