# -*- coding: utf-8 -*-
"""地基路由 UTF-8 query 还原回归测试。

背景:http.server 按 latin-1 解码请求行,URL 里未百分号编码的中文会变乱码,
导致 `?cat=混合` 之类的非 ASCII query 传到 handler 前已损坏(见 _recover_path)。
前端一律用 encodeURIComponent 编码,但地基本身应对未编码路径也稳健。
"""
import socket
import threading
import unittest
from http.server import ThreadingHTTPServer

from backend.app import Handler, _recover_path


class TestRecoverPath(unittest.TestCase):
    def test_ascii_noop(self):
        self.assertEqual(_recover_path("/api/market?page=2"), "/api/market?page=2")

    def test_percent_encoded_untouched(self):
        # %XX 均为 ASCII,round-trip 无损;实际解码交给 parse_qs/unquote
        s = "/api/market?cat=%E6%B7%B7%E5%90%88"
        self.assertEqual(_recover_path(s), s)

    def test_raw_utf8_mojibake_recovered(self):
        # 模拟 http.server 的 latin-1 解码:真实字节按 latin-1 变成逐字节字符
        raw_bytes = "/api/market?cat=混合".encode("utf-8")
        mojibake = raw_bytes.decode("latin-1")
        self.assertEqual(_recover_path(mojibake), "/api/market?cat=混合")

    def test_invalid_sequence_falls_back(self):
        # 无法按 utf-8 还原时原样返回,不抛异常
        self.assertEqual(_recover_path("/api/x?q=ÿ中"), "/api/x?q=ÿ中")


class TestUnencodedQueryOverHTTP(unittest.TestCase):
    """端到端:直接用 socket 发送未编码 UTF-8 query,复现浏览器/裸客户端行为。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _raw_get(self, path_bytes):
        """path_bytes 为原始字节(含未编码 UTF-8),按 HTTP 请求行原样发出。"""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            req = b"GET " + path_bytes + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            s.sendall(req)
            chunks = []
            while True:
                d = s.recv(4096)
                if not d:
                    break
                chunks.append(d)
            return b"".join(chunks)
        finally:
            s.close()

    def test_unencoded_chinese_query_reaches_handler(self):
        # /api/search 需登录会返回 401,但关键是不因中文乱码而 500;
        # 用一个未登录也可达、会读 query 的端点验证「请求行含裸 UTF-8 不崩、正常解析」。
        resp = self._raw_get("/api/search?q=混合".encode("utf-8"))
        status = resp.split(b"\r\n", 1)[0]
        # 期望正常 HTTP 响应(401 未登录),而非 400/500 崩溃
        self.assertTrue(status.startswith(b"HTTP/1.0 401") or status.startswith(b"HTTP/1.1 401"),
                        msg=f"未编码中文 query 应被正常解析,实际首行: {status!r}")


if __name__ == "__main__":
    unittest.main()
