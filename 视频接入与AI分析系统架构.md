# 视频接入与AI分析系统架构

## 1. 文档目的

本系统需要统一接入电脑桌面、电脑外接摄像头、网络摄像头以及不同品牌的独立摄像头，提供实时观看、录像、人工抽帧查看和多种AI分析能力。

总体原则是：

- 所有设备的视频先统一进入SRS流媒体服务。
- RecordingService负责视频源、录像和AI配置管理。
- AIService负责实际的视频分析，不负责设备品牌适配和录像控制。
- 同一路视频在一个AI节点只拉取、解码一次，再交给多个算法模块使用。
- 打架、安全帽、火灾以及后续算法使用统一的配置、任务、事件和证据模型。

## 2. 总体架构

```text
电脑桌面 ─────────────┐
电脑外接USB摄像头 ────┤
电脑连接RTSP摄像头 ───┤
品牌摄像头RTMP直推 ───┤
不支持推流的摄像头 ───┘ 由APP/NVR/边缘网关拉RTSP后转推
                         │
                         ▼
                    SRS流媒体服务
                         │
          ┌──────────────┼──────────────┐
          │              │              │
          ▼              ▼              ▼
      实时观看     RecordingService   AIService
                       │                │
                       │                ├─ 共享拉流与解码
                       │                ├─ 录像抽帧
                       │                ├─ 打架检测
                       │                ├─ 安全帽检测
                       │                ├─ 火灾检测
                       │                └─ 后续算法
                       │                     │
                       └───────────◄─────────┘
                             AI事件和证据上报
```

系统的数据分为两类：

```text
MySQL：设备、视频源、录像索引、AI规则、任务、事件、处理状态
共享磁盘/NAS：录像文件、抽帧图片、告警截图、告警短视频
```

## 3. 各服务职责

### 3.1 SRS流媒体服务

SRS是所有实时视频的统一入口和分发中心，负责：

- 接收电脑APP和摄像头推送的RTMP视频。
- 将同一路视频分发给观看端、RecordingService和AIService。
- 提供RTMP、HTTP-FLV/HLS等内部或观看协议。
- 隔离上游设备差异，让下游服务只处理标准视频流。

SRS不负责：

- 设备信息管理。
- 录像保留策略。
- AI算法配置。
- 异常行为判断。

### 3.2 RecordingService

目录：`D:\project\eyes\RecordingService`

RecordingService是系统控制中心，负责：

- 电脑和视频源登记。
- 为视频源生成唯一、稳定的`stream_name`。
- 提供品牌摄像头RTMP推流地址。
- 查询SRS中的在线流。
- 控制全局录像开启或关闭。
- 从SRS拉流并按时间分段录制MP4。
- 管理录像和图片索引、保留天数及过期清理。
- 保存每个视频源启用哪些AI算法及算法参数。
- 创建离线AI任务并接收AIService执行结果。
- 保存AI Worker心跳、AI异常事件和人工复核状态。
- 提供统一管理后台。

RecordingService不负责执行深度学习模型。录像完整性检查仍可使用FFprobe，但抽帧和未来AI推理由AIService执行。

### 3.3 AIService

目录：`D:\project\eyes\AIService`

AIService是统一的AI分析平台，负责：

- 向RecordingService报告Worker心跳和可用算法能力。
- 从RecordingService获取需要执行的任务和视频源AI配置。
- 从SRS自动拉取需要分析的在线流。
- 负责断线重连、解码、采样和短期帧缓存。
- 将解码结果分发给启用的算法模块。
- 执行录像抽帧、打架、安全帽、火灾等算法。
- 进行连续命中、告警冷却和事件合并。
- 生成截图、告警前后短视频等证据。
- 向RecordingService上报结构化AI事件。

AIService不负责：

- 为摄像头生成推流地址。
- 保存设备品牌账号或摄像头后台配置。
- 控制RecordingService是否录制完整录像。
- 直接修改RecordingService的MySQL数据。

## 4. 多种设备如何接入

系统使用统一的`VideoSource`描述视频来源。主要来源类型为：

| 来源类型 | 示例 | 接入方式 |
|---|---|---|
| `screen` | 电脑桌面 | 电脑APP采集并推送RTMP |
| `usb_camera` | 电脑外接USB摄像头 | 电脑APP调用FFmpeg采集并推送RTMP |
| `ip_camera` | 电脑能够访问的RTSP摄像头 | 电脑APP拉取RTSP并转推RTMP |
| `direct_camera` | 独立品牌摄像头 | 摄像头后台直接填写系统提供的RTMP地址 |

所有视频源最终都必须转换成：

```text
rtmp://<SRS地址>:1935/live/<stream_name>
```

下游录像和AI分析只识别`stream_name`，不关心视频来自电脑桌面、海康、大华、宇视或其他品牌。

如果摄像头只支持RTSP拉流、不支持主动RTMP推流，需要由以下任意组件完成协议转换：

