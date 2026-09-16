# 老叶 USB 摄像头视觉语音助手

这是一个面向 Windows 的最小可用语音交互程序：

1. 从 `麦克风 (Deli-1080P-Camera-Audio)` 持续监听。
2. 听到“老叶老叶”后回答“我在”。
3. 听到“现在几点了”后读取电脑当前时间。
4. 其他问题默认交给本机 Ollama `qwen3.5:4b` 回答，也可切换到在线 `qwen3.8-flash`。
5. 从 `Speakers (Deli-1080P-Camera Audio)` 播报中文答案。

启动时还会自动打开 `http://localhost:8765/` 摄像头页面。浏览器取得授权后，会显示
当前画面。问“你看到了什么”时，程序会通知网页立即拍摄一张同步快照，把该快照交给
当前选定的模型分析，并在网页显示同一张“本次分析快照”供人对照。在线模式会把快照发送
到在线模型服务；切换为 Ollama 模式后才会完全在本机分析。

程序同时驻留在 Windows 系统托盘。关闭摄像头页面不会退出老叶：网页画面中断约5秒后，
本机 OpenCV 会自动接管配置中的一个或多个 USB 摄像头，继续进行变化检测、YOLO人物确认、动态画面播报和
人脸识别；再次打开页面时，后台会先释放设备，再交还给浏览器。托盘菜单可以打开页面、
切换后台接管及各项视觉能力，或安全退出整个助手。
检测到持续变化时使用 Windows 10/11 原生通知中心提示；点击通知会打开控制台页面。

网页还会在本机浏览器中进行低分辨率背景差分监测。画面连续多帧发生明显变化时，才提交
一张变化快照给本机 GPU 上常驻的 YOLOv8n 确认是否有人。系统维护“无人/有人”状态，只在确认发生
“无人到有人”的变化时语音提醒“检测到有人进入画面”，并在网页保留本次事件快照；
同一个人持续停留不会反复播报。

语音识别使用 Vosk 小型中文模型，模型下载完成后可离线识别。中文语音合成使用
Microsoft Edge 在线语音服务，因此语音合成需要网络。
合成音频只作为本次播放的临时缓存；播放完成、被打断或播放失败后会立即删除。
唤醒后的交流默认启用四川口音普通话增强：Vosk保留多个声学识别候选，内置命令优先选择
语义可执行的候选，普通问题由当前Qwen结合候选和多轮上下文判断真实含义。该功能不改变
“老叶老叶”唤醒识别，也不会额外调用一次大模型。

普通问答和视觉描述都使用流式输出。模型生成出完整句子后会立即进入语音播放队列；播放
当前句子时，下一句会在后台预合成，以减少句子之间的停顿。

MM101S 的麦克风与扬声器距离很近且没有硬件回声消除，默认采用可靠的半双工模式：老叶
播报时不监听麦克风，避免把自己的声音识别成唤醒词或停止命令。若以后使用带 AEC 的设备，
可把 `barge_in_during_playback` 改为 `true`，恢复“停一下”等语音打断能力。

## 首次安装

在 PowerShell 中执行：

```powershell
cd D:\project\eyes\voiceInteractive
.\setup.ps1
```

安装脚本会创建独立的 `.venv`，安装固定版本依赖，并下载约 42 MB 的中文识别模型。

## 启动

```powershell
.\run.ps1
```

也可以直接双击 `启动老叶.cmd`。这是从资源管理器启动时的统一入口；启动失败时窗口会保留
错误信息。`logs/last-start.log` 会记录最近一次启动是否成功。

`run.ps1` 会自动检查运行环境。在线模式直接连接配置的 Qwen 服务，不启动 Ollama；切换到
本地模式后，脚本才会启动 Ollama 并检查本地模型是否已经安装。

看到“已启动”后依次说：

> 老叶老叶

听到“我在”后说：

> 现在几点了

也可以询问普通问题，例如：

> 给我讲一个简短的笑话

