import asyncio
import contextlib
import ctypes
import importlib
import os
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import edge_tts

_vlc_module = None
_vlc_import_error: Optional[Exception] = None
_resolved_vlc_dir: Optional[Path] = None
_resolved_vlc_lib: Optional[Path] = None
_vlc_device_token_by_index: dict[str, str] = {}
_dll_dir_handles: list[Any] = []


def _python_arch_label() -> str:
    return "x64" if sys.maxsize > 2**32 else "x86"


def _pe_arch_label(pe_file: Path) -> Optional[str]:
    try:
        with pe_file.open("rb") as handle:
            dos_header = handle.read(64)
            if len(dos_header) < 64 or dos_header[:2] != b"MZ":
                return None

            pe_offset = struct.unpack("<I", dos_header[0x3C:0x40])[0]
            handle.seek(pe_offset)
            pe_header = handle.read(6)
            if len(pe_header) < 6 or pe_header[:4] != b"PE\0\0":
                return None

            machine = struct.unpack("<H", pe_header[4:6])[0]
    except OSError:
        return None

    if machine == 0x8664:
        return "x64"
    if machine == 0x014C:
        return "x86"
    if machine == 0xAA64:
        return "arm64"
    return f"unknown(0x{machine:04x})"


def _discover_vlc_dir_from_registry_windows() -> Optional[Path]:
    if os.name != "nt":
        return None

    try:
        import winreg
    except ImportError:
        return None

    reg_roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    reg_keys = (
        r"Software\VideoLAN\VLC",
        r"Software\WOW6432Node\VideoLAN\VLC",
    )

    for root in reg_roots:
        for key_path in reg_keys:
            try:
                with winreg.OpenKey(root, key_path) as key:
                    for value_name in ("InstallDir", "InstallPath"):
                        with contextlib.suppress(OSError):
                            value, _ = winreg.QueryValueEx(key, value_name)
                            path = Path(str(value))
                            if path.is_dir():
                                return path
            except OSError:
                continue

    return None


