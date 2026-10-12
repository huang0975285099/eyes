# PP-Human × SRS 实时巡检实施方案（方案一：服务端渲染标注流）

> **文档状态（2026-10-11 与代码同步）**：阶段一~三已完成；阶段四已完成（SSE 告警三件套 /
> 标注流自动切换 / 降级 / 口罩模块 / **区域入侵**）。本版按实际实现校正了拉流协议、事件字段、
> 服务端点、`mask_engine` 取值，并新增 §4.9 区域入侵。**仍待办**见 §9。
> 代码为准：`assistant/paddle_service/`、`web/voice-srs.js`、`web/index-inspect.js`。

> 目标：USB 摄像头画面全部经 SRS 中转；飞桨 8771 服务拉流做 **MOT 跟踪 + 17 点骨骼 + 行人属性 + 行为识别**，
> 把**画好框、骨骼、属性、行为标签的画面**推回 SRS；前端巡检窗口直接播放这路"标注流"，
> 行为事件以 JSON 经 SSE 推送，复用现有告警窗 / 语音播报 / 历史链路。
> 观看流与分析流同源，天然不存在骨架错位问题。

---

## 1. 总体架构与流约定

**机器职责（10.0.20.219 无显卡）**

- **219（无 GPU，Docker）**：只跑 SRS，负责流中转（WHIP/WHEP/RTMP/FLV，均不重编码，不吃显卡）；
  可选开启 DVR 持续录像存盘。不安装任何推理组件。
- **本机 Windows（有 GPU）**：浏览器推/拉流；8770 ASR；Ollama；**8771 飞桨全部推理**。
  巡检只有在本机开机且 8771 运行时有效。

```
本机 USB 摄像头
   │ getUserMedia（浏览器，仅视频轨）
   ▼ WHIP 推流（/rtc/v1/publish/）
219 SRS  rtmp://.../live/usbN（原始流）
   │                        ▲
   ▼ RTMP 拉流（跨网段）      │ RTMP 推回（ffmpeg 管道，已画标注）
本机 飞桨服务 8771（.venv-paddle，GPU）
 PP-Human Pipeline           │
   ├ MOT   跟踪框 + track id │
   ├ KPT   17 点骨骼          │
   ├ ATTR  行人属性（实测 26 标签无口罩，口罩走独立模块，见 4.8）
   ├ STGCN 摔倒（骨骼行为）   │
   └ VIDEO_ACTION 打架（视频分类）
                             │
219 SRS  rtmp://.../live/usbN_labeled（标注流）── WHEP ──▶ 本机前端巡检窗口
   │
   └─ 事件 JSON（fall/fight/intrusion/no_mask）── SSE localhost:8771/events ──▶ 前端告警/播报/历史
```

每路流带宽约 2~4Mbps（720p）：本机向 219 推一路、拉两路（原始+标注），需 10.0.6↔10.0.20 网段路由可达。

### 1.1 各段协议与延迟（务必区分）

| 链路 | 协议 | 典型延迟 |
|---|---|---|
| 浏览器 → 219 推流 | WebRTC（WHIP `/rtc/v1/publish/`） | 50~200ms |
| 浏览器 ← 219 看原始流 usbN | WebRTC（`/rtc/v1/play/`） | 200~500ms |
| 219 ↔ 本机 8771（拉原始流 / 推标注流） | **HTTP-FLV 拉**（OpenCV）+ **RTMP 推**（ffmpeg） | 各 200~500ms |
| 飞桨推理（MOT+骨骼+属性+行为） | GPU | 100~300ms/帧 |

- 巡检窗口看**标注流**端到端约 **0.8~1.5 秒**，看 **SRS 原始流**约 0.3~0.5 秒；非巡检态看本地 getUserMedia 流为 0 延迟。
- 分析段不用 WebRTC 的原因：Python/OpenCV 侧无稳定轻量 WebRTC 栈（aiortc 在 Windows 兼容性差）；
  SRS 已开 `rtc_to_rtmp`/`rtmp_to_rtc` 自动桥接，浏览器侧全程只使用 WebRTC。
- 告警场景（摔倒/闯入/口罩）对亚秒级延迟不敏感；方案因此设计为"平时本地流、巡检才切 SRS"。

**流命名约定（多路按 camSlots 序号）**

| 流名 | 内容 | 产生者 | 消费者 |
|---|---|---|---|
| `usb1`、`usb2`… | 原始流 | 浏览器 WHIP 推流 | 8771 拉流分析；前端"原始视角" |
| `usb1_labeled`… | 标注流（框+骨骼+属性+行为） | 8771 ffmpeg 推回 | 前端巡检窗口（默认视角） |

URL 速查（N 为摄像头序号）：

| 用途 | 地址 |
|---|---|
| 浏览器推流信令 | `http://10.0.20.219:1985/rtc/v1/publish/`，streamurl=`webrtc://10.0.20.219/live/usbN` |
| 浏览器播放信令 | `http://10.0.20.219:1985/rtc/v1/play/`，streamurl=`webrtc://10.0.20.219/live/usbN_labeled` |
| PP-Human 拉流（**实际使用**） | `http://10.0.20.219:8080/live/usbN.flv`（HTTP-FLV，见 §4.0 末条） |
| 8771 推回标注流（**实际使用**） | `rtmp://10.0.20.219:1935/live/usbN_labeled` |
| ffmpeg 备用拉流 | `rtmp://10.0.20.219:1935/live/usbN` |

---

## 2. 阶段一：SRS 部署与裸流验收（✅ 已完成）

- [ ] 在 Windows 本机执行：

```powershell
cd D:\project\eyes\voiceInteractive\deploy\srs
.\deploy-srs.ps1
```

