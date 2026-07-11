# -*- coding: utf-8 -*-
"""POST /api/interpret — マクロイベント(FOMC/雇用統計/CPI)の発表内容をClaude APIで
Web検索・解釈し、結果を返しつつ config.json の macroCalendar.comments に保存する。
必要な環境変数: ANTHROPIC_API_KEY (+既存の GH_TOKEN/GH_REPO)
body: {"type": "FOMC"|"雇用統計"|"CPI", "date": "YYYY-MM-DD"}"""
from http.server import BaseHTTPRequestHandler

import base64, json, os, urllib.request

ALLOWED_TYPES = ("FOMC", "雇用統計", "CPI")


def gh_req(path, method="GET", body=None):
    token = os.environ.get("GH_TOKEN"); repo = os.environ.get("GH_REPO")
    if not token or not repo:
        raise RuntimeError("GH_TOKEN / GH_REPO 未設定")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "invest-alert-pwa"})
    with urllib.request.urlopen(req, timeout=25) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}


def ask_claude(event_type, event_date):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY 未設定(Vercel環境変数に追加してRedeploy)")
    prompt = (f"{event_date}前後に発表された米国の{event_type}"
              f"({'FOMC政策金利決定・声明・会見' if event_type == 'FOMC' else '雇用統計(NFP・失業率・平均時給)' if event_type == '雇用統計' else 'CPI(総合・コア)'}) の結果をWeb検索で確認し、"
              "個人のValue投資家(AI・エネルギー・宇宙・金銀・BTC保有、長期志向、現物のみ)向けに日本語で解釈してください。"
              "構成: ①結果の要点(数値) ②市場の初期反応 ③グロース株・金・BTCへの含意 ④検討アクション(断定でなく『検討』表現、2個まで)。"
              "全体で300字以内。出典に依存する数値のみ記載し、不確かな場合はその旨明記。")
    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 1500,
        "system": "簡潔で誠実な市場解説者。誇張せず、不確実性を明示する。投資助言ではなく情報整理として書く。",
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
    }
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        j = json.loads(r.read().decode())
    texts = [b.get("text", "") for b in j.get("content", []) if b.get("type") == "text"]
    out = "".join(texts).strip()
    if not out:
        raise RuntimeError("解釈の生成に失敗(空応答)")
    return out


def save_comment(event_date, text):
    j = gh_req("/contents/config.json")
    cfg = json.loads(base64.b64decode(j["content"]).decode("utf-8"))
    mc = cfg.setdefault("macroCalendar", {})
    mc.setdefault("comments", {})[event_date] = text
    content = base64.b64encode(json.dumps(cfg, ensure_ascii=False, indent=2).encode()).decode()
    gh_req("/contents/config.json", "PUT",
           {"message": f"interpret: {event_date} 解釈追加", "content": content, "sha": j["sha"]})



def auth_ok(h):
    """APP_SECRET(Vercel環境変数)設定時のみ、X-App-Keyヘッダの一致を要求。
    未設定なら従来通り許可(移行期の互換)。設定を強く推奨。"""
    secret = os.environ.get("APP_SECRET")
    return (not secret) or (h.headers.get("X-App-Key", "") == secret)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self._json(200, {"ok": True, "authEnabled": bool(os.environ.get("APP_SECRET")), "endpoint": "interpret", "apiKeyConfigured": ok})

    def do_POST(self):
        if not auth_ok(self):
            return self._json(401, {"ok": False, "error": "認証エラー: ⚙️設定の🔐アプリキーがサーバーのAPP_SECRETと不一致(未入力ならキーを設定)"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            etype = body.get("type"); edate = body.get("date")
            if etype not in ALLOWED_TYPES or not edate:
                return self._json(400, {"ok": False, "error": "type/dateが不正"})
            text = ask_claude(etype, edate)
            try:
                save_comment(edate, f"[{etype}] {text}")
                saved = True
            except Exception:
                saved = False
            self._json(200, {"ok": True, "comment": text, "saved": saved})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
