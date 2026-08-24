# AI分析平台架构

## 当前实时抽帧通道

`frame_sampler`与录像存储完全独立。客户在AIService后台对自己名下的每个视频源分别
设置录像和抽帧规则；MediaService根据视频源规则和SRS在线状态生成
持久化`live_stream`任务；AIService领取任务后直接拉取SRS当前实时流，生成JPEG并
回报图片索引。关闭MediaService录像不会停止实时抽帧。

```text
                    ┌-> MediaService逐路录像（可独立关闭） -> MP4
设备 -> SRS实时流 ──┤
                    └-> live_stream AIJob -> AIService FFmpeg -> JPEG
                                                   |
                                                   v
                                      RecordingFrame结果索引
```

任务状态、视频源、分析规则和结果索引都保存在共享MySQL数据库`eyes`。AIService通过
MediaService内部API访问，不建立第二套数据库连接。JPEG写共享证据目录的`_frames`
子目录，不读取录像MP4。

## 多客户控制面

```text
平台管理员 -> 创建客户账号 -> 分配VideoSource
客户账号 -> AIService HttpOnly会话 -> MediaService租户鉴权
                                      |-> 逐路录像规则
                                      |-> 逐路抽帧规则
                                      |-> 当前客户实时流和分析结果
```

`customers`、`users`、`user_sessions`和视频源的`customer_id`由MediaService管理。
AIService不直连数据库；浏览器也不会获得MediaService会话令牌。平台管理员能管理全部
视频源，客户管理员只能访问自己`customer_id`对应的数据。

## 实时算法扩展阶段

打架、安全帽和火灾同样分析SRS实时流。多算法启用后，继续扩展为每路流只拉取和解码
一次的共享会话：

```text
MediaService 视频源与规则
              |
              v
       Realtime Task Manager
              |
              v
 SRS -> Stream Session（每路只拉流和解码一次）
              |
       +------+------+
       |             |
 sampled frames   temporal ring buffer
       |             |
 helmet/fire        fight
       +------+------+
              |
          AIEvent API
```

约束：

- 原始全分辨率帧只在AI节点本机内存中流转，不写MySQL，也不通过Redis跨机广播。
- 图片模型按各自采样频率读取帧；时序模型读取同一流的环形缓存。
- 每个视频源通过`video_analysis_rules`独立启用算法、阈值、时段和ROI。
- 事件统一写`ai_events`，证据文件写共享磁盘或后续对象存储。
- 多GPU扩容时，以视频流为单位分配给Worker，保证一条流在一个节点只解码一次。

## 模块职责

- `AnalyzerRegistry`：模块注册与能力上报。
- `frame_sampler`：当前已实现的SRS实时流抽帧模块。
- `StreamManager`：下一阶段实现长连接拉流、重连和多算法共享解码。
- `EventAggregator`：下一阶段实现连续命中、事件合并、冷却和证据导出。
- MediaService：配置、任务、事件、录像索引和数据API的控制中心。

`fight`、`helmet`、`fire`已经进入算法目录但保持禁用，避免未接入模型时产生虚假能力。