- [ ] 防火墙/安全组放通：`1935/tcp 1985/tcp 8080/tcp 8000/udp`
- [ ] API 验证：浏览器打开 `http://10.0.20.219:1985/api/v1/versions` 有 JSON 返回
- [ ] ffmpeg 模拟推流（用项目自带 ffmpeg），验证 RTMP 通路：

```powershell
$ff = "D:\project\eyes\voiceInteractive\ffmpeg.exe"   # 已备妥（2026-05 构建），app\resources 下另有一份
& $ff -re -f lavfi -i testsrc=size=1280x720:rate=15 -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -t 8 -f flv "rtmp://10.0.20.219:1935/live/usb_test"
```

另开窗口拉流确认：

```powershell
& $ff -fflags nobuffer -i "http://10.0.20.219:8080/live/usb_test.flv" -f null -
```

看到持续 frame= 计数即通过。阶段一完成前不要进入阶段二。

**验收记录（2026-10-10 已通过）**：SRS 6.0.191 容器 healthy；端口 1935/1985/8080 TCP、8000 UDP 映射正常；
本机跨网段访问 `http://10.0.20.219:1985/api/v1/versions` 正常；ffmpeg 推 `usb_test` 后
`/api/v1/streams/` 可见 publish active、H264 1280x720（注意该接口流列表在返回体顶层 `streams` 字段，不在 `data` 下）；
HTTP-FLV 跨网段回拉成功。

---

## 3. 阶段二：前端推流 + SRS 同源播放

### 3.1 新增 `web/voice-srs.js`（在 index.html 中于 index-cam.js 之前加载）

[index.html](file:///d:/project/eyes/voiceInteractive/web/index.html#L41-L46) 第 44 行前插入：

```html
<script src="voice-srs.js"></script>
```

新文件内容要点（常量 + 两个函数）：

```javascript
// SRS 接入：USB 流统一推到 10.0.20.219，播放也走 SRS（同源，保证标注不错位）
const SRS_HOST = '10.0.20.219';
function usbStreamName(slot){ return 'usb' + (camSlots.indexOf(slot) + 1); }

// 等 ICE 收集完成（复用 index-cam.js 的 waitForIceGathering）
// 推流：与现有 startWebRtcPlayback 对称的 /rtc/v1/publish/ 信令
async function startSrsPublish(slot){
  if(slot.pubPc){ try{slot.pubPc.close();}catch(_){} slot.pubPc=null; }
  const conn = new RTCPeerConnection();
  slot.pubPc = conn;
  slot.stream.getVideoTracks().forEach(t => conn.addTrack(t, slot.stream)); // 只推视频，不推麦克风（防回环）
  const offer = await conn.createOffer();
  await conn.setLocalDescription(offer);
  await waitForIceGathering(conn, 2000);
  const name = usbStreamName(slot);
  const apiUrl = `http://${SRS_HOST}:1985/rtc/v1/publish/`;
  const resp = await fetch(apiUrl, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ api:apiUrl, streamurl:`webrtc://${SRS_HOST}/live/${name}`, sdp:conn.localDescription.sdp })
  });
  const data = await resp.json();
  if(data.code !== 0) throw new Error(data.message || `SRS publish code=${data.code}`);
  await conn.setRemoteDescription({type:'answer', sdp:data.sdp});
  slot.srsName = name;
}
```

播放直接**复用现有** [startWebRtcPlayback(slot, srsHost, streamName)](file:///d:/project/eyes/voiceInteractive/web/index-cam.js#L184-L214)，无需新代码。

### 3.2 摄像头窗口与巡检的切换时序（[index-cam.js](file:///d:/project/eyes/voiceInteractive/web/index-cam.js) / index-inspect.js）

**原则：平时看本地流（零延迟、零上行流量）；只有点"巡检"后该路才切 SRS。**

- [ ] `startCamStream` **保持现状不变**：打开窗口 → getUserMedia 本地流直接挂 video
- [ ] `_startOne`（开始巡检，[index-inspect.js](file:///d:/project/eyes/voiceInteractive/web/index-inspect.js#L267-L279)）中按序执行：
  1. `await startSrsPublish(slot)` —— WHIP 推 `usbN`
  2. 发布成功后 `POST :8771/analyze/start {"stream":"usbN"}`
  3. 先切 `startWebRtcPlayback(slot, SRS_HOST, 'usbN')` 保证有画面，8771 出流后自动（或手动）切 `usbN_labeled`
- [ ] `_stopOne`（停止巡检）中按序逆操作：
  1. `POST :8771/analyze/stop`
  2. 关闭 `slot.pubPc` 停止推流
  3. video 切回本地 `slot.stream`（恢复零延迟预览）
- [ ] `closeCamWindow` 清理中兜底：若仍在巡检先停推流/分析，再关闭所有 RTCPeerConnection
- [ ] 巡检中提供"原始 / 标注"视角切换按钮（换 streamName 调 startWebRtcPlayback）；非巡检态隐藏
- [ ] `captureCamFrame` 不变：标注视角下截到的证据图天然带框、骨骼和标签

好处：SRS 或 8771 宕机只影响巡检功能，打开摄像头、语音对话不受影响
（口罩巡检已并入 8771，见 §4.8 与 §9-2）。

注意：页面通过 `http://localhost:8765` 打开属 secure context，getUserMedia 可用。

