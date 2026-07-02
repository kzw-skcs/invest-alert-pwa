# -*- coding: utf-8 -*-
"""POST /api/subscribe — Web Push購読情報をsubscriptions.jsonへ保存。"""
import json
from http.server import BaseHTTPRequestHandler
from _github import get_file, put_file, gh_req


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            sub = json.loads(self.rfile.read(length) or b"{}")
            if not sub.get("endpoint"):
                return self._json(400, {"ok": False, "error": "endpointなし"})
            try:
                subs, sha = get_file("subscriptions.json")
            except Exception:
                subs, sha = [], None
            subs = [s for s in subs if s.get("endpoint") != sub["endpoint"]]
            subs.append(sub)
            if sha:
                put_file("subscriptions.json", subs, sha, "app: push購読更新")
            else:
                import base64
                gh_req("/contents/subscriptions.json", "PUT", {
                    "message": "app: push購読作成",
                    "content": base64.b64encode(
                        json.dumps(subs, ensure_ascii=False).encode()).decode()})
            self._json(200, {"ok": True, "count": len(subs)})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
