#!/usr/bin/env python
"""为 HTML 清单生成悬停预览小片（本地 6 秒无声 mp4）。

iwara 的 preview 流是带 expires 签名的临时链接（实测仅 1 小时），
所以这里直接下下来切一小段存本地，清单离线也能用。

用法（一般由 iwara_top.py --preview 调用）:
    python iwara_preview.py --id <video_id> --outdir downloads/previews
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

try:
    from curl_cffi import requests
except ImportError:
    sys.exit("[错误] 缺少 curl_cffi")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SALT = "mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"
API_VIDEO = "https://api.iwara.tv/video/{id}"


def detect_proxy():
    env = urllib.request.getproxies()
    p = env.get("https") or env.get("http")
    if p and "://" not in p:
        p = "http://" + p
    return p


def proto_fix(u):
    """src.view 常常是 //host/path 形式，yt-dlp 也是这么补的。"""
    if not u:
        return u
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://www.iwara.tv" + u
    return u


def resolve(video_id, proxy):
    """返回 (preview_url, file_id)。"""
    v = requests.get(API_VIDEO.format(id=video_id), impersonate="chrome",
                     timeout=40, proxy=proxy).json()
    fileurl = v.get("fileUrl")
    if not fileurl:
        raise RuntimeError("没有 fileUrl")
    file_id = ((v.get("file") or {}).get("id")) or ""

    up = urllib.parse.urlparse(fileurl)
    q = urllib.parse.parse_qs(up.query)
    expires = q["expires"][0]
    xv = hashlib.sha1("_".join((up.path.rstrip("/").split("/")[-1], expires, SALT)).encode()).hexdigest()
    data = requests.get(fileurl, impersonate="chrome", timeout=40, proxy=proxy,
                        headers={"X-Version": xv}).json()

    by_name = {d.get("name"): d for d in data}
    pick = by_name.get("preview") or by_name.get("360") or (data[-1] if data else None)
    if not pick:
        raise RuntimeError("没有可用清晰度")
    url = proto_fix(((pick.get("src") or {}).get("view")) or ((pick.get("src") or {}).get("download")))
    if not url:
        raise RuntimeError("没有可下载链接")
    duration = float(((v.get("file") or {}).get("duration")) or 0) or None
    return url, file_id, duration


def parse_duration(value):
    """ffprobe 输出 '123.456000' -> 秒。"""
    try:
        return float(str(value).strip())
    except Exception:
        return None


def probe_duration(path, ffprobe):
    if not ffprobe:
        return None
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return parse_duration(r.stdout)
    except Exception:
        return None


def plan_segments(duration, total, count):
    """把 total 秒均匀分配到整条片子的 count 段上。

    返回 [(start, dur), ...]。片长不足时自动缩短段长/段数，避免越界。
    """
    count = max(1, int(count))
    total = max(0.5, float(total))
    if not duration or duration <= 0.5:
        return [(0.0, total)]

    # 段数不能多过片长的整秒数，段长也不能超过均分
    count = max(1, min(count, int(duration) or 1))
    seg = min(total / count, duration / count)
    if count == 1:
        return [(max(0.0, (duration - seg) / 2), seg)]

    span = max(0.0, duration - seg)
    starts = [span * i / (count - 1) for i in range(count)]
    # 末段贴边时留一点余量，避免 trim 取到空
    starts[-1] = min(starts[-1], max(0.0, duration - seg - 0.05))
    return [(round(s, 3), round(seg, 3)) for s in starts]


def build_filter(segs, width):
    """生成 trim+concat 滤镜串。"""
    parts = []
    pads = []
    for i, (start, dur) in enumerate(segs):
        parts.append(
            f"[0:v]trim=start={start:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:-2,setsar=1[v{i}]"
        )
        pads.append(f"[v{i}]")
    chain = ";".join(parts)
    chain += ";" + "".join(pads) + f"concat=n={len(segs)}:v=1:a=0[out]"
    return chain


def download(url, dest, proxy):
    r = requests.get(url, impersonate="chrome", timeout=120, proxy=proxy, stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"下载失败 HTTP {r.status_code}")
    with open(dest, "wb") as f:
        for chunk in r.iter_content(256 * 1024):
            if chunk:
                f.write(chunk)
    r.close()
    return os.path.getsize(dest)


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    cand = os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\WinGet\Links\ffmpeg.exe")
    return cand if os.path.exists(cand) else None


def probe_info(path, ffprobe):
    """返回 (duration, fps, nb_frames)。"""
    if not ffprobe:
        return None, None, None
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=r_frame_rate,avg_frame_rate,nb_frames",
                            "-show_entries", "format=duration",
                            "-of", "json", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        d = json.loads(r.stdout or "{}")
        dur = None
        try:
            dur = float(d.get("format", {}).get("duration"))
        except Exception:
            pass
        st = (d.get("streams") or [{}])[0]
        fps = None
        for key in ("avg_frame_rate", "r_frame_rate"):
            v = st.get(key)
            if v and "/" in str(v):
                a, b = str(v).split("/")
                try:
                    if float(b) != 0:
                        fps = float(a) / float(b)
                        break
                except Exception:
                    pass
        nb = None
        try:
            nb = int(st.get("nb_frames"))
        except Exception:
            pass
        return dur, fps, nb
    except Exception:
        return None, None, None


