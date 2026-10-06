# 台股族群資金流向

每個交易日 18:00 由 GitHub Actions 抓取上市櫃收盤資料、計算產業成交比重；18:30 由 Claude Code routine 判讀強勢股新聞並歸類題材，結果顯示在 GitHub Pages 網頁並推播到 Telegram。

- `scripts/fetch_market.py`：抓資料與計算統計
- `.github/workflows/fetch.yml`：排程（平日台北 18:00）
- `config/sectors.json`：固定的題材族群清單，要新增族群請手動編輯
- `routine/PROMPT.md`：貼到 Claude routine 的指令
- `data/`：每日資料（自動產生，不要手動修改）
