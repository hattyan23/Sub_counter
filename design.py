"""
見た目の設定（design.json）と、利用者が追加した素材（フォント・背景・演出の画像や動画）の管理

design.json は手で書き換えられても動くよう、読み込むときは誤った項目だけ既定値に戻す。
保存するときは誤りがあれば保存せずにメッセージを返す。
"""
import json
import os
import re
import secrets

import config

DESIGN_PATH = config.data_dir() / "design.json"
ASSETS_DIR = config.data_dir() / "assets"
MAX_ASSET_BYTES = 200 * 1024 * 1024

ANIMATIONS = ("none", "slide", "odometer", "flip", "countup", "pop", "bounce")
EFFECTS = ("none", "glow", "sparkle", "confetti", "asset")

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".apng": "image/apng", ".avif": "image/avif"}
VIDEO_TYPES = {".webm": "video/webm", ".mp4": "video/mp4"}
FONT_TYPES = {".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2"}
# SVG はスクリプトを含められるので受け付けない
KINDS = {**{ext: "image" for ext in IMAGE_TYPES}, **{ext: "video" for ext in VIDEO_TYPES},
         **{ext: "font" for ext in FONT_TYPES}}
CONTENT_TYPES = {**IMAGE_TYPES, **VIDEO_TYPES, **FONT_TYPES}
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

DEFAULTS = {
    "font_family": "",          # パソコンに入っているフォントの名前
    "font_asset": "",           # 追加したフォントのファイル（指定があれば優先）
    "font_weight": 700,
    "size": 100,                # %
    "color": "#ffffff",
    "outline_color": "#000000",
    "outline": 3,
    "shadow": True,
    "separator": False,         # 3 桁ごとのカンマ
    "pos_x": 50,                # %
    "pos_y": 50,
    "label_place": "below",     # 「YouTube 登録者数」の位置（below / above / free）
    "label_size": 100,          # %
    "label_x": 50,              # free のときの位置（%）
    "label_y": 80,
    "label_loop": "none",       # 「YouTube 登録者数」をずっと動かすアニメーション
    "label_loop_ms": 2000,      # 1 回の動きにかける時間
    "animation": "slide",
    "duration_ms": 800,
    "effect": "none",           # 増えたときだけの演出
    "effect_asset": "",
    "bg_type": "none",          # none は透明
    "bg_color": "#000000",
    "bg_asset": "",
    "bg_fit": "cover",
    "bg_opacity": 100,
}
CHOICES = {
    "font_weight": (400, 700, 900),
    "label_place": ("below", "above", "free"),
    "label_loop": ("none", "float", "hop", "pulse", "sway", "blink", "glow"),
    "animation": ANIMATIONS,
    "effect": EFFECTS,
    "bg_type": ("none", "color", "asset"),
    "bg_fit": ("cover", "contain", "fill", "tile"),
}
RANGES = {"size": (30, 300), "outline": (0, 10), "pos_x": (0, 100), "pos_y": (0, 100),
          "label_size": (30, 300), "label_x": (0, 100), "label_y": (0, 100),
          "label_loop_ms": (500, 10000),
          "duration_ms": (200, 5000), "bg_opacity": (0, 100)}
COLORS = {"color", "outline_color", "bg_color"}
BOOLS = {"shadow", "separator"}
ASSET_KINDS = {"font_asset": ("font",), "effect_asset": ("image", "video"), "bg_asset": ("image", "video")}
LABELS = {
    "font_family": "フォント名", "font_asset": "フォントのファイル", "font_weight": "文字の太さ", "size": "文字の大きさ",
    "color": "文字の色", "outline_color": "縁取りの色", "outline": "縁取りの太さ", "shadow": "影", "separator": "カンマ",
    "pos_x": "横の位置", "pos_y": "縦の位置", "label_place": "「YouTube 登録者数」の位置",
    "label_size": "「YouTube 登録者数」の大きさ", "label_x": "「YouTube 登録者数」の横の位置",
    "label_y": "「YouTube 登録者数」の縦の位置", "label_loop": "「YouTube 登録者数」のループアニメーション",
    "label_loop_ms": "ループアニメーションの速さ", "animation": "アニメーション", "duration_ms": "アニメーションの長さ",
    "effect": "増えたときの演出", "effect_asset": "演出の素材", "bg_type": "背景の種類", "bg_color": "背景の色",
    "bg_asset": "背景の素材", "bg_fit": "背景の合わせ方", "bg_opacity": "背景の濃さ",
}


