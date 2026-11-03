"""
YouTube Data API v3 から登録者数を取得するモジュール

1,000 人以上の値は API の仕様で有効数字 3 桁に切り捨てられる。これを補正する処理は入れない。
例外は外に出さず、すべて FetchResult として返す。

リクエストの URL には API キーが含まれるので、URL や例外の文面をログに出さないこと。
"""
import dataclasses
import datetime
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo

import config

API_URL = "https://www.googleapis.com/youtube/v3/channels"
TIMEOUT_SEC = 15
RATE_LIMIT_PAUSE_SEC = 30 * 60
QUOTA_TIMEZONE = "America/Los_Angeles"   # クォータはこの地域の 0 時にリセットされる

# 状態
OK = "ok"
STARTING = "starting"                   # 最初の取得の完了待ち
ERROR = "error"                         # 通信エラーやサーバーエラー（間隔を延ばして再試行）
QUOTA_EXCEEDED = "quota_exceeded"       # クォータ超過（retry_after の間は取得しない）
NOT_CONFIGURED = "not_configured"       # 設定ファイルの問題
KEY_INVALID = "key_invalid"             # API キーの問題
CHANNEL_NOT_FOUND = "channel_not_found"
SUBSCRIBERS_HIDDEN = "subscribers_hidden"
# 利用者が直すまで解決しないので再試行しない
CONFIG_PROBLEMS = {NOT_CONFIGURED, KEY_INVALID, CHANNEL_NOT_FOUND, SUBSCRIBERS_HIDDEN}

DAILY_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
API_DISABLED_REASONS = {"accessNotConfigured", "SERVICE_DISABLED"}
KEY_REASONS = {"keyInvalid", "keyExpired", "forbidden", "ipRefererBlocked", "API_KEY_INVALID", "API_KEY_EXPIRED",
               "API_KEY_SERVICE_BLOCKED", "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED"}
# キーの作り方でつまずく人が多いので、原因ごとに直し方を案内する
KEY_APP_RESTRICTED_REASONS = {"ipRefererBlocked", "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED"}
KEY_API_RESTRICTED_REASONS = {"API_KEY_SERVICE_BLOCKED"}


@dataclasses.dataclass(frozen=True)
class FetchResult:
    status: str
    count: int | None = None
    source: str | None = None   # 取得元。今は "api" のみ
    detail: str = ""            # 利用者向けの説明。設定の値や URL を含めない
    retry_after: float | None = None   # この秒数は取得しない


def next_quota_reset(now=None):
    """次にクォータがリセットされる時刻（夏時間に合わせて日本時間の 16 時と 17 時が切り替わる）"""
    tz = zoneinfo.ZoneInfo(QUOTA_TIMEZONE)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    tomorrow = now.astimezone(tz).date() + datetime.timedelta(days=1)
    return datetime.datetime.combine(tomorrow, datetime.time(0), tz)


def seconds_until(moment, now=None):
    # 同じタイムゾーン同士の引き算は夏時間の切り替えを無視するので、UTC に揃えてから引く
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (moment.astimezone(datetime.timezone.utc) - now.astimezone(datetime.timezone.utc)).total_seconds()


def parse_response(data):
    """channels.list のレスポンスを FetchResult にする"""
    items = data.get("items") if isinstance(data, dict) else None
    if not items:
        return FetchResult(CHANNEL_NOT_FOUND, detail="チャンネルが見つかりません。チャンネル ID を確認してください")
    statistics = items[0].get("statistics", {})
    if statistics.get("hiddenSubscriberCount"):
        return FetchResult(SUBSCRIBERS_HIDDEN, detail="このチャンネルは登録者数を非公開にしています")
    value = statistics.get("subscriberCount")
    if not isinstance(value, str) or not value.isdecimal():
        return FetchResult(ERROR, detail="API の応答を読めませんでした")
    return FetchResult(OK, int(value), "api")


def error_reason(body):
    """エラーレスポンスから原因のコードだけを取り出す。ログに出すので英数字以外は落とす"""
    try:
        error = json.loads(body)["error"]
        reason = error["errors"][0]["reason"]
        if reason == "badRequest":
            # キーが無効なときは reason が badRequest になり、詳しい原因は details に入る
            reason = next((d["reason"] for d in error.get("details", []) if d.get("reason")), reason)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return ""
    return re.sub(r"\W", "", str(reason), flags=re.ASCII)[:64]


