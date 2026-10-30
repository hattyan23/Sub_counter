"""YouTube にアクセスせずに、値の決定と配信を確認するテスト（python -m unittest）"""
import http.client
import json
import threading
import unittest
from unittest import mock

import fetcher
from fetcher import FetchResult
import server


class ParseTest(unittest.TestCase):
    def test_parse_count(self):
        for value, expected in [(681, 681), ("681", 681), ("12,345", 12345), (0, 0),
                                ("1.2万", None), ("1,23", None), ("", None), (None, None),
                                (True, None), (-1, None)]:
            self.assertEqual(fetcher.parse_count(value), expected, value)

    def test_response_uses_own_channel(self):
        data = {"channels": [{"metric": {"subscriberCount": 681}}]}
        self.assertEqual(fetcher.count_from_response(data), 681)
        for broken in ({}, {"channels": []}, {"channels": [{}]}, []):
            self.assertIsNone(fetcher.count_from_response(broken))

    def test_dom_takes_line_after_label(self):
        self.assertEqual(fetcher.count_from_text(f"他\n{fetcher.DOM_LABEL}\n\n 681 \n過去28日間"), 681)
        self.assertIsNone(fetcher.count_from_text(f"他\n{fetcher.DOM_LABEL}"))


class StateTest(unittest.TestCase):
    def test_keeps_last_value_on_failure(self):
        state = server.CounterState()
        state.apply(FetchResult(fetcher.OK, 681, "net"))
        for status in (fetcher.ERROR, fetcher.NO_VALUE, fetcher.AUTH_EXPIRED):
            state.apply(FetchResult(status))
            self.assertEqual(state.snapshot()["count"], 681)
            self.assertEqual(state.snapshot()["status"], status)

    def test_same_value_does_not_notify(self):
        state = server.CounterState()
        state.apply(FetchResult(fetcher.OK, 681, "net"))
        version = state.version
        state.apply(FetchResult(fetcher.OK, 681, "dom"))
        self.assertEqual(state.version, version)


class FetchLoopTest(unittest.TestCase):
    def test_survives_exception_and_enforces_min_interval(self):
        stop = threading.Event()
        source = mock.Mock()
        source.fetch.side_effect = RuntimeError("boom")
        waits = []
        stop.wait = lambda seconds: (waits.append(seconds), stop.set())
        server.fetch_loop(source, server.CounterState(), stop, interval=1)
        self.assertEqual(waits, [server.MIN_INTERVAL_SEC])
        source.close.assert_called_once()


    def test_offers_login_once_until_success(self):
        stop = threading.Event()
        source = mock.Mock()
        source.fetch.return_value = FetchResult(fetcher.AUTH_EXPIRED)
        login = mock.Mock()
        stop.wait = lambda seconds: stop.set()
        server.fetch_loop(source, server.CounterState(), stop, 60, login=login)
        login.assert_called_once_with(stop)
        self.assertEqual(source.fetch.call_count, 2)  # ログイン直後は待たずに取り直す


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
        self.state.apply(FetchResult(fetcher.OK, 681, "net"))
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
        for count in (681, 680, 683):  # 複数人の同時増加も含む
            self.state.apply(FetchResult(fetcher.OK, count, "net"))
            self.assertEqual(next_count(), count)


if __name__ == "__main__":
    unittest.main()