def _discover_vlc_dirs_windows() -> list[Path]:
    candidates: list[Path] = []

    env_dir = os.environ.get("VLC_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    registry_dir = _discover_vlc_dir_from_registry_windows()
    if registry_dir is not None:
        candidates.append(registry_dir)

    preferred_order = ("ProgramFiles", "ProgramFiles(x86)")
    if _python_arch_label() == "x86":
        preferred_order = ("ProgramFiles(x86)", "ProgramFiles")

    for base_env in preferred_order:
        base = os.environ.get(base_env)
        if base:
            candidates.append(Path(base) / "VideoLAN" / "VLC")

    exe_dir = Path(sys.executable).resolve().parent
    candidates.extend(
        [
            exe_dir,
            exe_dir / "VLC",
            Path.cwd(),
            Path.cwd() / "VLC",
        ]
    )

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        meipass_path = Path(str(meipass))
        candidates.extend([meipass_path, meipass_path / "VLC"])

    unique: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique


def _prepare_vlc_dll_path_windows() -> None:
    global _resolved_vlc_dir, _resolved_vlc_lib

    if os.name != "nt":
        return

    _resolved_vlc_dir = None
    _resolved_vlc_lib = None
    existing_path = os.environ.get("PATH", "")

    for path in _discover_vlc_dirs_windows():
        if not path.is_dir():
            continue

        lib_path = path / "libvlc.dll"
        core_path = path / "libvlccore.dll"
        if not lib_path.exists() or not core_path.exists():
            continue

        py_arch = _python_arch_label()
        dll_arch = _pe_arch_label(lib_path)
        if dll_arch and dll_arch != py_arch:
            raise RuntimeError(
                f"检测到 VLC 位数不匹配：Python 为 {py_arch}，libvlc.dll 为 {dll_arch}。"
                " 请安装与 Python 位数一致的 VLC。"
            )

        if hasattr(os, "add_dll_directory"):
            handle = os.add_dll_directory(str(path))
            _dll_dir_handles.append(handle)

        path_str = str(path)
        if path_str.lower() not in existing_path.lower():
            os.environ["PATH"] = f"{path_str}{os.pathsep}{existing_path}" if existing_path else path_str
            existing_path = os.environ["PATH"]

        os.environ["PYTHON_VLC_LIB_PATH"] = str(lib_path)
        os.environ["PYTHON_VLC_MODULE_PATH"] = str(path)
        plugin_path = path / "plugins"
        if plugin_path.is_dir():
            os.environ["VLC_PLUGIN_PATH"] = str(plugin_path)

        # Preload explicit VLC runtime dlls to avoid accidental local dll hijack.
        ctypes.WinDLL(str(core_path))
        ctypes.WinDLL(str(lib_path))

        _resolved_vlc_dir = path
        _resolved_vlc_lib = lib_path
        return

    attempted = "；".join(str(p) for p in _discover_vlc_dirs_windows()) or "未配置"
    raise RuntimeError(f"未找到可用的 VLC 安装目录。请设置 VLC_DIR。已尝试：{attempted}")


def _get_vlc_module():
    global _vlc_module, _vlc_import_error
    if _vlc_module is not None:
        return _vlc_module
    if _vlc_import_error is not None:
        raise RuntimeError(_build_vlc_error_message(_vlc_import_error)) from _vlc_import_error

    try:
        _prepare_vlc_dll_path_windows()
        _vlc_module = importlib.import_module("vlc")
        return _vlc_module
    except Exception as exc:  # noqa: BLE001
        _vlc_import_error = exc
        raise RuntimeError(_build_vlc_error_message(exc)) from exc


def _build_vlc_error_message(exc: Exception) -> str:
    base_message = "未能加载 VLC 播放库。请先安装 VLC 桌面版，并确保 libvlc.dll 可被 Python 找到。"
    if os.name != "nt":
        return f"{base_message} 原因：{exc}"

    py_arch = _python_arch_label()
    dll_arch = _pe_arch_label(_resolved_vlc_lib) if _resolved_vlc_lib else None
    attempted = [str(p) for p in _discover_vlc_dirs_windows() if p.is_dir()]
    attempted_text = "；".join(attempted) if attempted else "未检测到常见 VLC 安装目录"
    cwd_dll = Path.cwd() / "libvlc.dll"

    details: list[str] = [f"Python 位数：{py_arch}"]
    if _resolved_vlc_lib:
        details.append(f"目标 libvlc：{_resolved_vlc_lib}")
    if dll_arch:
        details.append(f"libvlc 位数：{dll_arch}")
    if cwd_dll.exists():
        details.append(f"当前目录存在 libvlc.dll：{cwd_dll}（可能导致误加载）")

    details_text = "；".join(details)
    return (
        f"{base_message} 可尝试设置环境变量 VLC_DIR 指向 VLC 安装目录。"
        f" 已检测目录：{attempted_text}。{details_text}。原因：{exc}"
    )


def _create_vlc_instance() -> Any:
    vlc_module = _get_vlc_module()

    try:
        return vlc_module.Instance("--no-video", "--quiet")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(_build_vlc_error_message(exc)) from exc


EXIT_COMMANDS = {"/exit", "exit", "quit", "q"}
HELP_COMMANDS = {"/help", "help", "h", "?"}


async def _synthesize_audio_file(text: str, voice: str) -> Path:
    communicate = edge_tts.Communicate(text, voice)
    fd, temp_path = tempfile.mkstemp(prefix="SpeakerHelper_", suffix=".mp3")
    os.close(fd)
    path = Path(temp_path)
    got_audio = False

    try:
        with path.open("wb") as handle:
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    got_audio = True
                    handle.write(chunk["data"])
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise

    if not got_audio:
        with contextlib.suppress(OSError):
            path.unlink()
        raise RuntimeError("未收到可播放的音频数据。")

    return path


def _decode_vlc_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore").strip("\x00").strip()
    return str(value).strip()


def _collect_vlc_devices(raw_devices: Any) -> list[tuple[str, str]]:
    devices: list[tuple[str, str]] = []

    def append_item(device_id: Any, description: Any) -> None:
        token = _decode_vlc_text(device_id)
        name = _decode_vlc_text(description)
        if not token:
            return
        devices.append((token, name or token))

    if raw_devices is None:
        return devices

    if isinstance(raw_devices, (list, tuple)):
        for item in raw_devices:
            if isinstance(item, dict):
                append_item(item.get("device") or item.get("name"), item.get("description"))
                continue
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                append_item(item[0], item[1])
                continue
            append_item(getattr(item, "device", None), getattr(item, "description", None))
        return devices

    node = raw_devices
    seen_ptrs: set[int] = set()
    for _ in range(256):
        if not node:
            break

        pointer_id = id(node)
        if pointer_id in seen_ptrs:
            break
        seen_ptrs.add(pointer_id)

        current = getattr(node, "contents", node)
        append_item(
            getattr(current, "device", None) or getattr(current, "psz_device", None),
            getattr(current, "description", None) or getattr(current, "psz_description", None),
        )

        node = getattr(current, "next", None) or getattr(current, "p_next", None)

    return devices


def _enumerate_vlc_audio_devices_sync() -> list[tuple[str, str]]:
    instance = _create_vlc_instance()
    player = instance.media_player_new()
    devices: list[tuple[str, str]] = []

    try:
        if hasattr(player, "audio_output_device_enum"):
            with contextlib.suppress(Exception):
                devices = _collect_vlc_devices(player.audio_output_device_enum())

        if not devices and hasattr(instance, "audio_output_enumerate_devices"):
            with contextlib.suppress(Exception):
                devices = _collect_vlc_devices(instance.audio_output_enumerate_devices())
    finally:
        with contextlib.suppress(Exception):
            player.release()
        with contextlib.suppress(Exception):
            instance.release()

    deduped: list[tuple[str, str]] = []
    seen_tokens: set[str] = set()
    for token, name in devices:
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        deduped.append((token, name))

    return deduped


def _resolve_vlc_device_token(audio_device: Optional[str]) -> Optional[str]:
    if not audio_device:
        return None
    return _vlc_device_token_by_index.get(audio_device, audio_device)


def _apply_vlc_audio_device(player: Any, audio_device: Optional[str]) -> None:
    device_token = _resolve_vlc_device_token(audio_device)
    if not device_token:
        return

    attempts = (
        (None, device_token),
        ("", device_token),
        (device_token,),
    )

    for args in attempts:
        try:
            result = player.audio_output_device_set(*args)
            if result in (None, 0):
                return
        except TypeError:
            continue
        except Exception:
            continue

    raise RuntimeError("设置 VLC 输出设备失败，请先执行 /devices 重新选择，或切回 /device default。")


async def _wait_for_vlc_playback(player: Any) -> None:
    vlc_module = _get_vlc_module()

    state_enum = getattr(vlc_module, "State", None)
    sentinel = object()
    starting_states = {
        getattr(state_enum, "Opening", sentinel),
        getattr(state_enum, "Buffering", sentinel),
        getattr(state_enum, "Playing", sentinel),
        getattr(state_enum, "Paused", sentinel),
    }
    terminal_states = {
        getattr(state_enum, "Ended", sentinel),
        getattr(state_enum, "Stopped", sentinel),
        getattr(state_enum, "Error", sentinel),
    }

    for _ in range(200):
        state = player.get_state()
        if state in starting_states or state in terminal_states:
            break
        await asyncio.sleep(0.05)

    while True:
        state = player.get_state()
        if state in {
            getattr(state_enum, "Ended", sentinel),
            getattr(state_enum, "Stopped", sentinel),
        }:
            return
        if state == getattr(state_enum, "Error", sentinel):
            raise RuntimeError("VLC 播放失败。")
        await asyncio.sleep(0.1)


async def check_vlc_available() -> None:
    def _probe() -> None:
        instance = _create_vlc_instance()
        with contextlib.suppress(Exception):
            instance.release()

    await asyncio.to_thread(_probe)


async def list_audio_devices() -> list[str]:
    devices = await asyncio.to_thread(_enumerate_vlc_audio_devices_sync)

    _vlc_device_token_by_index.clear()
    rendered: list[str] = []
    for idx, (token, name) in enumerate(devices, start=1):
        key = str(idx)
        _vlc_device_token_by_index[key] = token
        rendered.append(f"{idx}: {name}")

    return rendered


async def _play_with_vlc(temp_path: Path, audio_device: Optional[str]) -> None:
    instance = _create_vlc_instance()
    player = instance.media_player_new()
    media = instance.media_new(temp_path.resolve().as_uri())
    player.set_media(media)
    _apply_vlc_audio_device(player, audio_device)

    if player.play() == -1:
        raise RuntimeError("VLC 播放失败。")

    try:
        await _wait_for_vlc_playback(player)
    finally:
        with contextlib.suppress(Exception):
            player.stop()


async def play_speech(
    text: str,
    voice: str = "zh-CN-XiaoxiaoNeural",
    audio_device: Optional[str] = None,
) -> None:
    temp_path = await _synthesize_audio_file(text, voice)

    try:
        await _play_with_vlc(temp_path, audio_device)
    finally:
        with contextlib.suppress(OSError):
            temp_path.unlink()


def _print_help() -> None:
    print("可用命令：")
    print("  /devices               列出可用输出设备")
    print("  /device list           列出可用输出设备")
    print("  /device <设备索引>      设置输出设备")
    print("  /device default        恢复系统默认输出设备")
    print("  /device                查看当前输出设备")
    print("  /help                  查看帮助")
    print("  /exit                  退出程序")


async def main() -> None:
    print("已进入待命模式：输入任意文本并回车即可播放；输入 /help 查看命令。")
    audio_device: Optional[str] = None
    available_devices: list[str] = []

    while True:
        text = ""
        lower_text = ""
        try:
            text = (await asyncio.to_thread(input, "请输入文本> ")).strip()
        except EOFError:
            print("检测到输入结束，程序退出。")
            break

        if not text:
            continue

        lower_text = text.lower()
        if lower_text in EXIT_COMMANDS:
            print("已退出。")
            break

        if lower_text in HELP_COMMANDS:
            _print_help()
            continue

        if lower_text in {"/devices", "/device list"}:
            outcome = (await asyncio.gather(list_audio_devices(), return_exceptions=True))[0]
            if isinstance(outcome, Exception):
                print(f"列出设备失败：{outcome}")
                continue

            fetched_devices = outcome
            available_devices = fetched_devices

            if not fetched_devices:
                print("未检测到可用输出设备，将使用系统默认设备。")
                continue

            print("可用设备：")
            for device in fetched_devices:
                print(f"  {device}")
            print("可用 /device <设备索引> 进行设置。")
            continue

        if lower_text.startswith("/device"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                current = audio_device if audio_device else "系统默认设备"
                print(f"当前输出设备：{current}")
                continue

            candidate = parts[1].strip()
            if not candidate:
                print("设备参数不能为空。")
                continue

            if candidate.lower() in {"default", "系统默认", "默认"}:
                audio_device = None
                print("已切换到系统默认输出设备。")
                continue

            if not available_devices:
                print("请先执行 /devices 获取设备列表后再设置。")
                continue

            if not candidate.isdigit():
                print("请输入设备索引，例如 /device 1。")
                continue

            index = int(candidate)
            if index < 1 or index > len(available_devices):
                print("设备序号超出范围，请先用 /devices 查看列表。")
                continue

            audio_device = str(index)
            print(f"已设置输出设备索引：{audio_device}")
            continue

        try:
            await play_speech(text, audio_device=audio_device)
        except RuntimeError as exc:
            print(f"播放失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"发生未预期错误：{exc}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断，程序退出。")
