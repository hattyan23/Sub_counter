"""YouTube にアクセスせずに、取得・値の決定・配信を確認するテスト（python -m unittest）"""
import datetime
import http.client
import http.server
import json
import pathlib
import tempfile
import threading
import time
import unittest
import zoneinfo
from unittest import mock

import config
import fetcher
from fetcher import FetchResult
import server

CHANNEL_ID = "UCn-3LMkbtJsTJ5aHyiRJntA"
SECRET_KEY = "SECRET-TEST-KEY-123"


def api_error(reason, code=403):
    return code, {"error": {"code": code, "errors": [{"reason": reason}]}}


class TempConfig(unittest.TestCase):
    """一時フォルダの config.json を使うテストの土台"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = pathlib.Path(folder.name) / "config.json"
        patcher = mock.patch.object(config, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, **values):
        data = {"api_key": SECRET_KEY, "channel_id": CHANNEL_ID, "interval_sec": 60} | values
        self.path.write_text(json.dumps(data), encoding="utf-8")


class ConfigTest(TempConfig):
    def test_valid(self):
        self.write(interval_sec=30)
        cfg = config.load()
        self.assertEqual((cfg.api_key, cfg.channel_id, cfg.interval_sec), (SECRET_KEY, CHANNEL_ID, 30))
        self.assertNotIn(SECRET_KEY, repr(cfg))

    def test_invalid_values(self):
        for values in ({"api_key": ""}, {"api_key": 1}, {"channel_id": ""}, {"channel_id": "UCshort"},
                       {"channel_id": "AB" + "x" * 22}, {"interval_sec": 29}, {"interval_sec": "60"},
                       {"interval_sec": True}):
            self.write(**values)
            with self.assertRaises(config.ConfigError, msg=values) as caught:
                config.load()
            self.assertNotIn(SECRET_KEY, str(caught.exception))

    def test_quota_guard(self):
        self.write()
        with mock.patch.object(config, "MIN_INTERVAL_SEC", 1), mock.patch.object(config, "DAILY_QUOTA_UNITS", 1000):
            self.write(interval_sec=107)   # 1 日 807 ユニット > 800
            with self.assertRaises(config.ConfigError):
                config.load()
            self.write(interval_sec=108)   # 1 日 800 ユニット
            self.assertEqual(config.load().interval_sec, 108)

    def test_missing_and_broken_file(self):
        with self.assertRaisesRegex(config.ConfigError, "ありません"):
            config.load()
        self.path.write_text('{"api_key": "%s",\n oops}' % SECRET_KEY, encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, "2 行目") as caught:
            config.load()
        self.assertNotIn(SECRET_KEY, str(caught.exception))

    def test_bom_from_editor(self):
        self.path.write_text(json.dumps({"api_key": SECRET_KEY, "channel_id": CHANNEL_ID}), encoding="utf-8-sig")
        self.assertEqual(config.load().interval_sec, config.DEFAULT_INTERVAL_SEC)

    def test_example_is_empty_and_copied(self):
        config.ensure_file()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((data["api_key"], data["channel_id"]), ("", ""))
        self.path.write_text("keep", encoding="utf-8")
        config.ensure_file()   # 既存の設定は上書きしない
        self.assertEqual(self.path.read_text(encoding="utf-8"), "keep")


class QuotaResetTest(unittest.TestCase):
    """2026 年の米国の夏時間は 11/1 2:00 に終わる"""

    def check(self, now_utc, expected_jst, expected_hours):
        now = datetime.datetime.fromisoformat(now_utc).replace(tzinfo=datetime.timezone.utc)
        reset = fetcher.next_quota_reset(now)
        jst = zoneinfo.ZoneInfo("Asia/Tokyo")
        self.assertEqual(reset.astimezone(jst).strftime("%m/%d %H:%M"), expected_jst)
        self.assertAlmostEqual(fetcher.seconds_until(reset, now) / 3600, expected_hours)

    def test_summer_time_is_16_jst(self):
        self.check("2026-07-01T12:00:00", "07/02 16:00", 19)

    def test_winter_time_is_17_jst(self):
        self.check("2026-12-01T12:00:00", "12/02 17:00", 20)

    def test_day_with_clock_change_has_25_hours(self):
        # 太平洋時間 11/1 0:30（夏時間）→ 11/2 0:00（標準時）は 24.5 時間後
        self.check("2026-11-01T07:30:00", "11/02 17:00", 24.5)

    def test_just_before_midnight(self):
        self.check("2026-07-02T06:59:00", "07/02 16:00", 1 / 60)


class FakeApi(http.server.BaseHTTPRequestHandler):
    responses = []
    requests = []

    def do_GET(self):
        type(self).requests.append(self.path)
        code, body = type(self).responses.pop(0)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


class FetcherTest(TempConfig):
    def setUp(self):
        super().setUp()
        self.write()
        FakeApi.responses, FakeApi.requests = [], []
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        patcher = mock.patch.object(fetcher, "API_URL", f"http://127.0.0.1:{httpd.server_address[1]}/channels")
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, code, body):
        FakeApi.responses.append((code, body))
        return fetcher.Fetcher().fetch()

    def test_success_and_request_parameters(self):
        result = self.fetch(200, {"items": [{"statistics": {"subscriberCount": "680"}}]})
        self.assertEqual((result.status, result.count, result.source), (fetcher.OK, 680, "api"))
        path = FakeApi.requests[0]
        for part in ("part=statistics", f"id={CHANNEL_ID}", f"key={SECRET_KEY}", "fields=items%28statistics%29"):
            self.assertIn(part, path)

    def test_rounded_value_is_used_as_is(self):
        self.assertEqual(self.fetch(200, {"items": [{"statistics": {"subscriberCount": "1230"}}]}).count, 1230)

    def test_config_problems(self):
        self.assertEqual(self.fetch(200, {}).status, fetcher.CHANNEL_NOT_FOUND)
        self.assertEqual(self.fetch(200, {"items": []}).status, fetcher.CHANNEL_NOT_FOUND)
        hidden = {"items": [{"statistics": {"hiddenSubscriberCount": True}}]}
        self.assertEqual(self.fetch(200, hidden).status, fetcher.SUBSCRIBERS_HIDDEN)
        for reason in ("keyInvalid", "keyExpired", "accessNotConfigured", "forbidden"):
            self.assertEqual(self.fetch(*api_error(reason)).status, fetcher.KEY_INVALID, reason)
        self.assertEqual(self.fetch(*api_error("keyInvalid", 400)).status, fetcher.KEY_INVALID)

    def test_invalid_key_reported_as_bad_request(self):
        body = {"error": {"code": 400, "errors": [{"reason": "badRequest"}],
                          "details": [{"reason": "API_KEY_INVALID"}]}}
        result = self.fetch(400, body)
        self.assertEqual(result.status, fetcher.KEY_INVALID)
        self.assertIn("API_KEY_INVALID", result.detail)

    def test_missing_config_makes_no_request(self):
        self.write(api_key="")
        result = fetcher.Fetcher().fetch()
        self.assertEqual(result.status, fetcher.NOT_CONFIGURED)
        self.assertEqual(FakeApi.requests, [])

    def test_temporary_errors(self):
        self.assertEqual(self.fetch(500, b"oops").status, fetcher.ERROR)
        self.assertEqual(self.fetch(503, b"").status, fetcher.ERROR)
        self.assertEqual(self.fetch(200, b"not json").status, fetcher.ERROR)
        self.assertEqual(self.fetch(200, {"items": [{"statistics": {"subscriberCount": "x"}}]}).status, fetcher.ERROR)

    def test_network_error(self):
        with mock.patch.object(fetcher, "API_URL", "http://127.0.0.1:1/channels"):
            result = fetcher.Fetcher().fetch()
        self.assertEqual(result.status, fetcher.ERROR)
        self.assertNotIn(SECRET_KEY, result.detail)

    def test_daily_quota_waits_until_reset(self):
        for reason in ("quotaExceeded", "dailyLimitExceeded"):
            result = self.fetch(*api_error(reason))
            self.assertEqual(result.status, fetcher.QUOTA_EXCEEDED)
            self.assertAlmostEqual(result.retry_after, fetcher.seconds_until(fetcher.next_quota_reset()), delta=5)

    def test_rate_limit_waits_30_minutes(self):
        result = self.fetch(*api_error("rateLimitExceeded"))
        self.assertEqual((result.status, result.retry_after), (fetcher.QUOTA_EXCEEDED, 1800))

    def test_key_never_appears_in_results_or_logs(self):
        cases = [api_error("keyInvalid"), api_error("quotaExceeded"), (500, b""),
                 (400, {"error": {"message": "x", "errors": [{"reason": "badRequest"}]}})]
        with self.assertLogs(level="DEBUG") as logs:
            server.log.warning("start")
            for code, body in cases:
                result = self.fetch(code, body)
                self.assertNotIn(SECRET_KEY, result.detail)
                server.log.warning("%s", result)
        self.assertNotIn(SECRET_KEY, "\n".join(logs.output))

    def test_config_change_detected(self):
        source = fetcher.Fetcher()
        FakeApi.responses.append((200, {"items": [{"statistics": {"subscriberCount": "1"}}]}))
        source.fetch()
        self.assertFalse(source.config_changed())
        self.write(interval_sec=120)
        self.assertTrue(source.config_changed())


class StateTest(unittest.TestCase):
    def test_keeps_last_value_on_failure(self):
        state = server.CounterState()
        state.apply(FetchResult(fetcher.OK, 680, "api"))
        for status in (fetcher.ERROR, fetcher.QUOTA_EXCEEDED, fetcher.KEY_INVALID, fetcher.NOT_CONFIGURED):
            state.apply(FetchResult(status, detail="原因"))
            snapshot = state.snapshot()
            self.assertEqual((snapshot["count"], snapshot["status"], snapshot["detail"]), (680, status, "原因"))

    def test_same_value_does_not_notify(self):
        state = server.CounterState()
        state.apply(FetchResult(fetcher.OK, 680, "api"))
        version = state.version
        state.apply(FetchResult(fetcher.OK, 680, "api"))
        self.assertEqual(state.version, version)


class FakeSource:
    def __init__(self, results, interval=60):
        self.results = list(results)
        self.interval = interval
        self.closed = False

    def fetch(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def config_changed(self):
        return False

    def close(self):
        self.closed = True


class FetchLoopTest(unittest.TestCase):
    def run_loop(self, results, interval=60, on_config_problem=None):
        """結果を順に返し、各回の待ち時間を返す"""
        source = FakeSource(results, interval)
        state = server.CounterState()
        stop = threading.Event()
        waits = []

        def fake_wait(stop, _, seconds):
            waits.append(seconds)
            if not source.results:
                stop.set()

        with mock.patch("server.wait_next", fake_wait):
            server.fetch_loop(source, state, stop, on_config_problem)
        self.assertTrue(source.closed)
        return waits, state

    def test_backoff_doubles_up_to_30_minutes_and_resets(self):
        error = FetchResult(fetcher.ERROR, detail="通信エラー")
        ok = FetchResult(fetcher.OK, 680, "api")
        waits, state = self.run_loop([ok] + [error] * 7 + [ok, error])
        self.assertEqual(waits, [60, 120, 240, 480, 960, 1800, 1800, 1800, 60, 120])
        self.assertEqual(state.snapshot()["count"], 680)

    def test_long_interval_is_not_shortened_by_cap(self):
        waits, _ = self.run_loop([FetchResult(fetcher.ERROR)], interval=3600)
        self.assertEqual(waits, [3600])

    def test_exception_does_not_stop_loop(self):
        waits, state = self.run_loop([RuntimeError("boom"), FetchResult(fetcher.OK, 1, "api")])
        self.assertEqual(waits, [120, 60])
        self.assertEqual(state.snapshot()["count"], 1)

    def test_quota_waits_retry_after(self):
        waits, state = self.run_loop([FetchResult(fetcher.OK, 680, "api"),
                                      FetchResult(fetcher.QUOTA_EXCEEDED, retry_after=5000)])
        self.assertEqual(waits, [60, 5000])
        self.assertEqual(state.snapshot()["count"], 680)

    def test_config_problem_does_not_retry_and_notifies_once(self):
        notify = mock.Mock()
        problem = FetchResult(fetcher.KEY_INVALID, detail="キー")
        waits, _ = self.run_loop([problem, problem], on_config_problem=notify)
        self.assertEqual(waits, [None, None])   # 設定が変わるまで待つ
        notify.assert_called_once_with(problem)

    def test_wait_next_wakes_on_config_change(self):
        source = FakeSource([])
        source.config_changed = lambda: True
        started = time.monotonic()
        server.wait_next(threading.Event(), source, None)
        self.assertLess(time.monotonic() - started, 1)

    def test_wait_next_returns_after_time(self):
        started = time.monotonic()
        server.wait_next(threading.Event(), FakeSource([]), 0.05)
        self.assertLess(time.monotonic() - started, 1)

    def test_loop_never_logs_key(self):
        problem = FetchResult(fetcher.KEY_INVALID, detail="キーを確認してください")
        with self.assertLogs("server", level="INFO") as logs:
            self.run_loop([problem])
        self.assertNotIn(SECRET_KEY, "\n".join(logs.output))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.state = server.CounterState()
        self.httpd = server.Server((server.HOST, 0), self.state)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def connect(self):
        conn = http.client.HTTPConnection(server.HOST, self.port, timeout=5)
        self.addCleanup(conn.close)
        return conn

    def get(self, path):
        conn = self.connect()
        conn.request("GET", path)
        res = conn.getresponse()
        return res.status, res.read()

    def test_pages(self):
        self.assertEqual(self.get("/")[0], 200)
        self.assertEqual(self.get("/nope")[0], 404)
        self.state.apply(FetchResult(fetcher.OK, 681, "api"))
        status, body = self.get("/api/count")
        self.assertEqual((status, json.loads(body)["count"]), (200, 681))

    def test_events_push_changes(self):
        conn = self.connect()
        conn.request("GET", "/events")
        res = conn.getresponse()
        self.assertEqual(res.getheader("Content-Type"), "text/event-stream")

        def next_count():
            line = res.readline()
            res.readline()  # 空行
            return json.loads(line.removeprefix(b"data: "))["count"]

        self.assertIsNone(next_count())
        for count in (681, 680, 1230):  # 1,000 人以上は一度に 10 人単位で動く
            self.state.apply(FetchResult(fetcher.OK, count, "api"))
            self.assertEqual(next_count(), count)


if __name__ == "__main__":
    unittest.main()
