"""
設定ファイル（config.json）の読み込みと検証

エラーメッセージに設定の値を含めないこと（API キーが画面やログに出てしまう）。
"""
import dataclasses
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent
EXAMPLE_PATH = BASE / "config.example.json"

DEFAULT_INTERVAL_SEC = 60
MIN_INTERVAL_SEC = 30
DAILY_QUOTA_UNITS = 10000
MAX_QUOTA_RATIO = 0.8
UNITS_PER_REQUEST = 1


def data_dir():
    """設定とログの保存先。配布版は exe の場所によらないようユーザーごとのフォルダに置く"""
    override = os.environ.get("YT_COUNTER_DATA_DIR")
    if override:
        return pathlib.Path(override)
    if getattr(sys, "frozen", False):
        return pathlib.Path(os.environ["LOCALAPPDATA"]) / "YouTubeSubCounter"
    return BASE


CONFIG_PATH = data_dir() / "config.json"


class ConfigError(Exception):
    """設定の問題。メッセージはそのまま利用者に見せる"""


@dataclasses.dataclass(frozen=True, repr=False)
class Config:
    api_key: str
    channel_id: str
    interval_sec: int


def load():
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError("設定ファイルがありません") from None
    except UnicodeDecodeError:
        raise ConfigError("設定ファイルの文字コードを読めません。UTF-8 で保存してください") from None
    except json.JSONDecodeError as e:
        raise ConfigError(f"設定ファイルの {e.lineno} 行目に書き方の誤りがあります") from None
    except OSError:
        raise ConfigError("設定ファイルを開けません") from None
    if not isinstance(data, dict):
        raise ConfigError("設定ファイルの書き方に誤りがあります")

    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ConfigError("API キーが設定されていません")

    channel_id = data.get("channel_id")
    if not isinstance(channel_id, str) or not re.fullmatch(r"UC[\w-]{22}", channel_id.strip(), re.ASCII):
        raise ConfigError("チャンネル ID の形式が正しくありません（UC で始まる 24 文字です）")

    interval = data.get("interval_sec", DEFAULT_INTERVAL_SEC)
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < MIN_INTERVAL_SEC:
        raise ConfigError(f"interval_sec は {MIN_INTERVAL_SEC} 以上の整数にしてください")
    daily_units = 86400 / interval * UNITS_PER_REQUEST
    if daily_units > DAILY_QUOTA_UNITS * MAX_QUOTA_RATIO:
        raise ConfigError("interval_sec が短すぎます（1 日のクォータの 8 割を超えます）")

    return Config(api_key.strip(), channel_id.strip(), interval)


def signature():
    """設定ファイルが変わったことを検出するための値"""
    try:
        stat = CONFIG_PATH.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def ensure_file():
    """設定ファイルがなければ見本から作る"""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)


def open_in_editor():
    # .json は関連付けがないことが多いので、メモ帳を直接指定する
    ensure_file()
    subprocess.Popen(["notepad.exe", str(CONFIG_PATH)])
