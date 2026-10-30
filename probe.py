"""
Phase 1 検証: YouTube Studio から正確な登録者数を取れるか調べる最小構成

準備:
  pip install playwright
  （Google Chrome がインストール済みであること）

使い方:
  python probe.py login   初回のみ。素の Chrome が開くので Google ログインし、
                          Studio のダッシュボードが表示されたら Chrome を閉じる
  python probe.py probe   ヘッドレスで 1 回取得。DOM と内部通信の両方から候補を出力し、
                          内部通信の JSON を dumps/ に保存
  python probe.py watch   60 秒ごとに取得を繰り返す（既定 30 分で自動終了、--minutes=N で変更）
                          終了時（Ctrl+C 含む）に集計を表示し watch_*.csv に保存
"""
import datetime
import json
import pathlib
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

BASE = pathlib.Path(__file__).resolve().parent
PROFILE = BASE / "chrome_profile"   # ログイン状態はここに保存（Cookie は Windows 側で暗号化される）
DUMP = BASE / "dumps"
STUDIO = "https://studio.youtube.com/"
INTERVAL_SEC = 60
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_chrome():
    for p in CHROME_CANDIDATES:
        if pathlib.Path(p).exists():
            return p
    sys.exit("chrome.exe が見つかりません。CHROME_CANDIDATES を編集してください。")


def login():
    # Playwright を介さず素の Chrome で開く。
    # 自動化ブラウザからの Google ログインは拒否されやすいため。
    PROFILE.mkdir(exist_ok=True)
    print("Chrome を開きます。ログインして Studio が表示されたら Chrome を閉じてください。")
    subprocess.run([find_chrome(), f"--user-data-dir={PROFILE}", STUDIO])
    print("完了。次は `python probe.py probe` を実行してください。")


