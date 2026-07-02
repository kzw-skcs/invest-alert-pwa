# -*- coding: utf-8 -*-
"""api共通: GitHub REST APIヘルパ。GH_TOKEN(repo+workflow権限PAT)とGH_REPO必須。"""
import base64
import json
import os
import urllib.request


def gh_req(path, method="GET", body=None):
    token = os.environ.get("GH_TOKEN")
    repo = os.environ.get("GH_REPO")
    if not token or not repo:
        raise RuntimeError("GH_TOKEN / GH_REPO 未設定")
    url = f"https://api.github.com/repos/{repo}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "invest-alert-pwa",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        txt = r.read().decode()
        return json.loads(txt) if txt else {}


def get_file(path):
    j = gh_req(f"/contents/{path}")
    return json.loads(base64.b64decode(j["content"]).decode("utf-8")), j["sha"]


def put_file(path, obj, sha, message):
    content = base64.b64encode(
        json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")).decode()
    return gh_req(f"/contents/{path}", "PUT", {
        "message": message, "content": content, "sha": sha})
