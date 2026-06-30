// 「アルゴリズム再分析」ボタンから reanalyze ワークフローを起動する（任意機能・Vercelサーバーレス）
// 必要な環境変数:
//   GH_TOKEN : GitHub Personal Access Token（actions:write / workflow 権限）
//   GH_REPO  : "kzw-skcs/invest-alert-pwa"
const API = "https://api.github.com";

export default async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  const token = process.env.GH_TOKEN, repo = process.env.GH_REPO;
  if (!token || !repo) return res.status(501).json({ error: "サーバー未設定" });
  try {
    const h = { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json" };
    const r = await fetch(`${API}/repos/${repo}/actions/workflows/reanalyze.yml/dispatches`, {
      method: "POST", headers: h, body: JSON.stringify({ ref: "main" })
    });
    if (r.status === 204) return res.status(200).json({ ok: true });
    return res.status(500).json({ error: "dispatch失敗", detail: await r.text() });
  } catch (e) {
    return res.status(500).json({ error: String(e) });
  }
}
