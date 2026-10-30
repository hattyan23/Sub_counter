"""タスクトレイのアイコンとメッセージ表示（コンソールなしで状況を伝える）"""
import ctypes
import logging
import os
import subprocess
import threading

import pystray
from PIL import Image, ImageDraw

import fetcher

TITLE = "YouTube 登録者数カウンター"
MB_ICONINFORMATION = 0x40
MB_TOPMOST = 0x40000
MB_SETFOREGROUND = 0x10000

log = logging.getLogger(__name__)


def message_box(text, wait=True):
    """最前面にメッセージを出す。wait=False なら閉じられるのを待たない"""
    def show():
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND)
    if wait:
        show()
    else:
        threading.Thread(target=show, daemon=True).start()


def make_image():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 12, 62, 52), radius=12, fill=(220, 30, 30, 255))
    draw.polygon([(26, 22), (26, 42), (44, 32)], fill=(255, 255, 255, 255))
    return image


def status_text(snapshot):
    if snapshot["status"] == fetcher.AUTH_EXPIRED:
        return "ログインが必要です"
    if snapshot["count"] is None:
        return "取得中…"
    return f"登録者数: {snapshot['count']}"


class Tray:
    def __init__(self, state, url, log_path, on_exit):
        self._state = state
        self._url = url
        self._log_path = log_path
        self._on_exit = on_exit
        self.icon = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._watch_state, daemon=True).start()

    def stop(self):
        if self.icon:
            self.icon.stop()

    def _run(self):
        # pystray はアイコンを作ったスレッドでメッセージループを回す必要がある
        self.icon = pystray.Icon("YouTubeSubCounter", make_image(), TITLE, pystray.Menu(
            pystray.MenuItem(lambda _: status_text(self._state.snapshot()), None, enabled=False),
            pystray.MenuItem("OBS 用の URL をコピー", self._copy_url, default=True),
            pystray.MenuItem("ログを開く", self._open_log),
            pystray.MenuItem("終了", self._exit),
        ))
        self.icon.run(self._on_ready)

    def _on_ready(self, icon):
        icon.visible = True
        icon.notify(f"起動しました。OBS のブラウザソースに {self._url} を指定してください", TITLE)

    def _watch_state(self):
        version = self._state.version
        while True:
            version, payload = self._state.wait_change(version, 60)
            if payload and self.icon:
                self.icon.title = f"{TITLE}\n{status_text(payload)}"
                self.icon.update_menu()

    def _copy_url(self, icon, item):
        subprocess.run("clip", input=self._url.encode("ascii"), creationflags=subprocess.CREATE_NO_WINDOW)
        icon.notify(f"{self._url} をコピーしました", TITLE)

    def _open_log(self, icon, item):
        os.startfile(self._log_path)

    def _exit(self, icon, item):
        self._on_exit()