def _valid(key, value):
    if key == "font_family":
        # CSS に入れるので、引用符や区切りの記号は使わせない
        return isinstance(value, str) and len(value) <= 100 and not re.search(r"[\"'\\;{}<>\x00-\x1f]", value)
    if key in CHOICES:
        return not isinstance(value, bool) and value in CHOICES[key]
    if key in RANGES:
        low, high = RANGES[key]
        return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    if key in COLORS:
        return isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None
    if key in BOOLS:
        return isinstance(value, bool)
    if key in ASSET_KINDS:
        return value == "" or (isinstance(value, str) and asset_kind(value) in ASSET_KINDS[key])
    return False


def load():
    try:
        data = json.loads(DESIGN_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {key: data[key] if key in data and _valid(key, data[key]) else default
            for key, default in DEFAULTS.items()}


def save(changes):
    """変更を検証して保存する。失敗したら利用者向けのメッセージを返す"""
    if not isinstance(changes, dict):
        return "入力を読めませんでした"
    design = load()
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if not _valid(key, value):
            return f"「{LABELS[key]}」の値が正しくありません"
        if key in ASSET_KINDS and value and not asset_path(value).is_file():
            return f"「{LABELS[key]}」のファイルが見つかりません"
        design[key] = value
    _write(design)
    return None


def _write(design):
    DESIGN_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = DESIGN_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(design, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, DESIGN_PATH)


def asset_kind(name):
    """素材として使える名前なら種類（image / video / font）を返す。使えなければ None"""
    if not isinstance(name, str) or not 0 < len(name) <= 120:
        return None
    # フォルダーの外を指したり、Windows で特別な意味を持ったりする名前は受け付けない
    if not re.fullmatch(r"[\w\-. ()（）]+", name) or name.startswith(".") or name.endswith((".", " ")):
        return None
    stem, dot, ext = name.rpartition(".")
    if not dot or stem.split(".")[0].strip().upper() in RESERVED_NAMES:
        return None
    return KINDS.get(f".{ext.lower()}")


def asset_path(name):
    return ASSETS_DIR / name


def content_type(name):
    return CONTENT_TYPES[f".{name.rpartition('.')[2].lower()}"]


def list_assets():
    try:
        files = sorted(ASSETS_DIR.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    return [{"name": p.name, "kind": asset_kind(p.name), "size": p.stat().st_size}
            for p in files if p.is_file() and asset_kind(p.name)]


def save_asset(name, stream, length):
    """素材を書き込む。途中で途切れたら書きかけのファイルを残さない"""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    temp = ASSETS_DIR / f".upload-{secrets.token_hex(8)}"
    try:
        with open(temp, "wb") as f:
            left = length
            while left:
                chunk = stream.read(min(left, 1024 * 1024))
                if not chunk:
                    raise OSError("途中で途切れました")
                f.write(chunk)
                left -= len(chunk)
        os.replace(temp, asset_path(name))
    finally:
        temp.unlink(missing_ok=True)


def delete_asset(name):
    """素材を消し、見た目の設定で使っていたら外す"""
    asset_path(name).unlink(missing_ok=True)
    design = load()
    used = [key for key in ASSET_KINDS if design[key] == name]
    for key in used:
        design[key] = ""
    if design["bg_type"] == "asset" and not design["bg_asset"]:
        design["bg_type"] = "none"
    if design["effect"] == "asset" and not design["effect_asset"]:
        design["effect"] = "none"
    if used:
        _write(design)
