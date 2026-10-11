# PP-Human × SRS 实时巡检实施方案（方案一：服务端渲染标注流）

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
| 219 ↔ 本机 8771（拉原始流 / 推标注流） | RTMP（OpenCV 拉、ffmpeg 推） | 各 200~500ms |
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
| PP-Human 拉流 | `rtmp://10.0.20.219:1935/live/usbN` |
| 8771 推回标注流 | `rtmp://10.0.20.219:1935/live/usbN_labeled` |
| ffmpeg 备用拉流 | `http://10.0.20.219:8080/live/usbN.flv` |

---

## 2. 阶段一：SRS 部署与裸流验收（你正在做）

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

好处：SRS 或 8771 宕机只影响巡检功能，打开摄像头、语音对话、现有 qwen 口罩巡检全部不受影响。

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
  （WebRTC→RTMP 桥接正常，即后续 8771 拉流入口）；前端日志"推流成功，巡检画面已切到 usb1"，
  现有 qwen 60s 口罩巡检在 SRS 回拉帧上照常运行；视角切换/停止恢复本地画面正常。

> 第二个实测坑：回拉 offer 不能加 audio transceiver。usbN 只推视频，offer 含 video+audio 两个 m-line 时
> SRS answer 只回一个，浏览器报 `order of m-lines in answer doesn't match offer`。播放端只 addTransceiver('video')。

---

## 4. 阶段三：飞桨 8771 服务（独立环境，核心）

### 4.0 实测记录（2026-010 探针通过，以下以本节为准）

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
- OpenCV `VideoCapture` 读 HTTP-FLV 在本机 0 帧，runner 拉流用 **RTMP**（推流侧 FLV 正常）。

探针结果：

- 图片探针（[probe_image.py](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/probe_image.py)）：
  PedestrianDetection_001.png 检出 7 人（0.79~0.94），`det boxes=[class,score,x1,y1,x2,y2]`，
  `attr.output` 为每人 10 个字符串。