- 部署在现场电脑上的现有APP。
- NVR。
- 独立边缘网关。
- 后续增加的摄像头接入网关服务。

不建议让每一种AI算法直接连接不同品牌摄像头，否则账号、协议、重连和品牌兼容问题会进入所有AI模块。

## 5. 录像控制逻辑

录像开关仍然由RecordingService控制：

```http
GET /api/recording-settings
PUT /api/recording-settings
```

### 开启录像

```text
设备推流 -> SRS -> RecordingService录制MP4
                   └-> 录像片段入库后创建frame_sampler任务
```

### 关闭录像

```text
设备继续推流 -> SRS继续接收
                  ├-> 实时观看正常
                  ├-> RecordingService停止生成新录像
                  └-> 实时AI分析仍然可以运行
```

当前AIService的第一个模块`frame_sampler`以已完成的录像片段作为输入。因此关闭录像后不会产生新的录像片段，也不会产生新的抽帧任务。

未来打架、安全帽、火灾等实时算法直接分析SRS实时流，不受录像开关影响。是否保存异常短视频由AI规则单独控制。

## 6. AI算法配置逻辑

业务人员在RecordingService管理后台选择具体摄像头，然后配置需要启用的算法。配置保存在RecordingService，AIService只读取和执行。

示例：

| 视频源 | 打架 | 安全帽 | 火灾 |
|---|---:|---:|---:|
| 学校操场摄像头 | 开启 | 关闭 | 关闭 |
| 工地入口摄像头 | 关闭 | 开启 | 关闭 |
| 仓库内部摄像头 | 关闭 | 关闭 | 开启 |
| 工厂车间摄像头 | 开启 | 开启 | 开启 |
| 电脑桌面 | 关闭 | 关闭 | 关闭 |

每条规则至少包含：

```json
{
  "video_source_id": 12,
  "algorithm_code": "helmet",
  "enabled": true,
  "config": {
    "threshold": 0.80,
    "sample_fps": 2,
    "cooldown_seconds": 60,
    "schedule": "08:00-18:00",
    "roi": [[100, 100], [1500, 100], [1500, 900], [100, 900]]
  }
}
```

字段含义：

- `threshold`：模型判断阈值。
- `sample_fps`：该算法每秒分析多少帧。
- `cooldown_seconds`：同类事件的重复告警冷却时间。
- `schedule`：算法生效时段。
- `roi`：只分析画面中的指定区域。

规则表使用通用`algorithm_code + config_json`结构，因此增加新算法时不需要为每个算法新建一套配置表。

## 7. AIService共享拉流与解码

错误方式：

```text
打架服务单独拉取摄像头A
安全帽服务再次拉取摄像头A
火灾服务第三次拉取摄像头A
```

对于75路视频和3种算法，上述方式可能产生225路拉流和解码任务。

正确方式：

```text
摄像头A -> 一个StreamSession -> 一次拉流和解码
                                ├-> 安全帽：每秒取2帧
                                ├-> 火灾：每秒取2～4帧
                                └-> 打架：读取连续2～4秒帧缓存
```

AIService按照视频流分配任务，而不是按照算法重复拉流。每个StreamSession负责：

- 拉流和自动重连。
- 视频解码。
- 最新帧缓存。
- 连续视频环形缓存。
- 根据算法采样频率分发帧。
- 统计丢帧、解码错误和分析延迟。

多GPU扩容时，以视频流为单位将StreamSession分配到不同Worker，确保同一视频流在同一节点只解码一次。

## 8. 不同AI模块的输入方式

| 算法 | 输入 | 建议频率 | 说明 |
|---|---|---:|---|
| 录像抽帧 | 已完成MP4片段 | 每片段2张起 | 用于人工浏览，不是实时检测输入 |
| 打架检测 | 连续视频序列 | 4～8 FPS | 需要分析连续2～4秒动作 |
| 安全帽检测 | 单帧或短连续帧 | 1～2 FPS | 需要人员、头部和安全帽关联 |
| 火灾检测 | 连续图片/视频 | 2～4 FPS | 建议同时识别烟雾和火焰并连续确认 |

不同算法共享解码结果，但可以拥有不同的采样频率、阈值、ROI和事件规则。

## 9. AI事件处理

AI模块不能只命中一帧就立即认定发生异常，统一事件流程为：

```text
模型首次命中
     |
连续多帧或多个窗口达到阈值
     |
合并成一个AI事件
     |
保存截图和告警前后短视频
     |
上报RecordingService
     |
后台显示“待确认”
     |
人工选择“确认”或“误报”
```

通用事件应包含：

```text
event_id
video_source_id
stream_name
algorithm_code
event_type
confidence
started_at / ended_at
snapshot_path
clip_path
model_version
status: pending / confirmed / false_positive
metadata_json
```

人工确认结果需要保留，后续可作为现场模型优化和误报分析数据。

## 10. 数据模型

当前系统已经建立以下通用模型：

