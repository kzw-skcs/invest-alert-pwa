# -*- coding: utf-8 -*-
"""POST /api/alpha_review — 4αの定性部分(Alpha Market / Alpha Management定性)を
Claude API(Web検索)で自動評価し、config.jsonの該当銘柄に alphaReview として保存する。
先生が自分でCEO経歴や市場規模を調べられなくても、ボタン一つで構造化評価が得られる。
必要な環境変数: ANTHROPIC_API_KEY (+GH_TOKEN/GH_REPO, 任意でAPP_SECRET)
body: {"ticker": "TSM"}"""
from http.server import BaseHTTPRequestHandler

import base64, json, os, re, urllib.request


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


def auth_ok(h):
    secret = os.environ.get("APP_SECRET")
    return (not secret) or (h.headers.get("X-App-Key", "") == secret)


PROMPT = """{ticker}({name})について、バリュー投資の4α観点のうち定性2項目をWeb検索で調査し、
厳密なJSONのみで回答してください(前後に文章を付けない)。

調査項目:
1. Alpha Market(0-25点): 対象市場の規模と成長性(5-10年)、メガトレンドとの関係、
   市場シェアの方向、モート(参入障壁)の強度、技術代替リスク
2. Alpha Management定性(0-25点): 現CEOの実績(就任後の業績・株価)、資本配分の評判、
   経営陣の持株・インサイダー売買の傾向、後継体制、直近のCEO交代や不祥事の有無

出力形式(このJSONのみ):
{{"market": <0-25の整数>, "mgmt": <0-25の整数>,
 "marketNote": "<80字以内: 市場評価の根拠>",
 "mgmtNote": "<80字以内: 経営陣評価の根拠。CEO名と就任年を含める>",
 "flags": ["<重大な懸念があれば。なければ空配列>"],
 "confidence": "<high|medium|low: 情報の確度>"}}

採点の目安: 20-25=傑出 / 14-19=良好 / 8-13=標準 / 0-7=懸念。
不確かな情報は点数を保守側に。確認できない項目はflagsに明記。"""


def ask_claude(ticker, name):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY 未設定(Vercel環境変数に追加してRedeploy)")
    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 2000,
        "system": "誠実な株式アナリスト。誇張せず、確認できた事実に基づき保守的に採点する。出力は厳密なJSONのみ。",
        "messages": [{"role": "user", "content": PROMPT.format(ticker=ticker, name=name or ticker)}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
    }
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        j = json.loads(r.read().decode())
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("JSON応答が得られませんでした")
    rv = json.loads(m.group(0))
    rv["market"] = max(0, min(25, int(rv.get("market", 0))))
    rv["mgmt"] = max(0, min(25, int(rv.get("mgmt", 0))))
    return rv


def save_review(ticker, review):
    from datetime import date
    j = gh_req("/contents/config.json")
    cfg = json.loads(base64.b64decode(j["content"]).decode("utf-8"))
    hit = False
    for s in cfg["stocks"]:
        if s["ticker"] == ticker:
            s["alphaReview"] = {**review, "date": date.today().isoformat()}
            hit = True
            break
    if not hit:
        raise RuntimeError(f"{ticker} はconfigに存在しません")
    content = base64.b64encode(json.dumps(cfg, ensure_ascii=False, indent=2).encode()).decode()
    gh_req("/contents/config.json", "PUT",
           {"message": f"alpha-review: {ticker} 定性評価更新", "content": content, "sha": j["sha"]})


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._json(200, {"ok": True, "authEnabled": bool(os.environ.get("APP_SECRET")),
                         "endpoint": "alpha_review",
                         "apiKeyConfigured": bool(os.environ.get("ANTHROPIC_API_KEY"))})

    def do_POST(self):
        if not auth_ok(self):
            return self._json(401, {"ok": False, "error": "認証エラー: ⚙️設定の🔐アプリキーがサーバーのAPP_SECRETと不一致"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            ticker = (body.get("ticker") or "").upper().strip()
            if not ticker or len(ticker) > 8:
                return self._json(400, {"ok": False, "error": "tickerが不正"})
            review = ask_claude(ticker, body.get("name"))
            try:
                save_review(ticker, review)
                saved = True
            except Exception as e:
                saved = False
                review["saveError"] = str(e)
            self._json(200, {"ok": True, "review": review, "saved": saved,
                             "message": "翌朝のdaily実行(または📈)後にカードへ反映されます"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