def find_keys(obj, path=""):
    """JSON 内で subscriber を含むキー名・文字列値を探す"""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if "subscriber" in k.lower() and not isinstance(v, (dict, list)):
                hits.append((p, v))
            hits += find_keys(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += find_keys(v, f"{path}[{i}]")
    elif isinstance(obj, str) and "SUBSCRIBER" in obj.upper():
        hits.append((path, obj))
    return hits


def summarize(records):
    """watch の結果を集計して表示し、CSV に保存する"""
    if not records:
        print("記録なし")
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BASE / f"watch_{stamp}.csv"
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("時刻,結果,内部通信,DOM\n")
        for r in records:
            f.write(",".join(f'"{x}"' for x in r) + "\n")

    total = len(records)
    ok = sum(1 for r in records if r[1] == "成功")
    dom_ok = sum(1 for r in records if r[3])
    mismatch = sum(1 for r in records if r[2] and r[3] and r[2] != r[3].replace(",", ""))
    print("\n========== 集計 ==========")
    print(f"期間        : {records[0][0]} 〜 {records[-1][0]}")
    print(f"試行回数    : {total}")
    print(f"内部通信成功: {ok} / {total}")
    print(f"DOM 成功    : {dom_ok} / {total}")
    print(f"両者の不一致: {mismatch} 回")
    for kind in ("取得できず", "読み込み失敗", "認証切れ"):
        n = sum(1 for r in records if r[1] == kind)
        if n:
            print(f"{kind:<10}: {n} 回")
    print("値の変化    :")
    prev = None
    for r in records:
        if r[2] and r[2] != prev:
            print(f"  {r[0]}  {r[2]}")
            prev = r[2]
    print(f"記録ファイル: {path.name}")


def run(watch=False):
    if not PROFILE.exists():
        sys.exit("先に `python probe.py login` を実行してください。")
    DUMP.mkdir(exist_ok=True)

    with sync_playwright() as p:
        headless = not (SHOW or OFFSCREEN)
        opts = dict(
            channel="chrome", headless=headless,
            args=["--window-position=-32000,-32000", "--window-size=1280,900"] if OFFSCREEN else [],
        )
        if headless:
            # ヘッドレス Chrome は UA に "HeadlessChrome" が入り、Studio に非対応ブラウザ扱いされる。
            # 一度起動して UA を読み、"Chrome" に置き換えて起動し直す。
            tmp = p.chromium.launch_persistent_context(str(PROFILE), **opts)
            ua = tmp.pages[0].evaluate("navigator.userAgent") if tmp.pages else tmp.new_page().evaluate("navigator.userAgent")
            tmp.close()
            opts["user_agent"] = ua.replace("HeadlessChrome", "Chrome")
            print(f"--- UA 置換: {'あり' if 'HeadlessChrome' in ua else 'なし（元から通常UA）'}")
        ctx = p.chromium.launch_persistent_context(str(PROFILE), **opts)
        hosts = {}

        def on_any(res):
            h = res.url.split("/")[2] if "://" in res.url else res.url
            hosts[h] = hosts.get(h, 0) + 1

        ctx.on("response", on_any)
        page = ctx.new_page()
        net_hits = []

        def on_response(res):
            if "youtubei/v1" not in res.url:
                return
            try:
                data = res.json()
            except Exception:
                return
            name = re.sub(r"\W+", "_", res.url.split("youtubei/v1/")[-1].split("?")[0])
            if not watch:
                stamp = datetime.datetime.now().strftime("%H%M%S_%f")
                (DUMP / f"{stamp}_{name}.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
                )
            for path, v in find_keys(data):
                net_hits.append((name, path, v))

        page.on("response", on_response)

        records = []  # (時刻, 結果, 内部通信の値, DOM の値)
        deadline = time.time() + WATCH_MIN * 60
        try:
            while True:
                net_hits.clear()
                now = datetime.datetime.now().strftime("%H:%M:%S")
                try:
                    page.goto(STUDIO, wait_until="networkidle", timeout=60000)
                except Exception as e:
                    print(f"[{now}] 読み込み失敗: {type(e).__name__}")
                    records.append((now, "読み込み失敗", "", ""))
                    if not watch or time.time() >= deadline:
                        break
                    time.sleep(INTERVAL_SEC)
                    continue

                if "accounts.google.com" in page.url:
                    print(f"[{now}] 未認証または認証切れ。`python probe.py login` を実行してください。")
                    records.append((now, "認証切れ", "", ""))
                    break

                page.wait_for_timeout(5000)  # ダッシュボードのカード描画待ち

                lines = [l.strip() for l in page.inner_text("body").splitlines() if l.strip()]
                dom_val = ""
                for i, l in enumerate(lines[:-1]):
                    if l == "現在のチャンネル登録者数":
                        dom_val = lines[i + 1]
                        break
                target = [v for n, path, v in net_hits
                          if n == "creator_get_creator_channels" and path == ".channels[0].metric.subscriberCount"]
                net_val = str(target[0]) if target else ""
                records.append((now, "成功" if net_val else "取得できず", net_val, dom_val))

                if watch:
                    print(f"[{now}] 内部通信: {net_val or '取得できず'} / DOM: {dom_val or '取得できず'}")
                else:
                    print(f"\n===== [{now}] {page.url}")
                    # ---- 診断情報（値は出さず、有無だけ表示） ----
                    print(f"--- タイトル: {page.title()!r}")
                    cookies = ctx.cookies()
                    names = {c["name"] for c in cookies if "google" in c["domain"] or "youtube" in c["domain"]}
                    for key in ("SID", "SAPISID", "__Secure-3PSID", "LOGIN_INFO"):
                        print(f"--- Cookie {key}: {'あり' if key in names else 'なし'}")
                    print(f"--- 通信先（件数）: {dict(sorted(hosts.items(), key=lambda x: -x[1])[:10])}")
                    page.screenshot(path=str(DUMP / "screen.png"), full_page=True)
                    (DUMP / "page.html").write_text(page.content(), encoding="utf-8")
                    print("--- dumps/screen.png と dumps/page.html を保存")
                    print("--- DOM 候補（ラベル行とその直後 2 行）")
                    for i, l in enumerate(lines):
                        if "登録者" in l or "subscriber" in l.lower():
                            print("  " + " | ".join(lines[i:i + 3]))
                    print(f"### 取得値（内部通信）: {net_val or '取得できず'}")
                    print("--- 内部通信候補")
                    for name, path, v in net_hits[:50]:
                        print(f"  {name}{path} = {v}")
                    if not net_hits:
                        print("  （なし。dumps/ の JSON を目視で確認してください）")

                if not watch or time.time() >= deadline:
                    break
                time.sleep(INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n手動で停止しました。")

        if watch:
            summarize(records)
        ctx.close()


SHOW = "--show" in sys.argv  # 付けると画面ありで実行（何が起きているか目視する用）
WATCH_MIN = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--minutes=")), 30)  # watch の実行時間（分）
OFFSCREEN = "--offscreen" in sys.argv  # 通常の Chrome を画面外に開く（ヘッドレスが弾かれる場合の代替）

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode = args[0] if args else "probe"
    if mode == "login":
        login()
    elif mode in ("probe", "watch"):
        run(watch=(mode == "watch"))
    else:
        sys.exit("使い方: python probe.py [login|probe|watch]")