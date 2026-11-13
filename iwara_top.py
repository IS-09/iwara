#!/usr/bin/env python
"""iwara 列表查询：按页抓取视频列表，按爱心数过滤，以表格形式列出。

用法:
    python iwara_top.py --pages 3 --min-likes 100
    python iwara_top.py --pages 2-5 --min-likes 100            # 指定页范围（1 起）
    python iwara_top.py --pages 3 --min-likes 100 --count 10   # 只看前 10 条
    python iwara_top.py --pages 5 --min-likes 50 --user someone
    python iwara_top.py --pages 3 --min-likes 100 --urls       # 输出链接
    python iwara_top.py --demo                                 # 只看表格样式

说明:
    * --min-likes 为「>=」，要严格大于 X 就传 X+1
    * 默认按爱心数降序；加 --keep-order 保持站点顺序（最新在前）
    * 表格打到 stdout，统计打到 stderr，方便管道
"""

import argparse
import os
import shutil
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime

try:
    from curl_cffi import requests
except ImportError:
    sys.exit("[错误] 缺少 curl_cffi，请先运行: python -m pip install --user curl_cffi")

# Windows 控制台/管道默认 GBK，日文标题会 UnicodeEncodeError，统一成 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

API = "https://api.iwara.tv/videos"

DEMO_ROWS = [
    {"id": "demo1", "title": "示例标题 Example Title With A Long Name For Truncation",
     "likes": 12431, "views": 1254331, "user": "somebody"},
    {"id": "demo2", "title": "短标题 テスト", "likes": 980, "views": 88201, "user": "another_author"},
    {"id": "demo3", "title": "中等长度标题｜带全角符号和・中点", "likes": 431, "views": 12045, "user": "third"},
]


# ---------- 显示宽度（中日韩全角算 2 列） ----------
def dwidth(s):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def fit(s, width):
    """按显示宽度左对齐填充；超长则截断并加省略号。"""
    s = s or ""
    w = dwidth(s)
    if w <= width:
        return s + " " * (width - w)
    out, acc = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if acc + cw > width - 1:
            break
        out.append(ch)
        acc += cw
    return "".join(out) + "…" + " " * (width - acc - 1)


def fit_right(s, width):
    """按显示宽度右对齐。"""
    s = s or ""
    w = dwidth(s)
    if w >= width:
        return fit(s, width)
    return " " * (width - w) + s


def clean(s):
    return " ".join((s or "").split())


def build_table(hits, footer, term_cols=None, include_url=False, title_max=52):
    """渲染表格。include_url=True 时追加「链接」列（用于落盘报告），URL 不截断。"""
    idx_w, like_w, view_w, user_w = 4, 7, 10, 16
    if include_url:
        title_w = title_max
        url_w = max(24, max((dwidth(h.get("url") or "") for h in hits), default=0))
    else:
        fixed = idx_w + like_w + view_w + user_w + 8
        title_w = max(24, min(title_max, (term_cols or 110) - fixed))
        url_w = 0

    cols = [idx_w, like_w, view_w, user_w, title_w] + ([url_w] if include_url else [])
    bar = "─" * (sum(cols) + 2 * (len(cols) - 1))

    head = [fit_right("序号", idx_w), fit_right("爱心", like_w), fit_right("播放", view_w),
            fit("作者", user_w), fit("标题", title_w)]
    if include_url:
        head.append(fit("链接", url_w))

    out = ["  ".join(head).rstrip(), bar]
    if not hits:
        out.append("（没有符合条件的视频）")
    for i, h in enumerate(hits, 1):
        url = h.get("url") or f"https://www.iwara.tv/video/{h.get('id')}"
        cells = [fit_right(str(i), idx_w), fit_right(f"{h['likes']:,}", like_w),
                 fit_right(f"{h['views']:,}", view_w), fit(h["user"] or "-", user_w),
                 fit(h["title"], title_w)]
        if include_url:
            cells.append(url.ljust(url_w) if dwidth(url) < url_w else url)
        out.append("  ".join(cells).rstrip())
    out.append(bar)
    out.append(footer)
    return out


# ---------- 网络 ----------
def detect_proxy():
    env = urllib.request.getproxies()
    p = env.get("https") or env.get("http")
    if p and "://" not in p:
        p = "http://" + p
    return p


def fetch_page(page, limit, sort, user, proxy, retries=3):
    params = {"page": page, "limit": limit, "sort": sort}
    if user:
        params["user"] = user
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(API, params=params, impersonate="chrome",
                             timeout=40, proxy=proxy)
            if r.status_code == 200:
                return r.json().get("results") or []
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"第 {page} 页请求失败（{last}）")


