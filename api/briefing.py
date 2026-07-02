# -*- coding: utf-8 -*-
"""GET /api/briefing — Notionのウィークリーブリーフィングを取得してアプリに返す。
必要な環境変数: NOTION_TOKEN (Notion Integrationのシークレット)
任意: NOTION_BRIEFING_PAGE (ページID。未設定時は既定のブリーフィングページ)"""
from http.server import BaseHTTPRequestHandler

import json, os, urllib.request

DEFAULT_PAGE = "39173049b81a8146a89deadb1bcc10bf"
MAX_BLOCKS = 200


def notion_req(path):
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN未設定")
    req = urllib.request.Request(f"https://api.notion.com/v1{path}", headers={
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "User-Agent": "invest-alert-pwa"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def rich_text(block_data):
    return "".join(t.get("plain_text", "") for t in block_data.get("rich_text", []))


def fetch_blocks(page_id):
    blocks, cursor = [], None
    while len(blocks) < MAX_BLOCKS:
        path = f"/blocks/{page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        j = notion_req(path)
        for b in j.get("results", []):
            t = b.get("type")
            data = b.get(t, {})
            text = rich_text(data) if isinstance(data, dict) else ""
            if t in ("heading_1", "heading_2", "heading_3"):
                blocks.append({"t": "h", "x": text})
            elif t == "paragraph" and text:
                blocks.append({"t": "p", "x": text})
            elif t in ("bulleted_list_item", "numbered_list_item"):
                blocks.append({"t": "li", "x": text})
            elif t == "divider":
                blocks.append({"t": "hr", "x": ""})
            elif t == "quote" and text:
                blocks.append({"t": "q", "x": text})
        if not j.get("has_more"):
            break
        cursor = j.get("next_cursor")
    return blocks


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            page = os.environ.get("NOTION_BRIEFING_PAGE", DEFAULT_PAGE).replace("-", "")
            blocks = fetch_blocks(page)
            self._json(200, {"ok": True, "blocks": blocks})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "s-maxage=3600, stale-while-revalidate=86400")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