def make_frames_clip(raw, segs, out, width, ffmpeg, td, fps=None, nframes=None):
    """按取样点抽单帧，再按 1 fps 编成 slideshow 片段。

    iwara 的 preview 流是 320x180@1fps，一帧=一秒。已知帧率时直接用
    帧号 select 抽取，避免 -ss 快速定位在稀疏帧率上偏 ±1 帧。
    """
    sparse = bool(fps) and fps <= 3
    picked = []
    scale = f"scale={width}:-2,setsar=1"

    def grab(extra_args, vf, png):
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", raw] + extra_args
        cmd += ["-vf", vf, "-frames:v", "1", "-fps_mode", "passthrough", png]
        subprocess.run(cmd, capture_output=True, text=True)
        return os.path.exists(png) and os.path.getsize(png) > 0

    for i, (start, dur) in enumerate(segs):
        png = os.path.join(td, f"p{i:03d}.png")
        t = start + dur / 2.0          # 该段中点
        got = False

        if sparse:
            idx = int(round(t * fps))
            if nframes:
                idx = max(0, min(idx, nframes - 1))
            # 帧号精确定位：select 必须和 scale 合并成一个 -vf
            got = grab([], f"select=eq(n\\,{idx}),{scale}", png)

        if not got:
            for tt in (t, start, max(0.0, start - dur / 2.0)):
                # -ss 放在 -i 之后是慢速精确定位
                if grab(["-ss", f"{tt:.3f}"], scale, png):
                    got = True
                    break
        if got:
            picked.append(png)

    if not picked:
        raise RuntimeError("没有抽到任何画面")

    seq = []
    for i, p in enumerate(picked):
        dst = os.path.join(td, f"s{i:03d}.png")
        os.replace(p, dst)
        seq.append(dst)

    total = sum(d for _, d in segs)
    out_fps = max(1.0, len(seq) / total)   # 6 帧 / 6 秒 = 1 fps
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-framerate", f"{out_fps:.3f}", "-i", os.path.join(td, "s%03d.png"),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
           "-pix_fmt", "yuv420p", "-r", f"{out_fps:.3f}",
           "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ffmpeg 失败").strip().splitlines()[-1])


def make_clip(video_id, outdir, proxy=None, seconds=6, width=480, segments=6):
    """下 preview 流，按整片时长均匀取 segments 段拼成 seconds 秒无声小片。"""
    proxy = proxy if proxy is not None else detect_proxy()
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"{video_id}.mp4")
    info = {"id": video_id, "ok": False, "path": out, "file_id": "", "err": ""}

    url, file_id, api_duration = resolve(video_id, proxy)
    info["file_id"] = file_id
    if os.path.exists(out) and os.path.getsize(out) > 4096:
        info["ok"] = True
        info["cached"] = True
        return info

    ffmpeg = find_ffmpeg()
    ffprobe = None
    if ffmpeg:
        cand = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe")
        ffprobe = cand if os.path.exists(cand) else (shutil.which("ffprobe") or None)

    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "raw.mp4")
        download(url, raw, proxy)

        if ffmpeg:
            probed_dur, src_fps, src_frames = probe_info(raw, ffprobe)
            duration = probed_dur or api_duration
            segs = plan_segments(duration, seconds, segments)
            info["duration"] = round(duration, 1) if duration else None
            info["segments"] = len(segs)
            info["starts"] = [s for s, _ in segs]
            info["src_fps"] = round(src_fps, 2) if src_fps else None

            if src_fps and src_fps <= 3:
                # 稀疏帧（iwara preview 就是 1fps）：按帧号精确抽帧再编码
                make_frames_clip(raw, segs, out, width, ffmpeg, td,
                                 fps=src_fps, nframes=src_frames)
            else:
                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                       "-i", raw,
                       "-filter_complex", build_filter(segs, width),
                       "-map", "[out]", "-an",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                       "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    # 滤镜路径失败再退回抽帧
                    make_frames_clip(raw, segs, out, width, ffmpeg, td)
        else:
            # 没有 ffmpeg 就退而求其次：直接用完整 preview（体积大但能播）
            shutil.copyfile(raw, out)

    if os.path.getsize(out) < 4096:
        raise RuntimeError("产出的预览片为空")
    info["ok"] = True
    info["bytes"] = os.path.getsize(out)
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--seconds", type=float, default=6, help="成片总时长")
    ap.add_argument("--segments", type=int, default=6, help="从整片均匀取几段")
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--no-proxy", action="store_true")
    a = ap.parse_args()
    proxy = None if a.no_proxy else (a.proxy or detect_proxy())
    try:
        info = make_clip(a.id, a.outdir, proxy, a.seconds, a.width, a.segments)
        print(json.dumps(info, ensure_ascii=False))
        sys.exit(0 if info["ok"] else 1)
    except Exception as e:
        print(json.dumps({"id": a.id, "ok": False, "err": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
