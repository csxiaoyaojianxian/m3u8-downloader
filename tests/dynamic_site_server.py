"""模拟动态视频站: m3u8 地址无后缀、由 JS 运行时请求接口获得"""
import http.server
from functools import partial

ROOT = "/tmp/fmp4test"

PAGE = """<html><head><title>Drama EP1</title></head><body>
<div id="app">Loading player...</div>
<script>
  // 播放地址由接口动态返回, 页面源码中无 .m3u8 字样
  fetch('/api/getplay?ep=1&token=abc123')
    .then(r => r.json())
    .then(cfg => { window.PLAY_CONFIG = cfg; });
</script>
</body></html>
"""

FAKE_API = '{"code":0,"data":{"playUrl":"/media/stream/ep1/index"}}'


class H(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/video/page"):
            body = PAGE.encode()
        elif self.path.startswith("/api/getplay"):
            body = FAKE_API.encode()
        elif self.path.startswith("/media/stream/ep1/index"):
            with open(ROOT + "/hls.m3u8", "rb") as f:
                body = f.read()
        elif self.path.startswith("/media/stream/ep1/"):
            name = self.path.rsplit("/", 1)[-1].split("?")[0]
            with open(ROOT + "/" + name, "rb") as f:
                body = f.read()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", 18768), partial(H, directory=ROOT)).serve_forever()
