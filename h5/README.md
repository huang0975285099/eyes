# 千里眼客户移动平台

Quasar/Vue 3客户门户，同一套代码支持：

- 浏览器：部署后访问`http://<AIService>:18887/customer/`。
- Android APK：Quasar Capacitor模式，工程位于`src-capacitor`。
- Windows EXE：Tauri 2，工程位于`src-tauri`。

## 功能

1. 实时视频：多个在线点位纵向滑动切换，只解码当前画面。
2. 设备管理：查看本客户全部点位及在线、编码、分辨率等信息。
3. AI服务：逐点位配置录像、保留小时数，以及“每N分钟抽M帧”的实时抽帧规则。
4. 个人中心：账号和点位统计、服务器信息、修改密码及退出。

## 本地开发和构建

```bash
npm install
npm run dev
npm run build
```

Android需要Node 22.22+、Android Studio和Android SDK：

```bash
cd src-capacitor && npm install && cd ..
npm run dev:android
npm run build:android
```

首次执行会自动生成并同步`src-capacitor/android`。该目录属于可再生构建产物，不提交到
仓库；正式签名APK/AAB在Android Studio或配置好`ANDROID_HOME`后生成。

Windows Tauri需要Rust stable-msvc、Visual Studio C++生成工具及WebView2：

```bash
npm run dev:tauri
npm run build:tauri
```

`build:tauri`会生成Windows NSIS安装包；需要预先安装Rust stable-msvc、Visual Studio C++生成工具及WebView2。

源码开发时登录页可修改服务器地址。部署在AIService中的浏览器版本会自动使用当前域名；
Capacitor/Tauri版本默认使用`http://112.18.238.6:18887`，并会记住用户修改后的地址。
