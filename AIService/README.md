# AIService

统一的视频AI分析平台。当前第一个可用模块是 `frame_sampler`：直接从SRS实时视频流
抽取当前画面，并在AIService Web后台配置和查看结果。

## 服务边界

- MediaService 管理视频源、录像、AI算法目录、持久化任务、Worker状态和分析结果。
- AIService和MediaService使用同一个MySQL数据库`eyes`。数据库连接仍由MediaService统一管理，AIService通过内部API读写同一份规则、任务和结果，不建立第二套数据库。
- 两个服务共享 `/var/recordings` 卷作为持久化媒体存储；录像和AI图片位于不同子目录，数据库只保存索引。
- SRS是实时视频的唯一入口。抽帧以及未来打架、安全帽、火灾模块都由AIService直接拉取SRS实时流。

## 当前模块

| code | 状态 | 输入 | 输出 |
|---|---|---|---|
| `frame_sampler` | 已实现、按视频源启用 | SRS实时视频流 | JPEG 图片 |
| `fight` | 已登记、未启用 | 连续视频帧 | 异常事件 |
| `helmet` | 已登记、未启用 | 视频帧 | 异常事件 |
| `fire` | 已登记、未启用 | 连续视频帧 | 异常事件 |

## 已完成的实时AI公共底座

P0实时基础设施已经接入，但不会注册或模拟尚未实现的业务算法：

- AIService按Worker实际注册的实时能力同步在线流和逐路规则。
- `StreamManager`保证同一路视频只启动一个FFmpeg长连接解码进程。
- 阈值、ROI、冷却等算法配置热更新不重连；只有输入地址、协商FPS或分辨率变化才重启该路。
- 解码帧按每种算法的`sample_fps`分发；每种算法有独立并发配额，繁忙时丢弃过期帧。
- 证据缓存低频长保留，时序缓存高频短保留；`TemporalRealtimeAnalyzer`直接读取连续帧窗口。
- 算法连续失败会按视频源和算法独立熔断，等待后自动恢复，不影响同路其他算法。
- 内存环形缓存写入`_events`目录的JPEG截图和MP4短视频；同一事件有序、不同事件并行处理。
- `EventAggregator`统一处理连续命中、事件打开/关闭、冷却和幂等上报。
- MediaService提供租户隔离的事件查询、证据读取和人工确认/误报复核接口。
- 事件及证据按规则`retain_hours`清理；未配置时使用`AI_EVENT_RETAIN_HOURS`（默认720小时）。
- 多Worker按整路所需能力分配视频；没有单个Worker覆盖全部能力时明确报告`unassigned_streams`，不会重复拉流。
- Worker心跳持续上报活跃流、丢帧、推理失败、熔断和未分配流指标。

抽帧规则按视频源配置。可在Web后台选择摄像头，并设置“每N分钟抽M帧”；例如设置
每10分钟1帧，系统约每10分钟从当前实时画面抽取一张。平均频率最高每分钟60帧。
离线视频源不创建空任务，已有但尚未执行的实时任务会被丢弃，恢复推流后自动继续。
每个视频源最多保留一个待执行任务，
不会在AIService暂停期间积压需要补做的历史实时抽帧。抽帧不读取MP4文件，也不依赖
MediaService录像开关。

## 两套Web入口

超级管理员访问`http://<AIService服务器>:18887/`，可以：

- 首次访问时创建平台管理员；以后使用账号和密码登录。
- 创建、启停客户账号或重置客户密码，并把摄像头或电脑视频源分配给客户。
- 全局查看视频源、在线画面、服务配置和抽帧结果。

客户访问`http://<AIService服务器>:18887/customer/`，使用`h5`中的Quasar移动平台：

- 客户只能看到自己名下的视频源和在线画面，多个画面可上下滑动切换。
- 查看点位基本信息和在线数量。
- 对每个视频源独立开启或关闭录像，并设置录像保留小时数。
- 对每个视频源独立开启实时抽帧，并设置“每N分钟抽M帧”。
- 在个人中心查看账号与点位统计、修改密码或退出。

