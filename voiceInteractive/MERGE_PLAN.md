# localCamera 合并记录

`voiceInteractive` 是唯一主项目。原 `localCamera` 的能力已按下面的边界合并，不再保留第二套
HTTP 服务、第二个 8765 端口或第二个进程生命周期。

## 能力映射

| localCamera 能力 | voiceInteractive 中的落点 | 结果 |
| --- | --- | --- |
| OpenCV 直接读取 USB 摄像头 | `assistant/native_camera.py` | 网页关闭后自动接管；网页打开时主动释放，避免抢设备 |
| 背景差分、灵敏度、面积阈值、连续帧、冷却 | `MotionDetector`，沿用人物监测参数 | 已合并 |
| 变化事件快照 | `data/events/`、`/api/motion-events/` | 默认保存，7 天自动清理，不进入 Git |
| 浏览器关闭后继续检测 | `NativeCameraMonitor` | 已合并；默认网页画面中断 5 秒后接管 |
| Windows 托盘 | `assistant/tray.py` | 打开页面、四个监测开关、退出助手 |
| Windows 通知 | 托盘通知 | 后台检测到持续变化时提示，不影响语音提醒 |
| 单实例和后台运行 | `run.ps1` 的 Windows 计划任务 | 沿用主项目已有实现 |
| 安全关闭 | 托盘、网页和 `stop.ps1` 共用 `shutdown_event` | 已统一 |
| 页面和状态接口 | 现有 `web/index.html`、`assistant/dashboard.py` | 增加“后台摄像头接管”开关和状态 |
| 人物确认、动态描述、人脸识别 | 主项目已有 YOLO、Qwen 视觉与 SFace | 后台帧直接复用，不重复实现 |
| 独立英文 Sentinel 页面 | 不迁移 | 主项目中文页面功能更完整，避免维护两套 UI |
| `LocalCamera.exe` 和 PyInstaller 脚本 | 不迁移 | 主项目包含语音、GPU worker 和模型，继续使用 `.venv` 与计划任务 |

## 生命周期

1. `run.ps1` 只启动一个老叶后台任务。
2. 网页连接摄像头前先向后台申请设备，后台监控立即释放摄像头。
3. 网页每秒提交画面，后台摄像头保持待机。
4. 网页关闭或画面中断约 5 秒后，OpenCV 自动接管摄像头。
5. 后台帧继续支持变化检测、YOLO人物确认、动态画面播报、人脸识别和视觉问答快照。
6. 托盘退出、网页关闭按钮和 `stop.ps1` 都走同一个安全关闭流程。

## 数据策略

- 人脸库继续保存在 `data/faces/`，合并和清理旧项目时不得删除。
- 后台变化快照保存在 `data/events/`，默认保留 7 天，过期自动删除。
- 原项目 `dist/data/events/` 中的两个 `.lnk` 文件不是摄像头快照，不迁移。
- 语音临时文件仍在播放完成、打断或失败后立即删除。

## 验收条件

- Python 编译通过，完整单元测试通过。
- `/api/config` 和 `/api/status` 返回后台摄像头配置及实时状态。
- 页面能切换后台接管开关；托盘能打开页面、切换监测并安全退出。
- 网页工作时后台不抢摄像头；网页停止提交画面后后台自动接管。
- 确认以上条件后，才删除原 `D:\project\eyes\localCamera` 目录。
