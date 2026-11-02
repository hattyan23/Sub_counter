"""
登録者数の取得ループと配信サーバーを 1 プロセスで動かす

使い方:
  python server.py [--port=8765]

  API キーなどの設定は初期設定画面から行う（config.json に保存される。config.example.json が見本）

  /               カウンターの画面（OBS のブラウザソースに指定する）
  /settings       初期設定の画面（トークンが必要）
  /design         見た目の設定の画面（トークンが必要）
  /privacy        プライバシーポリシー
  /assets/<名前>  利用者が追加した素材
  /api/count      現在の値（JSON）
  /api/settings   設定の取得と保存（トークンが必要）
  /api/design     見た目の取得と保存（保存はトークンが必要）
  /api/assets     素材の一覧・追加・削除（トークンが必要）
  /events         値が変わるたびに通知（SSE）
"""
import argparse
import hmac
import http.server
import json
import logging
import logging.handlers
import pathlib
import re
import secrets
import signal
import sys
import threading
import time
import urllib.parse
import webbrowser

import config
import design
import fetcher
import tray

BASE = pathlib.Path(__file__).resolve().parent
PAGES = {
    "/": BASE / "web" / "index.html",
    "/settings": BASE / "web" / "settings.html",
    "/design": BASE / "web" / "design.html",
    "/privacy": BASE / "web" / "privacy.html",
}
WRITE_PATHS = ("/api/settings", "/api/design", "/api/assets")
LOG_PATH = config.data_dir() / "app.log"
HOST = "127.0.0.1"   # 外部に公開しない
MAX_BACKOFF_SEC = 30 * 60
HEARTBEAT_SEC = 15
MAX_BODY_BYTES = 64 * 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    # 見た目の設定の画面がカウンターをプレビューとして埋め込むので、同じサイトからだけ許可する
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                               "connect-src 'self'; img-src 'self'; media-src 'self'; font-src 'self'; "
                               "frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'",
}

log = logging.getLogger("server")
_INVALID = object()   # 本文を読めなかった（応答は送信済み）


class CounterState:
    """配信する値。取得に失敗しても、前回の正常な値を維持する"""

    def __init__(self):
        self._changed = threading.Condition()
        self._version = 0
        self._count = None
        self._status = fetcher.STARTING
        self._detail = ""
        self._updated_at = None
        self._design_rev = 0

    def apply(self, result):
        with self._changed:
            count = result.count if result.status == fetcher.OK else self._count
            if result.status == fetcher.OK:
                self._updated_at = time.time()
            if (count, result.status, result.detail) != (self._count, self._status, self._detail):
                self._count, self._status, self._detail = count, result.status, result.detail
                self._version += 1
                self._changed.notify_all()

    def clear_count(self):
        """チャンネルが変わったときに呼ぶ。別のチャンネルの数字を出し続けないようにする"""
        with self._changed:
            if self._count is not None:
                self._count = self._updated_at = None
                self._version += 1
                self._changed.notify_all()

    def design_changed(self):
        """見た目が保存されたことを、表示中のカウンターに知らせる"""
        with self._changed:
            self._design_rev += 1
            self._version += 1
            self._changed.notify_all()

    def snapshot(self):
        with self._changed:
            return self._snapshot()

    def wait_change(self, version, timeout):
        """version より新しくなるまで待ち、(version, 値) を返す。時間切れなら値は None"""
        with self._changed:
            self._changed.wait_for(lambda: self._version != version, timeout)
            return self._version, self._snapshot() if self._version != version else None

    @property
    def version(self):
        with self._changed:
            return self._version

    def _snapshot(self):
        return {"count": self._count, "status": self._status, "detail": self._detail, "updated_at": self._updated_at,
                "design_rev": self._design_rev}


def wait_next(stop, source, seconds):
    """次の取得まで待つ。seconds が None なら頼まれるまで待つ。設定の変更や再取得の依頼ですぐ戻る"""
    deadline = None if seconds is None else time.monotonic() + seconds
    while not stop.is_set() and not source.should_wake():
        left = 1 if deadline is None else deadline - time.monotonic()
        if left <= 0:
            return
        stop.wait(min(left, 1))


