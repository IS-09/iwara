#!/usr/bin/env python
"""下载前体积闸门：解析某个视频要下的最高画质体积，判断是否达到下限。

退出码:
    0  体积达标，可以下载
    3  体积低于下限，建议跳过
    1  探测失败（调用方应放行，不要因探测失败阻断下载）

用法:
    python iwara_size.py <video_url> --min 100M
    python iwara_size.py <video_url> --min 100M --format 540
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request

try:
    from curl_cffi import requests
except ImportError:
    sys.exit(1)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


UNITS = [
    ("GIB", 1 << 30), ("MIB", 1 << 20), ("KIB", 1 << 10),
    ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10),
    ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10), ("B", 1),
]


def parse_bytes(text):
    """100M / 100MB / 104857600 都按 1024 进制解释。"""
    t = str(text).strip().upper().replace(" ", "").replace("_", "")
    mult = 1
    for suf, m in UNITS:
        if t.endswith(suf) and len(t) > len(suf):
            mult = m
            t = t[: -len(suf)]
            break
    return int(float(t) * mult)


def find_ytdlp():
    p = shutil.which("yt-dlp")
    if p:
        return p
    cand = os.path.join(os.environ.get("APPDATA", ""), r"Python\Python314\Scripts\yt-dlp.exe")
    return cand if os.path.exists(cand) else "yt-dlp"


def detect_proxy():
    env = urllib.request.getproxies()
    p = env.get("https") or env.get("http")
    if p and "://" not in p:
        p = "http://" + p
    return p


def get_size(url, proxy):
    """files.iwara.tv 不响应 HEAD(405)，用 Range 请求读 content-range 里的总长度。"""
    r = requests.get(url, impersonate="chrome", timeout=40, proxy=proxy,
                     headers={"Range": "bytes=0-1"}, stream=True)
    try:
        cr = r.headers.get("content-range")
        if cr and "/" in cr:
            return int(cr.split("/")[-1])
        cl = r.headers.get("content-length")
        if cl and r.status_code in (200, 206):
            return int(cl)
        return None
    finally:
        r.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--min", default="0", help="体积下限，如 100M / 104857600 / 0 表示不过滤")
    ap.add_argument("--format", default=None, help="指定格式 id（preview/360/540/Source）")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--no-proxy", action="store_true")
    args = ap.parse_args()

    try:
        min_bytes = parse_bytes(args.min)
    except Exception:
        print(f"无法解析 --min: {args.min}", file=sys.stderr)
        sys.exit(1)

    proxy = None if args.no_proxy else (args.proxy or detect_proxy())

    ytdlp = find_ytdlp()
    proc = subprocess.run(
        [ytdlp, "-J", "--no-warnings", "--impersonate", "chrome", args.url],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print((proc.stderr or "抓取失败").strip().splitlines()[-1], file=sys.stderr)
        sys.exit(1)

    try:
        info = json.loads(proc.stdout)
    except Exception:
        print("无法解析 yt-dlp 输出", file=sys.stderr)
        sys.exit(1)

    formats = info.get("formats") or []
    if not formats:
        print("没有可用格式", file=sys.stderr)
        sys.exit(1)

    if args.format:
        chosen = next((f for f in formats if str(f.get("format_id")) == str(args.format)), None)
        if not chosen:
            ids = ",".join(str(f.get("format_id")) for f in formats)
            print(f"没有格式 {args.format}，可选: {ids}", file=sys.stderr)
            sys.exit(1)
    else:
        # 与 yt-dlp 默认的 best 选择一致：quality 优先（preview<360<540<Source）
        chosen = max(formats, key=lambda f: (f.get("quality") or 0, f.get("height") or 0))

    size = None
    try:
        size = get_size(chosen["url"], proxy)
    except Exception as e:
        print(f"体积探测失败: {type(e).__name__}", file=sys.stderr)
        sys.exit(1)

    if not size:
        print("体积未知", file=sys.stderr)
        sys.exit(1)

    mb = size / 1048576
    fmt = chosen.get("format_id")
    if size < min_bytes:
        print(f"{mb:.1f} MB (格式 {fmt}) < 下限 {min_bytes/1048576:.0f} MB -> 跳过", file=sys.stderr)
        sys.exit(3)

    print(f"{mb:.1f} MB (格式 {fmt})", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