唤醒一次后可以连续提问，不需要每句话都重复“老叶老叶”。连续 30 秒没有提问时，会话自动
结束并清除本轮上下文；也可以说“不用了”“没事了”“结束对话”或“再见”主动结束会话。
需要彻底退出程序时，请说“关闭助手”或使用网页、`stop.ps1`。

查看当前摄像头画面时可以问：

> 你看到了什么

紧接着询问“这个人多大”“左边是什么”等视觉追问时，程序会继续附带最新摄像头画面，
避免只根据上一轮文字描述猜测。

## 工具

天气问题会自动路由到天气工具，不让大模型凭记忆猜测。支持例如：

> 查询天气预报

> 北京明天天气怎么样

> 洛杉矶未来三天天气

没有说城市时使用 `weather_default_location` 配置的默认城市。天气工具通过国内 UAPI 接口
取得实时天气、空气质量和未来天气预报；不需要额外 API 密钥。数据来源：
[UAPI 天气接口](https://uapis.cn/docs/api-reference/get-misc-weather)。可以用 `internet_tools_enabled` 关闭工具，
用 `internet_timeout_seconds` 调整单次请求超时，用 `internet_retry_count` 调整网络失败后的
重试次数。成功结果会缓存五分钟；接口短暂异常时，可在半小时内回退到最近一次成功数据。
“明天呢”之类的追问会沿用上一条天气问题中的城市。

电脑操作会优先路由到本机白名单工具，目前支持：

> 打开计算器

> 打开记事本

> 打开记事本，然后写几行 Python 代码，计算一到一百的和

> 打开记事本，写入今天下午三点开会

代码需求会由当前选中的模型生成纯代码，然后保存到系统临时目录并使用记事本打开。
本机工具只允许启动计算器和记事本，不会把任意 PowerShell、CMD 或程序执行权限交给模型。

也可以在同一轮连续对话中完成一个代码流程，每一步等老叶回答后再说下一句：

> 打开记事本

> 在记事本写代码

> 保存到桌面，文件名叫求和程序

> 打开运行

老叶会记住当前草稿。“保存到桌面”支持指定文件名，并自动补充 `.py` 扩展名；
“打开运行”是明确的执行确认，目前只支持 Python。运行结束后会播报标准输出，失败时会播报
错误并询问是否修复；确认修复后，老叶会再次询问是否重新运行，不会自动反复执行。
`code_run_timeout_seconds` 控制最长运行时间，默认十秒。含明显文件、网络或系统控制操作的
生成代码会被安全检查拦截。会话空闲超时或主动结束后，当前电脑任务上下文会清除。

首次打开页面时，需要点击浏览器的“允许”按钮授予摄像头权限。如果自动选择的摄像头不对，
可以在画面下方分别选择主摄像头和辅助摄像头。页面会同时显示两路画面；视觉问答、人物监测、
动态播报、人脸识别和人员视频采集只使用标记为“主摄像头”的一路。

右侧“摄像头音频”可以分别选择监听麦克风和回答扬声器。两个 MM101S 在 Windows 中通常会
显示为普通名称和带 `2-` 前缀的名称。保存选择后需要停止并重新启动老叶，因为运行中的
PortAudio 录音流和播放设备不能在不中断语音主循环的情况下安全切换。

需要关闭时，可点击网页右上角的“关闭老叶助手”，或运行 `stop.ps1`。

## 自检与配置

列出全部音频设备：

```powershell
.\run.ps1 --list-devices
```

只测试摄像头扬声器：

```powershell
.\run.ps1 --test-speaker
```

设备名、唤醒词、连续对话等待时间、上下文轮数、TTS 声音和模型设置均可在
`config.json` 中修改。`command_timeout_seconds` 控制会话空闲超时，
`conversation_history_turns` 控制当前会话最多保留多少轮问答。
默认语音为 `zh-CN-YunyangNeural`，使用偏沉稳的成年男声；当前 `tts_rate` 为 `+25%`。
`asr_accent_enhancement_enabled` 控制交流阶段的口音增强，`asr_max_alternatives` 控制
Vosk保留的候选数量，默认3个。
`tts_proxy` 控制 Edge 在线语音合成代理，`network_proxy` 控制在线 Qwen 和天气接口代理；
当前两项均设置为 `http://127.0.0.1:52351`。
`person_monitor_enabled` 设置动态人物监测的启动默认值，页面开关可在运行时启用或关闭；`person_alert_voice` 控制语音提醒；
`scene_broadcast_enabled` 设置动态画面播报的启动默认值，页面也可独立开关；开启后先播报当前画面，之后仅在画面连续发生明显变化时抓拍并播报。
`scene_broadcast_cooldown_seconds` 控制两次画面播报之间的最短间隔，默认12秒，避免频繁打扰。
`tray_enabled` 和 `tray_notifications_enabled` 分别控制托盘图标与 Windows 原生通知。
`native_camera_enabled` 控制浏览器关闭后的摄像头接管；`native_cameras` 支持配置多个摄像头。
每个启用的摄像头独立检测变化并告警；只有一个摄像头应标记 `primary: true`，它为视觉问答、
YOLO人物确认、动态播报和人脸识别提供统一画面。网页占用摄像头期间，后台会暂时释放全部
OpenCV 摄像头，网页关闭后自动恢复。

当前示例已启用索引0和1两个 USB 摄像头。更换设备或索引发生变化时，先停止后台助手，再运行
`run.ps1 --list-cameras` 查看 OpenCV 索引，然后调整配置：

```json
"native_cameras": [
  {"id": "camera_1", "name": "Deli MM101S 1", "index": 0, "enabled": true, "primary": true},
  {"id": "camera_2", "name": "Deli MM101S 2", "index": 1, "enabled": true, "primary": false}
]
```

后台变化快照保存在 `data/events/`，不会提交到 Git。默认保留7天且总量不超过1024 MB；
`native_camera_event_max_megabytes` 控制容量上限，目录清理默认每60分钟最多执行一次，避免
每次告警都扫描磁盘。
`person_detector` 默认使用 `yolo`，也可改为 `qwen`。YOLO配置项包括外部Python解释器、
模型路径、GPU设备、置信度、输入尺寸和超时时间。当前使用标准COCO版 `yolov8n.pt`，
只推理类别0 `person`，模型在独立GPU进程中加载一次，不污染老叶自身的Python环境。
灵敏度、
最小变化面积、连续确认帧数和冷却时间分别由 `person_motion_sensitivity`、
`person_motion_min_area_percent`、`person_motion_consecutive_frames`、
`person_motion_cooldown_seconds` 调整。默认每0.5秒做一次本地变化检测，连续3帧变化后才让
YOLO确认，以减少GPU推理次数和误报。

### 本地人脸识别与视频录入

页面中的“人脸识别”开关独立于动态人物监测。点击“管理人员库”，先填写姓名和可选别名，
确认已获得本人同意，然后点击该人员的“开始视频采集”。建议录制8～15秒：先正对镜头，
再缓慢左右转头，并轻微抬头、低头；点击“结束采集”后，本机会自动抽帧，只保存单人、
清晰且角度不重复的画面。背面、多人、模糊和人脸过小的帧会跳过。原始录制视频、抽取的
照片和SQLite特征库均保存在 `data/faces/`，不会交给在线大模型。
该目录已从Git版本控制中排除；代码仓库只保存建库逻辑，不保存人员照片、视频、姓名或
人脸特征。

每人建议保留10～18张有效样本。录入后打开人脸识别开关，画面会标出已知姓名；唤醒后可问
“王二在哪”，老叶会根据最近5秒内的本地识别结果回答画面左侧、中间或右侧。人员删除后，
其照片、视频和特征会一并从本机删除。

识别使用OpenCV YuNet检测模型和SFace特征模型。相关配置包括 `face_match_threshold`、
`face_match_margin`、`face_min_size`、`face_min_blur` 和 `face_result_max_age_seconds`。
默认阈值适合先行测试，但正式使用前应结合现场距离、光线和摄像头角度采样校准；门禁、考勤
等高风险场景不应只依赖这一识别结果。
程序优先选择同名端点的 Windows WASAPI 设备；也可以把设备名改成 `--list-devices`
显示的数字编号。

当前默认使用本机 Ollama。切换到在线模式时，API 密钥优先读取 `QWEN_API_KEY` 环境变量；未设置时，从
`online_config_db` 指向的 qwenchat 数据库读取现有连接。在线 Qwen 和国内天气接口使用
`network_proxy` 指定的代理；留空时才会直接连接：

```json
{
  "llm_provider": "online",
  "online_model": "qwen3.8-flash",
  "online_api_key_env": "QWEN_API_KEY",
  "online_config_db": "D:/project/qwenchat/data/webui.db"
}
```

需要切回本机模型时，只修改 `llm_provider`：

```json
{
  "llm_provider": "ollama",
  "ollama_enabled": true,
  "ollama_url": "http://127.0.0.1:11434",
  "ollama_model": "qwen3.5:4b"
}
```

若要换模型，先执行 `ollama list` 查看名称，再修改 `ollama_model`。Ollama 未运行时，可从
开始菜单启动 Ollama，或在终端运行 `ollama serve`。

## 常见问题

- 没有声音：先确认摄像头音量未静音，再运行 `--test-speaker`。
- 听不到唤醒词：在 Windows“隐私和安全性 → 麦克风”中允许桌面应用访问麦克风。
- 设备名变化：运行 `--list-devices`，把 `config.json` 中的输入/输出名称改成新名称的一部分。
- 回答失败但识别正常：中文 TTS 需要；控制台仍会打印准确时间。
- 旧 PowerShell 显示方框：请始终用 `run.ps1` 启动；脚本会切换 UTF-8，程序会尝试为
  旧控制台启用“新宋体”。如果窗口仍不支持中文字形，可在窗口属性中手动选择“新宋体”。
- 启动窗口关闭：`run.ps1` 会注册并启动名为 `LaoyeVoiceAssistant` 的本机 Windows 后台
  任务，因此启动完成后 PowerShell 窗口可以安全关闭，语音监听和摄像头页面会继续运行。
  标准输出和错误分别保存在
  `logs/assistant-output.log`、`logs/assistant-error.log`；启动器错误保存在
  `logs/launcher-error.log`。如果助手已经运行，再次启动只会打开现有摄像头页面。

停止后台语音助手：

```powershell
.\stop.ps1
```

也可以点击网页中的“关闭老叶助手”，或直接双击 `停止老叶.cmd`。后台提供固定的
`POST /api/shutdown` 安全关闭接口；`stop.ps1` 会先请求后台自行释放麦克风和网页端口，
确认端口释放后再移除后台任务。只有旧版本没有关闭接口或退出超时时，脚本才会核对项目路径
并结束本项目的 Python 进程。停止结果保存在 `logs/last-stop.log`。

## 稳定的后台边界

启动和关闭只由以下入口负责，后续增加搜索、长期记忆等功能时不再增加另一套生命周期：

1. `启动老叶.cmd` / `run.ps1`：准备所选模型和环境，并启动唯一后台任务。
2. `assistant-host.ps1`：仅作为计划任务宿主，原样转发 `--config`、`--no-tray` 等参数。
3. `voice_assistant.py`：持有麦克风、摄像头帧、模型会话和关闭事件。
4. `停止老叶.cmd` / `stop.ps1` / 网页关闭按钮：通过 `/api/shutdown` 通知后台安全退出。

搜索和记忆将作为 Python 后台内部的独立能力接入，不再另外启动一组需要单独关闭的进程。

Python实现位于 `assistant/` 包中，配置、语音主循环、模型、工具、摄像头和Dashboard分别
独立。人脸人员库与OpenCV worker位于 `assistant/face/`。根目录的 `voice_assistant.py`、
`face_service.py`、`face_recognition_worker.py` 和 `face_video_import.py` 仅作为旧调用方式的
兼容入口保留。除原有脚本外，也支持 `python -m assistant` 启动。
