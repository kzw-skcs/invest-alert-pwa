// アプリの監視銘柄リスト（hold/trade方針・追加/削除）を config.json に反映する（任意機能）。
// 必要な環境変数（Vercelプロジェクト設定）:
//   GH_TOKEN : GitHub Personal Access Token（repo / contents:write 権限）
//   GH_REPO  : "kzw-skcs/invest-alert-pwa"
// リクエストボディ: { "stocks": [ {ticker,name,sector,tradePolicy,stooq?,yahoo?}, ... ] }
const API = "https://api.github.com";

export default async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  const token = process.env.GH_TOKEN, repo = process.env.GH_REPO;
  if (!token || !repo) return res.status(501).json({ error: "サーバー未設定（GH_TOKEN/GH_REPO）" });
  try {
    const body = typeof req.body === "string" ? JSON.parse(req.body) : req.body;
    const stocks = body && body.stocks;
    if (!Array.isArray(stocks) || !stocks.length)
      return res.status(400).json({ error: "stocks配列が必要です" });

    // 正規化（危険な値を除去し、必要な補完）
    const clean = [];
    const seen = new Set();
    for (const s of stocks) {
      let t = (s.ticker || "").toString().trim().toUpperCase();
      if (!t || !/^[A-Z0-9.\-]{1,10}$/.test(t) || seen.has(t)) continue;
      seen.add(t);
      const pol = (s.tradePolicy === "trade") ? "trade" : "hold";
      clean.push({
        ticker: t,
        stooq: (s.stooq || (t.toLowerCase() + ".us")),
        name: (s.name || t).toString().slice(0, 60),
        sector: (s.sector || "その他").toString().slice(0, 20),
        tradePolicy: pol,
        ...(s.yahoo ? { yahoo: s.yahoo } : {})
      });
    }
    if (!clean.length) return res.status(400).json({ error: "有効な銘柄がありません" });

    const h = { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json" };
    const path = "config.json";
    const cur = await fetch(`${API}/repos/${repo}/contents/${path}`, { headers: h });
    if (cur.status !== 200) return res.status(500).json({ error: "config.json取得失敗" });
    const j = await cur.json();
    const cfg = JSON.parse(Buffer.from(j.content, "base64").toString("utf-8"));
    cfg.stocks = clean;
    const content = Buffer.from(JSON.stringify(cfg, null, 2), "utf-8").toString("base64");
    const put = await fetch(`${API}/repos/${repo}/contents/${path}`, {
      method: "PUT", headers: h,
      body: JSON.stringify({ message: "update watchlist from app", content, sha: j.sha })
    });
    if (!put.ok) return res.status(500).json({ error: "commit失敗", detail: await put.text() });
    return res.status(200).json({ ok: true, count: clean.length });
  } catch (e) {
    return res.status(500).json({ error: String(e) });
  }
}