- **ATTR 26 个标签无口罩（Mask）字段**（Hat/Glasses/衣着/包/年龄/性别/朝向…），
  口罩模块独立设计，阶段四用 PaddleHub pyramidbox mask 落地，见 [4.8](#48-口罩模块paddlehub纳入阶段四)；
  探针验收前继续由现有 qwen 60s 巡检兜底。
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

### 4.3 配置文件 `paddle_service/conf/pphuman.yml`

- [ ] 从官方示例复制后只改 enable 开关（不手写模型 URL，避免版本不一致）：

```powershell
Copy-Item third_party\PaddleDetection\deploy\pipeline\config\examples\infer_cfg_pphuman.yml paddle_service\conf\pphuman.yml
```

修改以下开关：

```yaml
visual: True        # 关键：Pipeline.process 返回的帧已画好 框/骨骼/属性/行为
MOT:        { enable: True }    # 跟踪框 + track id（其他模型都挂在它的 id 上）
KPT:        { enable: True }    # 17 点骨骼（STGCN 依赖，必须开）
ATTR:       { enable: True }    # 属性行（实测 26 标签无口罩，口罩见 4.8 独立模块）
SKELETON_ACTION: { enable: True, display_frames: 80 }   # 摔倒 fall
VIDEO_ACTION:    { enable: True }                        # 打架 fight
REID:       { enable: False }   # 跨镜 ReID 暂不需要，省显存
```

### 4.4 API 探针（写 runner 前先确认版本差异，10 分钟）

- [ ] `paddle_service/probe_api.py`：

```python
import sys, cv2
sys.path.insert(0, r'D:\project\eyes\voiceInteractive\third_party\PaddleDetection\deploy\pipeline')
from pipeline.pipeline import Pipeline
p = Pipeline(r'paddle_service\conf\pphuman.yml', device='gpu')  # CPU 验证时改 'cpu'
im = cv2.imread(r'paddle_service\test.jpg')
out, res = p.process(im)
print(out.shape)
print([list(x.keys()) for x in res] if res else 'empty')   # 记录属性/行为字段真实名称
cv2.imwrite(r'paddle_service\test_out.jpg', out)
```

要求：`test_out.jpg` 上能看到框 + 骨骼 + 属性行；打印出的字段名用于 4.6 事件映射（ATTR 已实测无口罩字段，口罩按 4.8 独立模块处理）。

### 4.5 `runner.py`：拉流 → 逐帧推理 → ffmpeg 管道推回标注流

不依赖 PP-Human 自带的文件输出，自行用 OpenCV 拉 RTMP、ffmpeg 管道推 RTMP（最稳、可控）：

```python
# 核心循环伪代码（实现时补重连、退出事件、异常计数）
cap = cv2.VideoCapture('rtmp://10.0.20.219:1935/live/usb1')   # FLV HTTP 备选
pipe = start_ffmpeg_push(width, height, fps=15,
        url='rtmp://10.0.20.219:1935/live/usb1_labeled')       # rawvideo bgr24 → libx264 → flv
while not stop.is_set():
    ok, frame = cap.read()
    if not ok: 重连()
    drawn, obj_res = pipeline.process(frame)   # visual=True 时 drawn 已含全部标注
    pipe.stdin.write(drawn.tobytes())
    emit_events_if_any(obj_res)                # 见 4.6
```

ffmpeg 管道参数（固定使用 `D:\project\eyes\voiceInteractive\ffmpeg.exe`，配置项可由 8771 config 覆盖）：

```
ffmpeg -y -f rawvideo -pix_fmt bgr24 -s {W}x{H} -r 15 -i -
       -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p
       -g 30 -profile:v baseline -f flv rtmp://10.0.20.219:1935/live/usb1_labeled
```

要点：宽高以首帧为准，流分辨率变化时重建管道；推流失败（SRS 暂无播放者不影响，RTMP 推流可独立存在）。

### 4.6 事件提取与 SSE（`server.py`，ThreadingHTTPServer，端口 8771）

| 方法/路径 | 作用 |
|---|---|
| `GET /health` | 健康检查 + 各 usbN 分析线程状态 |
| `POST /analyze/start` | body `{"stream":"usb1"}` 启动一路 runner（幂等） |
| `POST /analyze/stop` | 停止 |
| `GET /events` | **SSE**：`text/event-stream`，按 track id 去重/冷却后下发 |

事件 JSON 统一格式：

```json
{"type":"alert","event":"fall","stream":"usb1","track_id":3,
 "label":"3号人员摔倒","confidence":0.92,"ts":1760000000.0}
```

事件来源：

- `fall`：obj_res 中骨骼行为标签（首跑探针确认真实 key，常见为 `actions`/`skeleton_action`），同一 track id 冷却 30s
- `fight`：视频分类帧级标签，冷却 10s
- `intrusion`：**runner 内自实现**（不依赖套件区域功能）：配置多边形 `conf/regions.json`，用 bbox 中心点判定，进入即报
- `no_mask`：由阶段四新增的 **PaddleHub 口罩模块**产出，设计详见 [4.8 口罩模块（PaddleHub）](#48-口罩模块paddlehub纳入阶段四)。
  在该模块探针验收通过前，8771 不输出 `no_mask` 事件，口罩仍由现有 qwen 60s 巡检兜底。

### 4.7 启动与独立运行

首轮手动启动验证：

```powershell
.\.venv-paddle\Scripts\python.exe -m paddle_service.main --device gpu
```

验证通过后，仿照 [main.py 拉起 asr_server](file:///d:/project/eyes/voiceInteractive/assistant/main.py#L63-L68) 的方式，
用 **`.venv-paddle\Scripts\python.exe -u -m paddle_service.main`** 再 Popen 一条（8770、8771 各自独立解释器，DLL 彻底隔离）。

- [ ] 阶段三验收：前端已推 `usb1` → 调 `POST /analyze/start {"stream":"usb1"}` →
  ffmpeg 拉 `rtmp://10.0.20.219:1935/live/usb1_labeled` 能看到带框、骨骼、属性的画面；
  `GET /events` 能收到事件流。

### 4.8 口罩模块（PaddleHub，纳入阶段四）

背景：阶段三已实测 PP-Human ATTR 的 26 个属性标签**不含口罩**，因此口罩是 8771 内的独立模块，
与 PP-Human 并行，共用 `.venv-paddle` 与同一张 GPU，不引入第二个推理引擎。

**选型**：PaddleHub `pyramidbox_lite_server_mask`（PyramidBox 人脸检测 + 口罩二分类，
输出每张人脸 `[label, confidence, x1, y1, x2, y2]`，label 1=佩戴 / 0=未佩戴）。
模型几 MB、单帧毫秒级；优先 server 版，效果不足再换 mobile 版对比。

**实施步骤（随阶段四一起做，先探针后接线）**：

1. `.venv-paddle` 内 `pip install paddlehub`（注意与 paddle 2.6.2 / numpy<2 的版本兼容，
   装完先验证 `import paddlehub` 不拖挂 paddle 版本）；
2. 写 `paddle_service/probe_mask.py`：对真人本地截帧（戴 / 未戴 / 侧脸各几张）验证
   `hub.Module(name="pyramidbox_lite_server_mask")` 的真实返回字段与准确率，**探针不过不接线**；
   模型首次加载自动下载（若 bcebos 慢，复用 [download_models.py](file:///d:/project/eyes/voiceInteractive/assistant/paddle_service/download_models.py) 的分块思路）；
3. runner 每 N 帧（GPU 下 N=5，约 3Hz）对当前帧跑一次口罩检测，把人脸框叠加到标注帧：
   绿框+"口罩" / 红框+"未戴口罩"，与人体框、骨骼同帧进入 `usbN_labeled`；
4. 事件去抖与冷却：同摄像头**连续 3 个采样周期**检出未戴才发事件（防转脸误报），
   事件冷却 30s，沿用现有不重复告警逻辑；
5. 侧脸/远距离小脸未检出人脸时按"无证据"处理，**不把无人脸等同全员佩戴**，避免漏报。

**事件协议**（与 fall/fight 同通道，前端无需新订阅）：

```json
{"type":"alert","event":"no_mask","stream":"usb1",
 "label":"1号摄像头：发现2人未戴口罩","no_mask":2,"faces":[...],
 "screenshot":"usb1_labeled 当前帧","ts":1760000000.0}
```

**前端切换（阶段四一并完成）**：

- 巡检态口罩来源由"60s 定时 `/vision`"改为订阅 `no_mask` 事件；告警弹窗、语音播报、
  历史面板、证据存盘全部复用现有 `showAlertWin / inspectSpeak / inspectHistory / vision_save`；
- 证据图截 `usb1_labeled` 标注流（红框直接在图上）；
- 配置开关 `mask_engine: paddlehub | qwen | off`（config.json）：paddlehub 加载失败自动退回 qwen；
  新链路稳定后再删旧口罩提示词与解析代码；
- **`/vision` 接口保留**：语音"看看画面里有什么"的看图对话能力继续使用，仅卸下定时口罩巡检职责。

---

## 5. 阶段四：前端对接标注流与告警链路

- [ ] 推流 / analyze-start-stop / 视角切换已在阶段二 3.2 接入 `_startOne`、`_stopOne`，本阶段仅联调标注流自动切换（轮询 8771 状态或等 SSE 首个分析帧后由 `usbN` 切到 `usbN_labeled`）
- [ ] 新增 SSE 订阅（EventSource `http://localhost:8771/events`），收到事件后直接调用现有：
  - [showAlertWin](file:///d:/project/eyes/voiceInteractive/web/index-inspect.js#L102-L118)：扩展入参支持"行为类型 + track 标签"（证据图用 `captureCamFrame` 截当前标注流）
  - [inspectSpeak](file:///d:/project/eyes/voiceInteractive/web/index-inspect.js#L69-L85) 语音播报（如"1号摄像头：3号人员摔倒"）
  - [inspectHistory](file:///d:/project/eyes/voiceInteractive/web/index-inspect.js#L238-L264) 历史面板，新增事件类型字段
- [ ] 8771 不可用时降级：视角锁定原始流、巡检按钮仍可走现有 qwen 60s 口罩巡检，互不影响
- [ ] **口罩模块（4.8）**：`pip install paddlehub` → `probe_mask.py` 真人戴/未戴/侧脸探针验收
- [ ] runner 接入口罩推理（每 5 帧）+ 标注流人脸红绿框 + 连续 3 周期去抖 / 30s 冷却
- [ ] `no_mask` SSE 事件接入告警三件套；config 增加 `mask_engine` 开关，失败自动退回 qwen
- [ ] 巡检态停用 60s 定时 `/vision` 口罩巡检（`/vision` 语音看图保留）；新旧并行观察一段时间后再删旧逻辑

---

## 6. 端口 / 进程 / 显存一览

| 进程 | 端口 | 环境 | 显存占用方 |
|---|---|---|---|
| asr_server（现有） | 8770 | `.venv`（torch） | Qwen3-ASR fp16 |
| Ollama（现有） | 11434 | 独立安装 | qwen3.5:4b |
| paddle_service（新） | **8771** | **`.venv-paddle`（paddle）** | PP-YOLOE+HRNet+属性+STGCN，单路 720p 实测后记录 |
| SRS（219 服务器） | 1935/1985/8080/8000udp | Docker | 无 |

单路 720p GPU 预期 15~25fps；多路时 KPT 是最大开销，降级策略：远距离/低置信目标跳过关键点。

**本机显存预算（RTX 2060 仅 6GB，偏紧）**：Qwen3-ASR-0.6B fp16 ≈1.2GB；Ollama qwen3.5:4b ≈2.5GB
（空闲可部分让出）；飞桨轻量模型组（PP-YOLOE + TinyPose + 属性 + STGCN）预估 0.5~1.5GB。
峰值可能接近 6GB，阶段三启动后必须用 `nvidia-smi` 实测三家共存峰值并记录：
- 超预算时优先级：① 飞桨全程只选 tiny 模型、输入宽降到 640；② Ollama 设置 keep_alive 缩短或巡检时卸模型；
  ③ 兜底让飞桨跑 CPU（3~8fps 仅影响实时标注流畅度，事件检测仍可用）。**不可把飞桨挪到 219（无显卡）。**

---

## 7. 风险与回退

| 风险 | 应对 |
|---|---|
| Windows 无对应 CUDA 的 paddle GPU wheel | 先在本机 CPU 跑通链路（3~8fps 足够功能验证）；正式使用前必须装匹配本机 CUDA 的 GPU wheel（219 无显卡，不能挪到 219） |
| 模型自动下载失败（内网） | 按 4.2 手动下载并改 `model_dir` 本地路径 |
| PP-Human 版本间 `process()` 返回值/字段名差异 | 4.4 探针先行，以探针打印为准写事件映射 |
| ATTR 无口罩字段（已实测确认） | 阶段四用 PaddleHub `pyramidbox_lite_server_mask` 独立模块（4.8）；探针验收前现有 qwen 60s 口罩巡检兜底；paddlehub 与 paddle 2.6.2/numpy<2 若版本冲突，`mask_engine` 可退回 qwen，不阻塞 fall/fight 主链路 |
| 本机关机 / 8771 未运行 | 219 SRS 仍可中转与（可选）DVR 录像，但无分析与告警；设计上 SRS 与分析解耦 |
| 标注流卡顿 | 降推理帧率（抽帧，如每 2 帧推 1 帧）、关 VIDEO_ACTION、缩到 640 宽 |
| 全链路异常 | 前端切回"原始视角"；8771 停止即恢复为纯 SRS 中转，现有口罩巡检完全不受影响 |

## 8. 执行顺序总览

1. **阶段一** SRS 部署 + testsrc 裸流验收（当前）
2. **阶段二** `voice-srs.js` + 推流/同源播放，ffmpeg 验 usb1
3. **阶段三** `.venv-paddle` + PP-Human 探针 → runner + 8771 → 验 usb1_labeled
4. **阶段四** SSE 接入现有告警三件套 + 视角切换 + 降级 + PaddleHub 口罩模块（4.8，探针先行，qwen 兜底）
5. 多路（usb2…）与显存/帧率优化
