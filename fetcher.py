"""
YouTube Data API v3 から登録者数を取得するモジュール

1,000 人以上の値は API の仕様で有効数字 3 桁に切り捨てられる。これを補正する処理は入れない。
例外は外に出さず、すべて FetchResult として返す。

リクエストの URL には API キーが含まれるので、URL や例外の文面をログに出さないこと。
"""
import dataclasses
import datetime
import json
import logging
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
# 設定を直さない限り解決しないので再試行しない
CONFIG_PROBLEMS = {NOT_CONFIGURED, KEY_INVALID, CHANNEL_NOT_FOUND, SUBSCRIBERS_HIDDEN}

DAILY_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}

log = logging.getLogger(__name__)


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
    """エラーレスポンスから原因のコードだけを取り出す"""
    try:
        error = json.loads(body)["error"]
        reason = error["errors"][0]["reason"]
        if reason != "badRequest":
            return reason
        # キーが無効なときは reason が badRequest になり、詳しい原因は details に入ることがある
        for detail in error.get("details", []):
            if detail.get("reason"):
                return detail["reason"]
        return reason
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return ""


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
    if reason == "accessNotConfigured":
        return FetchResult(KEY_INVALID, detail="YouTube Data API v3 が有効になっていません。Google Cloud の設定を確認してください")
    return FetchResult(KEY_INVALID, detail=f"API キーを確認してください（{reason or f'HTTP {code}'}）")


class Fetcher:
    def __init__(self):
        self.interval = config.DEFAULT_INTERVAL_SEC
        self._seen_config = None

    def fetch(self):
        """1 回取得する。例外は投げない"""
        self._seen_config = config.signature()
        try:
            cfg = config.load()
        except config.ConfigError as e:
            return FetchResult(NOT_CONFIGURED, detail=str(e))
        self.interval = cfg.interval_sec
        try:
            return self._request(cfg)
        except urllib.error.HTTPError as e:
            with e:
                return classify_http_error(e.code, error_reason(e.read()))
        except (urllib.error.URLError, OSError):
            return FetchResult(ERROR, detail="通信エラー")
        except ValueError:
            return FetchResult(ERROR, detail="API の応答を読めませんでした")
        except Exception as e:
            return FetchResult(ERROR, detail=type(e).__name__)

    def config_changed(self):
        return config.signature() != self._seen_config

    def close(self):
        pass

    @staticmethod
    def _request(cfg):
        query = urllib.parse.urlencode({
            "part": "statistics",
            "id": cfg.channel_id,
            "key": cfg.api_key,
            "fields": "items(statistics)",
        })
        with urllib.request.urlopen(f"{API_URL}?{query}", timeout=TIMEOUT_SEC) as res:
            return parse_response(json.load(res))
