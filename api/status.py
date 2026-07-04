# -*- coding: utf-8 -*-
"""GET /api/status?workflow=all — 指定ワークフローの最新実行の進行状況を返す。
アプリの進捗バー表示用。"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import json, os, urllib.request

ALLOWED = {"daily": "daily.yml", "reanalyze": "reanalyze.yml",
           "backtest": "backtest.yml", "fundamentals": "fundamentals.yml",
           "all": "all.yml"}


def gh_req(path):
    token = os.environ.get("GH_TOKEN"); repo = os.environ.get("GH_REPO")
    if not token or not repo:
        raise RuntimeError("GH_TOKEN / GH_REPO 未設定")
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "invest-alert-pwa"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            q = parse_qs(urlparse(self.path).query)
            wf = ALLOWED.get((q.get("workflow") or ["all"])[0], "all.yml")
            runs = gh_req(f"/actions/workflows/{wf}/runs?per_page=1").get("workflow_runs", [])
            if not runs:
                return self._json(200, {"ok": True, "status": "none"})
            run = runs[0]
            out = {"ok": True, "status": run.get("status"), "conclusion": run.get("conclusion"),
                   "startedAt": run.get("run_started_at"), "updatedAt": run.get("updated_at"),
                   "htmlUrl": run.get("html_url")}
            if run.get("status") in ("in_progress", "queued"):
                jobs = gh_req(f"/actions/runs/{run['id']}/jobs").get("jobs", [])
                if jobs:
                    steps = [s for s in jobs[0].get("steps", []) if not s["name"].startswith("Set up")
                             and "checkout" not in s["name"].lower() and "Complete" not in s["name"]]
                    total = len(steps)
                    done = len([s for s in steps if s.get("status") == "completed"])
                    cur = next((s["name"] for s in steps if s.get("status") == "in_progress"), None)
                    out.update({"stepDone": done, "stepTotal": total, "currentStep": cur})
            self._json(200, out)
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
