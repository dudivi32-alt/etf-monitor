#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
五大被動ETF成分股每日監控腳本
追蹤: 0050 / 0056 / 006208 / 00878 / 00919
資料來源: 各投信公司官網
用法:  python fetch_etf.py
       python fetch_etf.py --no-browser   (純 requests，資料較少)
"""

import asyncio
import json
import re
import sys
import os
import argparse
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("請先安裝: pip install requests beautifulsoup4")
    sys.exit(1)

PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_HTML = BASE_DIR / "etf_monitor.html"

# 台灣金融保險股代碼（金管會分類：金融保險業）
FINANCIAL_CODES = {
    "2801","2809","2812","2820","2823","2824","2826","2828",
    "2834","2836","2838","2845","2847","2849","2850","2851",
    "2852","2855","2856","2867","2880","2881","2882","2883",
    "2884","2885","2886","2887","2888","2889","2890","2891",
    "2892","2897","5820","5880","6160","6616",
}

ETF_META = {
    "0050":   {"name":"元大台灣50",       "company":"元大投信","color":"#1E88E5","bg":"#E3F2FD"},
    "0056":   {"name":"元大台灣高股息",   "company":"元大投信","color":"#00897B","bg":"#E0F2F1"},
    "006208": {"name":"富邦台50",          "company":"富邦投信","color":"#F57C00","bg":"#FFF3E0"},
    "00878":  {"name":"國泰永續高股息",   "company":"國泰投信","color":"#7B1FA2","bg":"#F3E5F5"},
    "00919":  {"name":"群益台灣精選高息", "company":"群益投信","color":"#C62828","bg":"#FFEBEE"},
}

# CMoney Bearer token（由 pw_extract_cathay_cmoney 執行時自動擷取）
_cmoney_token: str = ""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# ──────────────────────────────────────────────
# 日期工具
# ──────────────────────────────────────────────
def today_key():
    return datetime.now().strftime("%Y%m%d")

def yesterday_key():
    return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

def prev_trading_key():
    """往前找最近的交易日 JSON（最多往前 7 天）"""
    for i in range(1, 8):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
        return d   # 先簡單傳回昨天；實際可加判斷

# ──────────────────────────────────────────────
# 文字解析工具
# ──────────────────────────────────────────────
def is_financial(code: str) -> bool:
    return code.strip() in FINANCIAL_CODES

def parse_quantity(s: str) -> int:
    """解析數量字串為整數"""
    return int(re.sub(r"[^\d]", "", s) or "0")

def parse_weight(s: str) -> float:
    """解析權重字串為浮點數"""
    m = re.search(r"[\d.]+", s)
    return float(m.group()) if m else 0.0

def extract_date(text: str) -> str:
    m = re.search(r"(\d{4})[/\-.](\d{2})[/\-.](\d{2})", text)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return datetime.now().strftime("%Y/%m/%d")

def build_holding(code, name, qty, weight, item_type="stock"):
    qty = int(qty)
    return {
        "code":          code.strip(),
        "name":          name.strip(),
        "quantity":      qty,
        "zhang":         qty // 1000,
        "weight":        round(float(weight), 4),
        "is_financial":  is_financial(code),
        "item_type":     item_type,
        "display_unit":  "張",
    }

def build_nonstock(code, name, quantity, weight, item_type, display_unit, contract_month=""):
    """建立非個股持倉（期貨、債券、現金等）"""
    s = str(quantity).replace(",", "")
    is_neg = s.lstrip().startswith("-")
    qty = int(re.sub(r"[^\d]", "", s) or "0") * (-1 if is_neg else 1)
    return {
        "code":           code.strip(),
        "name":           name.strip(),
        "quantity":       qty,
        "zhang":          0,
        "weight":         round(float(weight), 4),
        "is_financial":   False,
        "item_type":      item_type,
        "display_unit":   display_unit,
        "contract_month": contract_month,
    }

# ──────────────────────────────────────────────
# Playwright 抓取（完整資料）
# ──────────────────────────────────────────────
async def pw_extract_yuanta(page, code: str) -> dict:
    url = f"https://www.yuantaetfs.com/product/detail/{code}/ratio"
    print(f"    → 載入 {url}")
    await page.goto(url, wait_until="networkidle", timeout=40000)

    # 點「展開」取得完整清單
    try:
        btn = page.locator("text=展開").first
        if await btn.is_visible(timeout=3000):
            await btn.click()
            await page.wait_for_timeout(1500)
    except Exception:
        pass

    text = await page.inner_text("body")
    return parse_yuanta_text(text)

def parse_yuanta_text(text: str) -> dict:
    """解析元大 ETF 頁面文字（含期貨部位）
    個股段落 header: 商品代碼\t商品名稱\t商品數量\t商品權重  (4欄)
    期貨段落 header: 商品代碼\t商品名稱\t商品數量\t商品權重\t商品年月 (5欄)
    """
    date_str = extract_date(text)
    holdings = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    in_stock_section   = False
    in_futures_section = False
    for line in lines:
        # 期貨段落 header（含「商品年月」欄）
        if '商品代碼' in line and '商品名稱' in line and '商品年月' in line and '\t' in line:
            in_stock_section   = False
            in_futures_section = True
            continue
        # 個股段落 header（不含「商品年月」欄）
        if '商品代碼' in line and '商品名稱' in line and '\t' in line and '商品年月' not in line:
            in_stock_section   = True
            in_futures_section = False
            continue

        if in_stock_section:
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) >= 4 and re.match(r"^\d{4,6}$", parts[0]):
                code_v, name, qty_s, wt_s = parts[0], parts[1], parts[2], parts[3]
                holdings.append(build_holding(code_v, name, parse_quantity(qty_s), wt_s))

        elif in_futures_section:
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) >= 4 and parts[0] and re.match(r"^[A-Z]", parts[0]):
                code_v, name, qty_s, wt_s = parts[0], parts[1], parts[2], parts[3]
                month = parts[4] if len(parts) > 4 else ""
                holdings.append(build_nonstock(
                    code_v, name, parse_quantity(qty_s), parse_weight(wt_s),
                    "futures", "口", month))

    # SSR 備用（requests 取得格式，僅個股）
    if not any(h.get("item_type") == "stock" for h in holdings):
        flat = re.sub(r"\s+", " ", text)
        pattern = (
            r"商品代碼\s+(\d{4,6})\s+"
            r"商品名稱\s+([^\d\s][^\d]*?)\s+"
            r"商品數量\s+([\d,]+)\s+"
            r"商品權重\s+([\d.]+)"
        )
        for m in re.finditer(pattern, flat):
            code_v, name, qty_s, wt_s = m.groups()
            holdings.append(build_holding(code_v, name, parse_quantity(qty_s), wt_s))

    return {"date": date_str, "holdings": holdings}

async def pw_extract_fubon(page) -> dict:
    """富邦 006208 — 表格格式 [代碼, 名稱, 股數, 金額, 權重%]"""
    url = "https://websys.fsit.com.tw/FubonETF/Fund/Assets.aspx?stkId=006208"
    print(f"    → 載入 {url}")
    await page.goto(url, wait_until="networkidle", timeout=40000)
    await page.wait_for_timeout(2000)

    date_str = extract_date(await page.inner_text("body"))

    rows = await page.evaluate("""() => {
        const results = [];
        let section = '';
        document.querySelectorAll('table tr').forEach(tr => {
            const cells = Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim());
            if (!cells.length) return;
            const first = cells[0];
            if (first === '股票代碼') { section = 'stock';   return; }
            if (first === '期貨代碼') { section = 'futures'; return; }
            if (first === '代號')    { section = 'bond';    return; }
            if (first.includes('合計') || first.includes('代碼') || first.includes('名稱')) return;
            if (cells.length >= 2 && first) results.push({s: section, c: cells});
        });
        return results;
    }""")

    holdings = []
    for item in rows:
        section = item["s"]
        cells   = item["c"]
        code    = cells[0].strip()
        name    = cells[1].strip() if len(cells) > 1 else ""

        if section == "stock" and re.match(r"^\d{4,6}$", code):
            qty = parse_quantity(cells[2]) if len(cells) > 2 else 0
            wt  = parse_weight(cells[4])  if len(cells) >= 5 else parse_weight(cells[-1])
            holdings.append(build_holding(code, name, qty, wt))
        elif section == "futures" and code:
            qty = parse_quantity(cells[2]) if len(cells) > 2 else 0
            wt  = parse_weight(cells[4])  if len(cells) >= 5 else 0.0
            holdings.append(build_nonstock(code, name, qty, wt, "futures", "口"))
        elif section == "bond" and code and len(cells) >= 3:
            amount = parse_quantity(cells[2])
            holdings.append(build_nonstock(code, name, amount, 0.0, "bond", "元"))

    return {"date": date_str, "holdings": holdings}

async def pw_extract_cathay_cmoney(page) -> dict:
    """國泰 00878 — 從 CMoney 持股明細頁面擷取（含實際張數）
    資料來源: https://www.cmoney.tw/etf/tw/00878/fundholding
    """
    url = "https://www.cmoney.tw/etf/tw/00878/fundholding"
    print(f"    → 載入 {url}")
    # 擷取 Bearer token（供後續 AUM API 使用）
    global _cmoney_token
    async def _on_req(req):
        global _cmoney_token
        if not _cmoney_token and "customReport" in req.url:
            auth = req.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                _cmoney_token = auth[7:]
    page.on("request", _on_req)

    await page.goto(url, wait_until="networkidle", timeout=50000)
    await page.wait_for_timeout(3000)

    body_text = await page.inner_text("body")

    # 資料日期
    date_str = datetime.now().strftime("%Y/%m/%d")
    m = re.search(r"資料日期[：:][\s]*(\d{4}/\d{1,2}/\d{1,2})", body_text)
    if m:
        date_str = m.group(1)

    # AUM 資產規模
    aum_str = ""
    m2 = re.search(r"資產規模\(億\)[：:]\s*([\d,.]+)", body_text)
    if m2:
        aum_str = m2.group(1) + " 億"

    # 從表格 TD 擷取各欄
    rows_data = await page.evaluate("""() => {
        const result = [];
        document.querySelectorAll('table tr').forEach(tr => {
            const tds = tr.querySelectorAll('td');
            if (tds.length >= 5) {
                result.push(Array.from(tds).map(td => td.innerText.trim()));
            }
        });
        return result;
    }""")

    holdings = []
    for cells in rows_data:
        code    = cells[0].strip()
        name    = cells[1].strip()
        wt_str  = cells[2].strip()
        qty_str = cells[3].strip()
        unit    = cells[4].strip() if len(cells) > 4 else ""
        if re.match(r"^\d{1,2}$", code):   # 跳過行業分類序號
            continue
        wt = parse_weight(wt_str)
        if re.match(r"^\d{4,6}$", code) and unit == "股":
            qty = parse_quantity(qty_str)
            holdings.append(build_holding(code, name, qty, wt))
        elif unit == "口":
            qty = parse_quantity(qty_str)
            holdings.append(build_nonstock(code, name, qty, wt, "futures", "口"))
        elif unit == "元" and code:
            if "CASH" in code or code == "C_NTD":
                itype = "cash"
            elif "MARGIN" in code or code == "M_NTD":
                itype = "margin"
            elif code.startswith("PFUR") or "PAYABLE" in name.upper():
                itype = "payable"
            else:
                itype = "other"
            s = qty_str.lstrip(); is_neg = s.startswith("-")
            qty = parse_quantity(s) * (-1 if is_neg else 1)
            holdings.append(build_nonstock(code, name, qty, wt, itype, "元"))

    return {"date": date_str, "holdings": holdings, "aum": aum_str}

async def pw_extract_capital(page) -> dict:
    """群益 00919 — 自訂 pct-stock-table 格式
    格式: 股票代號, 股票名稱, 持股權重(%), 股數
    需點「展開全部」才能取得完整資料
    """
    url = "https://www.capitalfund.com.tw/etf/product/detail/195/portfolio"
    print(f"    → 載入 {url}")
    await page.goto(url, wait_until="load", timeout=40000)
    await page.wait_for_timeout(3000)

    # 點「展開全部」（用 class selector，比文字比對更精準）
    try:
        btn = page.locator(".pct-stock-table-tbody-toggle-btn").first
        if await btn.is_visible(timeout=3000):
            await btn.click()
            await page.wait_for_timeout(1500)
            print("    ✔ 已點擊展開全部")
        else:
            # 備用：文字比對
            btn2 = page.locator("text=展開全部").first
            if await btn2.is_visible(timeout=2000):
                await btn2.click()
                await page.wait_for_timeout(1500)
                print("    ✔ 已點擊展開全部（備用）")
    except Exception:
        pass

    date_str = extract_date(await page.inner_text("body"))

    # 從 pct-stock-table-tbody 解析
    # 格式：code\nname\nweight%\nquantity
    # 先嘗試最精確的選擇器（除錯已確認有效）
    raw = await page.evaluate("""() => {
        const tbody = document.querySelector('tbody.pct-stock-table-tbody')
                   || document.querySelector('.pct-stock-table-tbody');
        if (!tbody) return null;
        return tbody.innerText;
    }""")

    holdings = []
    if raw:
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        i = 0
        while i + 3 < len(lines):
            code = lines[i]
            if not re.match(r"^\d{4,6}$", code):
                i += 1
                continue
            name   = lines[i+1]
            wt_str = lines[i+2]   # e.g. "14.58%"
            qty_str= lines[i+3]   # e.g. "602,462,000"
            wt  = parse_weight(wt_str)
            qty = parse_quantity(qty_str)
            holdings.append(build_holding(code, name, qty, wt))
            i += 4

    if not holdings:
        # 備用：從整頁文字中解析
        raw_all = await page.evaluate("() => document.querySelector('[class*=\"pct-stock\"]')?.innerText || ''")
        if raw_all:
            lines = [l.strip() for l in raw_all.splitlines() if l.strip()]
            i = 0
            while i < len(lines) - 3:
                if re.match(r"^\d{4,6}$", lines[i]):
                    code, name = lines[i], lines[i+1]
                    wt  = parse_weight(lines[i+2])
                    qty = parse_quantity(lines[i+3])
                    holdings.append(build_holding(code, name, qty, wt))
                    i += 4
                else:
                    i += 1

    return {"date": date_str, "holdings": holdings}

async def fetch_all_playwright() -> dict:
    results = {}
    fetch_map = {
        "0050":   lambda p, pg: pw_extract_yuanta(pg, "0050"),
        "0056":   lambda p, pg: pw_extract_yuanta(pg, "0056"),
        "006208": lambda p, pg: pw_extract_fubon(pg),
        "00878":  lambda p, pg: pw_extract_cathay_cmoney(pg),
        "00919":  lambda p, pg: pw_extract_capital(pg),
    }
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for code, fn in fetch_map.items():
            meta = ETF_META[code]
            print(f"\n  [{code}] {meta['name']} ({meta['company']})")
            try:
                page = await browser.new_page()
                await page.set_extra_http_headers(HEADERS)
                data = await fn(p, page)
                await page.close()
                results[code] = data
                n = len(data.get("holdings", []))
                print(f"    ✓ 取得 {n} 筆成分股  (日期: {data.get('date','')})")
            except Exception as e:
                print(f"    ✗ 失敗: {e}")
                results[code] = {"date": datetime.now().strftime("%Y/%m/%d"),
                                  "holdings": [], "error": str(e)}
        await browser.close()
    return results

# ──────────────────────────────────────────────
# requests 備用抓取（部分資料）
# ──────────────────────────────────────────────
def fetch_yuanta_requests(code: str) -> dict:
    url = f"https://www.yuantaetfs.com/product/detail/{code}/ratio"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ")
        data = parse_yuanta_text(text)
        data["partial"] = True
        return data
    except Exception as e:
        return {"date": datetime.now().strftime("%Y/%m/%d"),
                "holdings": [], "error": str(e), "partial": True}

def fetch_all_requests() -> dict:
    results = {}
    for code in ETF_META:
        meta = ETF_META[code]
        print(f"\n  [{code}] {meta['name']}")
        if code in ("0050", "0056"):
            data = fetch_yuanta_requests(code)
            n = len(data.get("holdings", []))
            print(f"    ✓ SSR 取得 {n} 筆（僅前幾名，建議使用 Playwright 模式）")
        else:
            print(f"    ⚠ {meta['company']} 為動態載入，需 Playwright 模式")
            data = {"date": datetime.now().strftime("%Y/%m/%d"),
                    "holdings": [], "partial": True,
                    "msg": "需要 Playwright 才能取得完整資料"}
        results[code] = data
    return results

# ──────────────────────────────────────────────
# 資料存取與比較
# ──────────────────────────────────────────────

def fetch_aum_all(codes: list) -> dict:
    """用 CMoney customReport API 批次取得各 ETF 的 AUM（需先執行 pw_extract_cathay_cmoney）"""
    aum_map = {}
    if not _cmoney_token:
        return aum_map
    hdr = {
        "Authorization": f"Bearer {_cmoney_token}",
        "Content-Type": "application/json",
        "Referer": "https://www.cmoney.tw/etf/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    for code in codes:
        try:
            payload = {
                "Dtno": "60465380",
                "Params": f"AssignID={code};DTRange=1",
                "FilterNo": "0",
            }
            r = requests.post(
                "https://www.cmoney.tw/api/customReport/app/v2/dtno/JsonCsv",
                json=payload, headers=hdr, timeout=8,
            )
            if not r.ok:
                continue
            j = r.json()
            col_keys = list(j.get("columns", {}).keys())
            rows = j.get("rows", [])
            if not rows:
                continue
            # 找「資產規模(億)」欄
            for i, k in enumerate(col_keys):
                if "資產規模" in k:
                    val = rows[0][i]
                    if val:
                        aum_map[code] = f"{val:,.2f} 億" if isinstance(val, float) else f"{val} 億"
                    break
        except Exception as e:
            print(f"    ⚠ AUM {code}: {e}")
    return aum_map

def save_data(all_data: dict):
    DATA_DIR.mkdir(exist_ok=True)
    key = today_key()
    path = DATA_DIR / f"etf_{key}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 今日資料已存: {path.name}")

def load_prev_data(today_data_date: str = "") -> tuple:
    """返回 (prev_data, prev_data_date)
    
    以 ETF 資料日期（非執行日期）為準：
    找最近一個 ETF 資料日期 < today_data_date 的 JSON 檔。
    這樣週末多次執行時，不會把「同一交易日資料」當成前後兩日來比較。
    """
    DATA_DIR.mkdir(exist_ok=True)
    files = sorted(DATA_DIR.glob("etf_*.json"), reverse=True)
    today_key_val = today_key()

    for f in files:
        key = f.stem.replace("etf_", "")
        if key == today_key_val:
            continue  # 跳過今天的執行檔
        try:
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception:
            continue

        # 取該檔案的 ETF 資料日期
        file_data_date = ""
        for code_data in data.values():
            if isinstance(code_data, dict) and code_data.get("date"):
                file_data_date = code_data["date"]
                break

        # 若沒有參照日期，接受第一個非今日檔案（相容舊格式）
        if not today_data_date or not file_data_date:
            print(f"  📂 載入前日資料: {f.name}  （ETF資料日期: {file_data_date or '?'}）")
            return data, file_data_date

        # 核心邏輯：只接受 ETF 資料日期「更早」的檔案
        if file_data_date < today_data_date:
            print(f"  📂 載入前日資料: {f.name}  （ETF資料日期: {file_data_date}）")
            return data, file_data_date

        # 同一交易日資料 → 跳過，繼續往前找
        print(f"  ⏭  跳過 {f.name}（ETF資料日期同為 {file_data_date}，非前日）")

    print("  ℹ  無前日交易日資料，所有成分股標記為新進")
    return {}, ""

def merge_with_prev(today: dict, prev: dict, prev_date: str = "") -> dict:
    """將今日資料與前日比較，加入增減欄位"""
    merged = {}
    for code, data in today.items():
        prev_holdings = {h["code"]: h for h in prev.get(code, {}).get("holdings", [])}
        new_holdings = []
        for h in data.get("holdings", []):
            ph = prev_holdings.get(h["code"])
            h2 = dict(h)
            h2["prev_zhang"]      = ph["zhang"]    if ph else None
            h2["prev_weight"]     = ph["weight"]   if ph else None
            h2["prev_quantity"]   = ph["quantity"] if ph else None
            h2["change_zhang"]    = (h["zhang"]    - ph["zhang"])    if ph else None
            h2["change_weight"]   = round(h["weight"] - ph["weight"], 4) if ph else None
            h2["change_quantity"] = (h["quantity"] - ph["quantity"]) if ph else None
            h2["is_new"]          = ph is None
            new_holdings.append(h2)
        merged[code] = dict(data)
        merged[code]["holdings"] = new_holdings
        merged[code]["prev_date"] = prev_date
    return merged

# ──────────────────────────────────────────────
# HTML 產生
# ──────────────────────────────────────────────
def generate_html(merged: dict) -> str:
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")
    data_json = json.dumps(merged, ensure_ascii=False)

    # 製作各 ETF tab 標籤與 panel
    tabs_html = ""
    panels_html = ""
    for i, (code, meta) in enumerate(ETF_META.items()):
        active_cls = "active" if i == 0 else ""
        tabs_html += f"""
        <button class="tab-btn {active_cls}"
                onclick="showTab('{code}')"
                id="tab-{code}"
                style="--tab-color:{meta['color']}">
            <span class="tab-code">{code}</span>
            <span class="tab-name">{meta['name']}</span>
        </button>"""

        panels_html += f"""
        <div class="tab-panel" id="panel-{code}" style="display:{'block' if i==0 else 'none'}">
            <div class="panel-header" style="border-left:4px solid {meta['color']}">
                <div class="panel-title-row">
                    <span class="etf-badge" style="background:{meta['color']}">{code}</span>
                    <strong>{meta['name']}</strong>
                    <small class="company-name">（{meta['company']}）</small>
                </div>
                <div class="panel-meta">
                    <span id="date-{code}" class="date-label">－</span>
                    <span id="aum-{code}" class="aum-badge" style="display:none"></span>
                    <span id="count-{code}" class="count-badge">0 支</span>
                </div>
            </div>
            <div class="fin-summary-bar" id="fin-summary-{code}"></div>
            <div class="summary-bar" id="summary-{code}"></div>
            <div class="table-controls">
                <input type="text" placeholder="🔍 搜尋代碼或名稱…"
                       oninput="filterTable('{code}', this.value)"
                       class="search-input">
                <label class="filter-toggle">
                    <input type="checkbox" id="fin-filter-{code}"
                           onchange="filterTable('{code}')">
                    僅顯示金融股
                </label>
                <div class="type-filter-row" id="type-btns-{code}">
                  <span style="font-size:12px;color:var(--text-sub)">類型：</span>
                  <button class="active" onclick="setTypeFilter('{code}','all',this)">全部</button>
                  <button onclick="setTypeFilter('{code}','stock',this)">個股</button>
                  <button onclick="setTypeFilter('{code}','nonstock',this)">非個股</button>
                </div>
            </div>
            <div class="table-wrap">
                <table id="tbl-{code}">
                    <thead>
                        <tr>
                            <th onclick="sortTable('{code}',0)">代碼 ⇅</th>
                            <th onclick="sortTable('{code}',1)">名稱 ⇅</th>
                            <th onclick="sortTable('{code}',2)" class="num-col">今日張數 ⇅</th>
                            <th onclick="sortTable('{code}',3)" class="num-col">前日張數 ⇅</th>
                            <th onclick="sortTable('{code}',4)" class="num-col">增減(張) ⇅</th>
                            <th onclick="sortTable('{code}',5)" class="num-col">權重(%) ⇅</th>
                            <th>摘要</th>
                        </tr>
                    </thead>
                    <tbody id="tbody-{code}"></tbody>
                </table>
            </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>五大被動ETF成分股每日監控</title>
