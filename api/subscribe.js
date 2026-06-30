// 通知購読をリポジトリの subscriptions.json に保存する（任意機能・Vercelサーバーレス）
// 必要な環境変数（Vercelプロジェクト設定）:
//   GH_TOKEN : GitHub Personal Access Token（repo / contents:write 権限）
//   GH_REPO  : "kzw-skcs/invest-alert-pwa"
const API = "https://api.github.com";

export default async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  const token = process.env.GH_TOKEN, repo = process.env.GH_REPO;
  if (!token || !repo) return res.status(501).json({ error: "サーバー未設定" });
  try {
    const sub = typeof req.body === "string" ? JSON.parse(req.body) : req.body;
    if (!sub || !sub.endpoint) return res.status(400).json({ error: "invalid subscription" });

    const h = { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json" };
    const path = "subscriptions.json";
    // 既存を取得
    let list = [], sha = undefined;
    const cur = await fetch(`${API}/repos/${repo}/contents/${path}`, { headers: h });
    if (cur.status === 200) {
      const j = await cur.json();
      sha = j.sha;
      try { list = JSON.parse(Buffer.from(j.content, "base64").toString("utf-8")); } catch (e) { list = []; }
    }
    // 重複チェック（endpoint一致）
    if (!list.find(s => s.endpoint === sub.endpoint)) list.push(sub);
    const content = Buffer.from(JSON.stringify(list, null, 2), "utf-8").toString("base64");
    const put = await fetch(`${API}/repos/${repo}/contents/${path}`, {
      method: "PUT", headers: h,
      body: JSON.stringify({ message: "add push subscription", content, sha })
    });
    if (!put.ok) return res.status(500).json({ error: "commit失敗", detail: await put.text() });
    return res.status(200).json({ ok: true, count: list.length });
  } catch (e) {
    return res.status(500).json({ error: String(e) });
  }
}
