# 投資シグナル PWA（invest-alert-pwa）

個別株・暗号資産・金銀の「買い場／売り場」を自動判定し、推奨度・見込みリターン・ポートフォリオ損益・リバランスを表示するスマホアプリ（PWA）。毎朝6時に自動判定し、条件に合えばプッシュ通知します。

> ⚠️ **免責**：状態判定・推奨度・見込みリターン・的中率はすべて、トレンドライン（線形回帰チャネル）・移動平均・過去の値動きに基づく**機械的な計算値**です。将来の収益を保証するものではなく、投資助言ではありません。投資判断はご自身の責任で。

---

## 仕組み（2階建て）

1. **判定処理（GitHub Actions）** … 毎日6:00 JSTに `evaluate.py` が動き、Stooq（米国株・金銀・VIX・USD/JPY）とCoinGecko（BTC/ETH）から終値を取得 → 状態判定 → `data.json` を更新 → アラートがあればプッシュ送信。
2. **表示アプリ（PWA / Vercel）** … `index.html` が `data.json` を読み、シグナル一覧・資産損益・リバランス・分析を表示。

データ取得はサーバー側（GitHub Actions）で行うため、ブラウザのCORS制限を受けません。

---

## 判定ロジック（対象5資産：個別株・BTC・ETH・金・銀）

- **自動トレンドライン（マルチタイムフレーム）**：**1年（約252日）と5年（約1260日）の2本**の回帰チャネルを算出。ピボット高値どうし・安値どうしを線形回帰で結ぶ。1年でタイミング、5年で長期位置を評価する。
- **状態**：
  - 🟦 買い場：レンジ内で底ラインの3%以内
  - 🟥 売り場：レンジ内で天井ラインの3%以内
  - 🟢 上昇中：天井を上抜け / 🔻 下落中：底を下抜け
  - ⭐🟢 超買い場：下抜け後に **20日線が50日線を上抜け**（ゴールデンクロス）
  - ⭐🔴 超売り場：上抜け後に **20日線が50日線を下抜け**（デッドクロス）
  - VIX：前日比 ±20%／±50% で警戒・緊急、**または絶対水準 30／40** で警戒・緊急（超買い場/超売り場のとき強化）
- **マルチTFの押し目買い**：1年で「買い場」かつ**5年でも下段（上昇トレンドの押し目／ボックス下限）**なら推奨度を加点。長期上昇トレンドの下〜中段では、短期の「売り場」を格下げ（長期上昇の押し目を売らない）。
- **クロスは20/50を採用**：50/200は反応が遅く点灯時には動き終わっていることが多いため、数週間早く転換を捉える20/50に変更。
- **逆張りフィルタ**：**本物の長期(5年)下降トレンド**中の通常「買い場」のみアラートを抑制（落ちるナイフ回避）。横ばいボックスの下限や上昇チャネルの下限買いは残す。確認シグナルの「超買い場」は対象外。（5年データが無い銘柄は200日線で代替）
- **長期保有方針**：金・銀・BTC・ETH と「長期保有」分類の個別株は **売りシグナルを無効化**（買い増し専用）。
- **個別株の分類**（`tradePolicy`、`config.json`で編集可）：
  - 長期保有（hold・売りなし）：AAPL, MSFT, GOOGL, AMZN, AVGO, XOM, CVX, COP, NEE, LMT, RTX, NOC, IBM, ASML, IQV, CACI, META, MRVL, VRT
  - 短期売買（trade・買い売り両方）：NVDA, TSLA, AMD, PLTR, SLB, BA, RKLB, ANET, CLMB, SPCX
  - ※ SPCX(SpaceX)は新規上場のため約1年の履歴が貯まるまで「データ不足」表示になることがあります。CLMBは小型株（S&P500規模未満）。IQVはヘルスケア。
