"""
登録者数の取得ループと配信サーバーを 1 プロセスで動かす

使い方:
  python server.py [--port=8765]

  API キーなどの設定は config.json に書く（config.example.json が見本）

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

import config
import fetcher
import tray

BASE = pathlib.Path(__file__).resolve().parent
INDEX_HTML = BASE / "web" / "index.html"
LOG_PATH = config.data_dir() / "app.log"
HOST = "127.0.0.1"   # 外部に公開しない
MAX_BACKOFF_SEC = 30 * 60
HEARTBEAT_SEC = 15

log = logging.getLogger("server")


class CounterState:
    """配信する値。取得に失敗しても、前回の正常な値を維持する"""

    def __init__(self):
        self._changed = threading.Condition()
        self._version = 0
        self._count = None
        self._status = fetcher.STARTING
        self._detail = ""
        self._updated_at = None

    def apply(self, result):
        with self._changed:
            count = result.count if result.status == fetcher.OK else self._count
            if result.status == fetcher.OK:
                self._updated_at = time.time()
            if (count, result.status, result.detail) != (self._count, self._status, self._detail):
                self._count, self._status, self._detail = count, result.status, result.detail
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
        return {"count": self._count, "status": self._status, "detail": self._detail, "updated_at": self._updated_at}


def wait_next(stop, source, seconds):
    """次の取得まで待つ。seconds が None なら設定が変わるまで待つ。設定が変わったらすぐ戻る"""
    deadline = None if seconds is None else time.monotonic() + seconds
    while not stop.is_set() and not source.config_changed():
        left = 1 if deadline is None else deadline - time.monotonic()
        if left <= 0:
            return
        stop.wait(min(left, 1))


def fetch_loop(source, state, stop, on_config_problem=None):
    """取得を繰り返す。どんな例外が起きてもループは止めない"""
    failures = 0
    last_problem = None   # 同じ設定の問題を何度も知らせないようにする
    try:
        while not stop.is_set():
            try:
                result = source.fetch()
            except Exception as e:
                result = fetcher.FetchResult(fetcher.ERROR, detail=type(e).__name__)
            if stop.is_set():
                break
            state.apply(result)

            if result.status == fetcher.OK:
                failures = 0
                last_problem = None
                wait = source.interval
                log.info("取得: %d (%s)", result.count, result.source)
            elif result.status in fetcher.CONFIG_PROBLEMS:
                wait = None
                log.warning("取得を停止しました (%s): %s", result.status, result.detail)
                if on_config_problem and result.status != last_problem:
                    on_config_problem(result)
                last_problem = result.status
            elif result.retry_after is not None:
                wait = result.retry_after
                log.warning("取得を一時停止します (%s): %s", result.status, result.detail)
            else:
                failures += 1
                # 間隔が上限より長い設定でも、設定より短い間隔では再試行しない
                wait = min(source.interval * 2 ** failures, max(MAX_BACKOFF_SEC, source.interval))
                log.warning("取得失敗 (%s): %s。%d 秒後に再試行します", result.status, result.detail, wait)
            wait_next(stop, source, wait)
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


class ConfigProblemNotifier:
    """設定の問題を知らせる。数字を表示できているときは、配信中かもしれないので通知だけにする"""

    def __init__(self, state, icon, stop):
        self._state = state
        self._icon = icon
        self._stop = stop
        self._prompted = False

    def __call__(self, result):
        if self._prompted or self._state.snapshot()["count"] is not None:
            self._icon.notify(f"{result.detail}\n設定を直して保存すると、自動で再開します")
            return
        self._prompted = True
        tray.message_box_until(f"{result.detail}\n\n「OK」を押すと設定ファイルを開きます。\n"
                               "設定を直して保存すると、自動で再開します。", self._stop)
        if not self._stop.is_set():
            config.open_in_editor()


def setup_logging():
    """配布版にはコンソールがないのでファイルにも残す。API キーは出力しないこと"""
    config.data_dir().mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def main():
    parser =argparse.ArgumentParser(description="YouTube 登録者数カウンターのサーバー")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    setup_logging()
    config.ensure_file()

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
    icon = tray.Tray(state, f"http://localhost:{args.port}/", LOG_PATH, on_exit=stop.set)
    icon.start()
    worker = threading.Thread(
        target=fetch_loop,
        args=(fetcher.Fetcher(), state, stop, ConfigProblemNotifier(state, icon, stop)),
        daemon=True,
    )
    worker.start()
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
        worker.join(timeout=fetcher.TIMEOUT_SEC + 5)


if __name__ == "__main__":
    main()
