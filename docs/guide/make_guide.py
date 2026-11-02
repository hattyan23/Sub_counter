"""
説明書の PDF（docs\\導入ガイド.pdf）を作る

  python docs\\guide\\make_guide.py

実物の画面を一時フォルダの設定で開いてスクリーンショットを撮り、guide.html に入れて PDF にする。
img\\tray_desktop.png（タスクバー右下の写真）だけは手で用意したもので、このスクリプトでは作り直さない。
API キーは使わない（登録者数はこのスクリプトが仮の値を入れる）。
必要なもの: playwright（python -m playwright install chromium）、pillow
"""
import os
import pathlib
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
IMG = HERE / "img"
OUTPUT = HERE.parent / "導入ガイド.pdf"

# 本物の設定や素材に触れないよう、読み込む前に保存先を一時フォルダーにする
os.environ["YT_COUNTER_DATA_DIR"] = tempfile.mkdtemp(prefix="subcounter_guide_")
sys.path.insert(0, str(ROOT))
import fetcher  # noqa: E402
import server  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PORT = 8790
BASE = f"http://localhost:{PORT}"
CHANNEL_ID = "UC" + "x" * 22
ACCENT = (229, 57, 53)
FONT = ImageFont.truetype(r"C:\Windows\Fonts\meiryob.ttc", 26)


def mark(path, boxes, pad=8):
    """画面の写真に、番号つきの赤い枠を描き込む"""
    image = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(image)
    for number, box in enumerate(boxes, 1):
        x, y, w, h = box["x"] - pad, box["y"] - pad, box["width"] + pad * 2, box["height"] + pad * 2
        draw.rounded_rectangle((x, y, x + w, y + h), radius=10, outline=ACCENT, width=5)
        r = 22
        cx, cy = max(r, x - 4), max(r, y - 4)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ACCENT)
        draw.text((cx, cy), str(number), font=FONT, fill="white", anchor="mm")
    image.save(path)


def box_of(page, selector):
    return page.locator(selector).first.bounding_box()


def wait_animations(frame):
    """ボタンを押してからプレビューに届くまで少しかかるので、動き始めるのを待つ"""
    for _ in range(100):
        if frame.locator("body").evaluate("() => document.getAnimations().length"):
            return
        time.sleep(0.01)
    raise RuntimeError("アニメーションが始まりませんでした")


def pause_at(frame, ms):
    """アニメーションを止めて、指定の時間の様子にする"""
    frame.locator("body").evaluate(
        "(b, ms) => document.getAnimations().forEach((a) => { a.pause(); a.currentTime = ms; })", ms)


def main():
    IMG.mkdir(exist_ok=True)
    state = server.CounterState()
    httpd = server.Server((server.HOST, PORT), state)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)

        # OBS に出る案内（数字がまだないとき）
        counter = browser.new_page(viewport={"width": 960, "height": 400})
        counter.goto(f"{BASE}/")
        for status, name in [(fetcher.NOT_CONFIGURED, "notice_setup"), (fetcher.ERROR, "notice_dash")]:
            state.apply(fetcher.FetchResult(status, detail="guide"))
            counter.wait_for_timeout(400)
            counter.screenshot(path=IMG / f"{name}.png", omit_background=True)

        # 初期設定の画面
        page = browser.new_page(viewport={"width": 680, "height": 900}, device_scale_factor=1.5)
        page.goto(httpd.settings_url())
        page.wait_for_timeout(500)
        page.screenshot(path=IMG / "settings.png", full_page=True)
        mark(IMG / "settings.png", [{k: v * 1.5 for k, v in box_of(page, s).items()}
                                     for s in ("label.check", "#key", "#channel", "#save")])
        page.check("#terms")
        page.fill("#key", "guide-dummy-key")
        page.fill("#channel", CHANNEL_ID)
        page.click("#save")
        page.wait_for_timeout(300)
        state.apply(fetcher.FetchResult(fetcher.OK, 1230, "api"))
        page.wait_for_selector("#done:not([hidden])")
        # 撮影用に別のポートで動かしているので、実際に表示される URL にそろえる
        page.locator("#obs-url").evaluate("e => e.textContent = 'http://localhost:8765/'")
        # 保存したときに画面が動くので、ページの先頭からの位置で切り取る
        top, bottom = page.evaluate("""() => [
            document.querySelector('#save').getBoundingClientRect().top + scrollY - 16,
            document.querySelector('#done').getBoundingClientRect().bottom + scrollY + 16]""")
        page.screenshot(path=IMG / "settings_done.png", full_page=True,
                        clip={"x": 0, "y": top, "width": 680, "height": bottom - top})

        # OBS に出る数字（背景は透明のまま撮り、説明書の中でゲーム画面の上に重ねる）
        counter.set_viewport_size({"width": 960, "height": 540})
        counter.wait_for_timeout(400)
        counter.screenshot(path=IMG / "counter.png", omit_background=True)

        # 見た目の設定の画面
        design = browser.new_page(viewport={"width": 1280, "height": 900})
        design.goto(httpd.settings_url("design"))
        design.wait_for_timeout(800)
        design.screenshot(path=IMG / "design.png", full_page=True)
        mark(IMG / "design.png", [box_of(design, s) for s in
                                  (".screen", ".try", "#form section:nth-of-type(1)", "#form section:nth-of-type(3)",
                                   "#form section:nth-of-type(4)", "#save")])

        # アニメーションの途中の様子（増えると上、減ると下）
        frame = design.frame_locator("#screen")
        screen = design.locator(".screen")
        design.fill("#duration_ms", "1000")
        for animation, step, times in [("slide", 1, (0, 300, 1000)), ("odometer", 1, (0, 450, 1300)),
                                       ("odometer", -1, (0, 450, 1300))]:
            design.select_option("#animation", animation)
            design.dispatch_event("#animation", "change")
            design.wait_for_timeout(200)
            design.click(f"[data-step='{step}']")
            wait_animations(frame)
            for i, ms in enumerate(times):
                pause_at(frame, ms)
                design.wait_for_timeout(80)
                screen.screenshot(path=IMG / f"anim_{animation}_{'up' if step > 0 else 'down'}_{i}.png")
            frame.locator("body").evaluate("() => document.getAnimations().forEach((a) => a.finish())")
            design.wait_for_timeout(1500)

        # 増えたときの演出
        design.select_option("#animation", "pop")
        for effect, wait in [("glow", 350), ("sparkle", 450), ("confetti", 500)]:
            design.select_option("#effect", effect)
            design.dispatch_event("#effect", "change")
            design.wait_for_timeout(200)
            design.click("[data-step='1']")
            design.wait_for_timeout(wait)
            screen.screenshot(path=IMG / f"fx_{effect}.png")
            design.wait_for_timeout(3000)

        # できあがった PDF
        guide = browser.new_page()
        guide.goto((HERE / "guide.html").as_uri())
        guide.wait_for_timeout(500)
        guide.pdf(path=OUTPUT, format="A4", print_background=True, display_header_footer=True,
                  header_template="<span></span>",
                  footer_template='<div style="width:100%;text-align:center;font-size:9px;color:#888">'
                                  '<span class="pageNumber"></span> / <span class="totalPages"></span></div>',
                  margin={"top": "14mm", "bottom": "16mm", "left": "14mm", "right": "14mm"})
        browser.close()
    httpd.shutdown()
    print(f"作成しました: {OUTPUT}")


if __name__ == "__main__":
    main()
