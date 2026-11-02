"""YouTube にアクセスせずに、取得・値の決定・配信を確認するテスト（python -m unittest）"""
import datetime
import http.client
import http.server
import io
import json
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.parse
import zoneinfo
from unittest import mock

import config
import design
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
        self.design_path = pathlib.Path(folder.name) / "design.json"
        self.assets = pathlib.Path(folder.name) / "assets"
        for patcher in (mock.patch.object(config, "CONFIG_PATH", self.path),
                        mock.patch.object(design, "DESIGN_PATH", self.design_path),
                        mock.patch.object(design, "ASSETS_DIR", self.assets)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, **values):
        data = {"api_key": SECRET_KEY, "channel_id": CHANNEL_ID, "interval_sec": 60, "terms_accepted": True} | values
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
                       {"interval_sec": True}, {"interval_sec": 10 ** 400}, {"terms_accepted": False}):
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
        with self.assertRaisesRegex(config.ConfigError, "初期設定"):
            config.load()
        self.path.write_text('{"api_key": "%s",\n oops}' % SECRET_KEY, encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, "2 行目") as caught:
            config.load()
        self.assertNotIn(SECRET_KEY, str(caught.exception))

    def test_bom_from_editor(self):
        self.path.write_text(json.dumps({"api_key": SECRET_KEY, "channel_id": CHANNEL_ID, "terms_accepted": True}), encoding="utf-8-sig")
        self.assertEqual(config.load().interval_sec, config.DEFAULT_INTERVAL_SEC)

    def test_example_has_no_values(self):
        data = json.loads((config.BASE / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual((data["api_key"], data["channel_id"], data["terms_accepted"]), ("", "", False))

    def test_save_keeps_other_values_and_summary_hides_key(self):
        self.write(interval_sec=120)
        config.save("NEW-KEY", CHANNEL_ID, True)
        self.assertEqual(config.load().interval_sec, 120)
        self.assertEqual(config.load().api_key, "NEW-KEY")
        self.assertNotIn("NEW-KEY", json.dumps(config.summary()))
        self.assertEqual(config.summary(), {"has_key": True, "channel_id": CHANNEL_ID, "terms_accepted": True})

    def test_parse_channel(self):
        for text, expected in [(CHANNEL_ID, ("id", CHANNEL_ID)), (f" {CHANNEL_ID} ", ("id", CHANNEL_ID)),
                               ("@name", ("handle", "@name")), ("@日本語", ("handle", "@日本語")),
                               (f"https://www.youtube.com/channel/{CHANNEL_ID}", ("id", CHANNEL_ID)),
                               (f"youtube.com/channel/{CHANNEL_ID}/videos", ("id", CHANNEL_ID)),
                               ("https://www.youtube.com/@name/featured", ("handle", "@name")),
                               ("https://youtube.com/@%E6%97%A5%E6%9C%AC", ("handle", "@日本")),
                               ("", None), ("@", None), ("name", None), ("UCshort", None),
                               ("https://evil.example/@name", None), ("https://youtube.com.evil.example/@name", None)]:
            self.assertEqual(config.parse_channel(text), expected, text)


class DesignTest(TempConfig):
    def add_asset(self, name, data=b"x"):
        self.assets.mkdir(exist_ok=True)
        (self.assets / name).write_bytes(data)

    def test_defaults_and_save(self):
        self.assertEqual(design.load(), design.DEFAULTS)
        self.assertIsNone(design.save({"size": 150, "animation": "odometer", "unknown": 1}))
        loaded = design.load()
        self.assertEqual((loaded["size"], loaded["animation"]), (150, "odometer"))
        self.assertNotIn("unknown", loaded)

    def test_rejects_invalid_values(self):
        cases = [("font_family", 'a"b'), ("font_family", "a;}body{"), ("size", 999), ("size", True), ("size", 1.5),
                 ("color", "red"), ("animation", "explode"), ("label_place", "hidden"), ("label_size", 0), ("label_loop", "spin"),
                 ("label_loop_ms", 100), ("font_weight", 500), ("shadow", 1),
                 ("bg_asset", "../config.json"), ("font_asset", "bg.png")]
        for key, value in cases:
            error = design.save({key: value})
            self.assertIn(design.LABELS[key], error, (key, value))
        self.assertFalse(self.design_path.exists())

    def test_load_repairs_hand_edits(self):
        self.design_path.write_text(json.dumps({"size": "big", "color": "#123456", "font_family": "x'y"}), encoding="utf-8")
        loaded = design.load()
        self.assertEqual((loaded["size"], loaded["color"], loaded["font_family"]), (100, "#123456", ""))
        self.design_path.write_text("[1", encoding="utf-8")
        self.assertEqual(design.load(), design.DEFAULTS)

    def test_asset_names(self):
        for name, kind in [("背景.webm", "video"), ("a b(1).PNG", "image"), ("font.woff2", "font"), ("x.y.gif", "image")]:
            self.assertEqual(design.asset_kind(name), kind, name)
        for name in ["../x.png", "a/b.png", "a\\b.png", "a:b.png", "x.svg", "x.exe", "x.html", "CON.png", "con.x.png",
                     ".png", "a.png.", "a.png ", "png", "", "x" * 200 + ".png", None]:
            self.assertIsNone(design.asset_kind(name), name)

    def test_save_requires_existing_asset(self):
        self.assertIn("見つかりません", design.save({"bg_asset": "bg.png"}))
        self.add_asset("bg.png")
        self.assertIsNone(design.save({"bg_type": "asset", "bg_asset": "bg.png"}))

    def test_delete_asset_clears_design(self):
        self.add_asset("bg.webm")
        self.add_asset("pop.gif")
        design.save({"bg_type": "asset", "bg_asset": "bg.webm", "effect": "asset", "effect_asset": "pop.gif"})
        design.delete_asset("bg.webm")
        loaded = design.load()
        self.assertEqual((loaded["bg_type"], loaded["bg_asset"]), ("none", ""))
        self.assertEqual((loaded["effect"], loaded["effect_asset"]), ("asset", "pop.gif"))
        self.assertEqual([a["name"] for a in design.list_assets()], ["pop.gif"])

    def test_interrupted_upload_leaves_nothing(self):
        with self.assertRaises(OSError):
            design.save_asset("bg.png", io.BytesIO(b"abc"), 10)
        self.assertEqual(list(self.assets.iterdir()), [])


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

    def test_unknown_4xx_is_retried_not_treated_as_key_problem(self):
        for code, body in [(403, b"<html>proxy</html>"), (407, b""), (400, {"error": {"errors": [{"reason": "badRequest"}]}})]:
            self.assertEqual(self.fetch(code, body).status, fetcher.ERROR, code)

    def test_api_disabled(self):
        self.assertEqual(self.fetch(*api_error("accessNotConfigured")).status, fetcher.KEY_INVALID)

    def test_reason_cannot_inject_log_lines(self):
        result = self.fetch(*api_error("keyInvalid\nFAKE LOG"))
        self.assertNotIn("\n", result.detail)

    def test_resolve_handle(self):
        FakeApi.responses.append((200, {"items": [{"id": CHANNEL_ID}]}))
        self.assertEqual(fetcher.resolve_handle(SECRET_KEY, "@name"), (CHANNEL_ID, None))
        self.assertIn("forHandle=%40name", FakeApi.requests[-1])
        FakeApi.responses.append((200, {}))
        self.assertEqual(fetcher.resolve_handle(SECRET_KEY, "@none")[1].status, fetcher.CHANNEL_NOT_FOUND)
        FakeApi.responses.append(api_error("keyInvalid"))
        self.assertEqual(fetcher.resolve_handle(SECRET_KEY, "@name")[1].status, fetcher.KEY_INVALID)

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
        self.assertFalse(source.should_wake())
        self.write(interval_sec=120)
        self.assertTrue(source.should_wake())

    def test_retry_request_wakes(self):
        source = fetcher.Fetcher()
        FakeApi.responses.append((200, {"items": [{"statistics": {"subscriberCount": "1"}}]}))
        source.fetch()
        source.request_retry()
        self.assertTrue(source.should_wake())


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
        self.channel_id = CHANNEL_ID

    def fetch(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def should_wake(self):
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
        source.should_wake = lambda: True
        started = time.monotonic()
        server.wait_next(threading.Event(), source, None)
        self.assertLess(time.monotonic() - started, 1)

    def test_wait_next_returns_after_time(self):
        started = time.monotonic()
        server.wait_next(threading.Event(), FakeSource([]), 0.05)
        self.assertLess(time.monotonic() - started, 1)

    def test_different_problem_with_same_status_notifies_again(self):
        notify = mock.Mock()
        self.run_loop([FetchResult(fetcher.NOT_CONFIGURED, detail="A"), FetchResult(fetcher.NOT_CONFIGURED, detail="B")],
                      on_config_problem=notify)
        self.assertEqual(notify.call_count, 2)

    def test_notifier_exception_does_not_kill_loop(self):
        source = FakeSource([FetchResult(fetcher.KEY_INVALID, detail="A"), FetchResult(fetcher.OK, 1, "api")])
        state = server.CounterState()
        stop = threading.Event()
        real_wait = stop.wait
        stop.wait = lambda _: real_wait(0)
        def fake_wait(stop, *_):
            if not source.results:
                stop.set()
        with mock.patch("server.wait_next", fake_wait):
            server.fetch_loop(source, state, stop, mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(state.snapshot()["count"], 1)

    def test_channel_change_clears_old_count(self):
        source = FakeSource([FetchResult(fetcher.OK, 680, "api"), FetchResult(fetcher.CHANNEL_NOT_FOUND, detail="x")])
        state = server.CounterState()
        stop = threading.Event()
        def fake_wait(stop, *_):
            source.channel_id = "UC" + "z" * 22
            if not source.results:
                stop.set()
        with mock.patch("server.wait_next", fake_wait):
            server.fetch_loop(source, state, stop)
        self.assertIsNone(state.snapshot()["count"])

    def test_count_is_not_logged(self):
        with self.assertLogs("server", level="INFO") as logs:
            self.run_loop([FetchResult(fetcher.OK, 68012, "api")])
        self.assertNotIn("68012", "\n".join(logs.output))

    def test_loop_never_logs_key(self):
        problem = FetchResult(fetcher.KEY_INVALID, detail="キーを確認してください")
        with self.assertLogs("server", level="INFO") as logs:
            self.run_loop([problem])
        self.assertNotIn(SECRET_KEY, "\n".join(logs.output))


class ServerTest(TempConfig):
    def setUp(self):
        super().setUp()
        self.state = server.CounterState()
        self.httpd = server.Server((server.HOST, 0), self.state, token="test-token")
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def connect(self):
        conn = http.client.HTTPConnection(server.HOST, self.port, timeout=5)
        self.addCleanup(conn.close)
        return conn

    def request(self, method, path, body=None, **headers):
        conn = self.connect()
        conn.request(method, path, body, {k.replace("_", "-"): v for k, v in headers.items()})
        res = conn.getresponse()
        return res.status, res.read(), res

    def get(self, path):
        return self.request("GET", path)[:2]

    def post(self, data, **overrides):
        origin = f"http://127.0.0.1:{self.port}"
        headers = {"Origin": origin, "Content_Type": "application/json", "X_Settings_Token": "test-token"} | overrides
        return self.request("POST", "/api/settings", json.dumps(data), **{k: v for k, v in headers.items() if v})

    def valid(self, **values):
        return {"terms_accepted": True, "api_key": SECRET_KEY, "channel": CHANNEL_ID} | values

    def write(self, method, path, body, **overrides):
        origin = f"http://127.0.0.1:{self.port}"
        headers = {"Origin": origin, "X_Settings_Token": "test-token"} | overrides
        return self.request(method, path, body, **{k: v for k, v in headers.items() if v})

    def upload(self, name, data, **overrides):
        path = f"/api/assets?name={urllib.parse.quote(name)}"
        return self.write("POST", path, data, **({"Content_Type": "application/octet-stream"} | overrides))

    def test_pages(self):
        for path in ("/", "/settings", "/design", "/privacy"):
            self.assertEqual(self.get(path)[0], 200, path)
        self.assertEqual(self.get("/nope")[0], 404)
        self.state.apply(FetchResult(fetcher.OK, 681, "api"))
        status, body = self.get("/api/count")
        self.assertEqual((status, json.loads(body)["count"]), (200, 681))

    def test_security_headers(self):
        _, _, res = self.request("GET", "/settings")
        self.assertEqual(res.getheader("X-Frame-Options"), "SAMEORIGIN")
        self.assertIn("default-src 'none'", res.getheader("Content-Security-Policy"))
        self.assertIn("frame-ancestors 'self'", res.getheader("Content-Security-Policy"))

    def test_design_read_is_public_and_write_is_protected(self):
        status, body = self.get("/api/design")
        self.assertEqual((status, json.loads(body)), (200, design.DEFAULTS))
        data = json.dumps({"size": 150})
        rev = self.state.snapshot()["design_rev"]
        self.assertEqual(self.write("POST", "/api/design", data, Content_Type="application/json", X_Settings_Token="x")[0], 403)
        self.assertEqual(self.write("POST", "/api/design", data, Content_Type="application/json", Origin="https://evil.example")[0], 403)
        self.assertEqual(self.write("POST", "/api/design", data, Content_Type="text/plain")[0], 415)
        self.assertFalse(self.design_path.exists())
        self.assertEqual(self.write("POST", "/api/design", data, Content_Type="application/json")[0], 200)
        self.assertEqual(design.load()["size"], 150)
        self.assertEqual(self.state.snapshot()["design_rev"], rev + 1)
        status, body, _ = self.write("POST", "/api/design", json.dumps({"color": "red"}), Content_Type="application/json")
        self.assertEqual((status, json.loads(body)["error"]), (400, "「文字の色」の値が正しくありません"))

    def test_asset_upload_serve_and_delete(self):
        data = bytes(range(256)) * 10
        status, body, _ = self.upload("背景 1.webm", data)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["assets"], [{"name": "背景 1.webm", "kind": "video", "size": len(data)}])
        path = f"/assets/{urllib.parse.quote('背景 1.webm')}"
        status, body, res = self.request("GET", path)
        self.assertEqual((status, body, res.getheader("Content-Type")), (200, data, "video/webm"))
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        status, body, res = self.request("GET", path, Range="bytes=10-19")
        self.assertEqual((status, body, res.getheader("Content-Range")), (206, data[10:20], f"bytes 10-19/{len(data)}"))
        status, body, _ = self.request("GET", path, Range="bytes=-5")
        self.assertEqual((status, body), (206, data[-5:]))
        self.assertEqual(self.request("GET", path, Range=f"bytes={len(data)}-")[0], 416)
        self.assertEqual(self.request("GET", "/api/assets", X_Settings_Token="test-token")[0], 200)
        self.assertEqual(self.write("DELETE", f"/api/assets?name={urllib.parse.quote('背景 1.webm')}", None)[0], 200)
        self.assertEqual(self.get(path)[0], 404)

    def test_asset_rejections(self):
        self.assertEqual(self.get("/api/assets")[0], 403)
        self.assertEqual(self.upload("x.png", b"x", X_Settings_Token="x")[0], 403)
        self.assertEqual(self.upload("x.png", b"x", Origin="https://evil.example")[0], 403)
        self.assertEqual(self.upload("x.png", b"x", Content_Type="multipart/form-data")[0], 415)
        for name in ("../x.png", "x.svg", "x.html"):
            self.assertEqual(self.upload(name, b"x")[0], 400, name)
        with mock.patch.object(design, "MAX_ASSET_BYTES", 3):
            self.assertEqual(self.upload("x.png", b"xxxx")[0], 413)
        self.assertEqual(self.write("DELETE", "/api/assets?name=x.png", None, X_Settings_Token="x")[0], 403)
        self.assertEqual(self.write("DELETE", "/api/design", None)[0], 403)
        self.assertFalse(self.assets.exists())
        # 素材フォルダーの外のファイルは返さない
        self.path.write_text("{}", encoding="utf-8")
        for path in ("/assets/..%2Fconfig.json", "/assets/../config.json", "/assets/config.json"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_rejects_foreign_host(self):
        conn = self.connect()
        conn.putrequest("GET", "/api/count", skip_host=True)
        conn.putheader("Host", "evil.example")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 403)

    def test_settings_require_token(self):
        self.assertEqual(self.get("/api/settings")[0], 403)
        self.assertEqual(self.request("GET", "/api/settings", X_Settings_Token="wrong")[0], 403)
        self.assertEqual(self.post(self.valid(), X_Settings_Token="wrong")[0], 403)
        self.assertEqual(self.post(self.valid(), X_Settings_Token=None)[0], 403)
        self.assertFalse(self.path.exists())

    def test_settings_reject_cross_site_requests(self):
        self.assertEqual(self.post(self.valid(), Origin="https://evil.example")[0], 403)
        self.assertEqual(self.post(self.valid(), Origin=None)[0], 403)
        self.assertEqual(self.post(self.valid(), Content_Type="text/plain")[0], 415)
        self.assertFalse(self.path.exists())

    def test_settings_reject_large_body(self):
        status = self.post({"api_key": "x" * (server.MAX_BODY_BYTES + 1)})[0]
        self.assertEqual(status, 413)

    def test_save_and_read_never_returns_key(self):
        self.assertEqual(self.post(self.valid())[0], 200)
        self.assertEqual(config.load().api_key, SECRET_KEY)
        status, body, _ = self.request("GET", "/api/settings", X_Settings_Token="test-token")
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET_KEY, body.decode())
        self.assertEqual(json.loads(body)["has_key"], True)
        # キーを空で送ると保存済みのものを使う
        self.assertEqual(self.post(self.valid(api_key=""))[0], 200)
        self.assertEqual(config.load().api_key, SECRET_KEY)

    def test_save_validation(self):
        for values, text in [({"terms_accepted": False}, "同意"), ({"api_key": " "}, "API キー"), ({"channel": "bad"}, "チャンネル")]:
            status, body, _ = self.post(self.valid(**values))
            self.assertEqual(status, 400)
            self.assertIn(text, json.loads(body)["error"])
            self.assertNotIn(SECRET_KEY, body.decode())
        self.assertFalse(self.path.exists())

    def test_save_resolves_handle(self):
        with mock.patch("fetcher.resolve_handle", return_value=(CHANNEL_ID, None)) as resolve:
            self.assertEqual(self.post(self.valid(channel="@name"))[0], 200)
        resolve.assert_called_once_with(SECRET_KEY, "@name")
        self.assertEqual(config.load().channel_id, CHANNEL_ID)

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
