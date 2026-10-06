#!/usr/bin/env python3
"""
每日抓取台股上市櫃收盤資料，計算產業資金流向統計。

輸出：
  data/market/YYYY-MM-DD.json  當日市場概況、產業統計、強勢股清單（網頁讀這個）
  data/prices/YYYY-MM-DD.json  全部個股收盤價（之後算強勢股後續表現用）
  data/index.json              所有已有資料的日期清單

用法：
  python scripts/fetch_market.py           正常執行；今天的檔案已存在就跳過
  python scripts/fetch_market.py --force   強制重抓今天
"""
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TPE = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MARKET_DIR = DATA / "market"
PRICES_DIR = DATA / "prices"
INDUSTRY_CACHE = DATA / "industry_map.json"

STRONG_PCT = 8.0      # 強勢股門檻（%）
HISTORY_DAYS = 20     # 成交比重的比較基準天數
HEADERS = {"User-Agent": "Mozilla/5.0 (tw-sector-flow personal research)"}

TWSE_INDUSTRY = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造",
    "15": "航運業", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨",
    "19": "綜合", "20": "其他", "21": "化學工業", "22": "生技醫療",
    "23": "油電燃氣", "24": "半導體業", "25": "電腦及週邊設備", "26": "光電業",
    "27": "通信網路業", "28": "電子零組件", "29": "電子通路業", "30": "資訊服務業",
    "31": "其他電子業", "32": "文化創意", "33": "農業科技", "34": "電子商務",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}


def get_json(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {url} 第 {i + 1} 次失敗：{e}")
            time.sleep(3 * (i + 1))
    return None


def num(s):
    """把交易所的各種字串（含逗號、正負號、HTML、--）轉成 float，無法轉換回傳 None。"""
    if s is None:
        return None
    s = re.sub(r"<[^>]+>", "", str(s)).replace(",", "").strip()
    if s in ("", "--", "---", "----"):
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
        return float(m.group()) if m else None


def roc_to_iso(s):
    """民國日期 1150702 或 115/07/02 → 2026-07-02"""
    digits = re.sub(r"\D", "", str(s or ""))
    if len(digits) < 7:
        return None
    return f"{int(digits[:-4]) + 1911}-{digits[-4:-2]}-{digits[-2:]}"


def is_common_stock(code):
    """只保留四碼普通股，排除 ETF（00 開頭）、權證、特別股。"""
    return bool(re.fullmatch(r"[1-9]\d{3}", code))


def fetch_twse():
    j = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json")
    if j and j.get("stat") == "OK" and j.get("data"):
        fields = j.get("fields", [])
        idx = {f: i for i, f in enumerate(fields)}
        need = ["證券代號", "證券名稱", "成交金額", "收盤價", "漲跌價差"]
        if all(k in idx for k in need):
            d = str(j.get("date", ""))
            date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else None
            rows = [{
                "code": r[idx["證券代號"]].strip(),
                "name": r[idx["證券名稱"]].strip(),
                "close": num(r[idx["收盤價"]]),
                "change": num(r[idx["漲跌價差"]]),
                "value": num(r[idx["成交金額"]]),
            } for r in j["data"]]
            return date, rows
        print(f"[warn] TWSE 欄位與預期不同：{fields}")

    print("[info] 改用 TWSE OpenAPI")
    j = get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    if isinstance(j, list) and j:
        date = roc_to_iso(j[0].get("Date"))
        rows = [{
            "code": str(r.get("Code", "")).strip(),
            "name": str(r.get("Name", "")).strip(),
            "close": num(r.get("ClosingPrice")),
            "change": num(r.get("Change")),
            "value": num(r.get("TradeValue")),
        } for r in j]
        return date, rows
    return None, []


def fetch_tpex():
    j = get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
    if isinstance(j, list) and j:
        if "SecuritiesCompanyCode" not in j[0]:
            print(f"[warn] TPEx 欄位與預期不同：{list(j[0].keys())}")
        date = roc_to_iso(j[0].get("Date"))
        rows = [{
            "code": str(r.get("SecuritiesCompanyCode", "")).strip(),
            "name": str(r.get("CompanyName", "")).strip(),
            "close": num(r.get("Close")),
            "change": num(r.get("Change")),
            "value": num(r.get("TransactionAmount")),
        } for r in j]
        return date, rows
    return None, []


def fetch_industry_map():
    """代號 → 產業名稱。抓不到時沿用上次存下的快取。"""
    cache = json.loads(INDUSTRY_CACHE.read_text("utf-8")) if INDUSTRY_CACHE.exists() else {}
    fresh = {}
    sources = [
        ("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", ["公司代號"], ["產業別"]),
        ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
         ["SecuritiesCompanyCode", "公司代號"], ["SecuritiesIndustryCode", "產業別"]),
    ]
    for url, code_keys, ind_keys in sources:
        j = get_json(url)
        if not (isinstance(j, list) and j):
            continue
        before = len(fresh)
        for r in j:
            code = next((str(r[k]).strip() for k in code_keys if r.get(k)), "")
            ind = next((str(r[k]).strip() for k in ind_keys if r.get(k)), "")
            if code:
                fresh[code] = TWSE_INDUSTRY.get(ind.zfill(2) if ind.isdigit() else ind, ind or "未分類")
        if len(fresh) == before:
            print(f"[warn] {url} 沒有解析到產業資料，欄位為：{list(j[0].keys())}")
    if fresh:
        cache.update(fresh)
        DATA.mkdir(parents=True, exist_ok=True)
        INDUSTRY_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")
    return cache


