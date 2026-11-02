"""
YouTube Studio から登録者数を取得するモジュール

Studio の画面構造や通信内容が変わったときに直す場所は、このファイルだけに閉じ込める。
例外は外に出さず、すべて FetchResult として返す。

Playwright の同期 API はスレッドをまたげないので、
Fetcher のメソッドはすべて同じスレッドから呼ぶこと。
"""
import dataclasses
import logging
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE = pathlib.Path(__file__).resolve().parent


def data_dir():
    """ログイン状態の保存先。配布版は exe の場所によらないようユーザーごとのフォルダに置く"""
    override = os.environ.get("YT_COUNTER_DATA_DIR")
    if override:
        return pathlib.Path(override)
    if getattr(sys, "frozen", False):
        return pathlib.Path(os.environ["LOCALAPPDATA"]) / "YouTubeSubCounter"
    return BASE


PROFILE = data_dir() / "chrome_profile"   # ログイン状態はここに保存（Cookie は Windows 側で暗号化される）
STUDIO = "https://studio.youtube.com/"
CHROME_CANDIDATES = [
    pathlib.Path(os.environ.get(env, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if os.environ.get(env)
]

CHANNELS_API = "/youtubei/v1/creator/get_creator_channels"
DOM_LABEL = "現在のチャンネル登録者数"
# 画像・フォントは読み込まない。page.route は HTTP キャッシュを無効にしてしまい、
# 読み込みごとに約 7MB かかる（CDP なら 2 回目以降は約 200KB）ので使わない。
BLOCKED_URLS = ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.svg", "*.woff", "*.woff2", "*.ttf",
                "*i.ytimg.com/*", "*yt3.ggpht.com/*", "*yt3.googleusercontent.com/*", "*fonts.gstatic.com/*"]

GOTO_TIMEOUT_MS = 30000
# DOM は約 2 秒、内部通信は 11〜14 秒で届く。内部通信を優先するため、DOM が出ていてもここまでは待つ
NET_WAIT_SEC = 20
FETCH_TIMEOUT_SEC = 45
POLL_MS = 500

# 状態
OK = "ok"
AUTH_EXPIRED = "auth_expired"   # 再ログインが必要
NO_VALUE = "no_value"           # ページは開けたが値が見つからない
ERROR = "error"                 # 読み込み失敗やブラウザの異常
STARTING = "starting"           # 取得の完了待ち
LOGIN_WAITING = "login_waiting" # ログイン用 Chrome が閉じられるのを待っている

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class FetchResult:
    status: str
    count: int | None = None
    source: str | None = None   # "net" または "dom"
    detail: str = ""


def parse_count(value):
    """丸めのない登録者数だけを int にする。「1.2万」のような表記は None"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d+|\d{1,3}(,\d{3})+", value.strip()):
        return int(value.strip().replace(",", ""))
    return None


def count_from_response(data):
    """get_creator_channels のレスポンスから自分のチャンネルの登録者数を取り出す"""
    try:
        return parse_count(data["channels"][0]["metric"]["subscriberCount"])
    except (KeyError, IndexError, TypeError):
        return None


def count_from_text(text):
    """ページ本文から、見出し行の次の行にある登録者数を取り出す"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for label, value in zip(lines, lines[1:]):
        if label == DOM_LABEL:
            return parse_count(value)
    return None


def find_chrome():
    return next((p for p in CHROME_CANDIDATES if p.exists()), None)


def _login_command():
    return [find_chrome(), f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check", STUDIO]


def focus_login():
    """すでに開いているログイン用 Chrome を前面に出す（同じプロファイルで起動すると既存の窓が反応する）"""
    subprocess.Popen(_login_command())


def login(cancel):
    """素の Chrome を開き、閉じられるまで待つ。
    Playwright を介さないのは、自動化ブラウザからの Google ログインが拒否されやすいため"""
    PROFILE.mkdir(parents=True, exist_ok=True)
    log.warning("ログイン用の Chrome を開きます")
    proc = subprocess.Popen(_login_command())
    while proc.poll() is None:
        if cancel.wait(1):
            proc.terminate()
            return


class Fetcher:
    def __init__(self, cancel=None):
        self._cancel = cancel or threading.Event()
        self._playwright = None
        self._ctx = None
        self._page = None
        self._user_agent = None
        self._responses = []

    def fetch(self):
        """1 回取得する。内部通信 → DOM の順に値を決める。例外は投げない"""
        if not PROFILE.exists():
            return FetchResult(AUTH_EXPIRED, detail="プロファイルがありません")
        try:
            self._ensure_browser()
            return self._load_and_read()
        except PlaywrightTimeoutError as e:
            return FetchResult(ERROR, detail=type(e).__name__)
        except Exception as e:
            # ブラウザが落ちた可能性があるので、次回は起動し直す
            self.close()
            return FetchResult(ERROR, detail=type(e).__name__)

    def close(self):
        for closer in (self._ctx and self._ctx.close, self._playwright and self._playwright.stop):
            if closer:
                try:
                    closer()
                except Exception as e:
                    log.debug("終了処理で例外: %s", type(e).__name__)
        self._ctx = self._page = self._playwright = None

    def _ensure_browser(self):
        if self._page:
            return
        if not self._playwright:
            self._playwright = sync_playwright().start()
        chromium = self._playwright.chromium
        opts = dict(channel="chrome", headless=True)
        if not self._user_agent:
            # ヘッドレス Chrome は UA に "HeadlessChrome" が入り、Studio に非対応ブラウザ扱いされる。
            # 一度起動して UA を読み、"Chrome" に置き換えて起動し直す。
            tmp = chromium.launch_persistent_context(str(PROFILE), **opts)
            try:
                page = tmp.pages[0] if tmp.pages else tmp.new_page()
                self._user_agent = page.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            finally:
                tmp.close()
        self._ctx = chromium.launch_persistent_context(str(PROFILE), user_agent=self._user_agent, **opts)
        self._ctx.on("response", self._on_response)
        self._page = self._ctx.new_page()
        cdp = self._ctx.new_cdp_session(self._page)
        cdp.send("Network.enable")
        cdp.send("Network.setBlockedURLs", {"urls": BLOCKED_URLS})

    def _on_response(self, res):
        # ここでは本文を読まず、待機ループ側でまとめて読む
        if CHANNELS_API in res.url:
            self._responses.append(res)

    def _load_and_read(self):
        self._responses.clear()
        self._page.goto(STUDIO, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)

        start = time.monotonic()
        while time.monotonic() - start < FETCH_TIMEOUT_SEC and not self._cancel.is_set():
            if "accounts.google.com" in self._page.url:
                return FetchResult(AUTH_EXPIRED, detail="ログイン画面に転送されました")
            count = self._read_responses()
            if count is not None:
                return FetchResult(OK, count, "net")
            count = self._read_dom()
            if count is not None and time.monotonic() - start >= NET_WAIT_SEC:
                return FetchResult(OK, count, "dom")
            self._page.wait_for_timeout(POLL_MS)
        return FetchResult(NO_VALUE, detail="時間内に値が見つかりませんでした")

    def _read_responses(self):
        pending, self._responses = self._responses, []
        for res in pending:
            try:
                count = count_from_response(res.json())
            except (PlaywrightError, ValueError):
                continue  # OPTIONS やエラー応答など、JSON でないものは飛ばす
            if count is not None:
                return count
        return None

    def _read_dom(self):
        try:
            return count_from_text(self._page.evaluate("document.body ? document.body.innerText : ''"))
        except PlaywrightError:
            if self._page.is_closed():
                raise
            return None  # 転送中でページの文脈が入れ替わっただけ
