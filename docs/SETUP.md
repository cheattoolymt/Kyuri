# セットアップ手順

## 1. GitHub Pages の有効化

1. リポジトリの **Settings** → **Pages** を開く
2. **Source** を `Deploy from a branch` に設定
3. **Branch** を `main` / `/ (root)` に設定して保存

数分後、以下の URL で公開されます。

```
https://cheattoolymt.github.io/Kyuri/
```

`.nojekyll` を同梱しているため、Jekyll による処理はスキップされ、
`api/v1/` 配下の JSON もそのまま静的配信されます。

## 2. データ自動更新ワークフローの追加

自動更新用の GitHub Actions ワークフローは `docs/update-data.yml.txt` に
用意してあります。GitHub App の権限制約でこのファイルを直接
`.github/workflows/` に置けないため、以下のいずれかの方法で有効化してください。

### 方法 A: ローカルから配置する

```bash
git clone https://github.com/cheattoolymt/Kyuri.git
cd Kyuri
mkdir -p .github/workflows
cp docs/update-data.yml.txt .github/workflows/update-data.yml
git add .github/workflows/update-data.yml
git commit -m "ci: データ自動更新ワークフローを追加"
git push
```

### 方法 B: GitHub の Web UI から作成する

1. リポジトリの **Actions** タブ → **New workflow** → **set up a workflow yourself**
2. ファイル名を `update-data.yml` にする
3. `docs/update-data.yml.txt` の内容を貼り付けてコミット

### ワークフローの権限設定

配置後、**Settings** → **Actions** → **General** → **Workflow permissions** で
**Read and write permissions** を選択してください。
データ更新のコミットを push するために必要です。

## 3. 動作確認

**Actions** タブから `Update cucumber price data` を選び、
**Run workflow** で手動実行できます。全年度を取り直す場合は
`full_rebuild` を `true` にしてください（初回は 3〜5 分程度）。

実行が完了すると、新しいデータがあれば
`chore(data): YYYY-MM-DD 時点 NNN.N 円/kg に更新` というコミットが自動で入ります。

## 4. 更新スケジュール

既定では毎日 06:20 JST（21:20 UTC）に実行されます。
変更する場合は `update-data.yml` の `cron` を編集してください。

東京都のオープンデータは年度単位の更新であるため、卸売価格が動くのは
基本的に年度替わりのタイミングです。小売価格は週次で更新されます。

## 5. ローカルでの手動更新

Actions を使わずに手元でデータを更新する場合:

```bash
python3 tools/build_data.py --incremental   # 進行中の年度のみ
python3 tools/validate.py                   # 検証
git add data api/v1 && git commit -m "chore(data): 更新" && git push
```

外部ライブラリは不要です（Python 3.11 以上の標準ライブラリのみ）。