def fetch_loop(source, state, stop, on_config_problem=None):
    """取得を繰り返す。どんな例外が起きてもループは止めない"""
    failures = 0
    last_problem = None   # 同じ設定の問題を何度も知らせないようにする
    last_ok = False
    last_channel = None
    try:
        while not stop.is_set():
            wait = MAX_BACKOFF_SEC
            try:
                try:
                    result = source.fetch()
                except Exception as e:
                    result = fetcher.FetchResult(fetcher.ERROR, detail=type(e).__name__)
                if stop.is_set():
                    break
                if source.channel_id != last_channel:
                    if last_channel is not None:
                        state.clear_count()
                    last_channel = source.channel_id
                state.apply(result)

                if result.status == fetcher.OK:
                    failures = 0
                    last_problem = None
                    wait = source.interval
                    # 統計を長く残さないよう、数字そのものはログに出さない
                    if not last_ok:
                        log.info("取得を開始しました (%s)", result.source)
                elif result.status in fetcher.CONFIG_PROBLEMS:
                    wait = None
                    log.warning("取得を停止しました (%s): %s", result.status, result.detail)
                    problem = (result.status, result.detail)
                    if on_config_problem and problem != last_problem:
                        on_config_problem(result)
                    last_problem = problem
                elif result.retry_after is not None:
                    wait = result.retry_after
                    log.warning("取得を一時停止します (%s): %s", result.status, result.detail)
                else:
                    failures += 1
                    # 間隔が上限より長い設定でも、設定より短い間隔では再試行しない
                    wait = min(source.interval * 2 ** failures, max(MAX_BACKOFF_SEC, source.interval))
                    log.warning("取得失敗 (%s): %s。%d 秒後に再試行します", result.status, result.detail, wait)
                last_ok = result.status == fetcher.OK
                wait_next(stop, source, wait)
            except Exception as e:
                # 例外の文面には URL が含まれる可能性があるので、種類だけ記録する
                log.error("取得ループで例外: %s", type(e).__name__)
                stop.wait(MAX_BACKOFF_SEC)
    finally:
        source.close()


class Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = False   # Windows では有効にすると使用中のポートにも bind できてしまう

    def __init__(self, address, state, token=None):
        self.state = state
        self.token = token or secrets.token_urlsafe(32)
        super().__init__(address, Handler)

    def settings_url(self, page="settings"):
        return f"http://localhost:{self.server_address[1]}/{page}?token={self.token}"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if not self._host_allowed():
            self.send_error(403)
            return
        path = self.path.split("?")[0]
        if path in PAGES:
            self._send(PAGES[path].read_bytes(), "text/html; charset=utf-8")
        elif path.startswith("/assets/"):
            self._send_asset(urllib.parse.unquote(path.removeprefix("/assets/")))
        elif path == "/api/count":
            self._send_json(self.server.state.snapshot())
        elif path == "/api/design":
            self._send_json(design.load())
        elif path in ("/api/settings", "/api/assets"):
            if not self._token_valid():
                self._send_json({"error": "forbidden"}, 403)
            elif path == "/api/settings":
                self._send_json(config.summary())
            else:
                self._send_json(design.list_assets())
        elif path == "/events":
            self._stream_events()
        elif path == "/favicon.ico":
            self._send(b"", "image/x-icon", 204)
        else:
            self.send_error(404)

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if not self._write_allowed(path):
            return
        if path == "/api/assets":
            self._upload_asset(query)
            return
        body = self._read_json()
        if body is _INVALID:
            return
        if path == "/api/settings":
            error = save_settings(body)
        else:
            error = design.save(body)
        if error:
            self._send_json({"error": error}, 400)
            return
        if path == "/api/settings":
            log.info("設定を保存しました")
        else:
            self.server.state.design_changed()
        self._send_json({"ok": True})

    def do_DELETE(self):
        path, _, query = self.path.partition("?")
        if not self._write_allowed(path, ("/api/assets",)):
            return
        name = self._asset_name(query)
        if name:
            design.delete_asset(name)
            self.server.state.design_changed()
            self._send_json({"ok": True})

    def _write_allowed(self, path, paths=WRITE_PATHS):
        # 他のサイトからの書き込みを防ぐ。トークンに加えて、Host・Origin・Content-Type も確認する
        if path not in paths or not self._host_allowed() or not self._origin_allowed():
            self.send_error(403)
            return False
        if not self._token_valid():
            self._send_json({"error": "forbidden"}, 403)
            return False
        return True

    def _content_length(self, content_type, limit):
        """本文の形式と長さを確かめる。問題があれば応答を返して None"""
        if self.headers.get("Content-Type", "").split(";")[0].strip() != content_type:
            self.send_error(415)
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_error(411)
            return None
        if not 0 <= length <= limit:
            self.send_error(413)
            return None
        return length

    def _read_json(self):
        length = self._content_length("application/json", MAX_BODY_BYTES)
        if length is None:
            return _INVALID
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            self._send_json({"error": "入力を読めませんでした"}, 400)
            return _INVALID

    def _asset_name(self, query):
        name = urllib.parse.parse_qs(query).get("name", [""])[0]
        if not design.asset_kind(name):
            self._send_json({"error": "使えないファイル名です。画像（png・jpg・gif・webp など）、"
                                      "動画（webm・mp4）、フォント（ttf・otf・woff・woff2）を選んでください"}, 400)
            return None
        return name

    def _upload_asset(self, query):
        name = self._asset_name(query)
        if not name:
            return
        length = self._content_length("application/octet-stream", design.MAX_ASSET_BYTES)
        if length is None:
            return
        try:
            design.save_asset(name, self.rfile, length)
        except OSError:
            self.close_connection = True
            self._send_json({"error": "ファイルを保存できませんでした"}, 500)
            return
        self._send_json({"ok": True, "assets": design.list_assets()})

    def _send_asset(self, name):
        """素材を返す。動画の再生位置を動かせるよう、範囲の指定（Range）にも応じる"""
        path = design.asset_path(name) if design.asset_kind(name) else None
        try:
            f = open(path, "rb") if path and path.is_file() else None
        except OSError:
            f = None
        if not f:
            self.send_error(404)
            return
        with f:
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if match and match.group(1) + match.group(2):
                first, last = match.groups()
                start = int(first) if first else max(0, size - int(last))
                end = min(int(last), size - 1) if first and last else size - 1
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", design.content_type(name))
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-cache")
            for header, value in SECURITY_HEADERS.items():
                self.send_header(header, value)
            self.end_headers()
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(left, 1024 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except OSError:
                self.close_connection = True   # 再生をやめたなどでブラウザ側が閉じた

    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {f"localhost:{port}", f"127.0.0.1:{port}"}

    def _host_allowed(self):
        # DNS リバインディング対策
        return self.headers.get("Host", "").lower() in self._allowed_hosts()

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        return origin in {f"http://{host}" for host in self._allowed_hosts()}

    def _token_valid(self):
        given = self.headers.get("X-Settings-Token", "")
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    def _send(self, body, content_type, status=200):
        self.send_response(status)
        if status >= 400:
            self.send_header("Connection", "close")   # 読まずに残った本文を次のリクエストとして扱わない
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status=200):
        self._send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def _stream_events(self):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        state = self.server.state
        version = state.version
        payload = state.snapshot()
        try:
            while True:
                # 値がなければコメント行を送り、切断された接続を検出する
                frame = f"data: {json.dumps(payload)}\n\n" if payload else ": keep-alive\n\n"
                self.wfile.write(frame.encode())
                self.wfile.flush()
                version, payload = state.wait_change(version, HEARTBEAT_SEC)
        except OSError:
            pass  # ブラウザ側が閉じた

    def log_message(self, format, *args):
        # URL のトークンを残さないよう、リクエストの内容は記録しない
        pass


