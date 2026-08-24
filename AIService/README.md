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

抽帧规则按视频源配置。可在Web后台选择摄像头，并设置每分钟抽取1到60帧；例如设置
每分钟2帧，系统约每30秒从当前实时画面抽取一张。离线视频源不创建空任务，恢复推流
后自动继续。抽帧不读取MP4文件，也不依赖MediaService录像开关。

## Web管理后台

访问`http://<AIService服务器>:11111/`，可以：

- 首次访问时创建平台管理员；以后使用账号和密码登录。
- 平台管理员创建客户账号，并把摄像头或电脑视频源分配给客户。
- 客户只能看到自己名下的视频源、在线画面和抽帧结果。
- 对每个视频源独立开启或关闭录像，并设置录像保留天数。
- 对每个视频源独立开启实时抽帧，并设置每分钟抽帧数量。
- 查看视频源在线状态、累计结果和最后结果时间。
- 使用mpegts.js低延迟播放SRS中的H.264/H.265在线流。
- 按视频源和日期筛选、查看抽帧图片。

浏览器会话保存在`HttpOnly` Cookie中，MediaService只保存会话令牌的SHA-256摘要，默认
7天过期。账号登录只保护后台管理和客户数据；按当前接入策略，SRS的RTMP推流入口仍不
使用Token或API Key，应通过防火墙/VPN限制1935端口来源。

网页播放地址由`AI_SRS_PUBLIC_BASE`配置，例如
`http://10.0.20.219:8080`。该地址必须能够被访问后台页面的客户电脑直接访问，不能填写
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

AIService通过11111端口提供客户Web后台、`/health`和`/api/modules`。AI任务领取、结果和心跳接口
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

实时算法下一阶段会增加共享拉流/解码和帧环形缓存。不要让每一个算法模块独立
拉取同一路视频，否则会重复消耗网络、解码器和GPU资源。

## 本地测试

当前代码仅使用Python标准库：

```bash
python -m unittest discover -s tests -v
python -m compileall -q ai_service
```