def parse_pages(spec):
    """'3' -> 最近3页(0..2)；'2-5' -> 第2到第5页(1..4, 1-based)"""
    spec = str(spec).strip()
    if "-" in spec:
        a, b = spec.split("-", 1)
        start, end = int(a), int(b)
        if start < 1 or end < start:
            raise ValueError(f"页范围不合法: {spec}")
        return list(range(start - 1, end)), f"第 {start}-{end} 页"
    n = int(spec)
    if n < 1:
        raise ValueError("页数必须 >= 1")
    return list(range(n)), f"最近 {n} 页"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default="3", help="最近 N 页，或页范围如 2-5（1 起）")
    ap.add_argument("--min-likes", type=int, default=0, help="爱心数下限（>=），默认 0")
    ap.add_argument("--min-views", type=int, default=0, help="播放数下限（>=），默认 0")
    ap.add_argument("--count", type=int, default=0, help="最多显示几条，0=全部")
    ap.add_argument("--user", default=None, help="只看某个作者的视频（用户名）")
    ap.add_argument("--sort", default="date",
                    choices=["date", "trending", "popular", "views", "likes"])
    ap.add_argument("--limit", type=int, default=32, help="每页条数，默认 32")
    ap.add_argument("--urls", action="store_true", help="输出视频链接而不是表格")
    ap.add_argument("--links", action="store_true", help="终端表格里也显示完整链接列")
    ap.add_argument("--save-report", default=None,
                    help="把清单写成 HTML（浏览器打开，标题和链接可点击，dl 可直接读）")
    ap.add_argument("--preview", action="store_true",
                    help="为每条生成悬停预览小片（存 previews/，首次较慢，离线可播）")
    ap.add_argument("--preview-seconds", type=float, default=6, help="预览总时长，默认 6 秒")
    ap.add_argument("--preview-segments", type=int, default=6,
                    help="从整片均匀取几段拼成（默认 6 段，每段 1 秒）")
    ap.add_argument("--preview-width", type=int, default=480, help="预览宽度，默认 480px")
    ap.add_argument("--save-urls", default=None, help="把链接单独写成纯 URL 列表")
    ap.add_argument("--keep-order", action="store_true", help="保持站点顺序，不按爱心排序")
    ap.add_argument("--demo", action="store_true", help="用假数据渲染表格，检查排版")
    ap.add_argument("--proxy", default=None, help="手动指定代理，如 http://127.0.0.1:7890")
    ap.add_argument("--no-proxy", action="store_true", help="不走代理")
    args = ap.parse_args()

    if args.demo:
        hits = DEMO_ROWS
        scope = "演示数据"
    else:
        try:
            pages, scope = parse_pages(args.pages)
        except ValueError as e:
            sys.exit(f"[错误] {e}")

        proxy = None if args.no_proxy else (args.proxy or detect_proxy())
        if not args.no_proxy:
            print(f"[proxy] {proxy or '无'}", file=sys.stderr)

        hits, seen = [], set()
        for pg in pages:
            try:
                results = fetch_page(pg, args.limit, args.sort, args.user, proxy)
            except RuntimeError as e:
                print(f"[错误] {e}", file=sys.stderr)
                break
            if not results:
                print(f"[第 {pg + 1} 页] 空页，停止", file=sys.stderr)
                break
            page_hit = 0
            for it in results:
                vid = it.get("id")
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                if it.get("private") or it.get("unlisted"):
                    continue
                likes = it.get("numLikes") or 0
                views = it.get("numViews") or 0
                if likes < args.min_likes or views < args.min_views:
                    continue
                hits.append({
                    "id": vid,
                    "title": clean(it.get("title")),
                    "likes": likes,
                    "views": views,
                    "user": clean((it.get("user") or {}).get("name")),
                    "url": f"https://www.iwara.tv/video/{vid}",
                })
                page_hit += 1
            print(f"[第 {pg + 1} 页] {len(results)} 条 -> 命中 {page_hit} 条", file=sys.stderr)
            time.sleep(0.4)

        if not args.keep_order:
            hits.sort(key=lambda x: (-x["likes"], -x["views"]))

        if args.count and args.count > 0:
            total_hit = len(hits)
            hits = hits[: args.count]
            print(f"[显示] 命中 {total_hit} 条，按 --count 只显示前 {len(hits)} 条", file=sys.stderr)

    # ---------- 悬停预览小片 ----------
    if args.preview:
        if not args.save_report:
            print("[预览] 需要同时指定 --save-report，否则没有落点", file=sys.stderr)
        elif args.demo:
            print("[预览] demo 模式跳过（假 id 无法生成）", file=sys.stderr)
        else:
            clipdir = os.path.join(os.path.dirname(os.path.abspath(args.save_report)), "previews")
            print(f"[预览] 开始生成 {len(hits)} 条"
                  f"（{args.preview_seconds:g}s / {args.preview_segments} 段均匀采样 / {args.preview_width}px）",
                  file=sys.stderr)
            attach_previews(hits, clipdir, args.preview_seconds, args.preview_width, args.preview_segments)

    # ---------- 输出 ----------
    title_line = (f"共 {len(hits)} 条 ｜ {scope} ｜ 爱心 ≥ {args.min_likes}"
                  + (f" ｜ 播放 ≥ {args.min_views}" if args.min_views else "")
                  + (f" ｜ 作者 {args.user}" if args.user and not args.demo else ""))

    if args.urls:
        for h in hits:
            print(h.get("url") or f"https://www.iwara.tv/video/{h.get('id')}")
    else:
        try:
            term_cols = shutil.get_terminal_size(fallback=(110, 24)).columns
        except Exception:
            term_cols = 110
        for ln in build_table(hits, title_line, term_cols=term_cols, include_url=args.links):
            print(ln)

    # ---------- 落盘：可点击链接的 HTML 清单 ----------
    if args.save_report:
        try:
            write_html_report(args.save_report, hits, scope, args, title_line)
            print(f"[已保存] {len(hits)} 条 -> {args.save_report}", file=sys.stderr)
        except OSError as e:
            print(f"[警告] 写入 {args.save_report} 失败: {e}", file=sys.stderr)

    if args.save_urls:
        try:
            with open(args.save_urls, "w", encoding="utf-8") as f:
                for h in hits:
                    url = h.get("url") or f"https://www.iwara.tv/video/{h.get('id')}"
                    f.write(url + "\n")
            print(f"[已保存] {len(hits)} 条链接 -> {args.save_urls}", file=sys.stderr)
        except OSError as e:
            print(f"[警告] 写入 {args.save_urls} 失败: {e}", file=sys.stderr)


