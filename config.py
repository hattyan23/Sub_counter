"""
設定ファイル（config.json）の読み込み・検証・保存

エラーメッセージに設定の値を含めないこと（API キーが画面やログに出てしまう）。
"""
import dataclasses
import json
import os
import pathlib
import re
import sys
import urllib.parse

BASE = pathlib.Path(__file__).resolve().parent

DEFAULT_INTERVAL_SEC = 60
MIN_INTERVAL_SEC = 30
MAX_INTERVAL_SEC = 86400
DAILY_QUOTA_UNITS = 10000
MAX_QUOTA_RATIO = 0.8
UNITS_PER_REQUEST = 1

APP_DIR_NAME = "SubCounter"
LEGACY_APP_DIR_NAME = "YouTubeSubCounter"
CHANNEL_ID_PATTERN = r"UC[\w-]{22}"


def data_dir():
    """設定とログの保存先。配布版は exe の場所によらないようユーザーごとのフォルダに置く"""
    override = os.environ.get("YT_COUNTER_DATA_DIR")
    if override:
        return pathlib.Path(override)
    if getattr(sys, "frozen", False):
        return pathlib.Path(os.environ["LOCALAPPDATA"]) / APP_DIR_NAME
    return BASE


CONFIG_PATH = data_dir() / "config.json"


def migrate_legacy_dir():
    """旧名のフォルダから設定を引き継ぐ。統計を 30 日以上残さないよう、旧版のログは引き継がない"""
    if os.environ.get("YT_COUNTER_DATA_DIR") or not getattr(sys, "frozen", False):
        return
    legacy = data_dir().parent / LEGACY_APP_DIR_NAME
    if legacy.is_dir() and not data_dir().exists():
        legacy.rename(data_dir())
        for log_file in data_dir().glob("app.log*"):
            log_file.unlink(missing_ok=True)


class ConfigError(Exception):
    """設定の問題。メッセージはそのまま利用者に見せる"""


@dataclasses.dataclass(frozen=True, repr=False)
class Config:
    api_key: str
    channel_id: str
    interval_sec: int


def _read_raw():
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError
    return data


def load():
    try:
        data = _read_raw()
    except FileNotFoundError:
        raise ConfigError("初期設定が終わっていません") from None
    except UnicodeDecodeError:
        raise ConfigError("設定ファイルの文字コードを読めません。設定画面から保存し直してください") from None
    except json.JSONDecodeError as e:
        raise ConfigError(f"設定ファイルの {e.lineno} 行目に書き方の誤りがあります") from None
    except ValueError:
        raise ConfigError("設定ファイルの書き方に誤りがあります") from None
    except OSError:
        raise ConfigError("設定ファイルを開けません") from None

    if data.get("terms_accepted") is not True:
        raise ConfigError("利用規約とプライバシーポリシーへの同意が必要です")

    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ConfigError("API キーが設定されていません")

    channel_id = data.get("channel_id")
    if not isinstance(channel_id, str) or not re.fullmatch(CHANNEL_ID_PATTERN, channel_id.strip(), re.ASCII):
        raise ConfigError("チャンネル ID の形式が正しくありません（UC で始まる 24 文字です）")

    interval = data.get("interval_sec", DEFAULT_INTERVAL_SEC)
    if (not isinstance(interval, int) or isinstance(interval, bool)
            or not MIN_INTERVAL_SEC <= interval <= MAX_INTERVAL_SEC):
        raise ConfigError(f"interval_sec は {MIN_INTERVAL_SEC}〜{MAX_INTERVAL_SEC} の整数にしてください")
    daily_units = 86400 / interval * UNITS_PER_REQUEST
    if daily_units > DAILY_QUOTA_UNITS * MAX_QUOTA_RATIO:
        raise ConfigError("interval_sec が短すぎます（1 日のクォータの 8 割を超えます）")

    return Config(api_key.strip(), channel_id.strip(), interval)


def summary():
    """設定画面に見せる内容。API キーそのものは返さない"""
    try:
        data = _read_raw()
    except (OSError, ValueError):
        data = {}
    channel_id = data.get("channel_id")
    api_key = data.get("api_key")
    return {
        "has_key": isinstance(api_key, str) and bool(api_key.strip()),
        "channel_id": channel_id if isinstance(channel_id, str) else "",
        "terms_accepted": data.get("terms_accepted") is True,
    }


def stored_key():
    try:
        api_key = _read_raw().get("api_key")
    except (OSError, ValueError):
        return ""
    return api_key.strip() if isinstance(api_key, str) else ""


def save(api_key, channel_id, terms_accepted):
    """設定を書き込む。途中で失敗しても元のファイルを壊さないよう、一時ファイル経由で置き換える"""
    try:
        data = _read_raw()
    except (OSError, ValueError):
        data = {}
    data.setdefault("interval_sec", DEFAULT_INTERVAL_SEC)
    data.update(api_key=api_key, channel_id=channel_id, terms_accepted=terms_accepted)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, CONFIG_PATH)


def signature():
    """設定ファイルが変わったことを検出するための値"""
    try:
        stat = CONFIG_PATH.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def parse_channel(text):
    """入力を ("id", 値) か ("handle", 値) にする。チャンネル ID・@ハンドル・チャンネルの URL を受け付ける"""
    text = text.strip()
    if re.fullmatch(CHANNEL_ID_PATTERN, text, re.ASCII):
        return "id", text
    if text.startswith("@") and len(text) > 1 and not re.search(r"[\s/?#]", text):
        return "handle", text
    url = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
    if url.hostname in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        path = urllib.parse.unquote(url.path)
        match = re.fullmatch(rf"/channel/({CHANNEL_ID_PATTERN})/?.*", path, re.ASCII)
        if match:
            return "id", match.group(1)
        match = re.fullmatch(r"/(@[^/\s]+)(/.*)?", path)
        if match:
            return "handle", match.group(1)
    return None