<style>
:root {{
  --bg: #f5f7fa;
  --surface: #ffffff;
  --border: #e0e4ea;
  --text: #1a2030;
  --text-sub: #6b7280;
  --up: #16a34a;
  --down: #dc2626;
  --fin: #7c3aed;
  --new: #0369a1;
  --radius: 10px;
  --shadow: 0 2px 12px rgba(0,0,0,.08);
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: 'Segoe UI', 'PingFang TC', 'Microsoft JhengHei', sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  line-height: 1.5;
}}
.header {{
  background: linear-gradient(135deg, #1a2744 0%, #2d3e6b 100%);
  color: #fff;
  padding: 20px 28px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  box-shadow: 0 2px 8px rgba(0,0,0,.2);
}}
.header h1 {{ font-size: 20px; font-weight: 700; letter-spacing: .5px; }}
.header h1 span {{ font-size: 13px; font-weight: 400; opacity: .75; display: block; margin-top: 2px; }}
.header-meta {{ text-align: right; font-size: 12px; opacity: .8; }}
.header-meta strong {{ display: block; font-size: 13px; color: #a5f3fc; }}
.tabs-bar {{
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 0 20px;
  display: flex;
  gap: 4px;
  overflow-x: auto;
  box-shadow: var(--shadow);
}}
.tab-btn {{
  background: none;
  border: none;
  border-bottom: 3px solid transparent;
  padding: 14px 18px 11px;
  cursor: pointer;
  font-size: 13px;
  color: var(--text-sub);
  white-space: nowrap;
  transition: all .2s;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}}
.tab-btn:hover {{ color: var(--tab-color); background: #f8f9ff; }}
.tab-btn.active {{
  color: var(--tab-color);
  border-bottom-color: var(--tab-color);
  font-weight: 600;
}}
.tab-code {{ font-size: 16px; font-weight: 700; }}
.tab-name {{ font-size: 11px; }}
.main {{ padding: 20px; max-width: 1400px; margin: 0 auto; }}
.tab-panel {{ animation: fadeIn .25s ease; }}
@keyframes fadeIn {{ from {{ opacity:0; transform:translateY(4px) }} to {{ opacity:1; transform:none }} }}
.panel-header {{
  background: var(--surface);
  padding: 14px 18px;
  border-radius: var(--radius) var(--radius) 0 0;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border: 1px solid var(--border);
  border-bottom: none;
}}
.panel-title-row {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
.etf-badge {{
  color: #fff;
  padding: 2px 9px;
  border-radius: 5px;
  font-size: 13px;
  font-weight: 700;
}}
.company-name {{ color: var(--text-sub); font-size: 12px; }}
.panel-meta {{ font-size: 12px; color: var(--text-sub); display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
.date-label {{ color: var(--text-sub); }}
.aum-badge {{
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 12px;
  color: #1d4ed8;
  font-weight: 700;
}}
.count-badge {{
  background: #f1f5f9;
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 2px 10px;
  font-weight: 600;
  color: var(--text);
}}
/* 金融股每日變化摘要列 */
.fin-summary-bar {{
  background: #faf5ff;
  border: 1px solid #d8b4fe;
  border-bottom: none;
  padding: 10px 18px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
}}
.fin-summary-bar:empty {{ display: none; }}
.fin-chip {{
  background: var(--surface);
  border-radius: 6px;
  padding: 5px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  border: 1px solid #e9d5ff;
}}
.fin-chip .chip-label {{ color: #7c3aed; font-size: 11px; font-weight: 600; }}
.fin-chip .chip-val {{ font-weight: 600; font-size: 13px; color: var(--text); }}
.fin-chip .chip-sub {{ font-size: 11px; color: var(--text-sub); }}
.fin-chip-total {{ border-left: 3px solid #7c3aed; }}
.fin-chip-new   {{ border-left: 3px solid #0369a1; }}
.fin-chip-up    {{ border-left: 3px solid #16a34a; }}
.fin-chip-down  {{ border-left: 3px solid #dc2626; }}
/* 一般摘要列 */
.summary-bar {{
  background: #fafbff;
  border: 1px solid var(--border);
  border-bottom: none;
  padding: 10px 18px;
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  font-size: 12px;
}}
.summary-chip {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 5px 12px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}}
.summary-chip .chip-label {{ color: var(--text-sub); font-size: 11px; }}
.summary-chip .chip-val {{ font-weight: 700; font-size: 14px; }}
.chip-up  {{ border-left: 3px solid var(--up); }}
.chip-down{{ border-left: 3px solid var(--down); }}
.chip-new {{ border-left: 3px solid var(--new); }}
.table-controls {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-bottom: none;
  padding: 10px 18px;
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}}
.search-input {{
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 13px;
  width: 220px;
  outline: none;
}}
.search-input:focus {{ border-color: #6366f1; box-shadow: 0 0 0 2px #6366f120; }}
.filter-toggle {{
  font-size: 13px;
  color: var(--text-sub);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  user-select: none;
}}
.table-wrap {{
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 0 0 var(--radius) var(--radius);
  box-shadow: var(--shadow);
}}
table {{
  width: 100%;
  border-collapse: collapse;
  background: var(--surface);
}}
thead {{
  background: #f8f9fc;
  position: sticky;
  top: 0;
  z-index: 1;
}}
th {{
  padding: 10px 14px;
  text-align: left;
  font-weight: 600;
  font-size: 12px;
  color: var(--text-sub);
  border-bottom: 2px solid var(--border);
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
}}
th:hover {{ background: #eef0f7; }}
.num-col {{ text-align: right; }}
td {{
  padding: 9px 14px;
  border-bottom: 1px solid #f0f2f6;
  vertical-align: middle;
}}
tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: #f9faff; }}
td.num-col {{ text-align: right; font-variant-numeric: tabular-nums; }}
.code-cell {{ font-weight: 700; font-family: monospace; font-size: 14px; }}
.fin-badge {{
  background: #ede9fe;
  color: var(--fin);
  font-size: 10px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 4px;
  margin-left: 5px;
  border: 1px solid #c4b5fd;
}}
.new-badge {{
  background: #e0f2fe;
  color: var(--new);
  font-size: 10px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 4px;
  margin-left: 5px;
}}
.change-up   {{ color: var(--up);   font-weight: 700; }}
.change-down {{ color: var(--down); font-weight: 700; }}
.change-zero {{ color: var(--text-sub); }}
.change-null {{ color: #cbd5e1; font-size: 12px; }}
.arrow-up   {{ font-size: 11px; }}
.arrow-down {{ font-size: 11px; }}
.weight-bar {{
  display: inline-block;
  background: #e2e8f0;
  border-radius: 3px;
  height: 6px;
  vertical-align: middle;
  margin-right: 6px;
}}
.weight-fill {{
  display: inline-block;
  background: #6366f1;
  border-radius: 3px;
  height: 6px;
}}
.memo-cell {{ font-size: 12px; color: var(--text-sub); line-height: 1.4; }}
.memo-fin  {{ color: var(--fin); font-weight: 600; }}
.no-data {{
  padding: 40px;
  text-align: center;
  color: var(--text-sub);
  font-size: 14px;
}}
.error-msg {{
  background: #fff5f5;
  border: 1px solid #fed7d7;
  color: #c53030;
  padding: 12px 18px;
  border-radius: 8px;
  margin: 12px 0;
  font-size: 13px;
}}
.partial-msg {{
  background: #fffbeb;
  border: 1px solid #fde68a;
  color: #92400e;
  padding: 10px 18px;
  border-radius: 8px;
  margin: 10px 0;
  font-size: 12px;
}}
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>📊 五大被動ETF成分股每日監控
      <span>0050 ‧ 0056 ‧ 006208 ‧ 00878 ‧ 00919</span>
    </h1>
  </div>
  <div class="header-meta">
    <strong id="update-time">更新中…</strong>
    資料更新時間
  </div>
</div>

<div class="tabs-bar">
  {tabs_html}
</div>

<div class="main">
  {panels_html}
</div>

<script>
const ETF_META = {json.dumps(ETF_META, ensure_ascii=False)};
const RAW_DATA = {data_json};
const UPDATE_TIME = "{now_str}";

document.getElementById('update-time').textContent = UPDATE_TIME;

// ── 排序狀態
const sortState = {{}};

// ── 金融股摘要文字（表格 memo 欄）
function finSummary(h) {{
  if (!h.is_financial) return '';
  const parts = [];
  const chg = h.change_zhang;
  if (h.is_new) parts.push('🆕 新進成分股');
  else if (chg > 0)  parts.push(`↑ 增加 ${{Math.abs(chg).toLocaleString()}} 張`);
  else if (chg < 0)  parts.push(`↓ 減少 ${{Math.abs(chg).toLocaleString()}} 張`);
  else if (chg === 0) parts.push('持平');
  parts.push(`權重 ${{h.weight}}%`);
  if (h.change_weight != null && h.change_weight !== 0) {{
    const wSign = h.change_weight > 0 ? '+' : '';
    parts.push(`(${{wSign}}${{h.change_weight.toFixed(2)}}%)`);
  }}
  return parts.join('　');
}}

function renderTable(code) {{
  const data = RAW_DATA[code];
  if (!data) return;

  const meta = ETF_META[code];
  document.getElementById(`date-${{code}}`).textContent = data.date || '－';

  // AUM 規模
  const aumEl = document.getElementById(`aum-${{code}}`);
  if (aumEl) {{
    if (data.aum) {{
      aumEl.textContent = '規模 ' + data.aum;
      aumEl.style.display = '';
    }} else {{
      aumEl.style.display = 'none';
    }}
  }}

  const holdings = data.holdings || [];
  const stockCnt = holdings.filter(h => !h.item_type || h.item_type === 'stock').length;
  const nonsCnt  = holdings.length - stockCnt;
  document.getElementById(`count-${{code}}`).textContent =
    nonsCnt > 0 ? `${{stockCnt}} 支個股 + ${{nonsCnt}} 筆非個股` : `${{stockCnt}} 支`;

  // ── 金融股每日變化摘要
  const finList    = holdings.filter(h => h.is_financial);
  const finWeight  = finList.reduce((s, h) => s + (h.weight || 0), 0);
  const prevFinW   = finList.reduce((s, h) => s + (h.prev_weight || 0), 0);
  const finWChange = finWeight - prevFinW;
  const finNew     = finList.filter(h => h.is_new);
  const finUp      = finList.filter(h => !h.is_new && h.change_zhang > 0)
                            .sort((a,b) => b.change_zhang - a.change_zhang);
  const finDown    = finList.filter(h => !h.is_new && h.change_zhang < 0)
                            .sort((a,b) => a.change_zhang - b.change_zhang);

  let finHtml = '';
  if (finList.length > 0) {{
    const prevLabel = data.prev_date ? ` vs ${{data.prev_date}}` : '';
    const wSign = finWChange >= 0 ? '+' : '';
    const wChgStr = (prevFinW > 0) ? `　${{wSign}}${{finWChange.toFixed(2)}}%` : '';
    finHtml += `
      <div class="fin-chip fin-chip-total">
        <span class="chip-label">💜 金融股總計${{prevLabel}}</span>
        <span class="chip-val">${{finList.length}} 支｜${{finWeight.toFixed(2)}}%${{wChgStr}}</span>
      </div>`;
    if (finNew.length > 0) {{
      finHtml += `
        <div class="fin-chip fin-chip-new">
          <span class="chip-label">🆕 新進金融股</span>
          <span class="chip-val">${{finNew.map(h => h.code + ' ' + h.name).join('、')}}</span>
        </div>`;
    }}
    if (finUp.length > 0) {{
      finHtml += `
        <div class="fin-chip fin-chip-up">
          <span class="chip-label">↑ 加碼 (${{finUp.length}} 支)</span>
          <span class="chip-val">${{finUp.slice(0,4).map(h => h.code).join('、')}}</span>
          <span class="chip-sub">${{finUp.slice(0,4).map(h => '+' + h.change_zhang.toLocaleString() + '張').join(' ')}}</span>
        </div>`;
    }}
    if (finDown.length > 0) {{
      finHtml += `
        <div class="fin-chip fin-chip-down">
          <span class="chip-label">↓ 減碼 (${{finDown.length}} 支)</span>
          <span class="chip-val">${{finDown.slice(0,4).map(h => h.code).join('、')}}</span>
          <span class="chip-sub">${{finDown.slice(0,4).map(h => h.change_zhang.toLocaleString() + '張').join(' ')}}</span>
        </div>`;
    }}
  }}
  document.getElementById(`fin-summary-${{code}}`).innerHTML = finHtml;

  // ── 一般成分股摘要（漲減新）
  const upList   = holdings.filter(h => h.change_zhang > 0);
  const downList = holdings.filter(h => h.change_zhang < 0);
  const newList  = holdings.filter(h => h.is_new);
  let summaryHtml = '';
  if (upList.length > 0) summaryHtml += `
    <div class="summary-chip chip-up">
      <span class="chip-label">↑ 增加張數</span>
      <span class="chip-val">${{upList.length}} 支</span>
    </div>`;
  if (downList.length > 0) summaryHtml += `
    <div class="summary-chip chip-down">
      <span class="chip-label">↓ 減少張數</span>
      <span class="chip-val">${{downList.length}} 支</span>
    </div>`;
  if (newList.length > 0) summaryHtml += `
    <div class="summary-chip chip-new">
      <span class="chip-label">🆕 新進成分股</span>
      <span class="chip-val">${{newList.length}} 支</span>
    </div>`;
  document.getElementById(`summary-${{code}}`).innerHTML = summaryHtml;

  // ── 錯誤／警告訊息
  const panelEl = document.getElementById(`panel-${{code}}`);
  ['error-box','partial-box'].forEach(id => {{
    const el = panelEl.querySelector('#'+id);
    if (el) el.remove();
  }});
  if (data.error) {{
    const div = document.createElement('div');
    div.id = 'error-box'; div.className = 'error-msg';
    div.textContent = '⚠ ' + data.error;
    panelEl.querySelector('.table-controls').before(div);
  }}
  if (data.partial) {{
    const div = document.createElement('div');
    div.id = 'partial-box'; div.className = 'partial-msg';
    div.textContent = '⚠ 資料不完整（僅部分成分股）。完整資料請執行: python fetch_etf.py';
    panelEl.querySelector('.table-controls').before(div);
  }}

  renderRows(code, holdings);
}}

function getTypeLabel(t) {{
  return {{futures:'期貨',bond:'債券',cash:'現金',margin:'保證金',payable:'應付款',other:'其他'}}[t] || t;
}}
function fmtAmount(qty) {{
  const abs = Math.abs(qty), sign = qty < 0 ? '-' : '';
  if (abs >= 1e8) return sign + (abs/1e8).toFixed(2) + ' 億';
  if (abs >= 1e3) return sign + Math.round(abs/1e3).toLocaleString() + ' 千';
  return qty.toLocaleString();
}}
function renderRows(code, holdings) {{
  const stocks = holdings.filter(h => !h.item_type || h.item_type === 'stock');
  const maxWeight = Math.max(...stocks.map(h => h.weight || 0), 0.01);
  const tbody = document.getElementById(`tbody-${{code}}`);
  if (!holdings.length) {{
    tbody.innerHTML = '<tr><td colspan="7" class="no-data">暫無資料，請執行 python fetch_etf.py 更新</td></tr>';
    return;
  }}
  tbody.innerHTML = holdings.map(h => {{
    const isStock   = !h.item_type || h.item_type === 'stock';
    const isFutures = h.item_type === 'futures';
    const typeBadge = !isStock ? `<span class="type-badge type-${{h.item_type}}">${{getTypeLabel(h.item_type)}}</span>` : '';
    const finBadge  = h.is_financial ? '<span class="fin-badge">金融</span>' : '';
    const newBadge  = '';
    let todayQty, prevQty;
    if (isStock) {{
      todayQty = h.zhang != null ? h.zhang.toLocaleString() + ' 張' : 'N/A';
      prevQty  = h.prev_zhang != null ? h.prev_zhang.toLocaleString() + ' 張' : 'N/A';
    }} else if (isFutures) {{
      todayQty = h.quantity != null ? h.quantity.toLocaleString() + ' 口' : 'N/A';
      prevQty  = h.prev_quantity != null ? h.prev_quantity.toLocaleString() + ' 口' : '－';
    }} else {{
      todayQty = h.quantity != null ? fmtAmount(h.quantity) : 'N/A';
      prevQty  = h.prev_quantity != null ? fmtAmount(h.prev_quantity) : '－';
    }}
    let changeHtml = '<span class="change-null">－</span>';
    if (isStock && h.change_zhang !== null && h.change_zhang !== undefined) {{
      if (h.is_new)             changeHtml = `<span class="change-up">+${{h.zhang.toLocaleString()}} 🆕</span>`;
      else if (h.change_zhang > 0)  changeHtml = `<span class="change-up arrow-up">▲ ${{h.change_zhang.toLocaleString()}}</span>`;
      else if (h.change_zhang < 0)  changeHtml = `<span class="change-down arrow-down">▼ ${{Math.abs(h.change_zhang).toLocaleString()}}</span>`;
      else                          changeHtml = '<span class="change-zero">＝ 0</span>';
    }} else if (isFutures && h.change_quantity !== null && h.change_quantity !== undefined) {{
      if (h.change_quantity > 0)    changeHtml = `<span class="change-up arrow-up">▲ ${{h.change_quantity.toLocaleString()}} 口</span>`;
      else if (h.change_quantity < 0) changeHtml = `<span class="change-down arrow-down">▼ ${{Math.abs(h.change_quantity).toLocaleString()}} 口</span>`;
      else                            changeHtml = '<span class="change-zero">＝ 0</span>';
    }}
    const barW = isStock ? Math.round((h.weight/maxWeight)*70) : Math.min(Math.round(h.weight*10),70);
    const weightCell = h.weight > 0
      ? `<span class="weight-bar" style="width:${{barW}}px"><span class="weight-fill" style="width:100%;background:${{ETF_META[code].color}}"></span></span>${{h.weight.toFixed(2)}}%`
      : '－';
    const memo = isStock && h.is_financial ? `<span class="memo-fin">${{finSummary(h)}}</span>` : '';
    const rowCls = [h.is_financial ? 'fin-row' : '', !isStock ? 'nonstock-row' : ''].filter(Boolean).join(' ');
    return `<tr class="${{rowCls}}" data-code="${{h.code}}" data-name="${{h.name}}"
                data-fin="${{h.is_financial ? '1' : '0'}}" data-type="${{h.item_type || 'stock'}}">
      <td class="code-cell">${{h.code}}${{typeBadge}}${{finBadge}}${{newBadge}}</td>
      <td>${{h.name}}${{h.contract_month ? '<span style="color:#888;font-size:11px"> (' + h.contract_month + ')</span>' : ''}}</td>
      <td class="num-col">${{todayQty}}</td>
      <td class="num-col">${{prevQty}}</td>
      <td class="num-col">${{changeHtml}}</td>
      <td class="num-col">${{weightCell}}</td>
      <td class="memo-cell">${{memo}}</td>
    </tr>`;
  }}).join('');
}}

function showTab(code) {{
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
  document.getElementById('tab-' + code).classList.add('active');
  document.getElementById('panel-' + code).style.display = 'block';
}}

const _typeFilters = {{}};
function setTypeFilter(code, type, btn) {{
  _typeFilters[code] = type;
  document.querySelectorAll(`#type-btns-${{code}} button`).forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  filterTable(code);
}}
function filterTable(code, searchVal) {{
  const panel = document.getElementById(`panel-${{code}}`);
  const search = (searchVal !== undefined ? searchVal : panel.querySelector('.search-input').value).toLowerCase();
  const finOnly    = document.getElementById(`fin-filter-${{code}}`).checked;
  const typeFilter = _typeFilters[code] || 'all';
  document.querySelectorAll(`#tbody-${{code}} tr`).forEach(tr => {{
    const c = (tr.dataset.code || '').toLowerCase();
    const n = (tr.dataset.name || '').toLowerCase();
    const isFin   = tr.dataset.fin  === '1';
    const rtype   = tr.dataset.type || 'stock';
    const isStock = rtype === 'stock';
    const matchSearch = !search || c.includes(search) || n.includes(search);
    const matchFin    = !finOnly || isFin;
    const matchType   = typeFilter === 'all'
                      || (typeFilter === 'stock'    &&  isStock)
                      || (typeFilter === 'nonstock' && !isStock);
    tr.style.display = (matchSearch && matchFin && matchType) ? '' : 'none';
  }});
}}

function sortTable(code, colIdx) {{
  const key = `${{code}}_${{colIdx}}`;
  const asc = sortState[key] = !sortState[key];
  const tbody = document.getElementById(`tbody-${{code}}`);
  const rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {{
    const av = a.querySelectorAll('td')[colIdx]?.textContent.trim() || '';
    const bv = b.querySelectorAll('td')[colIdx]?.textContent.trim() || '';
    const an = parseFloat(av.replace(/[^0-9.\-]/g, ''));
    const bn = parseFloat(bv.replace(/[^0-9.\-]/g, ''));
    if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
    return asc ? av.localeCompare(bv, 'zh-TW') : bv.localeCompare(av, 'zh-TW');
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

// 初始化所有 panel
Object.keys(RAW_DATA).forEach(code => renderTable(code));
</script>
</body>
</html>"""

# ──────────────────────────────────────────────
# 主程式
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="五大被動ETF每日成分股監控")
    parser.add_argument("--no-browser", action="store_true",
                        help="使用 requests 模式（資料較少）")
    args = parser.parse_args()

    print("=" * 55)
    print("  五大被動ETF成分股監控  —  每日資料抓取")
    print(f"  {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}")
    print("=" * 55)

    use_playwright = PLAYWRIGHT_AVAILABLE and not args.no_browser
    if use_playwright:
        print("\n🌐 模式：Playwright（完整資料）")
        all_data = asyncio.run(fetch_all_playwright())
    else:
        if not PLAYWRIGHT_AVAILABLE:
            print("\n⚠  Playwright 未安裝，使用 requests 模式（資料不完整）")
            print("   完整安裝：pip install playwright && playwright install chromium")
        else:
            print("\n📡 模式：requests（部分資料）")
        all_data = fetch_all_requests()

    # 取得各 ETF AUM（CMoney token 在 00878 抓取後自動擷取）
    aum_map = fetch_aum_all(list(ETF_META.keys()))
    if aum_map:
        for code, aum in aum_map.items():
            if code in all_data:
                all_data[code]["aum"] = aum
        found = [f"{c}:{v}" for c, v in aum_map.items()]
        print(f"  💰 AUM 資料: {', '.join(found)}")
    else:
        print("  ℹ  AUM 無法取得（CMoney token 未擷取）")

    # 取今日 ETF 資料日期（從任一有效 ETF 取得）
    today_data_date = ""
    for d in all_data.values():
        if isinstance(d, dict) and d.get("date"):
            today_data_date = d["date"]
            break
    print(f"  📅 今日 ETF 資料日期: {today_data_date}")

    # 載入前一個不同交易日的資料作比較
    prev_data, prev_date = load_prev_data(today_data_date)
    merged = merge_with_prev(all_data, prev_data, prev_date)

    # 儲存今日原始資料
    save_data(all_data)

    # 產生 HTML
    html_content = generate_html(merged)
    out_path = Path(__file__).parent / "etf_monitor.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    total = sum(len(v.get("holdings", [])) for v in merged.values())
    print(f"\n  ✅ 完成！共 {total} 筆成分股資料")
    print(f"  📄 監控頁面: {out_path}")
    print("=" * 55)


if __name__ == "__main__":
    main()