def attach_previews(hits, outdir, seconds, width, segments=6, workers=4):
    """为每条下载 preview 流并切成本地小片，写入 h['clip'] / h['thumb']。"""
    try:
        import iwara_preview as P
    except Exception as e:
        print(f"[预览] 无法加载 iwara_preview：{e}", file=sys.stderr)
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed

    proxy = P.detect_proxy()
    done = 0

    def work(h):
        try:
            info = P.make_clip(h["id"], outdir, proxy, seconds, width, segments)
            return h, info
        except Exception as e:
            return h, {"ok": False, "err": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, h) for h in hits]
        for fut in as_completed(futs):
            h, info = fut.result()
            done += 1
            if info.get("ok"):
                h["clip"] = f"previews/{h['id']}.mp4"
                fid = info.get("file_id") or ""
                if fid:
                    h["thumb"] = f"https://files.iwara.tv/image/thumbnail/{fid}/thumbnail-00.jpg"
                print(f"[预览 {done}/{len(hits)}] {h['id']} 完成", file=sys.stderr)
            else:
                print(f"[预览 {done}/{len(hits)}] {h['id']} 失败：{info.get('err', '')}", file=sys.stderr)


def write_html_report(path, hits, scope, args, title_line):
    """写一份可在浏览器打开的清单：标题与链接可点击，可选悬停预览。"""
    import html as H

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cond = (f"{scope} ｜ 爱心 ≥ {args.min_likes}"
            + (f" ｜ 播放 ≥ {args.min_views}" if args.min_views else "")
            + (f" ｜ 作者 {args.user}" if args.user else "")
            + f" ｜ 排序 {args.sort}")

    with_preview = any(h.get("clip") for h in hits)
    ncol = 7 if with_preview else 6

    rows = []
    for i, h in enumerate(hits, 1):
        url = h.get("url") or f"https://www.iwara.tv/video/{h.get('id')}"
        eurl = H.escape(url, quote=True)
        cells = []
        if with_preview:
            thumb = H.escape(h.get("thumb") or "", quote=True)
            clip = H.escape(h.get("clip") or "", quote=True)
            poster = f'<img class="poster" src="{thumb}" loading="lazy" alt="">' if thumb else ""
            video = (f'<video class="clip" src="{clip}" muted loop playsinline preload="none"></video>'
                     if clip else "")
            badge = '<span class="badge">▶</span>' if clip else ""
            cells.append(f'<td class="thumb"><a href="{eurl}" target="_blank" rel="noopener">'
                         f'<div class="frame">{poster}{video}{badge}</div></a></td>')
        cells += [
            f'<td class="num">{i}</td>',
            f'<td class="num">{h["likes"]:,}</td>',
            f'<td class="num">{h["views"]:,}</td>',
            f'<td class="who">{H.escape(h["user"] or "-")}</td>',
            f'<td class="title"><a href="{eurl}" target="_blank" rel="noopener">{H.escape(h["title"])}</a></td>',
            f'<td class="url"><a href="{eurl}" target="_blank" rel="noopener">{H.escape(url)}</a></td>',
        ]
        rows.append("<tr>" + "".join(cells) + "</tr>")

    body = ("\n".join(rows) if rows
            else f'<tr><td colspan="{ncol}" class="empty">（没有符合条件的视频）</td></tr>')

    head_cells = ('<th class="thumb">预览</th>' if with_preview else "") + \
        '<th class="num">序号</th><th class="num">爱心</th><th class="num">播放</th><th>作者</th><th>标题</th><th>链接</th>'

    preview_note = ('<p class="hint">鼠标移到缩略图上会播放 6 秒无声预览（已存本地，离线可用）。</p>'
                    if with_preview else "")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iwara 清单 - {H.escape(now)}</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 28px 32px 48px;
    font: 14px/1.6 "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif;
    color: #1f2328; background: #f6f8fa;
  }}
  header {{ margin-bottom: 18px; }}
  h1 {{ margin: 0 0 6px; font-size: 22px; font-weight: 600; }}
  .meta {{ margin: 0; color: #57606a; font-size: 13px; }}
  .hint {{ margin: 8px 0 0; color: #6e7781; font-size: 12.5px; }}
  .hint code {{ background: #eaeef2; padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
  table {{
    width: 100%; border-collapse: separate; border-spacing: 0;
    background: #fff; border: 1px solid #d0d7de; border-radius: 8px; overflow: hidden;
  }}
  thead th {{
    position: sticky; top: 0; z-index: 2;
    background: #f0f3f6; text-align: left; font-weight: 600; font-size: 12.5px;
    color: #57606a; padding: 9px 12px; border-bottom: 1px solid #d0d7de; white-space: nowrap;
  }}
  tbody td {{ padding: 8px 12px; border-bottom: 1px solid #eaeef2; vertical-align: top; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:nth-child(even) {{ background: #fbfcfd; }}
  tbody tr:hover {{ background: #eef5ff; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .who {{ color: #57606a; white-space: nowrap; }}
  .title a {{ color: #0b5cad; text-decoration: none; font-weight: 500; }}
  .title a:hover {{ text-decoration: underline; }}
  .url {{ width: 30%; }}
  .url a {{ color: #6e7781; text-decoration: none; font-family: Consolas, "Cascadia Mono", monospace;
            font-size: 11.5px; word-break: break-all; }}
  .url a:hover {{ color: #0b5cad; text-decoration: underline; }}
  .thumb {{ width: 168px; padding: 6px 12px !important; }}
  .frame {{
    position: relative; width: 160px; height: 90px; border-radius: 6px; overflow: hidden;
    background: #0b0f14; display: block;
  }}
  .frame .poster, .frame .clip {{
    position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block;
  }}
  .frame .clip {{ opacity: 0; transition: opacity .16s ease; }}
  tr:hover .frame .clip {{ opacity: 1; }}
  .badge {{
    position: absolute; right: 5px; bottom: 5px; z-index: 1;
    font-size: 10px; color: #fff; background: rgba(0,0,0,.55);
    padding: 1px 5px; border-radius: 9px;
  }}
  .empty {{ text-align: center; color: #6e7781; padding: 24px 0; }}
  footer {{ margin-top: 14px; color: #6e7781; font-size: 12.5px; }}
</style>
</head>
<body>
<header>
  <h1>iwara 清单</h1>
  <p class="meta">{H.escape(cond)} · 共 {len(hits)} 条 · {now}</p>
  <p class="hint">点<strong>标题</strong>或<strong>链接</strong>都会在新标签页打开。序号就是工具里 <code>dl --skip 2,5</code> / <code>dl --only 1,4</code> 用的编号。</p>
  {preview_note}
</header>
<table>
  <thead>
    <tr>{head_cells}</tr>
  </thead>
  <tbody>
{body}
  </tbody>
</table>
<footer>{H.escape(title_line)}</footer>
<script>
(function () {{
  document.querySelectorAll("tr").forEach(function (tr) {{
    var v = tr.querySelector("video.clip");
    if (!v) return;
    tr.addEventListener("mouseenter", function () {{ v.play().catch(function () {{}}); }});
    tr.addEventListener("mouseleave", function () {{
      try {{ v.pause(); v.currentTime = 0; }} catch (e) {{}}
    }});
  }});
}})();
</script>
</body>
</html>
"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
