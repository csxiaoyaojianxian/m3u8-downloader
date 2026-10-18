#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
m3u8_dl.py — M3U8 自动探测并下载工具

功能:
  1. detect : 给定网页或 m3u8 链接, 自动嗅探页面中的 m3u8 地址(支持直接给定 m3u8)
  2. download: 下载 m3u8 视频, 支持 master/variant、AES-128 解密、并发分片、断点续传
  3. 合并为 .ts 文件(若系统装有 ffmpeg 且加 --mp4, 自动转封装为 .mp4)

用法:
  # 探测网页中的 m3u8 地址
  python m3u8_dl.py detect "https://example.com/video-page"

  # 下载(自动先探测再取第一个结果)
  python m3u8_dl.py download "https://example.com/video-page" -o out.mp4

  # 直接下载 m3u8 链接, 8 并发, 选最高码率
  python m3u8_dl.py download "https://example.com/index.m3u8" -w 8 --best

依赖:
  pip install requests pycryptodome
"""

import argparse
import base64
import concurrent.futures as futures
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse

import requests

try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

M3U8_PATTERNS = [
    # 直接出现在 html/JS 中的 .m3u8 链接
    r'''https?://[^\s"'\(\)<>]+?\.m3u8''',
    # 相对路径形式 src="xxx.m3u8" / "xxx.m3u8?token=..."
    r'''["'\(]([^"'\(\)\s]+\.m3u8[^"'\(\)\s]*)["'\)]''',
    # 常见播放器配置: url: "...", source: "...", file: "..." (值为 m3u8 的)
    r'''(?:url|source|file|src|playUrl|videoUrl|play_url|video_url)\s*[:=]\s*["']([^"']+\.m3u8[^"']*)["']''',
    # base64 编码的 m3u8 地址
    r'''["']([A-Za-z0-9+/=]{40,})["']''',
]


def log(msg):
    print(f"[*] {msg}", file=sys.stderr)


def err(msg):
    print(f"[!] {msg}", file=sys.stderr)


def make_session(referer=None, headers=None):
    s = requests.Session()
    h = dict(DEFAULT_HEADERS)
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    s.headers.update(h)
    return s


# ---------------------------------------------------------------- 探测部分

def try_decode_base64_m3u8(text):
    """在页面 JS 里找 base64 编码的 m3u8 地址"""
    results = []
    for blob in re.findall(r'''["']([A-Za-z0-9+/=]{60,})["']''', text):
        try:
            decoded = base64.b64decode(blob).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if ".m3u8" in decoded:
            m = re.search(r'https?://[^\s"\']+\.m3u8[^\s"\']*', decoded)
            if m:
                results.append(m.group(0))
    return results


def _fetch_limited(s, url, timeout=10, max_bytes=200000):
    """抓取内容(截断到 max_bytes), 失败返回 None"""
    try:
        r = s.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        data = b""
        for chunk in r.iter_content(32 * 1024):
            data += chunk
            if len(data) >= max_bytes:
                break
        r.close()
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return None


def classify_m3u8(s, url):
    """请求并判定一个 URL 是否 m3u8, 返回:
    None -> 不是 m3u8; 否则 {url, kind: master|media, duration, streams}
    """
    text = _fetch_limited(s, url)
    if text is None or not text.lstrip().startswith("#EXTM3U"):
        return None
    if "#EXT-X-STREAM-INF" in text:
        n_streams = len(re.findall(r"#EXT-X-STREAM-INF", text))
        return {"url": url, "kind": "master", "duration": None,
                "streams": n_streams}
    total = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text))
    return {"url": url, "kind": "media",
            "duration": round(total, 1) if total else None}


# 疑似视频接口路径关键词(用于无后缀候选 URL 筛选)
_HINT_RE = re.compile(
    r"video|play|stream|media|playlist|manifest|hls|m3u8|vod|live"
    r"|episode|movie|tv|film|source|geturl|getplay", re.I)
_STATIC_RE = re.compile(
    r"\.(js|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|html?|htm|xml|txt|pdf)([?#].*)?$", re.I)


def _collect_candidate_urls(html, base_url):
    """从页面文本收集疑似视频接口候选 URL(不限 .m3u8 后缀)"""
    cands = set()
    # 页面里所有绝对 http(s) URL
    for m in re.finditer(r'''https?://[^\s"'<>()\[\]{}\\]+''', html):
        cands.add(m.group(0).rstrip('\\').rstrip('",;'))
    # 带视频关键词的相对路径(常见于 /api/play?id=x 这类无后缀接口)
    for m in re.finditer(r'''["'](/[^"'\s]{3,300})["']''', html):
        p = m.group(1)
        if _HINT_RE.search(p) and not p.startswith("//"):
            cands.add(urljoin(base_url, p))
    # 过滤静态资源和明显的非接口地址
    return {u for u in cands
            if not _STATIC_RE.search(urlparse(u).path) and "m3u8" not in u.lower()}


def detect_m3u8_regex(url, session, timeout=15):
    """阶段1: 传统规则嗅探(正则找 HTML/JS 中的 .m3u8 字符串)"""
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        err(f"抓取页面失败: {e}")
        return []
    html = resp.text
    base_url = resp.url

    found = []  # (优先级, 链接)

    # 规则 1: 播放器配置字段(可信度最高)
    for m in re.finditer(M3U8_PATTERNS[2], html):
        found.append((0, m.group(1)))

    # 规则 2: 页面/内联 JS 中直接的 m3u8 完整链接
    for m in re.finditer(M3U8_PATTERNS[0], html):
        found.append((1, m.group(0)))

    # 规则 3: 相对路径 m3u8
    for m in re.finditer(M3U8_PATTERNS[1], html):
        found.append((2, m.group(1)))

    # 规则 4: base64 隐藏地址
    for u in try_decode_base64_m3u8(html):
        found.append((1, u))

    # 规则 5: <video>/<source> 标签
    for m in re.finditer(r'<(?:video|source)[^>]+?src\s*=\s*["\']([^"\']+)["\']', html, re.I):
        if ".m3u8" in m.group(1).lower():
            found.append((0, m.group(1)))

    # 去重并转为绝对地址
    seen, result = set(), []
    for prio, link in sorted(found, key=lambda x: x[0]):
        absu = urljoin(base_url, link.strip())
        if absu not in seen:
            seen.add(absu)
            result.append(absu)

    result = [u for u in result if "m3u8" in u.lower()]

    if not result:
        # 兜底: 嗅探页面引用的外链 JS
        log("页面未直接发现, 尝试嗅探页面引用的 JS 文件...")
        for js_url in re.findall(r'<script[^>]+src\s*=\s*["\']([^"\']+)["\']', html, re.I)[:10]:
            absu = urljoin(base_url, js_url)
            try:
                js = session.get(absu, timeout=timeout).text
            except Exception:
                continue
            for m in re.finditer(M3U8_PATTERNS[0], js):
                u = urljoin(absu, m.group(0))
                if u not in seen:
                    seen.add(u)
                    result.append(u)
            if result:
                break
    return result


def detect_m3u8_detail(url, session=None, timeout=15):
    """
    内容验证式嗅探(参考油猴脚本思路):
    1. 常规规则从 HTML/JS 嗅探 .m3u8 字符串
    2. 对嗅探结果逐个请求验证内容(#EXTM3U 开头), 并解析类型/时长
    3. 一无所获时, 把页面中带视频关键词的候选接口 URL 广撒网验证
    返回 [{url, kind, duration, streams}] 列表
    """
    s = session or make_session()
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        log("输入本身是 m3u8 链接, 直接验证")
        d = classify_m3u8(s, url)
        return [d] if d else [{"url": url, "kind": "unknown",
                               "duration": None, "streams": None}]

    # ---- 阶段 1: 常规嗅探 ----
    raw_urls = detect_m3u8_regex(url, s, timeout)

    verified = []
    for u in raw_urls:
        d = classify_m3u8(s, u)
        if d:
            verified.append(d)

    if verified:
        log(f"常规嗅探命中 {len(verified)} 个 m3u8 (已内容验证)")
        _sort_detail(verified)
        return verified

    # ---- 阶段 2: 广撒网内容验证(应对无后缀动态接口) ----
    log("常规嗅探无结果, 启动内容验证式嗅探(逐个请求候选接口)...")
    try:
        resp = s.get(url, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
        base_url = resp.url
    except Exception as e:
        err(f"抓取页面失败: {e}")
        return []

    # 最多两轮: 页面候选 -> 候选响应体里再提取(常见: 接口返回 JSON 含播放路径)
    pending = list(_collect_candidate_urls(html, base_url))[:60]
    bodies = [html]
    for round_no in (1, 2):
        if not pending:
            break
        log(f"第 {round_no} 轮验证, 候选接口 {len(pending)} 个...")
        next_cands, next_bodies = [], []
        with futures.ThreadPoolExecutor(max_workers=6) as ex:
            for u, text in ex.map(lambda u: (u, _fetch_limited(s, u)), pending):
                if text is None:
                    continue
                if text.lstrip().startswith("#EXTM3U"):
                    d = classify_m3u8(s, u)
                    if d:
                        verified.append(d)
                        log(f"命中: {d['url']} ({d['kind']})")
                elif round_no == 1:
                    # 非播列表 -> 从响应体(如 JSON)中再提取候选
                    next_bodies.append(text)
                    for c in _collect_candidate_urls(text, u):
                        if c not in pending and all(c != v["url"] for v in verified):
                            next_cands.append(c)
        pending = list(dict.fromkeys(next_cands))[:60]
        bodies = next_bodies

    _sort_detail(verified)
    return verified


def _sort_detail(items):
    """media > master > unknown; 同级有时长的优先"""
    rank = {"media": 0, "master": 1, "unknown": 2}
    items.sort(key=lambda d: (rank.get(d["kind"], 3),
                              0 if d.get("duration") else 1))


def detect_m3u8(url, session=None, timeout=15):
    """兼容旧接口: 返回嗅探到的 m3u8 URL 列表"""
    return [d["url"] for d in detect_m3u8_detail(url, session, timeout)]


# ---------------------------------------------------------------- 解析部分

class Key:
    def __init__(self, method, uri=None, iv=None):
        self.method, self.uri, self.iv = method, uri, iv


def parse_m3u8(text, base_url):
    """解析 m3u8 内容, 返回 dict:
    is_master / playlists [(bandwidth, url)] / segments [(url, key)] / keys {uri: Key}
    / init_url (EXT-X-MAP 的 fMP4 init 段, 可为 None)
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    info = {"is_master": False, "playlists": [], "segments": [], "keys": {},
            "init_url": None}
    cur_key = None

    for ln in lines:
        if ln.startswith("#EXT-X-STREAM-INF:"):
            info["is_master"] = True
            bw_m = re.search(r"BANDWIDTH=(\d+)", ln)
            bw = int(bw_m.group(1)) if bw_m else 0
            info["playlists"].append([bw, None])
        elif ln.startswith("#EXT-X-MAP:"):
            # fMP4 流的 init 段(含 ftyp/moov 头), 拼接时必须在最前面
            uri_m = re.search(r'URI="([^"]+)"', ln)
            if uri_m:
                info["init_url"] = urljoin(base_url, uri_m.group(1))
        elif ln.startswith("#EXT-X-KEY:"):
            method_m = re.search(r"METHOD=([A-Za-z0-9-]+)", ln)
            uri_m = re.search(r'URI="([^"]+)"', ln)
            iv_m = re.search(r'IV=0[xX]([0-9a-fA-F]+)', ln)
            method = method_m.group(1) if method_m else "NONE"
            cur_key = Key(
                method,
                urljoin(base_url, uri_m.group(1)) if uri_m else None,
                iv_m.group(1) if iv_m else None,
            )
            if cur_key.uri:
                info["keys"][cur_key.uri] = cur_key
        elif ln.startswith("#"):
            continue
        else:
            absu = urljoin(base_url, ln)
            if info["is_master"]:
                if info["playlists"] and info["playlists"][-1][1] is None:
                    info["playlists"][-1][1] = absu
            else:
                info["segments"].append((absu, cur_key))
    return info


def resolve_playlist(url, session, timeout=15, prefer_best=True):
    """递归解析: 若是 master playlist, 选码率并继续解析 variant, 直到拿到分片列表"""
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    info = parse_m3u8(resp.text, str(resp.url))

    if info["is_master"]:
        playlists = [(bw, u) for bw, u in info["playlists"] if u]
        if not playlists:
            raise RuntimeError("master playlist 中没有可用的子播放列表")
        if prefer_best:
            playlists.sort(key=lambda x: -x[0])
        bw, variant_url = playlists[0]
        log(f"master playlist 含 {len(playlists)} 路流, 选择码率 {bw/1000:.0f} kbps: {variant_url}")
        variant = resolve_playlist(variant_url, session, timeout, prefer_best)
        # variant 的 EXT-X-MAP 优先, 其次 master 级别声明的
        if not variant.get("init_url"):
            variant["init_url"] = info.get("init_url")
        return variant
    return info


# ---------------------------------------------------------------- 下载部分

def fetch_key(key, session, timeout=15):
    resp = session.get(key.uri, timeout=timeout)
    resp.raise_for_status()
    return resp.content  # 16 字节


def seg_filename(index, url):
    ext = os.path.splitext(urlparse(url).path)[1] or ".ts"
    if ext not in (".ts", ".mp4", ".aac", ".m4s", ".fmp4", ".mp3"):
        ext = ".ts"
    return f"seg_{index:06d}{ext}"


def download_segment(task):
    """下载单个分片。

    策略:
    1. 流式写入 .part 文件, 断连时已收到的字节保留, 之后带 Range 头续传;
    2. 每次重试都换全新连接(新 Session), 避免连接池中的坏连接反复失败;
    3. Range 行为异常时自愈: 服务器返回 200(不支持 Range) 或 206 起点不符,
       丢弃 .part 整段重下; 连续无进展也丢弃 .part 重来;
    4. 收齐后做 AES-128 解密并写出最终分片; 已存在则跳过(断点续传)。
    """
    index, url, out_dir, session, key_bytes, key, seq, retries = task
    path = os.path.join(out_dir, seg_filename(index, url))
    raw_path = path + ".part"
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return index, "skip", path

    expected = None  # 期望总字节数(来自 Content-Length / Content-Range)
    last_exc = None

    for attempt in range(retries):
        size_before = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
        offset = size_before
        if expected is not None and offset >= expected:
            break  # 已收齐

        # 重试时换全新连接: 老连接池里的坏连接是"重试必败、重下就好"的主因之一
        s2 = session
        if attempt > 0:
            s2 = make_session()
            s2.headers.update(session.headers)

        broke = False
        try:
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            resp = s2.get(url, timeout=(10, 60), stream=True, headers=headers)

            if offset and resp.status_code == 200:
                offset = 0  # 服务器不支持 Range, 整段重下
            resp.raise_for_status()

            if resp.status_code == 206:
                cr = resp.headers.get("Content-Range", "")
                m = re.match(r"bytes (\d+)-\d+/(\d+)", cr)
                if m:
                    start, total = int(m.group(1)), int(m.group(2))
                    if start != offset:
                        # 206 起点与本地断点不符, 数据对不上: 丢弃重下
                        offset = 0
                        resp.close()
                        resp = s2.get(url, timeout=(10, 60), stream=True)
                        resp.raise_for_status()
                    expected = total
            elif resp.headers.get("Content-Length"):
                expected = int(resp.headers["Content-Length"])

            got = 0
            with open(raw_path, "ab" if offset else "wb") as f:
                try:
                    for chunk in resp.iter_content(64 * 1024):
                        if chunk:
                            f.write(chunk)
                            got += len(chunk)
                except Exception as e:
                    broke = True
                    last_exc = e  # 连接中断, 已收字节保留待续传

            total_now = offset + got
            if not broke and (expected is None or total_now >= expected):
                last_exc = None
                break  # 收齐(或服务器未声明长度且正常读完)
        except Exception as e:
            broke = True
            last_exc = e

        # 无进展检测: 这一轮一个字节都没多, 说明 .part 状态已"卡死",
        # 丢弃它换全新下载, 避免"每次都在同一处失败"的死循环
        size_now = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
        if size_now <= size_before and attempt + 1 < retries:
            if os.path.exists(raw_path):
                os.remove(raw_path)
        time.sleep(min(1 + attempt, 5))

    raw_size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
    if raw_size == 0 or (expected is not None and raw_size < expected):
        return index, "fail", f"{url}: {last_exc or f'分片不完整 {raw_size}/{expected}'}"

    # 收齐, 解密并写出
    with open(raw_path, "rb") as f:
        data = f.read()
    try:
        if key and key.method == "AES-128" and key_bytes and HAS_CRYPTO:
            iv_bytes = (
                bytes.fromhex(key.iv.zfill(32)) if key.iv else seq.to_bytes(16, "big")
            )
            data = AES.new(key_bytes, AES.MODE_CBC, iv_bytes).decrypt(data)
        with open(path, "wb") as f:
            f.write(data)
        os.remove(raw_path)
        return index, "ok", path
    except Exception as e:
        return index, "fail", f"{url}: 解密失败 {e}"


def download_m3u8(url, out_path=None, workers=8, tmp_dir=None, keep_tmp=False,
                  referer=None, to_mp4=False, session=None, progress=None,
                  retries=3, retry_rounds=3):
    s = session or make_session(referer=referer)
    log(f"解析 m3u8: {url}")
    info = resolve_playlist(url, s)
    segments = info["segments"]
    if not segments:
        raise RuntimeError("未解析到任何视频分片")
    log(f"共 {len(segments)} 个分片 (单分片重试 {retries} 次, 最多 {retry_rounds} 轮)")

    out_path = out_path or "output.ts"
    out_dir = os.path.dirname(os.path.abspath(out_path))
    stem = os.path.splitext(os.path.basename(out_path))[0]
    # 临时目录按输出文件名隔离, 避免多任务并行时分片互相覆盖
    tmp_dir = tmp_dir or os.path.join(out_dir, f".m3u8_dl_tmp_{stem}")
    os.makedirs(tmp_dir, exist_ok=True)

    # 预取解密密钥(简单实现: 所有 key uri 缓存)
    key_cache = {}
    for k in set(k2 for _, k2 in segments if k2 and k2.uri):
        try:
            key_cache[k.uri] = fetch_key(k, s)
            log(f"已获取解密密钥: {k.uri} ({len(key_cache[k.uri])} 字节)")
        except Exception as e:
            err(f"获取密钥失败 {k.uri}: {e} (若为加密流, 输出可能无法播放)")

    # fMP4 流: 下载 EXT-X-MAP init 段, 合并时置于最前
    init_path = None
    if info.get("init_url"):
        init_path = os.path.join(tmp_dir, "initseg.bin")
        if not (os.path.exists(init_path) and os.path.getsize(init_path) > 0):
            log(f"检测到 fMP4 init 段 (EXT-X-MAP): {info['init_url']}")
            last_e = None
            for attempt in range(3):
                try:
                    d = s.get(info["init_url"], timeout=30)
                    d.raise_for_status()
                    with open(init_path, "wb") as f:
                        f.write(d.content)
                    log(f"已下载 init 段 ({len(d.content)} 字节)")
                    last_e = None
                    break
                except Exception as e:
                    last_e = e
                    time.sleep(1 + attempt)
            if last_e:
                err(f"init 段下载失败: {last_e} (输出文件可能无法播放)")
                init_path = None
        else:
            log("init 段已存在(续传)")

    def make_task(i):
        seg_url, key = segments[i]
        return (i, seg_url, tmp_dir, s,
                key_cache.get(key.uri) if key else None,
                key, i, retries)

    total = len(segments)
    pending = list(range(total))
    done_count = 0
    last_errors = {}
    t0 = time.time()

    for round_no in range(1, retry_rounds + 1):
        round_failed = []
        with futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for index, status, payload in ex.map(download_segment,
                                                 [make_task(i) for i in pending]):
                done_count += 1
                if status == "fail":
                    round_failed.append(index)
                    last_errors[index] = payload
                if done_count % 20 == 0 or done_count == total:
                    elapsed = time.time() - t0
                    log(f"进度 {done_count}/{total} ({done_count/elapsed:.1f} 片/秒)")
                if progress:
                    progress(done_count, total)

        if not round_failed:
            pending = []  # 本轮全部成功, 清空待重试列表
            break

        # 已存在的分片文件会在下一轮自动跳过, 所以 pending 只保留失败分片
        pending = round_failed
        if round_no < retry_rounds:
            log(f"第 {round_no} 轮结束, {len(pending)} 个分片失败, "
                f"2 秒后开始第 {round_no + 1} 轮重试: {sorted(pending)}")
            time.sleep(2)
        else:
            for i in pending:
                err(f"分片 {i} 最终失败: {last_errors[i]}")

    if pending:
        err(f"重试 {retry_rounds} 轮后仍有 {len(pending)} 个分片失败, "
            f"可重新运行本命令续传")
        raise RuntimeError(f"{len(pending)} 个分片下载失败")

    # 合并
    log(f"合并分片 -> {out_path}")
    with open(out_path, "wb") as out:
        if init_path and os.path.exists(init_path):
            with open(init_path, "rb") as f:
                out.write(f.read())
        for i in range(len(segments)):
            p = os.path.join(tmp_dir, seg_filename(i, segments[i][0]))
            if os.path.exists(p):
                with open(p, "rb") as f:
                    out.write(f.read())

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    log(f"完成: {out_path} ({size_mb:.1f} MB)")

    if not keep_tmp:
        for i in range(len(segments)):
            p = os.path.join(tmp_dir, seg_filename(i, segments[i][0]))
            if os.path.exists(p):
                os.remove(p)
            if os.path.exists(p + ".part"):
                os.remove(p + ".part")
        init_seg = os.path.join(tmp_dir, "initseg.bin")
        if os.path.exists(init_seg):
            os.remove(init_seg)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

    # 可选: ffmpeg 转封装 mp4 (无损, 仅重新封装)
    if to_mp4:
        try:
            target = os.path.splitext(out_path)[0] + ".mp4"
            if target == out_path:  # out_path 本身就是 .mp4 名: 先落到临时 ts
                merge_ts = out_path[:-4] + ".tmp_merge.ts"
                os.replace(out_path, merge_ts)
                ts_to_mp4(merge_ts, target, auto_install=True)
                os.remove(merge_ts)
            else:
                ts_to_mp4(out_path, target, auto_install=True)
                os.remove(out_path)
            out_path = target
        except Exception as e:
            err(f"{e} (已保留 .ts 文件)")
    return out_path


# ---------------------------------------------------------------- TS -> MP4

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_FFMPEG = os.path.join(TOOL_DIR, "bin", "ffmpeg")


def which_ffmpeg():
    """查找 ffmpeg: 系统 PATH -> 工具自带 bin/ffmpeg"""
    return shutil.which("ffmpeg") or (LOCAL_FFMPEG if os.path.exists(LOCAL_FFMPEG) else None)


def _install_ffmpeg_via_pip():
    """通过 imageio-ffmpeg 包获取静态 ffmpeg, 复制到工具 bin 目录"""
    log("尝试通过 pip 包 imageio-ffmpeg 获取静态 ffmpeg...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "imageio-ffmpeg"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        err(f"pip 安装失败: {r.stderr[-300:]}")
        return None
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        err(f"获取 ffmpeg 二进制失败: {e}")
        return None
    os.makedirs(os.path.dirname(LOCAL_FFMPEG), exist_ok=True)
    shutil.copy2(src, LOCAL_FFMPEG)
    os.chmod(LOCAL_FFMPEG, 0o755)
    log(f"已安装到 {LOCAL_FFMPEG}")
    return LOCAL_FFMPEG


def ensure_ffmpeg(auto_install=True):
    """定位 ffmpeg; 缺失且允许时依次尝试 Homebrew / pip 静态包自动安装"""
    p = which_ffmpeg()
    if p:
        return p
    if not auto_install:
        return None
    if shutil.which("brew"):
        log("未检测到 ffmpeg, 尝试通过 Homebrew 安装(可能需要几分钟)...")
        try:
            r = subprocess.run(["brew", "install", "ffmpeg"])
            if r.returncode == 0:
                return which_ffmpeg()
        except Exception as e:
            err(f"brew 安装失败: {e}")
    return _install_ffmpeg_via_pip()


def ts_to_mp4(ts_path, mp4_path=None, auto_install=True):
    """把已有 .ts 文件无损转封装为 .mp4 (流复制, 不重编码, 秒级完成)"""
    if not os.path.exists(ts_path):
        raise FileNotFoundError(f"输入文件不存在: {ts_path}")
    ff = ensure_ffmpeg(auto_install)
    if not ff:
        raise RuntimeError(
            "未安装 ffmpeg 且无法自动安装, 请先执行: brew install ffmpeg")
    mp4_path = mp4_path or os.path.splitext(ts_path)[0] + ".mp4"
    log(f"ffmpeg 转封装: {ts_path} -> {mp4_path}")
    r = subprocess.run(
        [ff, "-y", "-loglevel", "error", "-i", ts_path,
         "-c", "copy", "-bsf:a", "aac_adtstoasc", mp4_path],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 转封装失败: {r.stderr[-800:]}")
    log(f"完成: {mp4_path} ({os.path.getsize(mp4_path)/1024/1024:.1f} MB)")
    return mp4_path


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="M3U8 自动探测并下载工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_d = sub.add_parser("detect", help="探测网页中的 m3u8 地址")
    ap_d.add_argument("url", help="网页 URL 或 m3u8 URL")

    ap_dl = sub.add_parser("download", help="下载 m3u8 视频")
    ap_dl.add_argument("url", help="网页 URL(自动探测) 或 m3u8 URL")
    ap_dl.add_argument("-o", "--output", default=None, help="输出文件路径(默认 output.ts)")
    ap_dl.add_argument("-w", "--workers", type=int, default=8, help="并发下载数(默认 8)")
    ap_dl.add_argument("--best", action="store_true", default=True, help="master 中选最高码率(默认)")
    ap_dl.add_argument("--worst", action="store_true", help="master 中选最低码率")
    ap_dl.add_argument("--keep-tmp", action="store_true", help="保留分片临时目录(用于续传)")
    ap_dl.add_argument("--mp4", action="store_true", help="完成后用 ffmpeg 转封装为 mp4")
    ap_dl.add_argument("--retries", type=int, default=3,
                       help="单分片单轮内重试次数(默认 3)")
    ap_dl.add_argument("--retry-rounds", type=int, default=3,
                       help="失败分片整体重试轮数(默认 3)")
    ap_dl.add_argument("--referer", default=None, help="自定义 Referer 请求头")
    ap_dl.add_argument("-H", "--header", action="append", default=[],
                       help="附加请求头, 如 -H 'Cookie: xxx'")

    ap_c = sub.add_parser("convert", help="把已有 .ts 文件转封装为 .mp4")
    ap_c.add_argument("input", help="输入 .ts 文件路径")
    ap_c.add_argument("-o", "--output", default=None, help="输出 .mp4 路径(默认同名替换)")
    ap_c.add_argument("--no-install", action="store_true",
                      help="不尝试自动安装 ffmpeg")

    args = ap.parse_args()
    headers = {}
    for h in getattr(args, "header", []) or []:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()
    session = make_session(referer=getattr(args, "referer", None), headers=headers)

    if args.cmd == "detect":
        results = detect_m3u8_detail(args.url, session)
        if not results:
            err("未探测到 m3u8 地址")
            sys.exit(1)
        print(f"\n探测到 {len(results)} 个 m3u8 地址:")
        for i, d in enumerate(results):
            tag = d["kind"]
            if d.get("duration"):
                tag += f", 约 {d['duration']/60:.1f} 分钟"
            elif d.get("streams"):
                tag += f", {d['streams']} 路码率"
            print(f"  [{i}] ({tag}) {d['url']}")
        return

    if args.cmd == "convert":
        try:
            out = ts_to_mp4(args.input, args.output, auto_install=not args.no_install)
            print(f"\n转换完成: {out}")
        except Exception as e:
            err(f"转换失败: {e}")
            sys.exit(1)
        return

    # download
    prefer_best = not getattr(args, "worst", False)
    candidates = detect_m3u8(args.url, session)
    if not candidates:
        err("未探测到 m3u8 地址, 无法下载")
        sys.exit(1)
    if len(candidates) > 1:
        print(f"\n探测到 {len(candidates)} 个 m3u8 地址, 默认取第 1 个:", file=sys.stderr)
        for i, u in enumerate(candidates):
            print(f"  [{i}] {u}", file=sys.stderr)
        print("", file=sys.stderr)

    out = args.output or ("output.ts" if not getattr(args, "mp4", False) else "output.mp4")
    try:
        download_m3u8(candidates[0], out_path=out, workers=args.workers,
                      keep_tmp=args.keep_tmp, to_mp4=args.mp4,
                      referer=args.referer, session=session,
                      retries=args.retries, retry_rounds=args.retry_rounds)
    except KeyboardInterrupt:
        err("用户中断, 已下载分片保留, 重新运行可续传(加 --keep-tmp)")
        sys.exit(130)
    except Exception as e:
        err(f"下载失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
