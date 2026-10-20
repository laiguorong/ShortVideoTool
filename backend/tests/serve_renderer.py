# -*- coding: utf-8 -*-
"""Renderer 静态文件 server（修正 JS MIME type + 兼容 file:// CORS）。"""
import sys
import http.server
import socketserver
import pathlib

ROOT = pathlib.Path("E:/sourcecode/RongInfo/ShortVideoTool/frontend/dist/renderer")
PORT = 8766


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".ico": "image/x-icon",
    }

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, *args, **kwargs):
        pass  # 静默


def main() -> int:
    import os
    os.chdir(ROOT)
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    httpd.allow_reuse_address = True
    print(f"[serve] http://127.0.0.1:{PORT} → {ROOT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