def save_settings(body):
    """設定画面からの入力を検証して保存する。失敗したら利用者向けのメッセージを返す"""
    if not isinstance(body, dict):
        return "入力を読めませんでした"
    if body.get("terms_accepted") is not True:
        return "利用規約とプライバシーポリシーに同意してください"
    api_key = body.get("api_key")
    if not isinstance(api_key, str):
        return "API キーを読めませんでした"
    api_key = api_key.strip() or config.stored_key()
    if not api_key:
        return "API キーを入力してください"
    channel = body.get("channel")
    parsed = config.parse_channel(channel) if isinstance(channel, str) else None
    if not parsed:
        return "チャンネルは、UC で始まる ID、@ハンドル、チャンネルの URL のどれかで入力してください"
    kind, value = parsed
    if kind == "handle":
        value, failure = fetcher.resolve_handle(api_key, value)
        if failure:
            return failure.detail
    config.save(api_key, value, True)
    return None


class ConfigProblemNotifier:
    """設定の問題を知らせる。数字を表示できているときは、配信中かもしれないので通知だけにする"""

    def __init__(self, state, icon, settings_url):
        self._state = state
        self._icon = icon
        self._settings_url = settings_url
        self._opened = False

    def __call__(self, result):
        if self._opened or self._state.snapshot()["count"] is not None:
            self._icon.notify(f"{result.detail}\nアイコンの右クリックから直せます")
            return
        self._opened = True
        webbrowser.open(self._settings_url)


def raise_interrupt(*_):
    raise KeyboardInterrupt


def setup_logging():
    """配布版にはコンソールがないのでファイルにも残す。API キーは出力しないこと"""
    config.migrate_legacy_dir()
    config.data_dir().mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def main():
    parser = argparse.ArgumentParser(description=f"{tray.TITLE}のサーバー")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    setup_logging()

    state = CounterState()
    try:
        server = Server((HOST, args.port), state)
    except OSError:
        log.error("ポート %d を開けません", args.port)
        tray.message_box(f"すでに起動している可能性があります（ポート {args.port} を開けません）。\n"
                         "画面右下のタスクトレイにアイコンがないか確認してください。")
        sys.exit(1)

    # 開発時に Ctrl+Break や Ctrl+C でも同じ終了処理を通す
    signal.signal(signal.SIGBREAK, raise_interrupt)

    stop = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    source = fetcher.Fetcher()
    base_url = f"http://localhost:{args.port}"
    icon = tray.Tray(state, f"{base_url}/", server.settings_url(), server.settings_url("design"),
                     f"{base_url}/privacy", LOG_PATH, on_retry=source.request_retry, on_exit=stop.set)
    icon.start()
    worker = threading.Thread(
        target=fetch_loop,
        args=(source, state, stop, ConfigProblemNotifier(state, icon, server.settings_url())),
        daemon=True,
    )
    worker.start()
    log.info("%s/ で待ち受けます", base_url)
    try:
        while not stop.wait(0.5):   # 短く区切らないと Ctrl+C を受け取れない
            pass
    except KeyboardInterrupt:
        pass
    finally:
        log.info("終了します")
        stop.set()
        icon.stop()
        server.shutdown()
        server.server_close()
        worker.join(timeout=fetcher.TIMEOUT_SEC + 5)


if __name__ == "__main__":
    main()
