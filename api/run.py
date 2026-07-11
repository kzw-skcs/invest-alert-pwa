# -*- coding: utf-8 -*-
"""POST /api/run — GitHub Actionsワークフローをアプリから起動。
body: {"workflow": "daily" | "reanalyze" | "backtest"}  GET=疎通確認。"""
from http.server import BaseHTTPRequestHandler

import json, os, urllib.request

ALLOWED = {"daily": "daily.yml", "reanalyze": "reanalyze.yml",
           "backtest": "backtest.yml", "fundamentals": "fundamentals.yml",
           "all": "all.yml"}


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


def send_json(h, code, obj):
    b = json.dumps(obj, ensure_ascii=False).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)



def auth_ok(h):
    """APP_SECRET(Vercel環境変数)設定時のみ、X-App-Keyヘッダの一致を要求。
    未設定なら従来通り許可(移行期の互換)。設定を強く推奨。"""
    secret = os.environ.get("APP_SECRET")
    return (not secret) or (h.headers.get("X-App-Key", "") == secret)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        send_json(self, 200, {"ok": True, "authEnabled": bool(os.environ.get("APP_SECRET")), "endpoint": "run", "workflows": list(ALLOWED)})

    def do_POST(self):
        if not auth_ok(self):
            return send_json(self, 401, {"ok": False, "error": "認証エラー: ⚙️設定の🔐アプリキーがサーバーのAPP_SECRETと不一致(未入力ならキーを設定)"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            wf = ALLOWED.get(body.get("workflow", ""))
            if not wf:
                return send_json(self, 400, {"ok": False, "error": f"workflowは{list(ALLOWED)}のいずれか"})
            payload = {"ref": "main"}
            inputs = body.get("inputs")
            if isinstance(inputs, dict):
                safe = {k: str(v) for k, v in inputs.items() if k in ("run_backtest",)}
                if safe:
                    payload["inputs"] = safe
            gh_req(f"/actions/workflows/{wf}/dispatches", "POST", payload)
            send_json(self, 200, {"ok": True, "message": f"{body['workflow']} を開始しました"})
        except Exception as e:
            send_json(self, 500, {"ok": False, "error": str(e)})
