# SpeakerHelper

使用 `edge-tts` 合成语音，并通过 `python-vlc` / VLC 播放。支持命令行和桌面图形界面两种方式。

## 安装

```powershell
pip install -r requirements.txt
```

> 另外请先在本机安装 VLC 播放器，确保 `python-vlc` 可以正常加载到系统的 VLC 运行时。

## 方式 1：命令行

```powershell
python tts.py
```

交互命令：

- `/devices` 或 `/device list`：列出可用输出设备
- `/device`：查看当前输出设备
- `/device <设备序号>`：设置输出设备
- `/device default`：恢复系统默认设备
- `/help`：查看帮助
- `/exit`：退出程序

## 方式 2：桌面图形界面

```powershell
python desktop_app.py
```

界面功能：

- 聊天软件风格界面：上方消息区、下方输入区
- 输入文本后按 `Enter` 立即发送并播放
- 按 `Ctrl+Enter` 在输入框内换行
- 点击“刷新设备”拉取可用输出设备
- 在顶部选择输出设备，未选择时使用系统默认设备

## 打包（Windows）

```powershell
pip install pyinstaller
pyinstaller desktop_app.spec
```

打包后可执行文件位于 `dist/desktop_app.exe`。运行前请确保目标机器已安装 VLC，或能让程序找到 `libvlc.dll`。
若目标机器未配置 VLC 到系统路径，可设置环境变量 `VLC_DIR` 指向 VLC 安装目录（例如 `C:\Program Files\VideoLAN\VLC`）。
建议保持 Python/打包架构与 VLC 架构一致（x64 对 x64，x86 对 x86）。

## 说明

- 当前播放后端是 VLC，仅依赖 `python-vlc` 与本机 VLC 运行时。
- 设备列表与设备切换使用 VLC 提供的接口。
- 若提示未找到 `python-vlc` 或 VLC，请先安装 VLC 播放器并确认 `python-vlc` 可正常导入。
