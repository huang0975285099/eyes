# Sentinel: USB Camera Motion Alert

A fully local USB camera monitoring tool. It displays a live camera feed and warns you when sustained visual changes are detected. Alerts can flash on screen, play a sound, send a browser notification, and save a snapshot.

## Quick Start

Requirements: Windows 10/11, Python 3.10 or newer, and a USB camera.

For the ready-to-run version, double-click:

```text
dist\LocalCamera.exe
```

The application opens its interface at:

```text
http://127.0.0.1:8765
```

The browser is only the interface. The local application reads the camera. By default, the server listens only on `127.0.0.1`, so other devices on the network cannot access it.

## How to Use

1. Connect the USB camera and open `dist\LocalCamera.exe`. The dashboard opens in your default browser and a Sentinel icon appears in the Windows system tray.
2. If the wrong camera appears, change **Camera Index** from `0` to `1` or `2`, then click **Save**.
3. Position the camera and click **Start Change Detection**. The app learns the stationary background for about one second.
4. You may minimize or close the browser. Camera detection continues in the background.
5. Sustained visual changes trigger a Sentinel popup at the bottom-right and are added to **Recent Alerts**.
6. To reopen the dashboard or stop the application, click the Sentinel tray icon. Choose **Exit Sentinel** to fully exit.

Use **Test Notification** in the dashboard to confirm that background alerts are working. The Sentinel popup is generated directly by the EXE and does not depend on Chrome notification permission or the Windows notification-sender list.

## System Tray

- Click the Sentinel tray icon to open the dashboard.
- Right-click it for **Open Dashboard**, **Start / Stop Detection**, and **Exit Sentinel**.
- Closing the browser does not stop monitoring.
- Exiting Sentinel from the tray stops the camera and local server.

## Detection Settings

- **Sensitivity:** Higher values detect smaller visual differences but may react more often to lighting changes or camera noise.
- **Minimum Change Area:** Changes below this percentage of the image are ignored. Increase the default `0.8%` to `1.5%–3%` if there are too many false alerts.
- **Confirmation Frames:** The number of consecutive changed frames required before an alert. Increase the default `3` to `5–8` to reduce false alerts.
- **Save Alert Snapshots:** Stores images under `data\events\` beside the application. If the EXE directory is not writable, `%LOCALAPPDATA%\LocalCamera\events\` is used instead.

A sudden lighting change or physical camera movement is also considered a visual change and may trigger an alert.

## Run From Source

Double-click `start.cmd`, or run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py --camera 0
```

Optional arguments:

```text
--camera 1       Start with camera index 1
--port 9000      Change the web interface port
--no-browser     Do not open the browser automatically
```

## Build the EXE

Run in PowerShell:

```powershell
.\build-exe.ps1
```

The output is `dist\LocalCamera.exe`. It runs without a console window, and the target computer does not need Python installed.

## Privacy

- Camera images are not uploaded to the internet.
- Images are written to disk only when **Save Alert Snapshots** is enabled and an alert occurs.
- Browser desktop notifications require permission. The visual and sound alerts continue to work if permission is denied.
