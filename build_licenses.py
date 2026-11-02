"""
配布フォルダーに、同梱しているソフトウェアのライセンス文を集める（build.ps1 から呼ぶ）

  python build_licenses.py <配布フォルダー>

ビルド用の環境（.venv-build）で実行すること。実際に同梱したバージョンのライセンス文を読むため。
"""
import importlib.metadata
import pathlib
import sys

# exe に入るもの（ビルドにだけ使うものは含めない）
PACKAGES = [
    ("pystray", "GNU LGPL v3", "トレイのアイコン"),
    ("six", "MIT", "pystray が使う互換ライブラリ"),
    ("pillow", "MIT-CMU", "アイコンの画像"),
    ("tzdata", "Apache License 2.0", "クォータが戻る時刻の計算"),
    ("pyinstaller", "GPL v2+ (bootloader exception)", "exe の起動部分"),
]

HEADER = """\
======================================================
 同梱しているソフトウェアのライセンス
======================================================

登録者数カウンターには、次のソフトウェアが含まれています。
それぞれの著作権は各作者に帰属し、以下のライセンスに従って配布しています。

{table}

■ pystray（GNU LGPL v3）について
  pystray は「_internal\\pystray」フォルダーに、ソースコードのまま入っています。
  LGPL v3 に基づき、このフォルダーを改変したものや別のバージョンに置き換えて
  使うことができます。元のソースコードは次から入手できます。
    https://github.com/moses-palmer/pystray
    https://pypi.org/project/pystray/{pystray_version}/

■ PyInstaller について
  exe の起動部分（ブートローダー）は、GPL のブートローダー例外により、
  このソフトのライセンスとは別に配布できるものです。

以下に、各ソフトウェアのライセンスの全文を載せます。
"""


def license_files(dist):
    files = [f for f in dist.files or [] if f.parts[0].endswith(".dist-info")
             and (f.name.upper().startswith(("LICENSE", "COPYING")) or "licenses" in f.parts)]
    return sorted({pathlib.Path(dist.locate_file(f)) for f in files})


def section(title, texts):
    bar = "=" * 70
    return f"\n\n{bar}\n {title}\n{bar}\n\n" + "\n\n----\n\n".join(texts)


def main():
    out_dir = pathlib.Path(sys.argv[1])
    rows, sections = [], []
    python_license = pathlib.Path(sys.base_prefix) / "LICENSE.txt"
    rows.append(f"  Python {sys.version.split()[0]:<14} PSF License                      本体（Python で作っています）")
    sections.append(section(f"Python {sys.version.split()[0]}", [python_license.read_text(encoding="utf-8")]))
    for name, license_name, use in PACKAGES:
        dist = importlib.metadata.distribution(name)
        files = license_files(dist)
        if not files:
            raise SystemExit(f"{name} のライセンス文が見つかりません")
        rows.append(f"  {dist.name} {dist.version:<{20 - len(dist.name)}} {license_name:<32} {use}")
        sections.append(section(f"{dist.name} {dist.version}（{license_name}）",
                                [f.read_text(encoding="utf-8", errors="replace") for f in files]))
    text = HEADER.format(table="\n".join(rows), pystray_version=importlib.metadata.version("pystray"))
    (out_dir / "サードパーティライセンス.txt").write_text(text + "".join(sections) + "\n", encoding="utf-8-sig")
    print(f"ライセンス文を書き出しました（{len(rows)} 件）")


if __name__ == "__main__":
    main()