客户API在服务端强制要求`customer_admin`角色，并由MediaService继续按`customer_id`
过滤数据；平台管理员令牌不能绕过客户入口读取客户API。浏览器使用HttpOnly Cookie，
Capacitor/Tauri应用使用Bearer会话令牌。

浏览器会话保存在`HttpOnly` Cookie中，MediaService只保存会话令牌的SHA-256摘要，默认
7天过期。账号登录只保护后台管理和客户数据；按当前接入策略，SRS的RTMP推流入口仍不
使用Token或API Key，应通过防火墙/VPN限制1935端口来源。

网页播放地址由`AI_SRS_PUBLIC_BASE`配置，例如
`http://112.18.238.6:18889`（内网部署可使用对应内网地址和18889）。该地址必须能够被访问后台页面的客户电脑直接访问，不能填写
Docker内部地址`http://srs:8080`。H.265播放仅支持具备HEVC硬件解码能力的最新版Chrome；
页面会通过mpegts.js自动检测，不支持时提示使用H.264子码流。

正式Docker镜像在构建阶段下载并校验固定版本的mpegts.js，浏览器运行时从AIService本地
加载，不依赖公网CDN。直接从源码运行且本地库尚未生成时，服务会临时回退到固定版本CDN。

## 运行

生产部署使用 `mediaService/docker-compose.yml`，该 Compose 已加入
`ai-service` 并共享 `recordings` 卷：

```bash
cd mediaService
docker compose up -d --build --remove-orphans
```

检查状态：

```bash
docker compose ps
docker compose logs -f ai-service
curl http://127.0.0.1:22222/api/ai/algorithms
curl http://127.0.0.1:22222/api/ai/jobs/stats
curl http://127.0.0.1:11111/health
```

AIService通过11111端口提供超级管理后台、客户移动平台、`/health`和`/api/modules`。AI任务领取、结果和心跳接口
位于MediaService的22222端口，应仅允许Docker网络或可信局域网访问。

当前抽帧模块使用CPU和FFmpeg，不要求GPU。Compose已把服务器`.env`中的
`DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`、`QWEN_VL_MODEL`传入容器，供后续
Qwen视觉复核模块使用；`frame_sampler`当前不会消耗Qwen额度。Qwen适合对候选截图或
短视频进行二次判断和生成说明，多路实时打架、安全帽、火灾、入侵检测仍需要专用模型、
边缘计算或后续GPU Worker。

## 扩展一个算法

1. 在 `ai_service/modules` 中实现 `Analyzer`。
2. 为模块指定稳定的 `code`。
3. 在 `__main__.py` 注册模块。
4. 在 MediaService 算法目录启用对应能力并配置视频源规则。
5. 图片和视频证据写共享存储，只把路径、时间、置信度和模型版本上报。

单帧算法实现`RealtimeAnalyzer`，时序算法实现`TemporalRealtimeAnalyzer`，并通过
`register_realtime`注册；算法不应自行连接视频流。模块可用`max_concurrency`声明并发配额。
通用规则参数包括`sample_fps`、`min_hits`、`max_gap_seconds`、
`clear_after_seconds`、`cooldown_seconds`、`frame_width`和`window_seconds`。

实时控制面接口：

- `GET /api/internal/ai/realtime-config?worker_id=...&capabilities=...`：按Worker能力和稳定流归属返回在线流与规则。
- `POST /api/internal/ai/events`：幂等上报事件打开/关闭及证据路径。
- `GET/PUT /api/portal/analysis-rules`：查询或保存逐路通用算法规则。
- `GET /api/portal/events`：按租户、视频源、算法、状态和日期查询事件。
- `GET /api/portal/events/{id}/snapshot|clip`：读取事件证据。
- `PUT /api/portal/events/{id}/review`：保存`confirmed`或`rejected`人工复核结果。

## 本地测试

当前代码仅使用Python标准库：

```bash
python -m unittest discover -s tests -v
python -m compileall -q ai_service
```