def classify_http_error(code, reason):
    if code >= 500:
        return FetchResult(ERROR, detail=f"YouTube API のサーバーエラー（HTTP {code}）")
    if reason in DAILY_QUOTA_REASONS:
        reset = next_quota_reset()
        return FetchResult(
            QUOTA_EXCEEDED, retry_after=seconds_until(reset),
            detail=f"API の 1 日の上限に達しました（{reset.astimezone():%m/%d %H:%M} まで停止）",
        )
    if reason in RATE_LIMIT_REASONS or code == 429:
        return FetchResult(QUOTA_EXCEEDED, retry_after=RATE_LIMIT_PAUSE_SEC,
                           detail="API の呼び出しが制限されました（30 分後に再開）")
    if reason == "channelNotFound":
        return FetchResult(CHANNEL_NOT_FOUND, detail="チャンネルが見つかりません。チャンネル ID を確認してください")
    if reason in API_DISABLED_REASONS:
        return FetchResult(KEY_INVALID, detail="YouTube Data API v3 が有効になっていません。"
                                               "有効にしたら「今すぐ取得し直す」を選んでください")
    if reason in KEY_APP_RESTRICTED_REASONS:
        return FetchResult(KEY_INVALID, detail="API キーの「アプリケーションの制限」が原因で使えません。"
                                               f"Google Cloud でキーを開き、「なし」にしてください（{reason}）")
    if reason in KEY_API_RESTRICTED_REASONS:
        return FetchResult(KEY_INVALID, detail="API キーの「API の制限」に YouTube Data API v3 が入っていません。"
                                               f"キーの設定でチェックを入れてください（{reason}）")
    if reason in KEY_REASONS:
        return FetchResult(KEY_INVALID, detail="API キーを確認してください。「AIza」で始まるキーを、"
                                               f"前後に空白を入れずに貼り付けてください（{reason}）")
    # プロキシや一時的な応答の可能性があるので、設定の問題とは決めつけず再試行する
    return FetchResult(ERROR, detail=f"API のエラー（HTTP {code} {reason}）".replace(" ）", "）"))


def _call_api(params):
    """API を呼び、(成功した応答, None) か (None, 失敗の FetchResult) を返す"""
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SEC) as res:
            return json.load(res), None
    except urllib.error.HTTPError as e:
        with e:
            try:
                body = e.read()
            except OSError:
                body = b""
        return None, classify_http_error(e.code, error_reason(body))
    except (urllib.error.URLError, OSError):
        return None, FetchResult(ERROR, detail="通信エラー")
    except ValueError:
        return None, FetchResult(ERROR, detail="API の応答を読めませんでした")


def resolve_handle(api_key, handle):
    """@ハンドルからチャンネル ID を調べ、(チャンネル ID, None) か (None, 失敗の FetchResult) を返す"""
    try:
        data, failure = _call_api({"part": "id", "forHandle": handle, "key": api_key, "fields": "items(id)"})
        if failure:
            return None, failure
        items = data.get("items") if isinstance(data, dict) else None
        if not items or not isinstance(items[0].get("id"), str):
            return None, FetchResult(CHANNEL_NOT_FOUND, detail="このハンドルのチャンネルが見つかりません")
        return items[0]["id"], None
    except Exception as e:
        return None, FetchResult(ERROR, detail=type(e).__name__)


class Fetcher:
    def __init__(self):
        self.interval = config.DEFAULT_INTERVAL_SEC
        self.channel_id = None
        self._seen_config = None
        self._retry = threading.Event()

    def fetch(self):
        """1 回取得する。例外は投げない"""
        self._retry.clear()
        self._seen_config = config.signature()
        try:
            cfg = config.load()
        except config.ConfigError as e:
            return FetchResult(NOT_CONFIGURED, detail=str(e))
        self.interval = cfg.interval_sec
        self.channel_id = cfg.channel_id
        try:
            data, failure = _call_api({
                "part": "statistics",
                "id": cfg.channel_id,
                "key": cfg.api_key,
                "fields": "items(statistics)",
            })
            return failure or parse_response(data)
        except Exception as e:
            return FetchResult(ERROR, detail=type(e).__name__)

    def request_retry(self):
        """トレイの「今すぐ取得し直す」から呼ばれる"""
        self._retry.set()

    def should_wake(self):
        """待ち時間を切り上げて取得し直すべきか（設定が変わった、または利用者が頼んだ）"""
        return self._retry.is_set() or config.signature() != self._seen_config

    def close(self):
        pass