def load_history_shares(today):
    """讀取今天以前最近 HISTORY_DAYS 天各產業的成交比重。"""
    files = sorted(p for p in MARKET_DIR.glob("*.json") if p.stem < today)[-HISTORY_DAYS:]
    hist = defaultdict(list)
    for p in files:
        try:
            for ind in json.loads(p.read_text("utf-8")).get("industries", []):
                hist[ind["name"]].append(ind["share"])
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 讀取 {p.name} 失敗：{e}")
    return hist, len(files)


def main():
    force = "--force" in sys.argv
    today = datetime.now(TPE).strftime("%Y-%m-%d")
    out = MARKET_DIR / f"{today}.json"
    if out.exists() and not force:
        print(f"{out.name} 已存在，跳過。")
        return

    twse_date, twse = fetch_twse()
    if twse_date != today:
        print(f"TWSE 資料日期為 {twse_date}，不是今天 {today}：判定為休市或資料尚未更新，不寫檔。")
        return

    tpex_date, tpex = fetch_tpex()
    tpex_ok = tpex_date == today
    if not tpex_ok:
        print(f"[warn] TPEx 資料日期為 {tpex_date}，今天先只用上市資料。")
        tpex = []

    industry_of = fetch_industry_map()

    stocks = []
    for market, rows in (("上市", twse), ("上櫃", tpex)):
        for r in rows:
            if not is_common_stock(r["code"]):
                continue
            close, change, value = r["close"], r["change"], r["value"]
            if close is None or change is None or not value:
                continue
            prev = close - change
            if prev <= 0:
                continue
            stocks.append({
                "code": r["code"], "name": r["name"], "market": market,
                "industry": industry_of.get(r["code"], "未分類"),
                "close": close, "pct": round(change / prev * 100, 2), "value": value,
            })

    if len(stocks) < 500:
        print(f"[error] 只解析到 {len(stocks)} 檔，資料量異常，不寫檔。")
        sys.exit(1)

    total_value = sum(s["value"] for s in stocks)
    hist, hist_days = load_history_shares(today)

    groups = defaultdict(list)
    for s in stocks:
        groups[s["industry"]].append(s)

    industries = []
    for name, ss in groups.items():
        value = sum(s["value"] for s in ss)
        share = value / total_value
        past = hist.get(name, [])
        avg = sum(past) / len(past) if past else None
        industries.append({
            "name": name,
            "count": len(ss),
            "value": value,
            "share": round(share, 6),
            "share_avg": round(avg, 6) if avg else None,
            "share_ratio": round(share / avg, 3) if avg else None,
            "wavg_pct": round(sum(s["pct"] * s["value"] for s in ss) / value, 2),
            "up": sum(s["pct"] > 0 for s in ss),
            "down": sum(s["pct"] < 0 for s in ss),
            "strong": sum(s["pct"] >= STRONG_PCT for s in ss),
        })
    industries.sort(key=lambda x: x["value"], reverse=True)

    strong = sorted((s for s in stocks if s["pct"] >= STRONG_PCT), key=lambda s: s["pct"], reverse=True)

    MARKET_DIR.mkdir(parents=True, exist_ok=True)
    PRICES_DIR.mkdir(parents=True, exist_ok=True)

    result = {
        "date": today,
        "generated_at": datetime.now(TPE).isoformat(timespec="seconds"),
        "sources": {"twse": True, "tpex": tpex_ok},
        "history_days": hist_days,
        "market": {
            "total_value": total_value,
            "stocks": len(stocks),
            "up": sum(s["pct"] > 0 for s in stocks),
            "down": sum(s["pct"] < 0 for s in stocks),
            "strong": len(strong),
        },
        "industries": industries,
        "strong": strong,
    }
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")

    prices = {s["code"]: [s["close"], s["pct"]] for s in stocks}
    (PRICES_DIR / f"{today}.json").write_text(json.dumps(prices, ensure_ascii=False), "utf-8")

    dates = sorted((p.stem for p in MARKET_DIR.glob("*.json")), reverse=True)
    (DATA / "index.json").write_text(
        json.dumps({"dates": dates, "updated_at": result["generated_at"]}, ensure_ascii=False), "utf-8")

    print(f"完成：{len(stocks)} 檔、{len(industries)} 個產業、強勢股 {len(strong)} 檔"
          f"（上櫃資料：{'有' if tpex_ok else '無'}；比較基準 {hist_days} 天）")


if __name__ == "__main__":
    main()
