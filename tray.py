"""タスクトレイのアイコンとメッセージ表示（コンソールなしで状況を伝える）"""
import ctypes
import os
import pathlib
import subprocess
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw

# YouTube の規約により、アプリ名に YouTube の名前を入れたり、ロゴに似たアイコンを使ったりしないこと
TITLE = "登録者数カウンター"
YOUTUBE_TERMS_URL = "https://www.youtube.com/t/terms"
GOOGLE_PRIVACY_URL = "https://policies.google.com/privacy"
MB_ICONINFORMATION = 0x40
MB_TOPMOST = 0x40000
MB_SETFOREGROUND = 0x10000
# 名前だけで起動すると、カレントフォルダーなどに置かれた同名のファイルを実行する恐れがある
CLIP_EXE = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "clip.exe"


def message_box(text):
    """最前面にメッセージを出し、閉じられるまで待つ"""
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND)


def make_image():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=12, fill=(37, 99, 235, 255))
    for x, top in ((14, 38), (28, 28), (42, 16)):
        draw.rectangle((x, top, x + 8, 50), fill=(255, 255, 255, 255))
    return image


def status_text(snapshot):
    count, detail = snapshot["count"], snapshot["detail"]
    if count is None:
        return detail or "取得中…"
    return f"登録者数: {count}（{detail}）" if detail else f"登録者数: {count}"


def _open(url):
    return lambda icon, item: webbrowser.open(url)


class Tray:
    def __init__(self, state, url, settings_url, design_url, privacy_url, log_path, on_retry, on_exit):
        self._state = state
        self._url = url
        self._settings_url = settings_url
        self._design_url = design_url
        self._privacy_url = privacy_url
        self._log_path = log_path
        self._on_retry = on_retry
        self._on_exit = on_exit
        self.icon = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._watch_state, daemon=True).start()

    def stop(self):
        if self.icon:
            self.icon.stop()

    def notify(self, text):
        if self.icon:
            self.icon.notify(text, TITLE)

    def _run(self):
        # pystray はアイコンを作ったスレッドでメッセージループを回す必要がある
        self.icon = pystray.Icon("SubCounter", make_image(), TITLE, pystray.Menu(
            pystray.MenuItem(lambda _: status_text(self._state.snapshot()), None, enabled=False),
            pystray.MenuItem("OBS 用の URL をコピー", self._copy_url, default=True),
            pystray.MenuItem("見た目を変える", _open(self._design_url)),
            pystray.MenuItem("設定を開く", _open(self._settings_url)),
            pystray.MenuItem("今すぐ取得し直す", lambda icon, item: self._on_retry()),
            pystray.MenuItem("ログを開く", self._open_log),
            pystray.MenuItem("利用規約とプライバシー", pystray.Menu(
                pystray.MenuItem("YouTube 利用規約", _open(YOUTUBE_TERMS_URL)),
                pystray.MenuItem("Google プライバシー ポリシー", _open(GOOGLE_PRIVACY_URL)),
                pystray.MenuItem("このソフトのプライバシーポリシー", _open(self._privacy_url)),
            )),
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
                self.icon.title = f"{TITLE}\n{status_text(payload)}"[:127]
                self.icon.update_menu()

    def _copy_url(self, icon, item):
        subprocess.run([CLIP_EXE], input=self._url.encode("ascii"), creationflags=subprocess.CREATE_NO_WINDOW)
        icon.notify(f"{self._url} をコピーしました", TITLE)

    def _open_log(self, icon, item):
        os.startfile(self._log_path)

    def _exit(self, icon, item):
        self._on_exit()
