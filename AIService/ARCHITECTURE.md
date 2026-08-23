# AI分析平台架构

## 当前阶段

当前实现的是离线任务通道：RecordingService完成录像并建立索引后，创建
`frame_sampler`任务；AIService通过租约领取任务，执行抽帧，并把图片索引回报给
RecordingService。任务状态存放在MySQL，AIService重启不会丢任务。

```text
SRS -> RecordingService -> MP4 + AIJob
                              |
                              v
                        AIService Worker
                              |
                              v
                    JPEG + RecordingFrame
```

## 实时算法阶段

打架、安全帽和火灾不应使用低频录像抽帧。下一阶段在AIService增加实时数据通道：

```text
RecordingService 视频源与规则
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
- 每个视频源通过 `video_analysis_rules` 独立启用算法、阈值、时段和ROI。
- 事件统一写 `ai_events`，证据文件写共享磁盘或后续对象存储。
- 多GPU扩容时，以视频流为单位分配给Worker，保证一条流在一个节点只解码一次。

## 模块职责

- `AnalyzerRegistry`：模块注册与能力上报。
- `frame_sampler`：当前已实现的录像片段抽帧模块。
- `StreamManager`：下一阶段实现在线流同步、拉流、重连和共享解码。
- `EventAggregator`：下一阶段实现连续命中、事件合并、冷却和证据导出。
- RecordingService：始终是配置、任务、事件、录像索引和后台页面的控制中心。

`fight`、`helmet`、`fire`已经进入算法目录但保持禁用，避免未接入模型时产生虚假能力。
