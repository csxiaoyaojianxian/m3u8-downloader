"""无 Range 支持的恶劣服务器: 每个分片前 2 次请求固定断连, Range 请求一律返回整段 200"""
import http.server
import os
from functools import partial

ROOT = "/tmp/hls_test_root"
FAILS = 2
counts = {}


class H(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        n = counts.get(self.path, 0)
        counts[self.path] = n + 1
        # 故意无视 Range 头, 永远 200 整段
        try:
            super().do_GET()
        except BrokenPipeError:
            pass
        # do_GET 已发送完整内容, 无法中途截断 —— 改为覆写 copyfile 阶段
        # (实际截断逻辑见 copyfile 覆写)

    def copyfile(self, source, outputfile):
        n = counts.get(self.path.split("?")[0], 0)
        # 前 2 次请求只发 16000 字节即断
        if n <= FAILS and self.path.endswith((".ts", ".m4s")):
            outputfile.write(source.read(16000))
            self.close_connection = True
            raise BrokenPipeError("simulated truncation")
        super().copyfile(source, outputfile)

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(
    ("127.0.0.1", 18769), partial(H, directory=ROOT)).serve_forever()
