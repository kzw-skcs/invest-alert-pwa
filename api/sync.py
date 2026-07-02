# -*- coding: utf-8 -*-
"""POST /api/sync — 銘柄リスト変更をconfig.jsonへ反映。GET=疎通確認。"""
from http.server import BaseHTTPRequestHandler

import base64, json, os, urllib.request

def gh_req(path, method="GET", body=None):
    token = os.environ.get("GH_TOKEN"); repo = os.environ.get("GH_REPO")
    if not token or not repo:
        raise RuntimeError("GH_TOKEN / GH_REPO が未設定です(Vercel環境変数を確認しRedeploy)")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "invest-alert-pwa"})
    with urllib.request.urlopen(req, timeout=20) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}

def get_file(path):
    j = gh_req(f"/contents/{path}")
    return json.loads(base64.b64decode(j["content"]).decode("utf-8")), j["sha"]

def put_file(path, obj, sha, message):
    content = base64.b64encode(json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")).decode()
    body = {"message": message, "content": content}
    if sha: body["sha"] = sha
    return gh_req(f"/contents/{path}", "PUT", body)

def send_json(h, code, obj):
    b = json.dumps(obj, ensure_ascii=False).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = bool(os.environ.get("GH_TOKEN")) and bool(os.environ.get("GH_REPO"))
        send_json(self, 200, {"ok": True, "endpoint": "sync", "envConfigured": ok})

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
                base.update({"ticker": t,
                    "name": s.get("name", base.get("name", t)),
                    "sector": s.get("sector", base.get("sector", "その他")),
                    "subSector": s.get("subSector", base.get("subSector", "その他モート")),
                    "tradePolicy": s.get("tradePolicy", base.get("tradePolicy", "hold")),
                    "conviction": s.get("conviction", base.get("conviction", 3))})
                base.setdefault("stooq", t.lower() + ".us")
                if not s.get("hidden"):
                    merged.append(base)
            cfg["stocks"] = merged
            put_file("config.json", cfg, sha, "app: ウォッチリスト同期")
            send_json(self, 200, {"ok": True, "count": len(merged)})
        except Exception as e:
            send_json(self, 500, {"ok": False, "error": str(e)})