**实测坑：Chrome PNA（Private Network Access）拦截（2026-10-10）**
页面源 localhost 属 local 级、SRS 10.0.20.x 属 private 级，即使 SRS 开了 crossdomain（CORS `*`），
Chrome 仍要求预检响应带 `Access-Control-Allow-Private-Network: true`，SRS 6.0.191 不发该头，
浏览器直接 `Failed to fetch`（PowerShell/curl 不执行 PNA，故命令行测一切正常）。
**解法（已实现）**：[dashboard.py](file:///d:/project/eyes/voiceInteractive/assistant/dashboard.py) 内置
`/srs/* → http://10.0.20.219:1985/*` 同源反向代理（stdlib urllib，GET/POST/OPTIONS，512KB 上限），
前端推流/播放信令一律 POST `http://localhost:8765/srs/rtc/v1/publish|play/`，body 中 `api` 仍填 SRS 真实地址
（浏览器不会请求该字段）；WebRTC 媒体面 UDP 8000 直连不受影响。改动 dashboard.py 后需重启语音助手。

- [x] 阶段二验收（2026-10-10 通过）：点巡检后 `publish.active=true`，H264 Baseline 1280x720，
  clients=2（RTMP 桥接 + 浏览器回拉）；ffmpeg 拉 `http://10.0.20.219:8080/live/usb1.flv` exit=0
  （WebRTC→RTMP 桥接正常，即后续 8771 拉流入口）；前端日志"推流成功，巡检画面已切到 usb1"；
  视角切换/停止恢复本地画面正常。（当时的 qwen 60s 口罩巡检现已由 8771 口罩模块取代，见 §4.8）

> 第二个实测坑：回拉 offer 不能加 audio transceiver。usbN 只推视频，offer 含 video+audio 两个 m-line 时
> SRS answer 只回一个，浏览器报 `order of m-lines in answer doesn't match offer`。播放端只 addTransceiver('video')。

---

## 4. 阶段三：飞桨 8771 服务（独立环境，核心）

### 4.0 实测记录（2026-10-10 探针通过，以下以本节为准）

环境与安装：

- `uv venv` 默认不带 pip，必须加 `--seed`；已建 Python 3.10.20 + `.venv-paddle`。
- **Windows GPU wheel 不自带任何 CUDA/cuDNN DLL**（paddle/libs 默认只有 mkldnn 等），
  且**必须用 cuDNN 8.6，不能用 8.9**（8.9 下 fused_conv2d_add_act 内核报
  `CUDNN_STATUS_NOT_SUPPORTED`，paddle 2.6 官方匹配 8.6.0.163）。
  已在本机跑通的最小部署（全部 DLL 放进 `.venv-paddle/Lib/site-packages/paddle/libs/`，
  不装系统级 CUDA 工具包、不动 PATH）：
  1. CUDA 11.8 运行时组件（NVIDIA redist 组件包，只取 bin/*.dll）：
     cuda_cudart 11.8.89、libcublas 11.11.3.6（cublas64_11+cublasLt）、
     libcurand 10.3.0.86、libcusolver 11.4.1.48、libcusparse 11.7.5.86、cuda_nvrtc 11.8.89；
  2. cuDNN **8.6.0.163** cuda11 archive 的 7 个 bin DLL；
  3. **zlibwapi.dll（64 位，zlib123dllx64.zip）**——缺它首次推理原生崩溃 0xC0000409；
  4. `pip install paddlepaddle-gpu==2.6.2`（cu118 源）。run_check 输出
     "works well on 1 GPU"（RTX 2060 算力 7.5，Driver API 13.3 / Runtime 11.8）。
  安装包缓存于 `third_party/cudnn_dl/`（cuDNN 8.9 的 zip 已确认废弃，勿再使用）。
- **GPU 实测帧率：720p 全模块 ~17fps**（CPU 0.9fps，约 19 倍），speed=1.0x 标注流实时；
  空载（testsrc 无人）数据，真实多人场景因 KPT/ATTR 按人增长预计 10~15fps。
  飞桨进程显存占用约 1~1.6GB，6GB 卡上与 ASR(~1.2GB)+Ollama(~2.5GB) 可共存。
- pipeline 精简依赖见 [requirements-paddle.txt](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/requirements-paddle.txt)；
  官方 requirements 之外实际还需要 **imgaug、numba、requests**；numpy 必须 <2。
- 模型 5 个共 395MB，bcebos **单连接仅 0.05MB/s、代理 10.66.66.4:6384 无效**；
  服务端支持 Range，8 线程分块聚合 ~1-13MB/s，MOT 182MB 用时 69s。
  下载器：[download_models.py](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/download_models.py)
  （带断点分片、MD5 校验、解压、ppTSM_fight→ppTSM 重命名）。

2.8 真实 API（与早期设想不同，**没有 `Pipeline.process()`**）：

- 核心类是 `pipeline.pipeline.PipePredictor(args, cfg, is_video=True/False)`；
  args 需对齐 cfg_utils.argsparser() 全部字段（探针用 SimpleNamespace 构造）。
- 导入需要 sys.path **同时**含 `deploy`（包导入 `pipeline.ppvehicle`）和 `deploy/pipeline`
  （裸导入 `datacollector`），且 deploy 在前；运行时 cwd 需为 PaddleDetection 根（tracker_config 相对路径）。
- 视频模式 `predictor.run("http://.../usb1.flv")` 原生支持 URL 输入与 pushurl 推流；
  官方 `PushStream.initcmd` 写死 `-f rtsp` 且 ffmpeg 需在 PATH，已 monkey-patch 为
  RTMP/FLV + 本地 ffmpeg 绝对路径（见 [probe_stream.py](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/probe_stream.py)）。
- **低帧率推流必须修时间戳**：分析帧率(CPU~1fps) ≠ 源帧率(30)，输入侧不能声明 `-r`
  （会把墙钟 PTS 压回去，SRS 时间戳不前进、播放卡死），用
  `-use_wallclock_as_timestamps 1 -i -` + `-fps_mode passthrough`，speed 恢复 1.0x；GOP 设 4 帧。
- ~~OpenCV `VideoCapture` 读 HTTP-FLV 在本机 0 帧，runner 拉流用 **RTMP**~~ →
  **已更正（2026-10-11）**：早期 0 帧是探针写法问题；现在 `paddle_worker.py` 与
  `probe_stream.py` 统一用 **HTTP-FLV 拉流**（`http://10.0.20.219:8080/live/usbN.flv`），
  推流侧仍是 ffmpeg → RTMP。注意 `cv2.VideoCapture` 打开网络流**必须在主线程**（见 §4.5）。

探针结果：

- 图片探针（[probe_image.py](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/probe_image.py)）：
  PedestrianDetection_001.png 检出 7 人（0.79~0.94），`det boxes=[class,score,x1,y1,x2,y2]`，
  `attr.output` 为每人 10 个字符串。
- **ATTR 26 个标签无口罩（Mask）字段**（Hat/Glasses/衣着/包/年龄/性别/朝向…），
  口罩因此做成独立模块，已用 PaddleHub pyramidbox mask 落地，见 §4.8。
  （当时写的"qwen 60s 巡检兜底"已废弃——qwen 链路后来整体删除。）
- 流式探针：usb1 → MOT+KPT+ATTR+STGCN+ppTSM → `rtmp://10.0.20.219:1935/live/usb1_labeled`
  全程跑通；实测帧含检测框、track id（pedestrian_N）、绿色 17 点骨骼、属性行、左上角计数；
  **CPU 约 0.9~1.5fps**（720p），实时推流正常；GPU 是多路/可用性的必要条件。
- 配置：[pphuman_eyes.yml](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/pphuman_eyes.yml)
  （MOT/ATTR/VIDEO_ACTION/SKELETON_ACTION 全开；KPT 随 SKELETON_ACTION 自动初始化）。

### 4.1 硬性隔离约束（全部安装在本机 Windows，219 不装）

PaddlePaddle 与现有 torch（8770 ASR）在同一台本机 Windows 上存在 DLL 冲突，**必须独立虚拟环境**，不只是独立进程：

```
voiceInteractive/
  .venv/           # 现有：torch + Qwen3-ASR（8770），不动
  .venv-paddle/    # 新建：paddlepaddle-gpu + PaddleDetection（8771）
  paddle_service/  # 新代码包
  third_party/PaddleDetection/   # 官方套件源码（release/2.8）
```

### 4.2 环境安装（一次性，Windows PowerShell）

本机现状（2026-10-10 盘点）：RTX 2060 6GB、驱动 610.47；Python 3.8 / 3.12(uv) / 3.14（无 3.10/3.11）；
已装 uv、git；ffmpeg 位于 `D:\project\eyes\voiceInteractive\ffmpeg.exe`。**无需单独安装 CUDA/cuDNN**
（paddle 官方 GPU wheel 自带运行时，驱动版本足够）。

- [ ] 用 uv 装 Python 3.10（paddle 2.6 兼容 3.8~3.11；勿用 3.12+）并建独立 venv：

```powershell
cd D:\project\eyes\voiceInteractive
uv python install 3.10
uv venv --python 3.10 .venv-paddle
.\.venv-paddle\Scripts\python.exe -m pip install -U pip
# GPU 版 cu118（RTX 2060 图灵架构兼容；安装后必须跑 run_check 验证）
.\.venv-paddle\Scripts\python.exe -m pip install paddlepaddle-gpu==2.6.2 -i https://www.paddlepaddle.org.cn/packages/stable/cu118/
# GPU wheel 不可用时首轮先用 CPU：pip install paddlepaddle==2.6.2
.\.venv-paddle\Scripts\python.exe -c "import paddle; paddle.utils.run_check()"   # 看到 works well on GPU 即通过
git clone -b release/2.8 --depth 1 https://github.com/PaddlePaddle/PaddleDetection.git third_party/PaddleDetection
.\.venv-paddle\Scripts\python.exe -m pip install -r third_party/PaddleDetection/deploy/pipeline/requirements.txt
.\.venv-paddle\Scripts\python.exe -m pip install "numpy<2"
# 不装 imageio-ffmpeg：runner 直接使用 voiceInteractive\ffmpeg.exe
```

> 模型首次运行自动从官方配置内 URL 下载到 `~/.paddledet`（下载发生在本机）；本机若无法访问公网，
> 按 PaddleDetection `deploy/pipeline/docs/` 文档表手动下载 MOT / ATTR / KPT / STGCN / 视频分类五个模型，
> 解压后把下节配置中的 `model_dir` 改为本地目录。

### 4.3 配置文件 `assistant/paddle_service/pphuman_eyes.yml`

- [x] 从官方示例 `deploy/pipeline/config/examples/infer_cfg_pphuman.yml` 复制后**只改 enable 开关**
  （不手写模型 URL，避免版本不一致）。文件名落地为 `pphuman_eyes.yml`（非 `conf/pphuman.yml`），
  由 `paddle_worker.py` 在 `run()` 内按绝对路径读取。
- [x] 模型 `model_dir` 指向 `third_party/…` 本地目录（内网不便自动下载，见
  [download_models.py](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/download_models.py)：分片断点 + MD5 校验）。

修改以下开关：

```yaml
visual: True        # 关键：visualize_video 输出的帧已画好 框/骨骼/属性/行为，且已 RGB->BGR
MOT:        { enable: True }    # 跟踪框 + track id（其他模型都挂在它的 id 上）
KPT:        { enable: True }    # 17 点骨骼（STGCN 依赖，必须开）
ATTR:       { enable: True }    # 属性行（实测 26 标签无口罩，口罩见 4.8 独立模块）
SKELETON_ACTION: { enable: True, display_frames: 80 }   # 摔倒 fall
VIDEO_ACTION:    { enable: True }                        # 打架 fight
REID:       { enable: False }   # 跨镜 ReID 暂不需要，省显存
```

### 4.4 API 探针（已完成，**早期设想的 `Pipeline.process()` 不存在**）

- [x] `assistant/paddle_service/probe_image.py`（静态图）与 `probe_stream.py`（流式）已跑通。
- [x] `probe_event_logic.py`：不加载模型，验证去抖/冷却/入侵/叠加等事件逻辑（见 §4.9）。

真实 API（以 §4.0 实测为准）：

```python
# 核心类是 PipePredictor，不是 Pipeline.process()
predictor = pipeline.pipeline.PipePredictor(args, cfg, is_video=True)   # args 需对齐 cfg_utils.argsparser() 全部字段
predictor.run("http://10.0.20.219:8080/live/usb1.flv")                  # 视频模式原生支持 URL + pushurl 推流
```

- 导入需要 `sys.path` **同时**含 `deploy`（包导入 `pipeline.ppvehicle`）与 `deploy/pipeline`
  （裸导入 `datacollector`），且 `deploy` 在前；运行时 cwd 必须是 PaddleDetection 根
  （`tracker_config` 用相对路径）。
- 结果从 `Result.update(res, name)` 逐帧拿到，`name ∈ {det, mot, kpt, attr, video_action,
  skeleton_action, …}`，真实字段名已用于 §4.6 事件映射。

### 4.5 拉流 → 逐帧推理 → ffmpeg 管道推回标注流（**已实现，与早期伪代码不同**）

实际实现**不自建拉流循环**，而是复用 PP-Human 官方的 `PipePredictor`，只 monkey-patch 三处
（见 [paddle_worker.py](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/paddle_worker.py)）：

| 被 patch 的点 | 原因 / 做法 |
|---|---|
| `PushStream.initcmd` | 官方写死 `-f rtsp` 且要求 ffmpeg 在 PATH。改为本地 ffmpeg 绝对路径 + **libx264 → FLV/RTMP**；输入侧**不能声明 `-r`**，用 `-use_wallclock_as_timestamps 1 -i -` + `-fps_mode passthrough`（低帧率下声明 `-r` 会把墙钟 PTS 压回去、SRS 时间戳不前进）；GOP=4 |
| `Result.update` | 拦截逐帧结果字典，抽 `mot` / `skeleton_action` / `video_action` 事件（见 §4.6） |
| `PipePredictor.capturevideo` | 官方实现无法中断。加 `_eyes_stop` 轮询以支持运行中停止，并逐帧回调 `analyzer.on_frame()` 做口罩检测（`cv2.VideoCapture` 必须主线程，故推理与检测线程分工明确） |

**标注流叠加**：管线把画好人体框/骨骼/属性的帧（**BGR**）写进 ffmpeg stdin，worker 把 stdin
包一层 `_FrameSink`，在写入前叠加禁区多边形、口罩人脸框、入侵目标高亮（`overlay.py`）。
叠加失败一律退回原始字节，绝不因画框中断推流。

**为什么分析在子进程**：`cv2.VideoCapture` 打开网络流在非主线程会永久挂起，而 PP-Human 管线是
主线程设计 → worker 必须是独立进程主线程；顺带获得崩溃隔离与 DLL 隔离。

```
8771 主进程 (paddle_server → paddle_runner)
   └─ .venv-paddle\python.exe -u -m assistant.paddle_service.paddle_worker --stream usbN
        stdin : "stop\n"（优雅停）           stdout : "EVENT {json}\n" + 日志透传
        predictor 常驻复用；FLV 断线后仅重开 VideoCapture（1~3s），不重载模型（~25s）
```

推流失败不影响告警：SSE 事件与标注流相互独立。

### 4.6 事件提取与 SSE（`paddle_server.py`，ThreadingHTTPServer，端口 8771）

| 方法/路径 | 作用 |
|---|---|
| `GET /ready` | 8771 进程存活（worker 模型加载状态由 SSE 的 `status` 事件表达） |
| `GET /status` | `{"streams":[{"stream","running","error","modules"}]}` |
| `POST /analyze/start` | body `{"stream":"usb1","modules":["fall","fight","intrusion","mask"]}`，幂等 |
| `POST /analyze/stop` | body `{"stream":"usb1"}`，立即返回，后台 join 回收 |
| `POST /shutdown` | 需请求头 `X-Local-Shutdown: 1`（防页面 CSRF）；停全部 worker 后关服务，供 main.py 退出调用 |
| `GET /events?stream=usbN` | **SSE**：`text/event-stream`，15s 心跳 `: heartbeat` |

事件 JSON **没有 `{"type":"alert"}` 外层包装**，直接用 `type` 区分；字段为 `stream/type/timestamp`
（不是 `ts`）：

```json
{"type":"fall","stream":"usb1","track_id":3,"score":0.92,"timestamp":1760000000.0}
{"type":"fight","stream":"usb1","score":0.88,"timestamp":1760000000.0}
{"type":"intrusion","stream":"usb1","region":"柜台禁区","track_id":3,"point":[800,400],"timestamp":1760000000.0}
{"type":"mask","stream":"usb1","faces":2,"no_mask":1,"boxes":[{"label":"no_mask","confidence":0.93,"box":[..]}],"timestamp":1760000000.0}
{"type":"status","running":true,"stream":"usb1","modules":["fall","fight","intrusion","mask"],"timestamp":1760000000.0}
```

事件来源与去重（**全部在 worker 内按 track / 区域维度去重**，前端再做一层播报节流）：

| 事件 | 来源 | 冷却 | 说明 |
|---|---|---|---|
| `fall` | `Result.update('skeleton_action')`，`class==0` | 同一 track_id **30s** | |
| `fight` | `Result.update('video_action')`，`class==1` | **10s** | |
| `intrusion` | `Result.update('mot')`，bbox 中心点落入禁区多边形 | 每「区域+track_id」默认 **30s** | 见 §4.9 |
| `mask` | 独立口罩模块（§4.8），每 3s 采样 | 连续 **3** 个采样周期检出未戴才发；告警后 **30s** 冷却 | 恢复（检出人脸且无人未戴）时补发一条 `no_mask:0` 解除告警 |
| `status` | worker 启动就绪/退出时 | — | **带 `modules` 的那条才是模型就绪**；8771 建连时发的合成 status 不带 `modules` |

> 前端就绪判定必须用 `Array.isArray(ev.modules)` 过滤：见
> [voice-srs.js](file:///d:/project/eyes/voiceInteractive/web/voice-srs.js) `openPaddleEvents`。
> 若用 `running===true` 直接 resolve，`PADDLE_READY_TIMEOUT` 与"模型加载中崩溃 reject"双双失效。

- **`no_mask` 现在由 8771 直出**（PaddleHub 模型，见 §4.8）；旧的 qwen 60s 轮询口罩链路
  已从前后端删除，8770 的 `/vision` 端点仅保留给语音"看看画面里有什么"的看图对话。
- **8771 挂掉时口罩检测没有兜底**（qwen 链路已移除）：巡检仍可看原始流，但没有口罩告警。
  如需恢复兜底，见 §9 待办。

### 4.7 启动与独立运行

模块入口是 `assistant.paddle_service.paddle_server`（不是早期设想的 `paddle_service.main`）：

```powershell
.\.venv-paddle\Scripts\python.exe -u -m assistant.paddle_service.paddle_server   # 手动调试
```

正式由 [main.py](file:///d:/project/eyes/voiceInteractive/assistant/main.py) 随语音助手一起拉起
（**各自独立解释器，DLL 彻底隔离**；`.venv` 不 import torch 以外的飞桨依赖）：

- 8765 主进程启动时 Popen 8770（`.venv`）与 8771（`.venv-paddle`）两个子进程；
  `_PADDLE_PYTHON.exists()` + 端口占用双保险，避免重复拉起。
- **退出必须走 HTTP**：`_stop_paddle_service()` 先 `POST /shutdown`（自定义头 `X-Local-Shutdown: 1`）
  并等最多 40s，失败才回退 `terminate()`。直接 terminate 在 Windows 是硬杀，`finally` 不执行，
  worker 与 worker 里的 ffmpeg 会变孤儿继续占显存、继续推 RTMP。
- 本机有 `HTTP_PROXY` 时，`urllib` 访问 127.0.0.1 会被转发（曾出现 502）：本机请求一律用
  `ProxyHandler({})` 显式绕过，不依赖 `no_proxy`。

- [x] 阶段三验收：前端推 `usb1` → `POST /analyze/start {"stream":"usb1"}` →
  ffmpeg 拉 `rtmp://10.0.20.219:1935/live/usb1_labeled` 有带框/骨骼/属性画面；`GET /events` 收到事件流。

### 4.8 口罩模块（PaddleHub 模型，已完成）

背景：阶段三已实测 PP-Human ATTR 的 26 个属性标签**不含口罩**，因此口罩是 8771 内的独立模块，
与 PP-Human 并行，共用 `.venv-paddle` 与同一张 GPU，不引入第二个推理引擎。

**选型**：PaddleHub `pyramidbox_lite_server_mask`（PyramidBox 人脸检测 + 口罩二分类，
输出每张人脸 `[label, confidence, x1, y1, x2, y2]`，label 1=佩戴 / 0=未佩戴）。
模型几 MB、单帧毫秒级；优先 server 版，效果不足再换 mobile 版对比。

**实现（已完成，与早期设想有 3 处偏差，以本节为准）**：

1. **没有安装 `paddlehub` 包**。`paddlehub` 与 paddle 2.6.2 / numpy<2 的依赖容易打架，改为
   直接用 `paddle.inference` 以**目录方式加载旧版 fluid 散参数模型**
   （`third_party/paddlehub_modules/pyramidbox_lite_server{,_mask}/`，含 `__model__`），
   前后处理严格复刻官方 `data_feed.py`/`processor.py`。实现见
   [mask_detector.py](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/mask_detector.py)。
   管线为：pyramidbox_lite_server 人脸检测（shrink=0.5、阈值 0.6）→ 每张脸 128×128 仿射裁剪
   → 口罩二分类（0=NO MASK / 1=MASK）。
2. **采样节奏是每 3s 一次**（捕获线程节流 `MASK_INTERVAL=3.0`），不是"每 5 帧"；
   指标是时间而非帧数，低帧率下更可控。
3. **人脸框叠加进标注流**：`overlay.py` 在帧写入 ffmpeg 前绘制
   **绿框+"口罩 0.93" / 红框+"未戴口罩 0.93"**，与人体框、骨骼同帧进入 `usbN_labeled`；
   中文标签用 PIL + 微软雅黑渲染（`cv2.putText` 画中文会变成 `??`），整帧只做一次 PIL 往返。

去抖与冷却（`paddle_worker.py`）：

- 同一路**连续 3 个采样周期**（约 9s）都检出未戴口罩才发事件（防转脸误报）；
- 告警后 **30s 冷却**；
- 检出人脸且无人未戴时**补发一条 `no_mask:0`** 让前端解除告警，并重置去抖计数；
- **未检出人脸 = 无证据**：不复位去抖计数、也不发"全员佩戴"，避免漏报（侧脸 / 远景小脸）。

**事件协议**（与 fall/fight 同通道，前端无需新订阅；字段见 §4.6）：

```json
{"type":"mask","stream":"usb1","faces":2,"no_mask":1,
 "boxes":[{"label":"no_mask","confidence":0.93,"box":[100,100,200,220]}],
 "timestamp":1760000000.0}
```

**前端（已完成）**：

- 巡检态口罩来源为 SSE `mask` 事件；告警弹窗 / 语音播报 / 历史面板 / 证据存盘全部复用
  `showAlertWin / inspectSpeak / inspectHistory / vision_save`；
- 证据图截 `usbN_labeled` 标注流（红框直接在图上）；
- 配置开关 `mask_engine`（config.json / 8765 `/app-config`）**只剩 `paddlehub | off`**：
  qwen 链路已删除，8771 未就绪时不再有任何口罩检测（见 §9）；
- 前端对 `mask` 事件有 20s 播报/存盘节流，告警窗保留 30s（`ALERT_TTL.mask`）与后端冷却对齐；
- **`/vision` 接口保留**：语音"看看画面里有什么"的看图对话继续可用，仅卸下定时口罩巡检职责。

---

### 4.9 区域入侵模块（`intrusion`，已完成）

**不依赖 PP-Human 自带区域功能**（套件的 `do_break_in_counting`/`region_polygon` 只做人数进出统计），
在 worker 内自行判定：拦截 `Result.update('mot')`，取 MOT 检测框，用**框中心点**做多边形包含测试，
落入即报。

配置：`assistant/paddle_service/conf/regions.json`（首次运行需按现场画禁区）

```json
{
  "regions": {
    "usb1": [
      {"name": "柜台禁区",
       "polygon": [[420,180],[1180,180],[1180,700],[420,700]],
       "cooldown": 30, "enabled": true}
    ],
    "*": []
  }
}
```

| 字段 | 说明 |
|---|---|
| 键名 | 流名 `usb1`/`usb2`…；未列出的流回退到 `"*"`（单独列出的流**只**用自己的区域，不叠加 `"*"`） |
| `polygon` | 顶点列表，**原始帧像素**（1280×720 按该分辨率填），至少 3 点 |
| `cooldown` | 同一「区域 + track_id」重复告警冷却秒数，缺省 30 |
| `enabled` | `false` 可临时停用而不删坐标 |

实现要点（[regions.py](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/regions.py)）：

- `Region.contains()` 用射线法，水平边与除零已处理；
- 去重键是 `(区域名, track_id)`，所以**同一个人留在禁区内只报一次**（冷却期后按 30s 周期复报）；
- 目标高亮框（橙色 3px + "禁区:区域名"）保留 2s，与禁区多边形（半透明橙色填充 + 名称标签）
  一起叠加到标注流（[overlay.py](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/overlay.py)）；
- 该路没配禁区时 worker 自动把 `intrusion` 从生效模块中剔除，并在 `status` 事件里如实上报，
  前端日志会打印"未配置禁区，区域入侵未启用"；
- 模块开关：`POST /analyze/start` 的 `modules` 里是否需要 `intrusion`（前端默认带上）。

前端接入（[index-inspect.js](file:///d:/project/eyes/voiceInteractive/web/index-inspect.js)）：
`ALERT_META.intrusion` + 告警窗显示「禁区」与目标 ID + 语音"检测到人员进入禁区XXX" +
历史/存盘；同类事件 10s 内只播报一次，防多目标并发时 TTS 排队。

**验收探针**（不加载模型，秒级）：

```powershell
.\.venv-paddle\Scripts\python.exe assistant\paddle_service\probe_event_logic.py
```

覆盖：口罩去抖第 3 周期才告警并会解除、口罩 30s 冷却、区域入侵命中/冷却/区域外不误报、
`render_frame` 叠加与长度一致、`_FrameSink` 字节透传。

---

## 5. 阶段四：前端对接标注流与告警链路

- [x] 推流 / analyze-start-stop / 视角切换在阶段二 3.2 接入 `_startOne`、`_stopOne`
- [x] 标注流自动切换：`beginSrsInspect` 先回拉 `usbN` 原始流（不黑屏等模型加载 ~25s）→
  SSE 收到**带 `modules` 的 status**（worker 模型就绪）→ 轮询切 `usbN_labeled`（3s×20 次，
  失败保持原始流）；`_withTimeout` 90s 兜底
- [x] SSE 订阅（EventSource `http://localhost:8771/events?stream=usbN`），事件直接复用现有三件套：
  - [showAlertWin](file:///d:/project/eyes/voiceInteractive/web/index-inspect.js) 扩展 `ALERT_META`
    支持口罩 / 摔倒 / 打架 / **区域入侵**（证据图用 `captureCamFrame` 截当前标注流）
  - [inspectSpeak](file:///d:/project/eyes/voiceInteractive/web/index-inspect.js) 语音播报
    （如"1号摄像头巡检告警：检测到人员摔倒。"）
  - [inspectHistory](file:///d:/project/eyes/voiceInteractive/web/index-inspect.js) 历史面板，
    新增 `kind`/`region` 字段；存盘复用 8770 `/vision_save`
  - 节流：口罩 20s、摔倒/打架/入侵同类 10s 内只播报一次
- [x] 8771 不可用时降级：视角保持原始流、SSE 无事件；**口罩检测随之完全不可用**（qwen 链路已删，见 §9）
- [x] **口罩模块（4.8）**：改用 `paddle.inference` 直载 PaddleHub 模型目录，**未安装 paddlehub 包**
- [x] runner 接入口罩推理（每 3s）+ 标注流人脸红绿框 + 连续 3 周期去抖 / 30s 冷却
- [x] `mask` SSE 事件接入告警三件套；`mask_engine: paddlehub | off`（**qwen 取值已移除**）
- [x] 巡检态已停用 60s 定时 `/vision` 口罩巡检（`/vision` 语音看图保留）；旧链路已删除
- [x] **区域入侵（4.9）**：`conf/regions.json` + MOT 中心点判定 + SSE `intrusion` + 标注流禁区/目标高亮

---

## 6. 端口 / 进程 / 显存一览

| 进程 | 端口 | 环境 | 显存占用方 |
|---|---|---|---|
| asr_server（现有） | 8770 | `.venv`（torch） | Qwen3-ASR fp16 |
| Ollama（现有） | 11434 | 独立安装 | qwen3.5:4b |
| paddle_service | **8771** | **`.venv-paddle`（paddle）** | PP-YOLOE + HRNet-tiny + 属性 + STGCN + ppTSM + 口罩双模型；**单路实测 1~1.6GB**（§4.0） |
| SRS（219 服务器） | 1935/1985/8080/8000udp | Docker | 无 |

单路 720p GPU 预期 15~25fps；多路时 KPT 是最大开销，降级策略：远距离/低置信目标跳过关键点。

**本机显存预算（RTX 2060 仅 6GB，偏紧）**：Qwen3-ASR-0.6B fp16 ≈1.2GB；Ollama qwen3.5:4b ≈2.5GB
（空闲可部分让出）；飞桨轻量模型组（PP-YOLOE + TinyPose + 属性 + STGCN）预估 0.5~1.5GB。
峰值可能接近 6GB，阶段三启动后必须用 `nvidia-smi` 实测三家共存峰值并记录：
- 超预算时优先级：① 飞桨全程只选 tiny 模型、输入宽降到 640；② Ollama 设置 keep_alive 缩短或巡检时卸模型；
  ③ 兜底让飞桨跑 CPU（3~8fps 仅影响实时标注流畅度，事件检测仍可用）。**不可把飞桨挪到 219（无显卡）。**

> **待办（§9-1）**：以上是**单路**实测值。多路巡检时每路是**独立 worker 进程、各自加载一整套
> predictor**，显存线性增长——RTX 2060 6GB 在 ASR 1.2GB + Ollama 2.5GB 之后，两路同时巡检
> 大概率爆显存。上线双路前必须实测：
>
> ```powershell
> # 开 1 路 / 2 路巡检后各记一次
> nvidia-smi --query-gpu=memory.used,memory.total --format=csv
> ```

---

## 7. 风险与回退

| 风险 | 应对 |
|---|---|
| Windows 无对应 CUDA 的 paddle GPU wheel | 先在本机 CPU 跑通链路（3~8fps 足够功能验证）；正式使用前必须装匹配本机 CUDA 的 GPU wheel（219 无显卡，不能挪到 219） |
| 模型自动下载失败（内网） | 按 4.2 手动下载并改 `model_dir` 本地路径 |
| PP-Human 版本间 `process()` 返回值/字段名差异 | 4.4 探针先行，以探针打印为准写事件映射 |
| ATTR 无口罩字段（已实测确认） | 已用 PaddleHub `pyramidbox_lite_server_mask` 独立模块落地（4.8，`paddle.inference` 直载，未装 paddlehub 包）。**当前无兜底**：8771 不可用时口罩检测完全失效，`mask_engine` 只有 `paddlehub \| off`（见 §9-2） |
| 本机关机 / 8771 未运行 | 219 SRS 仍可中转与（可选）DVR 录像，但无分析与告警；设计上 SRS 与分析解耦 |
| 标注流卡顿 | 降推理帧率（抽帧，如每 2 帧推 1 帧）、关 VIDEO_ACTION、缩到 640 宽 |
| 全链路异常 | 前端切回"原始视角"；8771 停止即恢复为纯 SRS 中转，现有口罩巡检完全不受影响 |

## 8. 执行顺序总览（2026-10-11 进度）

| # | 阶段 | 状态 |
|---|---|---|
| 1 | 阶段一 SRS 部署 + testsrc 裸流验收 | ✅ 完成 |
| 2 | 阶段二 `voice-srs.js` + 推流/同源播放（PNA 反向代理） | ✅ 完成 |
| 3 | 阶段三 `.venv-paddle` + PP-Human 探针 → worker + 8771 → 验 `usb1_labeled` | ✅ 完成 |
| 4 | 阶段四 SSE 告警三件套 + 标注流自动切换 + 降级 + 口罩模块（4.8） | ✅ 完成 |
| 5 | 阶段四补：**区域入侵 `intrusion`（4.9）** + 口罩框入标注流 + 3 周期去抖 | ✅ 完成（2026-10-11） |
| 6 | 多路（usb2…）显存/帧率实测与优化、降级策略落地 | ⏳ 待办（§9） |

---

## 9. 待办清单（代码未做，按优先级）

| # | 事项 | 依据 | 说明 |
|---|---|---|---|
| 1 | **多路巡检显存实测** | §6 | 每路一个 worker + 一整套 predictor，显存线性增长；RTX 2060 6GB 两路大概率爆。上线双路前用 `nvidia-smi` 记录 1 路/2 路峰值，必要时启用 §6 的降级优先级 |
| 2 | **8771 挂掉后的口罩兜底** | §4.6/§4.8 | qwen 60s 轮询链路已删除，现在 8771 不可用 = 无口罩检测。产品上是否接受需拍板；不接受则恢复一条降级链路（或把"口罩不可用"在巡检窗口明确提示） |
| 3 | **降级策略落地** | §6/§7 | 均为文档设计、代码未实现：远距离/低置信目标跳过 KPT、抽帧推流（`MOT.skip_frame_num` 现为 -1）、关 VIDEO_ACTION、输入宽降 640 |
| 4 | **口罩真人探针证据** | §4.8 步骤 2 | `probe_frames/` 目前只有 `lab.jpg`、`gpu_lab.jpg`（静态图）与 `overlay_probe.jpg`（叠加渲染自测）。缺真人戴/未戴/侧脸各若干张的准确率记录 |
| 5 | **区域入侵坐标标定** | §4.9 | `conf/regions.json` 的 `usb1` 是 1280×720 示例矩形，需按现场机位实画；多路需补 `usb2`… 或 `"*"` |
| 6 | DVR 录像 | §1/§6 | `srs.conf` 无 `dvr` 段，可选未开 |
