"""
紹介動画と使い方解説の動画（promo\\out\\<名前>_字幕あり.mp4 / _字幕なし.mp4）と、音声を合わせるための台本を作る

  python promo\\make_promo.py <ffmpeg.exe のパス> [紹介動画] [使い方解説]   （名前を省くと両方）

実物のカウンター画面と見た目の設定画面を、一時フォルダの設定で動かして録画する。API キーは使わない。
必要なもの: playwright、pillow、ffmpeg（libx264 入り）
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out"

os.environ["YT_COUNTER_DATA_DIR"] = tempfile.mkdtemp(prefix="subcounter_promo_")
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
import design  # noqa: E402
import fetcher  # noqa: E402
import server  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PORT = 8794
W, H = 1920, 1080


def make_assets():
    """デザイン見本の背景（夜景・パステル・またたく星の GIF）と、使い方解説で見せる画面右下の画像"""
    design.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "docs" / "guide" / "img" / "tray_desktop.png", design.ASSETS_DIR / "tray.png")
    night = Image.new("RGB", (784, 330))
    d = ImageDraw.Draw(night)
    for y in range(330):
        d.line([(0, y), (784, y)], fill=(15 + y // 8, 23 + y // 10, 60 + y // 4))
    x = 0
    for i in range(30):
        w, h = 20 + (i * 37) % 40, 40 + (i * 53) % 120
        d.rectangle((x, 330 - h, x + w, 330), fill=(10, 14, 35))
        for wy in range(330 - h + 8, 322, 14):
            if (i + wy) % 3:
                d.rectangle((x + 5, wy, x + 9, wy + 5), fill=(253, 224, 120))
        x += w + 4
    night.save(design.ASSETS_DIR / "night.png")

    pastel = Image.new("RGB", (784, 330))
    d = ImageDraw.Draw(pastel)
    for x in range(784):
        t = x / 783
        d.line([(x, 0), (x, 330)], fill=(int(255 - 40 * t), int(214 + 20 * t), int(232 - 10 * t)))
    pastel.save(design.ASSETS_DIR / "pastel.png")

    frames = []
    stars = [((i * 97) % 784, (i * 61) % 330, i % 4) for i in range(70)]
    for f in range(8):
        im = Image.new("RGB", (784, 330), (24, 16, 64))
        d = ImageDraw.Draw(im)
        for sx, sy, phase in stars:
            b = 120 + int(135 * abs(((f + phase * 2) % 8) - 4) / 4)
            r = 1 + (phase % 2)
            d.ellipse((sx - r, sy - r, sx + r, sy + r), fill=(b, b, min(255, b + 30)))
        frames.append(im)
    frames[0].save(design.ASSETS_DIR / "stars.gif", save_all=True, append_images=frames[1:], duration=160, loop=0)


def wait_title(page, title, timeout):
    end = time.time() + timeout
    while page.title() != title:
        if time.time() > end:
            raise TimeoutError(f"{title} になりませんでした（{page.title()}）")
        time.sleep(0.05)


def fresh_server():
    """録画ごとに、設定も数字もまっさらなサーバーを立てる（2 本目に 1 本目の設定が残らないように）"""
    folder = pathlib.Path(tempfile.mkdtemp(prefix="subcounter_promo_"))
    config.CONFIG_PATH = folder / "config.json"
    design.DESIGN_PATH = folder / "design.json"
    design.ASSETS_DIR = folder / "assets"
    make_assets()
    state = server.CounterState()
    httpd = server.Server((server.HOST, PORT), state)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return state, httpd


def demo_setup(state):
    """使い方解説の録画用。ハンドルを仮のチャンネル ID にし、保存したら仮の登録者数を出す（API には接続しない）"""
    fetcher.resolve_handle = lambda api_key, handle: ("UC" + "x" * 22, None)
    original = server.save_settings

    def save_and_show(body):
        error = original(body)
        if not error:
            threading.Timer(1.2, lambda: state.apply(fetcher.FetchResult(fetcher.OK, 1230, "api"))).start()
        return error
    server.save_settings = save_and_show


def record(browser, page_path, nocap):
    state, httpd = fresh_server()
    demo_setup(state)
    video_dir = tempfile.mkdtemp(prefix="promo_video_")
    context = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=video_dir,
                                  record_video_size={"width": W, "height": H})
    page = context.new_page()
    t0 = time.time()
    page.goto(f"http://localhost:{PORT}{page_path}?token={httpd.token}{'&nocap' if nocap else ''}")
    page.evaluate("() => prepare()")
    wait_title(page, "ready", 60)
    start = time.time() - t0
    page.evaluate("() => { play(); }")
    # ページから「登録者数を変えて」と頼まれたら、サーバーの値を変える（OBS 側の本物のカウンターが動く）
    deadline, last = time.time() + 400, None
    while (title := page.title()) != "done":
        if title.startswith("bump:") and title != last:
            state.apply(fetcher.FetchResult(fetcher.OK, int(title[5:]), "api"))
            last = title
        if time.time() > deadline:
            raise TimeoutError(f"録画が終わりませんでした（{title}）")
        time.sleep(0.05)
    captions = page.evaluate("() => captions")
    chapters = page.evaluate("() => typeof chapters === 'undefined' ? [] : chapters")
    page.wait_for_timeout(500)
    context.close()
    httpd.shutdown()
    httpd.server_close()
    return page.video.path(), start, captions, chapters


def encode(ffmpeg, src, start, length, dst):
    # 準備の時間を切り落とし、どの編集ソフトでも読める H.264 / 30fps にする
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", src, "-t", f"{length:.3f}",
                    "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                    "-movflags", "+faststart", "-an", str(dst)], check=True)


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def write_script(video, captions, chapters):
    lines = [c for c in captions if c["text"]]
    ends = [c["t"] for c in captions[1:]]
    srt = []
    for i, (c, end) in enumerate(zip(lines, ends), 1):
        srt.append(f"{i}\n{srt_time(c['t'])} --> {srt_time(end - 0.3)}\n{c['text']}\n")
    (OUT / f"{video}_台本.srt").write_text("\n".join(srt), encoding="utf-8")
    text = [f"{video}の台本（ずんだもん）", "",
            "VOICEVOX にそのまま貼り付けられるよう、1 行 1 セリフにしています。",
            "かっこ内は、動画の中でセリフを始める時刻です。", ""]
    text += [f"（{c['t']:.1f} 秒）{c['text']}" for c in lines]
    text += ["", "--- VOICEVOX に貼り付ける用 ---"] + [c["text"] for c in lines]
    (OUT / f"{video}_台本.txt").write_text("\n".join(text) + "\n", encoding="utf-8")
    if chapters:
        # YouTube の概要欄にそのまま貼れるチャプター（最初は 0:00 から始める決まり）
        rows = [("0:00", "はじめに")] + [(f"{int(c['t']) // 60}:{int(c['t']) % 60:02}", c["title"] or "おわりに")
                                         for c in chapters[1:]]
        (OUT / f"{video}_チャプター.txt").write_text("\n".join(f"{t} {name}" for t, name in rows) + "\n", encoding="utf-8")


VIDEOS = {"紹介動画": "/promo", "使い方解説": "/howto"}


def main():
    ffmpeg = sys.argv[1]
    videos = sys.argv[2:] or list(VIDEOS)
    OUT.mkdir(exist_ok=True)
    server.PAGES["/promo"] = HERE / "promo.html"
    server.PAGES["/howto"] = HERE / "howto.html"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        for video in videos:
            for nocap, suffix in [(False, "字幕あり"), (True, "字幕なし")]:
                src, start, captions, chapters = record(browser, VIDEOS[video], nocap)
                name = f"{video}_{suffix}.mp4"
                # 最後に字幕を消した時刻で終える
                encode(ffmpeg, src, start, captions[-1]["t"] + 0.3, OUT / name)
                print(f"作成しました: {OUT / name}（準備 {start:.1f} 秒を切り落とし）")
                if not nocap:
                    write_script(video, captions, chapters)
        browser.close()


if __name__ == "__main__":
    main()
