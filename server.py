"""
登録者数の取得ループと配信サーバーを 1 プロセスで動かす

使い方:
  python server.py [--port=8765] [--interval=60]

  /            確認用の画面
  /api/count   現在の値（JSON）
  /events      値が変わるたびに通知（SSE）
"""
import argparse
import http.server
import json
import logging
import logging.handlers
import pathlib
import signal
import sys
import threading
import time

import fetcher
import tray

BASE = pathlib.Path(__file__).resolve().parent
INDEX_HTML = BASE / "web" / "index.html"
LOG_PATH = fetcher.data_dir() / "app.log"
HOST = "127.0.0.1"   # 外部に公開しない
MIN_INTERVAL_SEC = 60   # YouTube への負荷を抑えるため、これより短くしない
HEARTBEAT_SEC = 15

log = logging.getLogger("server")


class CounterState:
    """配信する値。取得に失敗しても、前回の正常な値を維持する"""

    def __init__(self):
        self._changed = threading.Condition()
        self._version = 0
        self._count = None
        self._status = "starting"
        self._updated_at = None

    def apply(self, result):
        with self._changed:
            count = result.count if result.status == fetcher.OK else self._count
            if result.status == fetcher.OK:
                self._updated_at = time.time()
            if (count, result.status) != (self._count, self._status):
                self._count, self._status = count, result.status
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
        return {"count": self._count, "status": self._status, "updated_at": self._updated_at}


def fetch_loop(source, state, stop, interval, login=None):
    """取得を繰り返す。どんな例外が起きてもループは止めない"""
    interval = max(interval, MIN_INTERVAL_SEC)
    login_offered = False   # ログインせずに閉じられたとき、Chrome を開き続けないようにする
    try:
        while not stop.is_set():
            try:
                result = source.fetch()
            except Exception as e:
                result = fetcher.FetchResult(fetcher.ERROR, detail=type(e).__name__)
            if not stop.is_set():
                state.apply(result)
            if result.status == fetcher.OK:
                login_offered = False
                log.info("取得: %d (%s)", result.count, result.source)
            elif result.status == fetcher.AUTH_EXPIRED:
                log.warning("ログインが必要です: %s", result.detail)
                if login and not login_offered:
                    login_offered = True
                    source.close()   # 同じプロファイルは同時に開けない
                    login(stop)
                    continue
                log.warning("ログインできるまで再試行します。Chrome を開き直すにはアプリを再起動してください")
            else:
                log.warning("取得失敗 (%s): %s", result.status, result.detail)
            stop.wait(interval)
    finally:
        source.close()


class Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = False   # Windows では有効にすると使用中のポートにも bind できてしまう

    def __init__(self, address, state):
        self.state = state
        super().__init__(address, Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/count":
            self._send(json.dumps(self.server.state.snapshot()).encode(), "application/json")
        elif path == "/events":
            self._stream_events()
        else:
            self.send_error(404)

    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream_events(self):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
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
        log.debug("%s %s", self.address_string(), format % args)


def raise_interrupt(*_):
    raise KeyboardInterrupt


def request_login(cancel):
    if not fetcher.find_chrome():
        tray.message_box("Google Chrome が見つかりません。\nインストールしてから、もう一度起動してください。", wait=False)
        return
    tray.message_box("Chrome を開きます。\n\n1. Google にログインしてください\n"
                     "2. YouTube Studio が表示されたら、Chrome を閉じてください", wait=False)
    fetcher.login(cancel)


def setup_logging():
    """配布版にはコンソールがないのでファイルにも残す。認証情報は出力しないこと"""
    fetcher.data_dir().mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def main():
    parser =argparse.ArgumentParser(description="YouTube 登録者数カウンターのサーバー")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=int, default=MIN_INTERVAL_SEC,
                        help=f"取得の待機秒数（{MIN_INTERVAL_SEC} 未満は {MIN_INTERVAL_SEC} として扱う）")
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
    worker = threading.Thread(
        target=fetch_loop, args=(fetcher.Fetcher(cancel=stop), state, stop, args.interval, request_login),
        daemon=True,
    )
    worker.start()
    icon = tray.Tray(state, f"http://localhost:{args.port}/", LOG_PATH, on_exit=stop.set)
    icon.start()
    log.info("http://localhost:%d/ で待ち受けます", args.port)
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
        worker.join(timeout=fetcher.GOTO_TIMEOUT_MS / 1000 + 5)


if __name__ == "__main__":
    main()