- **推奨度**：状態・底/天井への近さ・上値余地・VIX・トレンド整合から0〜100で算出（★1〜5、S/A/B/C）。
- **見込みリターン（買い）**：天井ラインまでの上昇率(%)と、過去サイクルから推定した想定期間、年率換算（参考）。
- **リバランス**：保有数量×最新価格で比率を計算し、目標 個別株6:BTC1:ETH1:金1:銀1 から±5%超で資産移動を提案。
- **アルゴリズム再分析**：過去シグナルを実際の値動きと突き合わせ的中率を計算し、判定パラメータを自動調整。

設定値はすべて `config.json` の `settings` にあり、編集して `git push` すれば反映されます。

---

## セットアップ（初回のみ）

### 1. ファイルを配置
ZIPを展開し、フォルダ名を `invest-alert-pwa` にしてホームへ置く（例：`~/invest-alert-pwa`）。

### 2. GitHub リポジトリ作成（アカウント: kzw-skcs）
- リポジトリ名：`invest-alert-pwa`（README/.gitignoreは追加しない）
- ターミナルで：
```bash
cd ~/invest-alert-pwa
git init
git add .
git commit -m "初回コミット: 投資シグナル PWA"
git branch -M main
git remote add origin https://github.com/kzw-skcs/invest-alert-pwa.git
git push -u origin main
```

### 3. Vercel でデプロイ
- Import → GitHub → `invest-alert-pwa`
- Framework Preset：**Other**
- Root Directory：`./`
- Deploy → `https://invest-alert-pwa.vercel.app` などが発行されます。

### 4. 毎日の自動判定を有効化
- GitHubリポジトリの **Actions** タブを開き、ワークフローを有効化。
- 動作確認：Actions →「daily」→ **Run workflow** で即実行 → 数分後 `data.json` が更新されアプリに反映。
- 以後は毎朝6:00 JSTに自動実行されます。

### 5.（任意）プッシュ通知の設定
通知を使わない場合はスキップ可（アプリを開けばシグナルは見られます）。

1. VAPID鍵を生成：
```bash
npx web-push generate-vapid-keys
```
2. 表示された **Public Key** で `vapid.json` を作成し、リポジトリ直下に置いて push：
```json
{ "publicKey": "（ここにPublic Key）" }
```
3. GitHubリポジトリ → Settings → Secrets and variables → Actions に追加：
   - `VAPID_PRIVATE_KEY` … 生成された Private Key
   - `VAPID_CLAIMS_EMAIL` … `mailto:kazawa@skclinicalsupport.com`
4. iPhoneは「ホーム画面に追加」したアプリから開き、分析タブの「🔔 この端末で通知を受け取る」をタップ。

> 通知の購読先（subscriptions.json）を自動保存し、「再分析」ボタンをアプリから動かすには、任意で `api/`（Vercelサーバーレス）を使います。Vercelプロジェクトの環境変数に `GH_TOKEN`（repo権限のPAT）と `GH_REPO=kzw-skcs/invest-alert-pwa` を設定してください。未設定でも、画面に表示される購読テキストを手動で `subscriptions.json` に貼り付ければ通知できます。

---

## 使い方

- **📊 シグナル**：VIX状況、推奨ランキング（推奨度順）、資産ごとの状態カード（状態・推奨度・買いの見込みリターン/期間・レンジ・50/200線・チャート）。
- **💼 資産**：各資産の数量と取得単価(円)を入力 → 評価額・損益（％と円）・合計損益、目標比率との乖離とリバランス提案。
- **🤖 分析**：アルゴリズム再分析（的中率・自動調整の履歴）、通知設定、現在の判定設定。

---

## 銘柄の増減
`config.json` の `stocks` 配列を編集して `git push`。Stooqシンボルは「ティッカー小文字 + `.us`」（例：`nvda.us`）。

## ローカルでのロジック確認
```bash
python3 test_engine.py      # 状態判定・推奨度のテスト
python3 test_reanalyze.py   # 再分析ロジックのテスト
python3 make_sample_data.py # 合成データでdata.jsonを生成
```
