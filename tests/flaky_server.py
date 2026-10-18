"""不稳定测试服务器 v2

模拟真实 CDN 断连场景:
- trunc 前缀分片: 前 N 次「非 Range」请求返回完整 Content-Length 但中途掐断连接
- 收到 Range 请求时正常返回 206 续传数据(模拟服务器支持断点)
- badseg: 永远 404
"""
import http.server
import os
import re
from functools import partial

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testsite")
TRUNC_FAILS = {"trunc1": 2, "trunc2": 2}  # 各前 2 次请求掐断
CHUNK = 64 * 1024
counts = {}


def bootstrap_fixtures():
    """首次运行时自动生成测试分片(有 pycryptodome 则加密, 否则明文)"""
    hls = os.path.join(ROOT, "hls")
    if os.path.exists(os.path.join(hls, "seg0.ts")):
        return
    os.makedirs(hls, exist_ok=True)
    try:
        from Crypto.Cipher import AES
        import secrets
        key = secrets.token_bytes(16)
        open(os.path.join(hls, "key.bin"), "wb").write(key)
        iv = b"\x00" * 16
        enc = lambda d: AES.new(key, AES.MODE_CBC, iv).encrypt(
            d + bytes([16 - len(d) % 16]) * (16 - len(d) % 16))
        make = lambda i: enc(f"TEST-SEG-{i:03d}-".encode() + b"X" * 200)
    except ImportError:
        make = lambda i: f"TEST-SEG-{i:03d}-".encode() + b"X" * 200
        open(os.path.join(hls, "key.bin"), "wb").write(b"0" * 16)
    for i in range(4):
        open(os.path.join(hls, f"seg{i}.ts"), "wb").write(make(i))
    pl = ('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:0\n'
          '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000000\n'
          + "".join(f"#EXTINF:10.0,\nseg{i}.ts\n" for i in range(4))
          + "#EXT-X-ENDLIST\n")
    open(os.path.join(hls, "index.m3u8"), "w").write(pl)
    print("[flaky_server] 测试分片已生成于", hls)


bootstrap_fixtures()


class H(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        m = re.search(r"/hls/(\w+)\.ts$", self.path)
        if not m:
            super().do_GET()
            return
        name = m.group(1)
        if name == "badseg":
            self.send_response(404)
            self.end_headers()
            return

        fp = os.path.join(ROOT, "hls", name + ".ts")
        if not os.path.exists(fp):
            # trunc* 分片复用 seg 数据
            base = re.sub(r"^trunc\d+", "seg0", name)
            fp = os.path.join(ROOT, "hls", base + ".ts")
            if not os.path.exists(fp):
                self.send_response(404)
                self.end_headers()
                return
        size = os.path.getsize(fp)

        rng = self.headers.get("Range")
        if rng:
            # 正常支持断点续传
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{size-1}/{size}")
            self.send_header("Content-Length", str(size - start))
            self.end_headers()
            with open(fp, "rb") as f:
                f.seek(start)
                remain = size - start
                while remain > 0:
                    buf = f.read(min(CHUNK, remain))
                    self.wfile.write(buf)
                    remain -= len(buf)
            return

        n = counts.get(name, 0)
        counts[name] = n + 1
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if name in TRUNC_FAILS and n <= TRUNC_FAILS[name]:
            # 发一半就掐断(不关连接, 直接 close 触发客户端 IncompleteRead)
            with open(fp, "rb") as f:
                self.wfile.write(f.read(CHUNK))
            self.close_connection = True
            return
        with open(fp, "rb") as f:
            while True:
                buf = f.read(CHUNK)
                if not buf:
                    break
                self.wfile.write(buf)

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(
    ("127.0.0.1", 18766), partial(H, directory=ROOT)).serve_forever()