- `video_sources`：所有视频来源的统一登记信息。
- `recording_settings`：全局录像开关和录像保留时间。
- `recording_segments`：录像片段索引。
- `recording_frames`：人工查看图片索引。
- `ai_algorithms`：AI算法能力目录。
- `video_analysis_rules`：每个视频源的算法开关和参数。
- `ai_jobs`：可重试、可租约领取的离线AI任务。
- `ai_workers`：AIService Worker能力和心跳。
- `ai_events`：打架、安全帽、火灾及后续算法的统一事件表。

视频文件和图片文件不能直接存入MySQL。MySQL只保存路径和元数据，文件保存在共享磁盘、NAS或后续对象存储。

## 11. 服务接口边界

### 当前已实现

| 接口 | 用途 |
|---|---|
| `GET /api/streams` | 查询当前在线流 |
| `GET/PUT /api/recording-settings` | 查询或控制录像 |
| `GET /api/ai/algorithms` | 查询AI算法目录 |
| `GET /api/ai/jobs/stats` | 查询AI任务和Worker状态 |
| `POST /api/internal/ai/jobs/claim` | AIService领取离线任务 |
| `POST /api/internal/ai/jobs/report` | AIService上报任务结果 |
| `POST /api/internal/ai/workers/heartbeat` | AIService上报心跳 |

### 后续需要实现

| 接口 | 用途 |
|---|---|
| `GET/PUT /api/video-sources/{id}/ai-rules` | 管理某个视频源的AI规则 |
| `GET /api/internal/ai/realtime-config` | AIService同步在线流和实时算法配置 |
| `POST /api/internal/ai/events` | AIService上报实时异常事件 |
| `GET /api/ai/events` | 后台查询异常事件 |
| `PUT /api/ai/events/{id}` | 人工确认或标记误报 |

内部AI写接口不使用Token时，必须通过Docker内部网络、防火墙、反向代理访问规则或可信局域网限制访问，不能直接向公网开放。

## 12. 故障隔离

### AIService停止

- 设备推流不受影响。
- 实时观看不受影响。
- RecordingService录像不受影响。
- 离线抽帧任务保留在MySQL，AIService恢复后继续处理。
- 实时AI检测暂停。

### RecordingService关闭录像

- 推流和观看继续。
- 不再生成完整录像和新的录像抽帧任务。
- 未来实时AI检测继续。

### 单个AI算法异常

- 只停止或降级对应算法。
- 其他算法继续使用同一视频流。
- Worker上报错误和心跳，后台显示故障状态。

### SRS停止

- 上游推流、实时观看、录像和实时AI都会中断。
- SRS是媒体链路核心，应配置自动重启和健康检查。

## 13. 部署结构

第一阶段：

```text
mysql
srs
recording-service
ai-service
```

AIService和RecordingService共享`/var/recordings`存储卷。AIService的健康端口只在Docker内部开放。

后续多GPU扩容：

```text
ai-manager
ai-worker-gpu-01
ai-worker-gpu-02
...
```

Worker通过心跳报告：

- Worker编号。
- 主机和GPU信息。
- 支持的算法列表。
- 当前任务数。
- 最近错误。
- 最后心跳时间。

调度器根据视频流数量、启用算法和GPU负载分配StreamSession。

## 14. 当前完成情况

已经完成：

- 多种视频来源的统一登记和稳定`stream_name`。
- 电脑桌面、USB摄像头、网络摄像头和品牌摄像头直推接入基础。
- RecordingService全局录像开关。
- 独立AIService项目和模块注册机制。
- 将原RecordingService抽帧执行迁移为AIService的`frame_sampler`模块。
- 持久化AI任务、任务租约、超时恢复、失败重试和结果幂等入库。
- AI Worker心跳和管理后台状态展示。
- `fight`、`helmet`、`fire`算法目录及通用规则、事件数据结构。
- Docker Compose和离线构建部署脚本支持AIService。

尚未完成：

- 按摄像头配置AI算法的管理接口和后台页面。
- AIService实时在线流同步。
- 每路视频只解码一次的StreamSession和环形缓存。
- 打架、安全帽和火灾的实际模型及推理代码。
- 实时事件上报、告警证据和人工确认页面。
- 75路视频的GPU压力测试和容量评估。

## 15. 推荐后续实施顺序

1. 实现每个视频源的AI规则管理接口和后台页面。
2. 实现AIService实时配置同步和StreamSession。
3. 实现共享解码、图片采样和时序环形缓存。
4. 先使用模拟算法跑通事件、截图、短视频和人工确认流程。
5. 接入打架检测模型并使用少量视频验证。
6. 接入安全帽检测模型。
7. 接入烟雾和火焰检测模型。
8. 从5路、20路逐步压力测试到75路。
9. 根据实际GPU负载、误报和漏报调整采样频率、阈值及硬件数量。

以上架构保证增加新设备品牌时主要修改接入层，增加新AI能力时主要增加算法模块，两者不会相互耦合。
