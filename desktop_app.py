import asyncio
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Any, Callable, Coroutine, Optional
import re

from tts import check_vlc_available, list_audio_devices, play_speech

FONT_FAMILY = "Microsoft YaHei"
FONT_SIZE = 10


class AsyncWorker:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(
        self,
        coro: Coroutine[Any, Any, Any],
        on_done: Callable[[object, Optional[BaseException]], None],
    ) -> None:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _finish(done_future: Any) -> None:
            try:
                result = done_future.result()
                on_done(result, None)
            except BaseException as exc:  # noqa: BLE001
                on_done(None, exc)

        future.add_done_callback(_finish)

    async def _shutdown(self) -> None:
        self._loop.stop()

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        self._thread.join(timeout=1)


class SpeakerHelperApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("SpeakerHelper")
        self.root.geometry("820x600")

        self._apply_fonts()

        self.worker = AsyncWorker()
        self.devices: list[str] = []
        self.is_playing = False
        self._vlc_startup_unavailable = False
        self._vlc_checking = False
        self._vlc_startup_alert_shown = False

        self.voice_var = tk.StringVar(value="zh-CN-XiaoxiaoNeural")
        self.device_var = tk.StringVar(value="系统默认设备")
        self.status_var = tk.StringVar(value="待命中。")

        self._build_ui()
        self._bind_shortcuts()
        self._probe_vlc_on_startup()

    def _friendly_runtime_error(self, error: BaseException) -> str:
        raw = str(error)
        lowered = raw.lower()
        if "vlc" in lowered or "libvlc" in lowered or "winerror 193" in lowered:
            return (
                f"{raw}。请确认已安装与当前 Python/程序位数一致的 VLC，"
                "并可尝试设置环境变量 VLC_DIR 指向 VLC 安装目录。"
            )
        return raw

    def _apply_fonts(self) -> None:
        # Set Tk named fonts so ttk and tk widgets share the same Chinese-friendly font.
        for font_name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkHeadingFont",
            "TkCaptionFont",
            "TkSmallCaptionFont",
            "TkTooltipFont",
            "TkIconFont",
            "TkFixedFont",
        ):
            try:
                named_font = tkfont.nametofont(font_name)
                named_font.configure(family=FONT_FAMILY, size=FONT_SIZE)
            except tk.TclError:
                continue

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill="both", expand=True)

        top_bar = ttk.Frame(container)
        top_bar.pack(fill="x", pady=(0, 8))

        ttk.Label(top_bar, text="输出设备").pack(side="left")
        self.device_combo = ttk.Combobox(
            top_bar,
            textvariable=self.device_var,
            state="readonly",
            values=["系统默认设备"],
            width=34,
        )
        self.device_combo.pack(side="left", padx=(8, 8))

        self.refresh_button = ttk.Button(top_bar, text="刷新设备", command=self._refresh_devices)
        self.refresh_button.pack(side="left", padx=(0, 6))

        self.retry_vlc_button = ttk.Button(top_bar, text="重试VLC", command=self._probe_vlc_on_startup)
        self.retry_vlc_button.pack(side="left", padx=(0, 12))

        ttk.Label(top_bar, text="语音").pack(side="left")
        self.voice_entry = ttk.Entry(top_bar, textvariable=self.voice_var)
        self.voice_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

        chat_frame = ttk.Frame(container)
        chat_frame.pack(fill="both", expand=True)

        self.chat_log = tk.Text(
            chat_frame,
            wrap="word",
            state="disabled",
            bg="#f4f6fb",
            relief="flat",
            font=(FONT_FAMILY, FONT_SIZE),
        )
        self.chat_log.pack(side="left", fill="both", expand=True)

        chat_scroll = ttk.Scrollbar(chat_frame, orient="vertical", command=self.chat_log.yview)
        chat_scroll.pack(side="right", fill="y")
        self.chat_log.configure(yscrollcommand=chat_scroll.set)

        # Chat-like alignment and bubble visual style.
        self.chat_log.tag_configure("meta_user", foreground="#6b7280", justify="right", spacing1=6)
        self.chat_log.tag_configure("meta_system", foreground="#6b7280", justify="left", spacing1=6)
        self.chat_log.tag_configure(
            "bubble_user",
            foreground="#ffffff",
            background="#2563eb",
            justify="right",
            lmargin1=220,
            lmargin2=220,
            rmargin=12,
            spacing3=10,
        )
        self.chat_log.tag_configure(
            "bubble_system",
            foreground="#0f172a",
            background="#e2e8f0",
            justify="left",
            lmargin1=12,
            lmargin2=12,
            rmargin=220,
            spacing3=10,
        )

        compose_frame = ttk.Frame(container)
        compose_frame.pack(fill="x", pady=(8, 0))

        self.text_input = tk.Text(compose_frame, height=4, wrap="word", font=(FONT_FAMILY, FONT_SIZE))
        self.text_input.pack(side="left", fill="x", expand=True)

        self.send_button = ttk.Button(compose_frame, text="发送", command=self._play)
        self.send_button.pack(side="left", padx=(8, 0))

        status = ttk.Label(container, textvariable=self.status_var)
        status.pack(anchor="w", pady=(8, 0))

        self._append_system_message("欢迎使用 SpeakerHelper。Enter 发送播放，Ctrl+Enter 换行。")
        self.text_input.focus_set()

    def _bind_shortcuts(self) -> None:
        self.text_input.bind("<Return>", self._on_enter_send)
        self.text_input.bind("<Control-Return>", self._on_ctrl_enter_newline)

    def _on_enter_send(self, _event: tk.Event) -> str:
        self._play()
        return "break"

    def _on_ctrl_enter_newline(self, _event: tk.Event) -> str:
        self.text_input.insert(tk.INSERT, "\n")
        return "break"

    def _append_message(self, speaker: str, text: str, tag: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.chat_log.configure(state="normal")

        meta_tag = "meta_user" if tag == "bubble_user" else "meta_system"
        self.chat_log.insert(tk.END, f"{speaker} {timestamp}\n", meta_tag)
        self.chat_log.insert(tk.END, f"  {text}\n", tag)
        self.chat_log.insert(tk.END, "\n")

        self.chat_log.configure(state="disabled")
        self.chat_log.see(tk.END)

    def _append_user_message(self, text: str) -> None:
        self._append_message("你", text, "bubble_user")

    def _append_system_message(self, text: str) -> None:
        self._append_message("系统", text, "bubble_system")

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _current_audio_device(self) -> Optional[str]:
        selected = self.device_var.get()
        if selected == "系统默认设备":
            return None

        match = re.match(r"\s*(\d+)\s*:", selected)
        if match:
            return match.group(1)

        # Backward compatibility if user manually types an index-like value.
        if selected.isdigit():
            return selected

        return None

    def _set_input_controls_enabled(self, enabled: bool) -> None:
        if enabled:
            self.text_input.configure(state="normal")
        else:
            self.text_input.configure(state="disabled")
        self.send_button.configure(state=tk.NORMAL if enabled and not self.is_playing else tk.DISABLED)
        self.voice_entry.configure(state="normal" if enabled else "disabled")
        self.device_combo.configure(state="readonly" if enabled else "disabled")
        if enabled:
            self.text_input.focus_set()

    def _probe_vlc_on_startup(self) -> None:
        if self._vlc_checking:
            return

        self._vlc_checking = True
        self._set_input_controls_enabled(False)
        self.refresh_button.configure(state=tk.DISABLED)
        self.retry_vlc_button.configure(state=tk.DISABLED)
        self._set_status("正在检查 VLC 运行环境...")

        def on_done(_result: object, error: Optional[BaseException]) -> None:
            self.root.after(0, self._after_probe_vlc, error)

        self.worker.submit(check_vlc_available(), on_done)

    def _after_probe_vlc(self, error: Optional[BaseException]) -> None:
        self._vlc_checking = False
        self.retry_vlc_button.configure(state=tk.NORMAL)

        if error is not None:
            self._vlc_startup_unavailable = True
            self._set_input_controls_enabled(False)
            self.refresh_button.configure(state=tk.NORMAL)
            detail = self._friendly_runtime_error(error)
            msg = f"VLC 初始化失败: {detail}"
            self._set_status(msg)
            self._append_system_message(msg)
            if not self._vlc_startup_alert_shown:
                self._vlc_startup_alert_shown = True
                messagebox.showwarning("VLC 初始化失败", msg)
            return

        recovered = self._vlc_startup_unavailable
        self._vlc_startup_unavailable = False
        self._vlc_startup_alert_shown = False
        self._set_input_controls_enabled(True)
        if recovered:
            self._append_system_message("VLC 已恢复，可正常播放。")
        self._set_status("VLC 可用，正在获取设备列表...")
        self._refresh_devices()

    def _refresh_devices(self) -> None:
        if self._vlc_checking:
            self._set_status("正在检查 VLC，请稍候...")
            return

        if self._vlc_startup_unavailable:
            self._set_status("VLC 不可用，已跳过设备刷新。")
            return

        self.refresh_button.configure(state=tk.DISABLED)
        self._set_status("正在获取设备列表...")

        def on_done(result: object, error: Optional[BaseException]) -> None:
            self.root.after(0, self._after_refresh, result, error)

        self.worker.submit(list_audio_devices(), on_done)

    def _after_refresh(self, result: object, error: Optional[BaseException]) -> None:
        self.refresh_button.configure(state=tk.NORMAL)
        if error is not None:
            detail = self._friendly_runtime_error(error)
            self._set_status(f"获取设备失败: {detail}")
            self._append_system_message(f"获取设备失败: {detail}")
            return

        self.devices = list(result or [])
        values = ["系统默认设备", *self.devices]
        self.device_combo.configure(values=values)

        current = self.device_var.get()
        if current not in values:
            self.device_var.set("系统默认设备")

        if self.devices:
            msg = f"设备列表已更新，共 {len(self.devices)} 个（按索引选择输出设备）。"
        else:
            msg = "未检测到可用设备，将使用系统默认设备。"
        self._set_status(msg)
        self._append_system_message(msg)

    def _play(self) -> None:
        if self._vlc_checking:
            self._set_status("正在检查 VLC，请稍候...")
            return

        if self._vlc_startup_unavailable:
            self._set_status("VLC 不可用，请先修复 VLC 环境后再播放。")
            return

        text = self.text_input.get("1.0", tk.END).strip()
        voice = self.voice_var.get().strip() or "zh-CN-XiaoxiaoNeural"
        audio_device = self._current_audio_device()

        if not text:
            self._set_status("请输入要播放的文本。")
            return

        self.text_input.delete("1.0", tk.END)
        self._append_user_message(text)

        self.is_playing = True
        self.send_button.configure(state=tk.DISABLED)
        self._set_status("正在播放...")
        self._append_system_message("正在播放...")

        def on_done(_result: object, error: Optional[BaseException]) -> None:
            self.root.after(0, self._after_play, error)

        self.worker.submit(play_speech(text=text, voice=voice, audio_device=audio_device), on_done)

    def _after_play(self, error: Optional[BaseException]) -> None:
        self.is_playing = False
        self._set_input_controls_enabled(not self._vlc_startup_unavailable)
        if error is not None:
            detail = self._friendly_runtime_error(error)
            msg = f"播放失败: {detail}"
            self._set_status(msg)
            self._append_system_message(msg)
            return

        self._set_status("播放完成，继续待命。")
        self._append_system_message("播放完成。")

    def on_close(self) -> None:
        self.worker.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = SpeakerHelperApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()

