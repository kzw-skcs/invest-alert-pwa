# -*- coding: utf-8 -*-
"""POST /api/reanalyze — GitHub Actionsのreanalyzeワークフローをdispatch起動。"""
import json
from http.server import BaseHTTPRequestHandler
from _github import gh_req


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            gh_req("/actions/workflows/reanalyze.yml/dispatches", "POST", {"ref": "main"})
            self._json(200, {"ok": True, "message": "再分析を開始しました(数分後に反映)"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
