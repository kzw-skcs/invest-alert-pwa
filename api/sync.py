# -*- coding: utf-8 -*-
"""POST /api/sync — アプリの銘柄リスト変更(追加/hold⇔trade/非表示)をconfig.jsonへ反映。
body: {"stocks": [{"ticker","name","sector","subSector","tradePolicy","conviction","hidden"}...]}"""
import json
from http.server import BaseHTTPRequestHandler
from _github import get_file, put_file


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            incoming = {s["ticker"]: s for s in body.get("stocks", [])}
            cfg, sha = get_file("config.json")
            existing = {s["ticker"]: s for s in cfg["stocks"]}
            merged = []
            for t, s in incoming.items():
                base = existing.get(t, {})
                base.update({
                    "ticker": t,
                    "name": s.get("name", base.get("name", t)),
                    "sector": s.get("sector", base.get("sector", "その他")),
                    "subSector": s.get("subSector", base.get("subSector", "その他モート")),
                    "tradePolicy": s.get("tradePolicy", base.get("tradePolicy", "hold")),
                    "conviction": s.get("conviction", base.get("conviction", 3)),
                })
                base.setdefault("stooq", t.lower() + ".us")
                if not s.get("hidden"):
                    merged.append(base)
            cfg["stocks"] = merged
            put_file("config.json", cfg, sha, "app: 銘柄リスト同期")
            self._json(200, {"ok": True, "count": len(merged)})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
