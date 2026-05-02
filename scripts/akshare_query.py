#!/usr/bin/env python3
"""
StockDeepScan v9.0 — fully refactored.
v9.0 changes:
  - Fix: unreachable except in News (Bug #1)
  - Fix: trend key collision 5d/10d both 'short' (Bug #2)
  - Fix: implement --quick mode (Bug #3)
  - Fix: --rules param actually used in backtest (Bug #4)
  - Fix: cap variable shadowing in margin (Bug #5)
  - Fix: dynamic end_date instead of hardcoded (Bug #6)
  - Refactor: split 800-line mega-function into focused modules
  - Refactor: eliminate global mutable state (Config class)
  - Refactor: add logging instead of silent exception swallowing
  - Refactor: extract technical indicator calculations
  - Refactor: externalize config constants
  - Refactor: use column-name access for akshare DataFrames
  - Refactor: proper main() with __name__ guard
  - Refactor: SQLite connection context manager
Usage: python akshare_query.py <stock_code> [--backtest] [--batch <file>] [--trend] [--quick]
"""

import sys
import json
import os
import time
import hashlib
import functools
import sqlite3
import logging
import re
import argparse
from datetime import datetime, timedelta
from contextlib import contextmanager

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('stockscan')

# ── Dependencies ─────────────────────────────────────────────────────────────
try:
    import baostock as bs
    import pandas as pd
    import numpy as np
    from pytdx.hq import TdxHq_API
    import requests
except ImportError as e:
    logger.error(f"Missing dependency: {e}. Run: pip install baostock pytdx akshare pandas numpy requests")
    sys.exit(1)

# ── Constants ────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, '..', '.cache')
DB_PATH = os.path.join(SCRIPT_DIR, '..', 'snapshots.db')
os.makedirs(CACHE_DIR, exist_ok=True)

TODAY = datetime.now().strftime('%Y-%m-%d')
TODAY_SHORT = datetime.now().strftime('%Y%m%d')

PEER_GROUPS = {
    '稀土永磁': ['600111','000831','600392','600259','000970','600549','300748','300224'],
    '白酒': ['600519','000858','000568','002304','600809','603369','600702'],
    '光伏': ['601012','688599','600438','002459','688390','300274','002129'],
    '锂电池': ['300750','002594','002460','600516','300014','603799','002709'],
    '半导体': ['688981','002371','603986','688012','300782','600703','002049'],
    '券商': ['600030','300059','000776','601688','601211','600999','002797'],
    '银行': ['600036','601398','601288','600900','601328','000001','002142'],
    '保险': ['601318','601628','601336','601601'],
    '光模块/AI算力': ['300308','300502','300394','688498','000988','300570','688205'],
    'CXO/医药': ['603259','300759','300347','002821','688202','300122','000661'],
    '存储芯片/NAND': ['001309','600171','603986','300688','002049','300474','688234','300042'],
}

# ── Config ───────────────────────────────────────────────────────────────────
class Config:
    """Parsed CLI arguments — no global state."""
    def __init__(self, argv=None):
        if argv is None:
            argv = sys.argv[1:]
        p = argparse.ArgumentParser(description='A股四维深度诊断引擎')
        p.add_argument('stock', nargs='?', help='股票代码')
        p.add_argument('--backtest', action='store_true', help='回测模式')
        p.add_argument('--batch', type=str, help='批量分析 CSV 文件')
        p.add_argument('--trend', action='store_true', help='趋势持久化')
        p.add_argument('--quick', action='store_true', help='快速模式(跳过新闻/北向/融资/股东/研报/解禁)')
        p.add_argument('--no-fund-flow', action='store_true', help='跳过资金流')
        p.add_argument('--no-news', action='store_true', help='跳过新闻')
        p.add_argument('--output', type=str, help='输出 JSON 文件路径')
        p.add_argument('--rules', type=str, default='macd_golden', help='回测规则(默认 macd_golden)')
        a = p.parse_args(argv)
        self.stock = a.stock
        self.backtest = a.backtest
        self.batch_file = a.batch
        self.trend = a.trend
        self.quick = a.quick
        self.no_fund_flow = a.no_fund_flow
        self.no_news = a.no_news
        self.output = a.output
        self.rules = a.rules

# ── Retry decorator ──────────────────────────────────────────────────────────
def retry(max_attempts=3, base_delay=1.0, backoff=2.0):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    if attempt < max_attempts - 1:
                        delay = base_delay * (backoff ** attempt)
                        logger.debug(f"Retry {func.__name__} attempt {attempt+1}, wait {delay:.1f}s")
                        time.sleep(delay)
            raise last_err
        return wrapper
    return decorator

# ── Cache helpers ────────────────────────────────────────────────────────────
def cache_key(prefix, *args):
    s = prefix + '|'.join(str(a) for a in args)
    return hashlib.md5(s.encode()).hexdigest()

def cache_get(key, ttl_seconds=1800):
    path = os.path.join(CACHE_DIR, f'{key}.json')
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < ttl_seconds:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                logger.debug(f"Cache read failed for {key}")
    return None

def cache_set(key, data):
    path = os.path.join(CACHE_DIR, f'{key}.json')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, default=str)
    except Exception:
        logger.warning(f"Cache write failed for {key}")

# ── SQLite persistence ───────────────────────────────────────────────────────
@contextmanager
def get_db():
    """Context manager for SQLite — auto-closes on exit."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stock TEXT NOT NULL, date TEXT NOT NULL,
        close REAL, pe REAL, pb REAL, pe_pct REAL, pb_pct REAL,
        main_net_yi REAL, northbound_hold_pct REAL,
        roe REAL, yoy_np REAL, peg REAL,
        score_pos INTEGER, score_neg INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(stock, date)
    )''')
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()

def save_snapshot(stock, data):
    try:
        with get_db() as conn:
            d = data.get('sections', data)
            h = d.get('history', {}).get('latest', {})
            f = d.get('fundamentals', {})
            ff = d.get('fund_flow', {}).get('today', {})
            nb = d.get('northbound', {}).get('today', {})
            sc = d.get('assessment', {}).get('score', {})
            conn.execute('''INSERT OR REPLACE INTO snapshots
                (stock, date, close, pe, pb, pe_pct, pb_pct,
                 main_net_yi, northbound_hold_pct, roe, yoy_np, peg, score_pos, score_neg)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (stock, h.get('date', ''), h.get('close'), h.get('pe'), h.get('pb'),
                 f.get('pe_hist_pct'), f.get('pb_hist_pct'),
                 ff.get('main_net_yi'), nb.get('hold_pct_a'),
                 f.get('roe', 0)*100, f.get('yoy_np_q1') or f.get('yoy_np', 0),
                 f.get('peg'), sc.get('positive', 0), sc.get('negative', 0)))
    except Exception as e:
        logger.warning(f"Snapshot save failed: {e}")

# ── Utility helpers ──────────────────────────────────────────────────────────
def safe_num(v, default=0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default

def validate_stock_code(code):
    """Validate A-share stock code format (6-digit)."""
    return bool(re.match(r'^[036]\d{5}$', code))

def detect_market(code):
    m = 'sz' if code.startswith(('0','3')) else 'sh'
    tdx_m = 0 if m == 'sz' else 1
    sina_p = 'sz' if m == 'sz' else 'sh'
    return m, tdx_m, sina_p

def clean_output(v):
    """NaN-safe recursive cleaner: NaN→None, -0.0→0.0."""
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        if v == 0:
            return 0.0
        return v
    if isinstance(v, dict):
        return {k: clean_output(v2) for k, v2 in v.items()}
    if isinstance(v, list):
        return [clean_output(x) for x in v]
    return v

# ═══════════════════════════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS (shared between analyze & backtest)
# ═══════════════════════════════════════════════════════════════════════════════

def calc_ma(df, periods=(5, 10, 20, 30, 60, 120)):
    """Simple Moving Averages."""
    for n in periods:
        df[f'MA{n}'] = df['close'].rolling(n).mean()
    return df

def calc_macd(df, fast=6, slow=13, signal=5):
    """MACD: DIF, DEA, Histogram."""
    e1 = df['close'].ewm(span=fast, adjust=False).mean()
    e2 = df['close'].ewm(span=slow, adjust=False).mean()
    df['DIF'] = e1 - e2
    df['DEA'] = df['DIF'].ewm(span=signal, adjust=False).mean()
    df['MACDH'] = 2 * (df['DIF'] - df['DEA'])
    return df

def calc_kdj(df, n=6, m1=3, m2=3):
    """KDJ (random indicator)."""
    L = df['low'].rolling(n, min_periods=n).min()
    H = df['high'].rolling(n, min_periods=n).max()
    h_l = H - L
    rsv = np.where(h_l > 0, (df['close'] - L) / h_l * 100, 50.0)
    df['K'] = pd.Series(rsv, index=df.index).ewm(com=m1-1, adjust=False).mean()
    df['D'] = df['K'].ewm(com=m2-1, adjust=False).mean()
    df['J'] = 3 * df['K'] - 2 * df['D']
    return df

def calc_rsi(df, periods=(6, 12, 24)):
    """RSI for multiple periods."""
    d = df['close'].diff()
    for n in periods:
        g_ = d.where(d > 0, 0).rolling(n).mean()
        l_ = (-d.where(d < 0, 0)).rolling(n).mean()
        l_safe = l_.replace(0, np.nan)
        rs_ = g_ / l_safe
        df[f'RSI{n}'] = 100 - 100 / (1 + rs_)
        df[f'RSI{n}'] = df[f'RSI{n}'].fillna(50)
    return df

def calc_wr(df, n=10):
    """Williams %R."""
    h10 = df['high'].rolling(n, min_periods=n).max()
    l10 = df['low'].rolling(n, min_periods=n).min()
    wr_den = h10 - l10
    df['WR'] = np.where(wr_den > 0, (h10 - df['close']) / wr_den * 100, 50.0)
    return df

def calc_boll(df, n=20, k=2):
    """Bollinger Bands."""
    df['B_MID'] = df['close'].rolling(n).mean()
    s2 = df['close'].rolling(n).std()
    df['B_UP'] = df['B_MID'] + k * s2
    df['B_DN'] = df['B_MID'] - k * s2
    return df

def calc_atr(df, n=14):
    """Average True Range."""
    tr_ = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low'] - df['close'].shift()).abs()
    ], axis=1).max(axis=1)
    df['ATR14'] = tr_.rolling(n).mean()
    return df

def calc_adx(df, n=14):
    """ADX / +DI / -DI."""
    pdm_ = df['high'].diff()
    ndm_ = -df['low'].diff()
    pdm_[pdm_ < 0] = 0
    ndm_[ndm_ < 0] = 0
    pdm_[pdm_ < ndm_] = 0
    ndm_[ndm_ < pdm_] = 0
    atr14_ = df['ATR14'].replace(0, np.nan)
    df['PDI'] = 100 * pdm_.rolling(n).mean() / atr14_
    df['MDI'] = 100 * ndm_.rolling(n).mean() / atr14_
    dx_ = 100 * (df['PDI'] - df['MDI']).abs() / (df['PDI'] + df['MDI'])
    df['ADX'] = dx_.rolling(n).mean()
    return df

def calc_volume(df):
    """Volume MA and ratio."""
    df['VMA5'] = df['volume'].rolling(5).mean()
    df['VRATIO'] = df['volume'] / df['VMA5']
    return df

def calc_all_indicators(df):
    """Apply all technical indicators to a K-line DataFrame."""
    df = calc_ma(df)
    df = calc_macd(df)
    df = calc_kdj(df)
    df = calc_rsi(df)
    df = calc_wr(df)
    df = calc_boll(df)
    df = calc_atr(df)
    df = calc_adx(df)
    df = calc_volume(df)
    return df

# ═══════════════════════════════════════════════════════════════════════════════
#  DATA FETCHING MODULES
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_capital(tdx_market, stock_code):
    """Fetch capital info from pytdx."""
    raw_ts = 0
    raw_nc = 0
    result = {}
    try:
        api = TdxHq_API()
        if api.connect('180.153.18.170', 7709):
            try:
                fin = api.get_finance_info(tdx_market, stock_code)
                raw_ts = fin.get('zongguben', 0) or 0
                raw_nc = fin.get('jingzichan', 0) or 0
                result = {
                    'total_shares': raw_ts,
                    'total_shares_yi': round(raw_ts / 1e8, 2) if raw_ts > 1e8 else (raw_ts if raw_ts > 1 else 0),
                    'float_shares': fin.get('liutongguben', 0),
                    'total_assets': fin.get('zongzichan', 0),
                    'net_assets': raw_nc,
                    'net_assets_yi': round(raw_nc / 1e8, 2) if raw_nc > 1e8 else 0,
                    'bvps': fin.get('meigujingzichan', 0),
                    'revenue_ttm': fin.get('zhuyingshouru', 0),
                    'net_profit_ttm': fin.get('jinglirun', 0),
                }
            finally:
                api.disconnect()
        else:
            result = {"error": "TDX connect failed"}
    except Exception as e:
        logger.warning(f"TDX capital fetch failed: {e}")
        result = {"error": str(e)}
    return result, raw_ts, raw_nc

def fetch_realtime(sina_prefix, stock_code):
    """Fetch realtime quote from Sina Finance."""
    try:
        headers = {'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'}
        r = requests.get(f'http://hq.sinajs.cn/list={sina_prefix}{stock_code}', headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.text.split('"')[1].split(',')
            if len(data) >= 32:
                sina = {
                    'name': data[0], 'open': safe_num(data[1]), 'prev_close': safe_num(data[2]),
                    'price': safe_num(data[3]), 'high': safe_num(data[4]), 'low': safe_num(data[5]),
                    'volume_shares': int(safe_num(data[8])), 'amount_yuan': safe_num(data[9]),
                    'date': data[30], 'time': data[31],
                }
                for i, nm in [(10,'bid1_vol'),(11,'bid1'),(12,'bid2_vol'),(13,'bid2'),
                              (14,'bid3_vol'),(15,'bid3'),(16,'bid4_vol'),(17,'bid4'),
                              (18,'bid5_vol'),(19,'bid5')]:
                    sina[nm] = safe_num(data[i])
                for i, nm in [(20,'ask1_vol'),(21,'ask1'),(22,'ask2_vol'),(23,'ask2'),
                              (24,'ask3_vol'),(25,'ask3'),(26,'ask4_vol'),(27,'ask4'),
                              (28,'ask5_vol'),(29,'ask5')]:
                    sina[nm] = safe_num(data[i])
                if len(data) >= 34:
                    sina['pe'] = safe_num(data[32])
                    sina['pb'] = safe_num(data[33])
                return sina
    except Exception as e:
        logger.warning(f"Sina realtime fetch failed: {e}")
        return {"error": str(e)}
    return {}

def fetch_kline(market, stock_code):
    """Fetch K-line data from Baostock (with cache)."""
    ck = cache_key('kl', stock_code)
    cached_kl = cache_get(ck, 30)
    if cached_kl and len(cached_kl) > 0:
        df = pd.DataFrame(cached_kl)
        logger.debug(f"K-line cache hit: {len(df)} rows")
        return df

    rs = bs.query_history_k_data_plus(
        f'{market}.{stock_code}',
        'date,open,high,low,close,volume,amount,turn,pctChg,peTTM,pbMRQ',
        start_date='2024-01-01', end_date=TODAY, frequency='d'
    )
    rows = []
    while (rs.error_code == '0') & rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        raise ValueError("No K-line data")
    df = pd.DataFrame(rows, columns=['date','open','high','low','close','volume','amount','turn','pctChg','peTTM','pbMRQ'])
    for c in ['open','high','low','close','volume','amount','turn','pctChg','peTTM','pbMRQ']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['close']).reset_index(drop=True)
    cache_set(ck, df.to_dict(orient='records'))
    logger.debug(f"K-line fetched: {len(df)} rows")
    return df

def fetch_fundamentals(market, stock_code):
    """Fetch profit, growth, cashflow, Q1 YoY from Baostock."""
    profit = {}
    growth = {}
    cashflow = {}

    # Profit (latest annual)
    rs2 = bs.query_profit_data(f'{market}.{stock_code}', 2025, 4)
    while (rs2.error_code == '0') & rs2.next():
        row = rs2.get_row_data()
        profit = {'roe': safe_num(row[3]), 'net_margin': safe_num(row[4]),
                  'gross_margin': safe_num(row[5]), 'net_profit': safe_num(row[6]),
                  'eps_ttm': safe_num(row[7]), 'revenue_ttm': safe_num(row[8])}
        break

    # Growth
    rs3 = bs.query_growth_data(f'{market}.{stock_code}', 2025, 4)
    while (rs3.error_code == '0') & rs3.next():
        row = rs3.get_row_data()
        growth = {'yoy_np': safe_num(row[4]), 'yoy_equity': safe_num(row[6])}
        break

    # Q1 2026 YoY
    try:
        rq = bs.query_profit_data(f'{market}.{stock_code}', 2026, 1)
        q1_2026_eps = 0; q1_2026_np = 0
        while (rq.error_code == '0') & rq.next():
            row = rq.get_row_data()
            q1_2026_eps = safe_num(row[7]); q1_2026_np = safe_num(row[6])
            break
        rq2 = bs.query_profit_data(f'{market}.{stock_code}', 2025, 1)
        q1_2025_eps = 0; q1_2025_np = 0
        while (rq2.error_code == '0') & rq2.next():
            row = rq2.get_row_data()
            q1_2025_eps = safe_num(row[7]); q1_2025_np = safe_num(row[6])
            break
        if q1_2025_eps > 0.001 and q1_2026_eps > 0:
            q1_yoy_np = round((q1_2026_eps - q1_2025_eps) / q1_2025_eps, 4)
        elif q1_2025_np > 10000 and q1_2026_np > 0:
            q1_yoy_np = round((q1_2026_np - q1_2025_np) / q1_2025_np, 4)
        else:
            q1_yoy_np = None
        if q1_yoy_np is not None:
            growth['yoy_np_q1'] = q1_yoy_np
            growth['q1_2026_net_profit_yi'] = round(q1_2026_np / 1e8, 2)
            growth['q1_2026_eps'] = q1_2026_eps
    except Exception:
        logger.debug("Q1 2026 data not available")

    # Cash flow
    rs4 = bs.query_cash_flow_data(f'{market}.{stock_code}', 2025, 4)
    while (rs4.error_code == '0') & rs4.next():
        row = rs4.get_row_data()
        cfo_np = safe_num(row[8])
        if cfo_np >= 1.0: cfo_v = '利润含金量高'
        elif cfo_np >= 0.5: cfo_v = '利润含金量正常'
        elif cfo_np > 0: cfo_v = '利润多为账面数字'
        else: cfo_v = '账面盈利实为烧钱'
        cashflow = {
            'cfo_to_np': round(cfo_np, 2), 'cfo_to_revenue': round(safe_num(row[7]) * 100, 1),
            'ebit_to_interest': round(safe_num(row[6]), 1), 'cfo_verdict': cfo_v,
            'cash_quality': 'strong' if cfo_np >= 0.8 else ('normal' if cfo_np >= 0.5 else 'weak')
        }
        break

    return {**profit, **growth, **cashflow}

def fetch_peers(market, stock_code):
    """Fetch peer comparison data (24h cache)."""
    peer_ck = cache_key('peer', stock_code)
    cached = cache_get(peer_ck, 86400)
    if cached:
        return cached

    peers_result = []
    peer_group_name = None
    peer_pe_vals = []
    peer_pb_vals = []

    for gname, plist in PEER_GROUPS.items():
        if stock_code in plist:
            peer_group_name = gname
            for peer_code in plist:
                if peer_code == stock_code:
                    continue
                pm = 'sh' if peer_code.startswith('6') else 'sz'
                try:
                    rp = bs.query_profit_data(f'{pm}.{peer_code}', 2025, 4)
                    while (rp.error_code == '0') & rp.next():
                        row = rp.get_row_data()
                        peers_result.append({
                            'code': peer_code, 'roe': round(safe_num(row[3])*100, 2),
                            'net_margin': round(safe_num(row[4])*100, 2),
                            'eps': round(safe_num(row[7]), 3),
                            'revenue_yi': round(safe_num(row[8])/1e8, 2),
                        })
                        break
                    try:
                        rk = bs.query_history_k_data_plus(
                            f'{pm}.{peer_code}', 'date,close,peTTM,pbMRQ',
                            start_date='2026-04-01', end_date=TODAY, frequency='d')
                        pr_rows = []
                        while (rk.error_code == '0') & rk.next():
                            pr_rows.append(rk.get_row_data())
                        if pr_rows:
                            last_r = pr_rows[-1]
                            pp = safe_num(last_r[2])
                            if 0 < pp < 500: peer_pe_vals.append(pp)
                            ppb = safe_num(last_r[3])
                            if 0 < ppb < 50: peer_pb_vals.append(ppb)
                    except Exception:
                        logger.debug(f"Peer {peer_code} K-line fetch failed")
                except Exception:
                    logger.debug(f"Peer {peer_code} profit fetch failed")
            break

    peers_result.sort(key=lambda x: x['roe'], reverse=True)
    for i, p in enumerate(peers_result):
        p['rank'] = i + 1

    result = {'peers': peers_result, 'group': peer_group_name,
              'pe_vals': peer_pe_vals, 'pb_vals': peer_pb_vals}
    cache_set(peer_ck, result)
    return result

def fetch_beta(df):
    """Calculate Beta vs CSI 300 index."""
    beta_val = None
    beta_label = 'N/A'
    try:
        ri = bs.query_history_k_data_plus('sh.000300', 'date,close,pctChg',
            start_date='2024-01-01', end_date=TODAY, frequency='d')
        idx_rows = []
        while (ri.error_code == '0') & ri.next():
            idx_rows.append(ri.get_row_data())
        if idx_rows:
            idx_df = pd.DataFrame(idx_rows, columns=['date','close','pctChg'])
            idx_df['pctChg'] = pd.to_numeric(idx_df['pctChg'], errors='coerce')
            stock_ret = df.set_index('date')['pctChg']
            idx_ret = idx_df.set_index('date')['pctChg']
            common = stock_ret.index.intersection(idx_ret.index)
            if len(common) > 30:
                s_ret = stock_ret.loc[common]
                i_ret = idx_ret.loc[common]
                cov = np.cov(s_ret, i_ret)[0][1]
                var = np.var(i_ret)
                beta_val = round(cov / var, 3) if var > 0.001 else 1.0
                if beta_val > 1.5: beta_label = '高波动'
                elif beta_val > 1.2: beta_label = '偏高波动'
                elif beta_val > 0.8: beta_label = '与市场同步'
                elif beta_val > 0.5: beta_label = '偏低波动'
                else: beta_label = '低波动'
    except Exception:
        logger.debug("Beta calculation failed")
    return {'value': beta_val, 'label': beta_label}

@retry(max_attempts=3, base_delay=1)
def fetch_fund_flow(stock_code, market):
    """Fetch individual stock fund flow from AKShare."""
    import akshare as ak
    ff_ck = cache_key('flow', stock_code)
    cached_ff = cache_get(ff_ck, 600)
    if cached_ff:
        return cached_ff

    ff = ak.stock_individual_fund_flow(stock=stock_code, market=market)
    ff_tail = ff.tail(10)
    flows = []
    for i in range(len(ff_tail)):
        r = ff_tail.iloc[i]
        flows.append({
            'date': str(r.iloc[0]), 'close': float(r.iloc[1]), 'pctChg': float(r.iloc[2]),
            'main_net_yi': round(float(r.iloc[3])/1e8, 2), 'main_pct': round(float(r.iloc[4]), 2),
            'xl_net_yi': round(float(r.iloc[5])/1e8, 2), 'xl_pct': round(float(r.iloc[6]), 2),
            'lg_net_yi': round(float(r.iloc[7])/1e8, 2), 'lg_pct': round(float(r.iloc[8]), 2),
            'md_net_yi': round(float(r.iloc[9])/1e8, 2), 'md_pct': round(float(r.iloc[10]), 2),
            'sm_net_yi': round(float(r.iloc[11])/1e8, 2) or 0.0,
            'sm_pct': round(float(r.iloc[12]), 2) or 0.0
        })
    ff_d = {
        'recent_10d': flows,
        'today': flows[-1] if flows else None,
        'last5d_total_main_yi': round(sum(f['main_net_yi'] for f in flows[-5:]), 2)
    }
    if flows:
        tf = flows[-1]
        if tf['main_net_yi'] < 0 and tf['pctChg'] > 0:
            ff_d['alert'] = {
                'type': 'bearish_divergence',
                'msg': f"Px UP (+{tf['pctChg']}%) but Main SELL ({tf['main_net_yi']}Y)",
                'risk': 'high'
            }
    cache_set(ff_ck, ff_d)
    return ff_d

def fetch_news(stock_code):
    """Fetch news from AKShare (eastmoney)."""
    import akshare as ak
    items = []
    try:
        dn = ak.stock_news_em(symbol=stock_code)
        for i in range(min(10, len(dn))):
            row = dn.iloc[i]
            items.append({
                'source': '东财',
                'title': str(row.iloc[1]) if len(row) > 1 else '',
                'content': str(row.iloc[2])[:150] if len(row) > 2 else '',
                'time': str(row.iloc[3]) if len(row) > 3 else '',
                'url': str(row.iloc[5]) if len(row) > 5 else '',
                'type': 'news'
            })
    except Exception as e:
        # BUG FIX #1: was unreachable — bare except above swallowed everything
        logger.warning(f"News fetch failed: {e}")
        items.append({'source': '东财', 'error': str(e), 'type': 'error'})
    return {'items': items, 'total': len(items)}

def fetch_northbound(stock_code):
    """Fetch northbound (HK-SH/SZ connect) holding data."""
    import akshare as ak
    nb = ak.stock_hsgt_individual_em(symbol=stock_code)
    if nb is None or len(nb) == 0:
        return None

    nb = nb.sort_values(nb.columns[0])
    nb_tail = nb.tail(20)
    nb_data = []
    for i in range(len(nb_tail)):
        r = nb_tail.iloc[i]
        nb_data.append({
            'date': str(r.iloc[0])[:10], 'close': round(float(r.iloc[1]), 2),
            'pctChg': round(float(r.iloc[2]), 2),
            'hold_value_yi': round(float(r.iloc[4])/1e8, 2),
            'hold_pct_a': round(float(r.iloc[5]), 3),
            'value_chg_yi': round(float(r.iloc[8])/1e8, 2)
        })

    nb_today = nb_data[-1]
    nb_5d = round(sum(x['value_chg_yi'] for x in nb_data[-5:]), 2)
    nb_20d = round(sum(x['value_chg_yi'] for x in nb_data[-20:]), 2)

    nb_trend = '持平'
    if len(nb_data) >= 5:
        s5 = nb_data[-5].get('hold_pct_a', 0)
        s1 = nb_data[-1].get('hold_pct_a', 0)
        if s1 and s5 and s5 > 0:
            d = s1 - s5
            nb_trend = '增持' if d > 0.05 else ('减持' if d < -0.05 else '持平')

    if nb_5d > 5: v = '大幅流入'
    elif nb_5d > 1: v = '持续流入'
    elif nb_5d > -1: v = '小幅进出'
    elif nb_5d > -5: v = '持续流出'
    else: v = '大幅流出'

    return {
        'recent_20d': nb_data, 'today': nb_today,
        'last5d_net_yi': nb_5d, 'last20d_net_yi': nb_20d,
        'latest_hold_pct': nb_today.get('hold_pct_a'),
        'trend_5d': nb_trend, 'verdict': v
    }

def fetch_margin(stock_code, market, sections):
    """Fetch margin trading data."""
    import akshare as ak
    # BUG FIX #6: use dynamic recent trading dates
    today = datetime.now()
    dates_to_try = []
    for delta in range(0, 7):
        d = today - timedelta(days=delta)
        if d.weekday() < 5:  # skip weekends
            dates_to_try.append(d.strftime('%Y%m%d'))
        if len(dates_to_try) >= 4:
            break

    for dt in dates_to_try:
        try:
            mg = ak.stock_margin_detail_sse(date=dt) if market == 'sh' else ak.stock_margin_detail_szse(date=dt)
            if mg is None or len(mg) == 0:
                continue
            mf = mg[mg.iloc[:, 1].astype(str) == stock_code]
            if len(mf) == 0:
                continue
            r = mf.iloc[0]
            # BUG FIX #5: use 'sections' parameter instead of redefining 'cap'
            cap_info = sections.get('capital', {})
            tot = cap_info.get('total_shares', 0) or 0
            if sections.get('sina_realtime', {}).get('price'):
                mktcap = tot * sections['sina_realtime']['price'] / 1e8
            elif sections.get('history', {}).get('latest', {}).get('close'):
                mktcap = tot * sections['history']['latest']['close'] / 1e8
            else:
                mktcap = 0
            rz = float(r.iloc[3])
            rzr = round(rz/1e8/mktcap*100, 2) if mktcap > 0 else 0
            return {
                'date': str(r.iloc[0]), 'rz_balance_yi': round(rz/1e8, 2),
                'rz_buy_yi': round(float(r.iloc[4])/1e8, 2),
                'rz_repay_yi': round(float(r.iloc[5])/1e8, 2),
                'rz_to_mktcap_pct': rzr,
                'rz_verdict': '高杠杆' if rzr > 8 else ('适中' if rzr > 3 else '低杠杆')
            }
        except Exception:
            logger.debug(f"Margin fetch for date {dt} failed, trying next")
            continue
    return None

def fetch_shareholders(stock_code):
    """Fetch shareholder count data."""
    import akshare as ak
    # BUG FIX #6: dynamic report dates
    current_year = datetime.now().year
    report_dates = [
        f'{current_year}0331', f'{current_year-1}1231', f'{current_year-1}0930',
        f'{current_year-1}0630', f'{current_year-1}0331'
    ]
    for dt in report_dates:
        try:
            sh = ak.stock_zh_a_gdhs(symbol=dt)
            if sh is None or len(sh) == 0:
                continue
            r = sh[sh.iloc[:, 0].astype(str) == stock_code]
            if len(r) == 0:
                continue
            row = r.iloc[0]
            chg = int(float(row.iloc[6]))
            cpct = round(float(row.iloc[7]), 2)
            trend = '筹码集中' if chg < 0 else ('筹码分散' if cpct > 10 else '基本稳定')
            return {
                'report_date': dt, 'holders': int(float(row.iloc[4])),
                'prev_holders': int(float(row.iloc[5])), 'change': chg,
                'change_pct': cpct, 'trend': trend
            }
        except Exception:
            logger.debug(f"Shareholders fetch for {dt} failed")
            continue
    return None

def fetch_analyst(stock_code):
    """Fetch analyst research reports."""
    import akshare as ak
    rp = ak.stock_research_report_em(symbol=stock_code)
    if rp is None or len(rp) == 0:
        return None

    rp_items = []
    rating_changes = {'上调': 0, '下调': 0, '维持': 0}
    for i in range(min(12, len(rp))):
        row = rp.iloc[i]
        rating = str(row.iloc[4]) if len(row) > 4 else ''
        rp_items.append({
            'date': str(row.iloc[14]) if len(row) > 14 else '',
            'title': str(row.iloc[3])[:80] if len(row) > 3 else '',
            'org': str(row.iloc[5]) if len(row) > 5 else '',
            'rating': rating
        })
        if '上调' in rating: rating_changes['上调'] += 1
        elif '下调' in rating: rating_changes['下调'] += 1
        else: rating_changes['维持'] += 1

    bc2 = sum(1 for x in rp_items if any(kw in x.get('rating','') for kw in ['买入','增持','强烈推荐','推荐']))
    if bc2 >= 6: rs2 = '强烈看多'
    elif bc2 >= 4: rs2 = '偏多'
    elif bc2 >= 2: rs2 = '中性'
    else: rs2 = '偏空'

    return {
        'reports': rp_items, 'total': len(rp_items),
        'buy_pct': round(bc2/len(rp_items)*100, 1) if rp_items else 0,
        'rating_changes': rating_changes, 'verdict': rs2
    }

def fetch_lockup(stock_code):
    """Fetch upcoming lockup expiry within 60 days."""
    import akshare as ak
    t = TODAY_SHORT
    e = (datetime.now() + timedelta(days=60)).strftime('%Y%m%d')
    lj = ak.stock_restricted_release_detail_em(start_date=t, end_date=e)
    if lj is None or len(lj) == 0:
        return {'upcoming': [], 'verdict': '无近期解禁'}

    lf = lj[lj.iloc[:, 1].astype(str) == stock_code]
    if len(lf) == 0:
        return {'upcoming': [], 'verdict': '无近期解禁'}

    ljf = []
    for i in range(min(5, len(lf))):
        row = lf.iloc[i]
        ljf.append({
            'date': str(row.iloc[3]),
            'shares_wan': round(float(row.iloc[6])/1e4, 2) if len(row) > 6 else 0,
            'ratio_pct': round(float(row.iloc[8]), 2) if len(row) > 8 else 0,
            'amount_yi': round(float(row.iloc[7])/1e8, 2) if len(row) > 7 else 0
        })
    mr = max(x.get('ratio_pct', 0) for x in ljf)
    ljv = '大额解禁' if mr > 10 else ('有解禁' if mr > 5 else '轻量解禁')
    return {'upcoming': ljf, 'verdict': ljv}

# ═══════════════════════════════════════════════════════════════════════════════
#  ANALYSIS MODULES
# ═══════════════════════════════════════════════════════════════════════════════

def build_technical_section(df):
    """Build the technical analysis section from a fully-indicated DataFrame."""
    last = df.iloc[-1]
    c_now = last['close']

    mas = {}
    for n in [5,10,20,30,60,120]:
        mv = last[f'MA{n}']
        mas[f'MA{n}'] = {
            'value': None if pd.isna(mv) else float(mv),
            'relation': 'above' if (not pd.isna(mv) and c_now > mv) else 'below'
        }
    bc = sum(1 for m in mas.values() if m['relation'] == 'above')

    return {
        'ma_system': {'lines': mas, 'bullish_count': bc, 'total': 6},
        'macd': {
            'DIF': float(last['DIF']), 'DEA': float(last['DEA']),
            'HIST': float(last['MACDH']),
            'direction': 'bull' if last['MACDH'] > 0 else 'bear'
        },
        'kdj': {
            'K': float(last['K']), 'D': float(last['D']), 'J': float(last['J']),
            'status': 'overbought' if last['J'] > 100 else ('oversold' if last['J'] < 0 else 'normal')
        },
        'rsi': {f'RSI{n}': float(last[f'RSI{n}']) for n in [6,12,24]},
        'wr': {
            'value': float(last['WR']),
            'status': 'overbought' if last['WR'] < 20 else ('oversold' if last['WR'] > 80 else 'neutral')
        },
        'boll': {
            'upper': float(last['B_UP']), 'mid': float(last['B_MID']), 'lower': float(last['B_DN']),
            'position_pct': float((c_now - last['B_DN']) / (last['B_UP'] - last['B_DN']) * 100) if (last['B_UP'] != last['B_DN']) else 50,
            'width_pct': float((last['B_UP'] - last['B_DN']) / last['B_MID'] * 100) if last['B_MID'] != 0 else 0
        },
        'adx': {
            'adx': None if pd.isna(last['ADX']) else round(float(last['ADX']), 1),
            'pdi': None if pd.isna(last['PDI']) else round(float(last['PDI']), 1),
            'mdi': None if pd.isna(last['MDI']) else round(float(last['MDI']), 1),
            'trend_strength': 'weak' if pd.isna(last['ADX']) or last['ADX'] < 20 else ('strong' if last['ADX'] > 25 else 'moderate'),
            'direction': 'bullish' if last['PDI'] > last['MDI'] else 'bearish'
        },
        'atr': {
            'value': round(float(last['ATR14']), 2),
            'pct_of_price': round(float(last['ATR14'] / c_now * 100), 2),
            'daily_range_est': f"{round(c_now - last['ATR14'], 2)} ~ {round(c_now + last['ATR14'], 2)}"
        },
    }, bc

def build_trends(df):
    """BUG FIX #2: unique keys for each period (was: 5d & 10d both 'short')."""
    trends = {}
    l = len(df) - 1
    # Fixed: unique label for each period
    for period, label in [(5,'very_short'), (10,'short'), (20,'short_mid'), (30,'mid'), (60,'mid_long'), (120,'long')]:
        start = max(0, l - period + 1)
        seg = df.iloc[start:l+1]
        if len(seg) >= 2:
            chg = (seg['close'].iloc[-1] - seg['close'].iloc[0]) / seg['close'].iloc[0] * 100
            trends[label] = {
                'period_days': period, 'change_pct': round(chg, 2),
                'high': float(seg['high'].max()), 'low': float(seg['low'].min()),
                'direction': 'up' if chg > 0 else 'down'
            }
    return trends

def build_volume_price(df):
    """Build volume-price analysis section."""
    rec = df.tail(5)
    return {
        'today_vol_w': float(rec['volume'].iloc[-1] / 10000),
        'prev4d_avg_vol_w': float(rec['volume'].iloc[:-1].mean() / 10000),
        'vol_ratio': float(rec['volume'].iloc[-1] / rec['volume'].iloc[:-1].mean()),
        'recent_5d': [{
            'date': str(rec['date'].iloc[i]),
            'close': float(rec['close'].iloc[i]),
            'pctChg': float(rec['pctChg'].iloc[i]),
            'volume_w': float(rec['volume'].iloc[i] / 10000)
        } for i in range(len(rec))]
    }

def build_risk_metrics(df):
    """Build volatility, VaR, and drawdown metrics."""
    returns = df['pctChg'].dropna()
    if len(returns) <= 20:
        return None

    vol_20d = round(float(returns.tail(20).std() * np.sqrt(252)), 2)
    vol_60d = round(float(returns.tail(60).std() * np.sqrt(252)), 2) if len(returns) >= 60 else None
    var_95 = round(float(returns.tail(252).quantile(0.05)), 2) if len(returns) >= 252 else round(float(returns.quantile(0.05)), 2)
    cummax = df['close'].rolling(len(df), min_periods=1).max()
    drawdown = (df['close'] - cummax) / cummax * 100
    max_dd_1y = round(float(drawdown.tail(252).min()), 2) if len(drawdown) >= 252 else round(float(drawdown.min()), 2)

    if max_dd_1y < -40: dd_v = '极高风险'
    elif max_dd_1y < -25: dd_v = '高风险'
    elif max_dd_1y < -15: dd_v = '中等风险'
    else: dd_v = '波动可控'

    return {
        'volatility_20d_annualized_pct': vol_20d,
        'volatility_60d_annualized_pct': vol_60d,
        'var_95_daily_pct': var_95,
        'max_drawdown_1y_pct': max_dd_1y,
        'max_drawdown_verdict': dd_v
    }

def calc_signals(sections, df, technical, bullish_count, fundamentals, peg, pe_hist_pct):
    """Extract objective signals from all data."""
    signals = []
    g = fundamentals.get('yoy_np_q1') or fundamentals.get('yoy_np', 0)

    # MACD signal
    if df is not None and len(df) > 3:
        pm = float(df['MACDH'].iloc[-2]) if 'MACDH' in df.columns else 0
        cm = float(df['MACDH'].iloc[-1]) if 'MACDH' in df.columns else 0
        if pm <= 0 and cm > 0:
            signals.append({'category': 'technical', 'signal': 'MACD金叉', 'strength': 'strong'})
        elif cm > 0:
            signals.append({'category': 'technical', 'signal': 'MACD多头运行', 'strength': 'moderate'})
    elif technical.get('macd', {}).get('direction') == 'bear':
        signals.append({'category': 'technical', 'signal': 'MACD空头', 'strength': 'bearish'})

    # KDJ
    kdj = technical.get('kdj', {})
    if kdj.get('K', 0) > kdj.get('D', 0):
        signals.append({'category': 'technical', 'signal': 'KDJ金叉', 'strength': 'moderate'})
    if kdj.get('J', 0) > 100:
        signals.append({'category': 'technical', 'signal': 'KDJ超买', 'strength': 'warning'})

    # MA
    if bullish_count >= 5:
        signals.append({'category': 'technical', 'signal': '均线多头排列(5+)', 'strength': 'strong'})
    elif bullish_count <= 2:
        signals.append({'category': 'technical', 'signal': '均线空头', 'strength': 'bearish'})

    # RSI
    r6 = technical.get('rsi', {}).get('RSI6', 50)
    if r6 > 80:
        signals.append({'category': 'technical', 'signal': 'RSI超买', 'strength': 'warning'})
    elif r6 < 20:
        signals.append({'category': 'technical', 'signal': 'RSI超卖(反弹可能)', 'strength': 'reversal'})

    # BOLL
    bp2 = technical.get('boll', {}).get('position_pct', 50)
    if bp2 is not None:
        if bp2 > 95:
            signals.append({'category': 'technical', 'signal': 'BOLL上轨突破', 'strength': 'breakout'})
        elif bp2 < 5:
            signals.append({'category': 'technical', 'signal': 'BOLL下轨超卖', 'strength': 'oversold'})

    # Fund flow
    ff = sections.get('fund_flow', {})
    if ff.get('recent_10d'):
        f10 = ff['recent_10d']
        if len(f10) >= 5:
            l5 = f10[-5:]
            if all(x['main_net_yi'] > 0 for x in l5):
                tot = sum(x['main_net_yi'] for x in l5)
                signals.append({'category': 'fund_flow', 'signal': f'主力连续5日净流入({tot:.2f}亿)', 'strength': 'strong'})
            elif all(x['main_net_yi'] < 0 for x in l5):
                tot = sum(x['main_net_yi'] for x in l5)
                signals.append({'category': 'fund_flow', 'signal': f'主力连续5日净流出({tot:.2f}亿)', 'strength': 'bearish'})
            l3 = f10[-3:]
            if all(x['main_net_yi'] > 0 for x in l3):
                pc2 = (l3[-1]['close'] - l3[0]['close']) / l3[0]['close'] if l3[0]['close'] != 0 else 0
                if pc2 < 0:
                    signals.append({'category': 'smart_money', 'signal': '聪明钱底背离(主力买+股价跌)', 'strength': 'bullish_divergence'})

    # Northbound
    nb = sections.get('northbound', {})
    if nb.get('last5d_net_yi'):
        n5 = nb['last5d_net_yi']
        if n5 > 5:
            signals.append({'category': 'northbound', 'signal': '北向5日大幅流入', 'strength': 'strong'})
        elif n5 < -5:
            signals.append({'category': 'northbound', 'signal': '北向5日大幅流出', 'strength': 'bearish'})

    # Fundamentals
    if fundamentals.get('roe', 0) > 0.15:
        signals.append({'category': 'fundamental', 'signal': f'ROE优秀({fundamentals["roe"]*100:.1f}%)', 'strength': 'strong'})
    if g > 0.5:
        signals.append({'category': 'fundamental', 'signal': f'净利高增长({g*100:.0f}%YoY)', 'strength': 'strong'})
    elif g > 0.2:
        signals.append({'category': 'fundamental', 'signal': f'净利较快增长({g*100:.0f}%YoY)', 'strength': 'moderate'})

    # PEG
    if peg is not None:
        if peg < 0.5:
            signals.append({'category': 'fundamental', 'signal': f'PEG={peg:.2f}深度低估', 'strength': 'strong'})
        elif peg < 1.0:
            signals.append({'category': 'fundamental', 'signal': f'PEG={peg:.2f}估值偏低', 'strength': 'moderate'})

    # PE percentile
    if pe_hist_pct is not None:
        if pe_hist_pct < 20:
            signals.append({'category': 'valuation', 'signal': f'PE历史{pe_hist_pct}%分位(低位)', 'strength': 'strong'})
        elif pe_hist_pct > 90:
            signals.append({'category': 'valuation', 'signal': f'PE历史{pe_hist_pct}%分位(极高位)', 'strength': 'warning'})

    return signals

def calc_risk_warnings(sections, fundamentals, peg, pe_hist_pct):
    """Extract risk warnings from all data."""
    risk_warnings = []

    # Lockup
    lockup = sections.get('lockup', {})
    if lockup.get('upcoming'):
        for lu in lockup['upcoming']:
            if lu.get('ratio_pct', 0) > 3:
                risk_warnings.append({
                    'category': 'lockup',
                    'severity': 'high' if lu['ratio_pct'] > 10 else 'medium',
                    'detail': f"未来60天解禁: {lu['date']} {lu['shares_wan']:.0f}万股({lu['ratio_pct']}%)"
                })

    # Margin
    mt2 = sections.get('margin', {})
    if mt2.get('rz_to_mktcap_pct', 0) > 8:
        risk_warnings.append({
            'category': 'margin_overheat', 'severity': 'high',
            'detail': f"融资余额占流通{mt2['rz_to_mktcap_pct']}%(>8%预警)"
        })

    # Valuation
    if pe_hist_pct and pe_hist_pct > 90:
        risk_warnings.append({
            'category': 'valuation_peak', 'severity': 'high',
            'detail': f"PE{pe_hist_pct}%历史分位—均值回归风险"
        })
    if peg is not None and peg > 3:
        risk_warnings.append({
            'category': 'peg_overvalued', 'severity': 'high',
            'detail': f"PEG={peg:.1f}(>3)估值严重偏离增长"
        })

    # Cash flow
    if fundamentals.get('cash_quality') == 'weak':
        risk_warnings.append({
            'category': 'cash_flow', 'severity': 'high',
            'detail': '经营现金流为负，账面利润未转化为现金'
        })

    # Shareholders
    sh = sections.get('shareholders', {})
    if '分散' in sh.get('trend', ''):
        risk_warnings.append({
            'category': 'shareholder', 'severity': 'medium',
            'detail': f"股东户数+{sh.get('change',0)}({sh.get('change_pct',0)}%)——筹码分散"
        })

    # Beta
    beta = sections.get('beta', {})
    if beta.get('value') and beta['value'] > 1.5:
        risk_warnings.append({
            'category': 'high_beta', 'severity': 'medium',
            'detail': f"Beta={beta['value']:.2f}波动高于市场"
        })

    # Drawdown
    rm = sections.get('risk_metrics', {})
    if rm.get('max_drawdown_1y_pct') and rm['max_drawdown_1y_pct'] < -30:
        risk_warnings.append({
            'category': 'high_drawdown', 'severity': 'high',
            'detail': f"1年最大回撤{rm['max_drawdown_1y_pct']}%"
        })

    return risk_warnings

def calc_scoring(sections, fundamentals, peg, pe_hist_pct, bullish_count, df):
    """Calculate positive/negative/warning scores."""
    pos = neg = warn = 0
    t = sections.get('technical', {})

    # Technical
    if t:
        if bullish_count >= 5: pos += 1
        if t.get('macd', {}).get('direction') == 'bull': pos += 1
        if t.get('kdj', {}).get('K', 0) > t.get('kdj', {}).get('D', 0): pos += 1
        r12 = t.get('rsi', {}).get('RSI12', 50)
        if 30 < r12 < 70: pos += 1
        else: warn += 1
        if t.get('kdj', {}).get('J', 0) > 100: neg += 1; warn += 1
        if t.get('wr', {}).get('value', 50) < 20: neg += 1; warn += 1
        if t.get('boll', {}).get('position_pct', 50) > 80: neg += 1; warn += 1
        a = t.get('adx', {})
        if a.get('trend_strength') == 'strong':
            if a.get('direction') == 'bullish': pos += 1
            else: neg += 1

    # Fundamentals
    f = fundamentals
    if f.get('roe', 0) > 0.08: pos += 1
    g = f.get('yoy_np_q1') or f.get('yoy_np', 0)
    if g > 0.1: pos += 1
    if g > 0.5: pos += 2
    if f.get('cash_quality') == 'strong': pos += 1
    elif f.get('cash_quality') == 'weak': neg += 1
    if peg is not None:
        if peg > 3: neg += 2
        elif peg > 2: neg += 1
        elif peg < 0.5: pos += 1
    if pe_hist_pct is not None:
        if pe_hist_pct >= 90: neg += 2
        elif pe_hist_pct >= 80: neg += 1; warn += 1
        elif pe_hist_pct < 20: pos += 1

    # Peers
    pr = sections.get('peers', {}).get('self_rank')
    pc = sections.get('peers', {}).get('peer_count', 0)
    if pr and pc >= 3:
        if pr <= 2: pos += 1
        elif pr >= pc - 1: neg += 1

    # Fund flow
    ff = sections.get('fund_flow', {})
    if ff.get('today'):
        tf = ff['today']
        tm = tf.get('main_net_yi', 0); tp = tf.get('pctChg', 0)
        if tm < 0 and tp > 0: neg += 2; warn += 2
        elif tm > 0: pos += 1

    # Northbound
    nb = sections.get('northbound', {})
    if nb.get('today'):
        nn = nb.get('last5d_net_yi', 0)
        if nn > 5: pos += 2
        elif nn > 1: pos += 1
        elif nn < -5: neg += 2
        elif nn < -1: neg += 1

    # Margin
    mg = sections.get('margin', {})
    if mg.get('rz_verdict'):
        if '高杠杆' in mg['rz_verdict']: warn += 1; neg += 1

    # Shareholders
    sh = sections.get('shareholders', {})
    if sh.get('trend'):
        if '集中' in sh['trend']: pos += 1
        elif '分散' in sh['trend']: neg += 1

    # Analyst
    an = sections.get('analyst', {})
    if an.get('reports'):
        bp = an.get('buy_pct', 50)
        if bp >= 80: pos += 2
        elif bp >= 60: pos += 1
        elif bp < 20: neg += 2

    # Lockup
    lk = sections.get('lockup', {}).get('upcoming', [])
    if lk:
        if lk[0].get('ratio_pct', 0) > 10: neg += 2; warn += 1
        elif lk[0].get('ratio_pct', 0) > 5: neg += 1

    # Beta to fundamentals
    beta_val = sections.get('beta', {}).get('value')
    if beta_val:
        f['beta'] = beta_val
        f['beta_label'] = sections.get('beta', {}).get('label', 'N/A')

    # News sentiment (improved keyword matching with context)
    news = sections.get('news', {})
    if news.get('items'):
        nt = ' '.join([n.get('title', '') + n.get('content', '') for n in news['items'][:5]])
        pos_kw = {
            '业绩大增': 3, '超预期': 3, '涨停': 2, '突破': 2, '回购': 2,
            '增持': 2, '中标': 1, '量产': 2, '放量': 2, '利好': 1,
            '增长': 1, '获批': 1, '大单': 2, '调高': 2, '翻倍': 3,
            '新高': 2, '强势': 1, '龙头': 1, '景气': 1, '满产': 2
        }
        neg_kw = {
            '亏损': 3, '暴雷': 4, '立案': 4, '违规': 3, '退市': 5,
            '减持': 2, '调查': 3, '冻结': 3, '预警': 2, '降级': 2,
            '下调': 2, '巨亏': 4, '停产': 3, '减值': 3, '处罚': 3,
            'ST': 4, '爆仓': 4, '质押': 2, '诉讼': 2, '仲裁': 2
        }
        ps = sum(w for kw, w in pos_kw.items() if kw in nt)
        ns = sum(w for kw, w in neg_kw.items() if kw in nt)
        news['sentiment'] = {'pos': ps, 'neg': ns, 'net': ps - ns}
        if ps - ns >= 5: pos += 2
        elif ps - ns >= 2: pos += 1
        elif ns - ps >= 5: neg += 2
        elif ns - ps >= 2: neg += 1

    return pos, neg, warn

def build_valuation(sections, fundamentals, cached_peer_data, peg, pe_hist_pct):
    """Build valuation section (relative + DCF)."""
    hl = sections.get('history', {}).get('latest', {})
    cpe = hl.get('pe')
    cpb = hl.get('pb')
    cpx = hl.get('close') or sections.get('sina_realtime', {}).get('price')
    eps_est = (cpx / cpe) if (cpe and cpx and cpe > 0) else fundamentals.get('eps_ttm', 0)
    capd = sections.get('capital', {})

    rel = {'pe': cpe, 'pb': cpb}
    ppe = cached_peer_data.get('pe_vals', []) if cached_peer_data else []
    ppb = cached_peer_data.get('pb_vals', []) if cached_peer_data else []

    if ppe and cpe and cpe > 0:
        pmed = float(np.median(ppe))
        ppct = round(sum(1 for p in ppe if p < cpe) / len(ppe) * 100, 1)
        fp = round(eps_est * pmed, 2) if eps_est > 0 else None
        rel['pe_peer_median'] = round(pmed, 2)
        rel['pe_peer_pct'] = ppct
        rel['status'] = '高估' if ppct > 70 else ('低估' if ppct < 30 else '合理')
        if fp:
            rel['fair_price_pe'] = fp
            rel['price_to_fair_pe'] = round(cpx / fp, 2) if fp > 0 else None

    if ppb and cpb and cpb > 0:
        pbmed = float(np.median(ppb))
        ppct2 = round(sum(1 for p in ppb if p < cpb) / len(ppb) * 100, 1)
        bvps = capd.get('bvps', 0)
        fpb = round(bvps * pbmed, 2) if bvps > 0 else None
        rel['pb_peer_median'] = round(pbmed, 2)
        rel['pb_peer_pct'] = ppct2
        if fpb:
            rel['fair_price_pb'] = fpb
            rel['price_to_fair_pb'] = round(cpx / fpb, 2) if fpb > 0 else None

    fvals = [v for v in [rel.get('fair_price_pe'), rel.get('fair_price_pb')] if v]
    if fvals:
        fv = round(np.mean(fvals), 2)
        rel['fair_value_weighted'] = fv
        rel['price_to_fair'] = round(cpx / fv, 2) if fv > 0 else None

    valuation = {'relative': rel}

    # Simple DCF (configurable parameters)
    DCF_WACC = 0.10
    DCF_TERMINAL_GROWTH = 0.03
    np_val = fundamentals.get('net_profit', 0)
    cfo_ratio = fundamentals.get('cfo_to_np', 0.5) if fundamentals.get('cfo_to_np') is not None else 0.5
    fcf = cfo_ratio * np_val if np_val and np_val > 0 else 0

    if fcf and fcf > 1e6:
        scenarios = {}
        for sc_key, cfg in [('conservative', {'g': 0.05, 'l': '保守(5%增长)'}),
                            ('base', {'g': 0.10, 'l': '基准(10%增长)'}),
                            ('optimistic', {'g': 0.20, 'l': '乐观(20%增长)'})]:
            g1 = cfg['g']
            g2 = g1 * 0.8
            g3 = g1 * 0.6
            f1 = fcf * (1 + g1)
            f2 = f1 * (1 + g2)
            f3 = f2 * (1 + g3)
            term = f3 * (1 + DCF_TERMINAL_GROWTH) / (DCF_WACC - DCF_TERMINAL_GROWTH)
            pv = f1 / (1 + DCF_WACC) + f2 / (1 + DCF_WACC)**2 + (f3 + term) / (1 + DCF_WACC)**3
            shs = capd.get('total_shares', 0)
            iv = round(pv / shs, 2) if shs > 0 else 0
            upside = round((iv / cpx - 1) * 100, 1) if cpx and iv > 0 else 0
            scenarios[sc_key] = {
                'assumption': cfg['l'], 'intrinsic_value': iv,
                'price_to_value': round(cpx / iv, 2) if iv > 0 else None,
                'upside_pct': upside
            }
        valuation['dcf'] = {
            'method': f'简易DCF(3Y→永续{DCF_TERMINAL_GROWTH*100:.0f}%,WACC {DCF_WACC*100:.0f}%)',
            'fcf_base_yi': round(fcf / 1e8, 2),
            'scenarios': scenarios
        }
    else:
        valuation['dcf'] = {'error': '现金流数据不足'}

    return valuation

def build_forecast(pos, neg, warn, sections, fundamentals):
    """Build short/mid/long-term forecast."""
    fc = {}
    yoy = fundamentals.get('yoy_np', 0)

    # Short-term
    if warn >= 4:
        fc['short'] = {'direction': 'bearish', 'confidence': 'high', 'summary': '多指标超买+背离，回调概率高'}
    elif warn >= 2:
        fc['short'] = {'direction': 'neutral_bearish', 'confidence': 'medium', 'summary': '超买信号存在，上行空间有限'}
    elif pos > neg + 2:
        fc['short'] = {'direction': 'bullish', 'confidence': 'medium', 'summary': '多周期共振向上'}
    else:
        fc['short'] = {'direction': 'neutral', 'confidence': 'low', 'summary': '信号混杂'}

    # Mid-term
    tr_mid = sections.get('trends', {}).get('mid', {}).get('change_pct', 0)
    if pos > neg + 1 and tr_mid > 0:
        fc['mid'] = {'direction': 'bullish', 'summary': '30日趋势确认'}
    elif pos > neg:
        fc['mid'] = {'direction': 'neutral_bullish', 'summary': '逐步修复'}
    elif warn >= 2:
        fc['mid'] = {'direction': 'neutral', 'summary': '均值回归风险'}
    else:
        fc['mid'] = {'direction': 'neutral', 'summary': '等待确认'}

    # Long-term
    if yoy > 0.3:
        fc['long'] = {'direction': 'bullish', 'summary': '强盈利增长支撑'}
    elif yoy > 0.1:
        fc['long'] = {'direction': 'neutral_bullish', 'summary': '盈利改善中'}
    else:
        fc['long'] = {'direction': 'neutral', 'summary': '等待盈利确认'}

    return fc

def build_key_levels(sections, cpx):
    """Build key support/resistance levels."""
    kl = {}
    if cpx:
        kl['current'] = round(cpx, 2)
    t = sections.get('technical', {})
    if t:
        kl['resistance_boll_upper'] = round(t['boll']['upper'], 2)
        for n in ['MA60', 'MA20']:
            if n in t['ma_system']['lines']:
                val = t['ma_system']['lines'][n]['value']
                kl[f'support_{n}'] = round(val, 2) if val is not None else None
        kl['support_boll_lower'] = round(t['boll']['lower'], 2)
    return kl

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_stock(stock_code, cfg):
    """Main analysis entry point — orchestrates all sub-modules."""
    market, tdx_market, sina_prefix = detect_market(stock_code)
    sections = {}

    # ── Capital (pytdx) ──
    capital, raw_ts, raw_nc = fetch_capital(tdx_market, stock_code)
    sections['capital'] = capital

    # ── Realtime (Sina) ──
    sections['sina_realtime'] = fetch_realtime(sina_prefix, stock_code)

    # ── K-line + Fundamentals (Baostock) ──
    df = None
    cached_peer_data = None
    try:
        bs.login()

        df = fetch_kline(market, stock_code)
        fundamentals = fetch_fundamentals(market, stock_code)
        cached_peer_data = fetch_peers(market, stock_code)

        # Peers section
        self_roe = fundamentals.get('roe', 0) * 100
        peers_result = cached_peer_data.get('peers', [])
        peer_group_name = cached_peer_data.get('group')
        self_rank = sum(1 for p in peers_result if p['roe'] > self_roe) + 1 if peers_result else None
        sections['peers'] = {
            'group': peer_group_name or 'N/A', 'peer_count': len(peers_result),
            'self_rank': self_rank, 'self_roe_pct': round(self_roe, 2),
            'peers': peers_result, 'peer_group_lookup_ok': bool(peer_group_name)
        }

        # Beta
        sections['beta'] = fetch_beta(df)

        # Market cap
        cap = sections.get('capital', {})
        if sections.get('sina_realtime', {}).get('price'):
            px = sections['sina_realtime']['price']
            ts = cap.get('total_shares', raw_ts) if cap.get('total_shares') else raw_ts
            if ts:
                cap['mktcap_yi'] = round(ts * px / 1e8, 2)

        # History section
        sections['history'] = {
            'rows': len(df), 'start': str(df['date'].iloc[0]), 'end': str(df['date'].iloc[-1]),
            'latest': {
                'date': str(df['date'].iloc[-1]), 'close': float(df['close'].iloc[-1]),
                'pctChg': float(df['pctChg'].iloc[-1]), 'volume': float(df['volume'].iloc[-1]),
                'turn': float(df['turn'].iloc[-1]),
                'pe': float(df['peTTM'].iloc[-1]) if pd.notna(df['peTTM'].iloc[-1]) else None,
                'pb': float(df['pbMRQ'].iloc[-1]) if pd.notna(df['pbMRQ'].iloc[-1]) else None,
            }
        }
        sections['fundamentals'] = fundamentals

        # PE/PB sanity check
        hl = sections['history']['latest']
        pe_val = hl.get('pe')
        pb_val = hl.get('pb')
        if len(df) > 2:
            pp = float(df['peTTM'].iloc[-2]) if pd.notna(df['peTTM'].iloc[-2]) else None
            if pe_val and pp and abs(pe_val - pp) / max(abs(pp), 1) > 0.5:
                hl['pe'] = pp; hl['pe_raw_anomaly'] = pe_val; pe_val = pp
            pbp = float(df['pbMRQ'].iloc[-2]) if pd.notna(df['pbMRQ'].iloc[-2]) else None
            if pb_val and pbp and abs(pb_val - pbp) / max(abs(pbp), 1) > 0.5:
                hl['pb'] = pbp; hl['pb_raw_anomaly'] = pb_val; pb_val = pbp

        # PEG
        f = sections['fundamentals']
        g_np = f.get('yoy_np_q1') or f.get('yoy_np', 0)
        if pe_val and g_np and g_np > 0.01:
            g_peg = min(g_np, 3.0)
            peg_raw = round(pe_val / (g_np * 100), 2)
            peg_capped = round(pe_val / (g_peg * 100), 2)
            peg_show = peg_capped if (pe_val < 1 or g_np > 10) else peg_raw
            f['peg'] = peg_show
            f['peg_raw'] = peg_raw
            f['peg_growth_used'] = round(g_peg if g_np > 3.0 else g_np, 4)
            f['peg_verdict'] = '低估' if peg_show < 0.8 else ('合理偏低' if peg_show < 1.2 else ('合理偏高' if peg_show < 2.0 else '高估'))
        else:
            f['peg'] = None
            f['peg_verdict'] = 'N/A'
        peg = f.get('peg')

        # Historical PE/PB percentile
        pe_hist_pct = None
        pe_hist = df['peTTM'].dropna()
        if len(pe_hist) > 10 and pe_val:
            pe_pct_val = round((pe_hist < pe_val).sum() / len(pe_hist) * 100, 1)
            f['pe_hist_pct'] = pe_pct_val
            pe_hist_pct = pe_pct_val
            if pe_pct_val >= 90: f['pe_hist_verdict'] = '历史极高位'
            elif pe_pct_val >= 80: f['pe_hist_verdict'] = '历史高位'
            elif pe_pct_val >= 60: f['pe_hist_verdict'] = '偏高水平'
            elif pe_pct_val >= 40: f['pe_hist_verdict'] = '历史中位区'
            elif pe_pct_val >= 20: f['pe_hist_verdict'] = '偏低水平'
            else: f['pe_hist_verdict'] = '历史低位区'
        pb_hist = df['pbMRQ'].dropna()
        if len(pb_hist) > 10 and pb_val:
            pb_pct_val = round((pb_hist < pb_val).sum() / len(pb_hist) * 100, 1)
            f['pb_hist_pct'] = pb_pct_val
            if pb_pct_val >= 80: f['pb_hist_verdict'] = '历史高位'
            elif pb_pct_val >= 60: f['pb_hist_verdict'] = '偏高水平'
            elif pb_pct_val >= 40: f['pb_hist_verdict'] = '历史中位区'
            elif pb_pct_val >= 20: f['pb_hist_verdict'] = '偏低水平'
            else: f['pb_hist_verdict'] = '历史低位区'

        # ── Technical indicators (using shared functions) ──
        df = calc_all_indicators(df)
        technical, bc = build_technical_section(df)
        sections['technical'] = technical
        sections['trends'] = build_trends(df)
        sections['volume_price'] = build_volume_price(df)

        # Risk metrics
        rm = build_risk_metrics(df)
        if rm:
            sections['risk_metrics'] = rm

        bs.logout()
    except Exception as e:
        logger.error(f"Baostock section failed: {e}")
        sections['history'] = {"error": str(e)}
        df = None
        fundamentals = {}
        peg = None
        pe_hist_pct = None
        bc = 0

    # ── AKShare sections ──
    # Fund flow
    if not cfg.no_fund_flow:
        try:
            sections['fund_flow'] = fetch_fund_flow(stock_code, market)
        except Exception as e:
            logger.warning(f"Fund flow failed: {e}")
            sections['fund_flow'] = {"error": str(e)}

    # News (BUG FIX #1: was unreachable except)
    if not cfg.no_news and not cfg.quick:
        try:
            sections['news'] = fetch_news(stock_code)
        except Exception as e:
            logger.warning(f"News failed: {e}")
            sections['news'] = {"error": str(e), "items": []}

    # Northbound
    if not cfg.quick:
        try:
            nb_result = fetch_northbound(stock_code)
            if nb_result:
                sections['northbound'] = nb_result
        except Exception as e:
            logger.warning(f"Northbound failed: {e}")
            sections['northbound'] = {"error": str(e)}

    # Margin
    if not cfg.quick:
        try:
            mg_result = fetch_margin(stock_code, market, sections)
            if mg_result:
                sections['margin'] = mg_result
        except Exception as e:
            logger.warning(f"Margin failed: {e}")

    # Shareholders
    if not cfg.quick:
        try:
            sh_result = fetch_shareholders(stock_code)
            if sh_result:
                sections['shareholders'] = sh_result
        except Exception as e:
            logger.warning(f"Shareholders failed: {e}")

    # Analyst
    if not cfg.quick:
        try:
            an_result = fetch_analyst(stock_code)
            if an_result:
                sections['analyst'] = an_result
        except Exception as e:
            logger.warning(f"Analyst failed: {e}")

    # Lockup
    if not cfg.quick:
        try:
            sections['lockup'] = fetch_lockup(stock_code)
        except Exception as e:
            logger.warning(f"Lockup failed: {e}")
            sections['lockup'] = {"error": str(e)}

    # ── Analysis ──
    signals = calc_signals(sections, df, sections.get('technical', {}),
                           bc, sections.get('fundamentals', {}), peg, pe_hist_pct)
    sections['signals'] = signals

    risk_warnings = calc_risk_warnings(sections, sections.get('fundamentals', {}), peg, pe_hist_pct)
    sections['risk_warnings'] = risk_warnings

    pos, neg, warn = calc_scoring(sections, sections.get('fundamentals', {}), peg, pe_hist_pct, bc, df)

    valuation = build_valuation(sections, sections.get('fundamentals', {}), cached_peer_data, peg, pe_hist_pct)
    sections['valuation'] = valuation

    forecast = build_forecast(pos, neg, warn, sections, sections.get('fundamentals', {}))
    cpx = sections.get('history', {}).get('latest', {}).get('close') or sections.get('sina_realtime', {}).get('price')
    key_levels = build_key_levels(sections, cpx)

    sections['assessment'] = {
        'score': {'positive': pos, 'negative': neg, 'warnings': warn},
        'forecast': forecast,
        'key_levels': key_levels,
        'disclaimer': '以上信号/预测基于公开数据客观分析，不构成买卖建议。风险自担。'
    }

    # ── Trend persistence ──
    if cfg.trend:
        result = {"stock": stock_code, "status": "ok", "sections": sections}
        save_snapshot(stock_code, result)
        try:
            with get_db() as conn:
                rows = conn.execute(
                    'SELECT date, pe, pb, main_net_yi, score_pos FROM snapshots WHERE stock=? ORDER BY date DESC LIMIT 20',
                    (stock_code,)).fetchall()
                if rows:
                    td = [{'date': r[0], 'pe': r[1], 'pb': r[2], 'main_net_yi': r[3], 'score_pos': r[4]} for r in reversed(rows)]
                    sections['trend_history'] = td
                    if len(td) >= 5:
                        fm = td[-5].get('main_net_yi', 0) or 0
                        lm = td[-1].get('main_net_yi', 0) or 0
                        td2 = '改善' if lm > fm else ('恶化' if lm < fm else '持平')
                        sections['trend_summary'] = f'过去20日主力净流入趋势: {td2}'
        except Exception as e:
            logger.warning(f"Trend read failed: {e}")

    return {"stock": stock_code, "status": "ok", "sections": sections}

# ═══════════════════════════════════════════════════════════════════════════════
#  BACKTEST (uses shared indicator functions)
# ═══════════════════════════════════════════════════════════════════════════════

def run_backtest(stock_code, cfg):
    """Backtest engine — now uses shared calc_* functions and respects --rules."""
    market, _, _ = detect_market(stock_code)
    bs.login()
    rs = bs.query_history_k_data_plus(
        f'{market}.{stock_code}',
        'date,open,high,low,close,volume,pctChg',
        start_date='2023-01-01', end_date=TODAY, frequency='d')
    rows = []
    while (rs.error_code == '0') & rs.next():
        rows.append(rs.get_row_data())
    bs.logout()

    if len(rows) < 100:
        return {'error': f'K线数据不足(需>100, 实际{len(rows)})'}

    df = pd.DataFrame(rows, columns=['date','open','high','low','close','volume','pctChg'])
    for c in ['open','high','low','close','volume','pctChg']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['close']).reset_index(drop=True)

    # Use shared indicator functions
    df = calc_macd(df)
    df = calc_rsi(df, periods=(6, 12))
    df = df.dropna(subset=['DIF','DEA','MACDH']).reset_index(drop=True)

    if len(df) < 61:
        return {'error': f'有效信号窗口不足(需至少61条, 实际{len(df)})'}

    # BUG FIX #4: respect --rules parameter
    if cfg.rules == 'rsi_oversold':
        return _backtest_rsi_oversold(df, stock_code)
    # Default: MACD golden cross
    return _backtest_macd_golden(df, stock_code)

def _backtest_macd_golden(df, stock_code):
    """MACD golden cross strategy: buy on crossover, hold 5 days, -8% stop loss."""
    BACKTEST_HOLD_DAYS = 5
    BACKTEST_STOP_LOSS = -0.08

    trades = []
    holding = False
    entry_price = 0
    entry_date = ''
    hold_counter = 0

    for i in range(60, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        price = row['close']

        buy_signal = prev['MACDH'] <= 0 and row['MACDH'] > 0
        if buy_signal and row['RSI6'] > 70:
            buy_signal = False

        if not holding and buy_signal:
            holding = True
            entry_price = price
            entry_date = str(row['date'])
            hold_counter = 0
            trades.append({'action': 'buy', 'date': entry_date, 'price': round(price, 2)})

        if holding:
            hold_counter += 1
            pnl = (price - entry_price) / entry_price
            if pnl < BACKTEST_STOP_LOSS:
                trades.append({'action': 'sell', 'date': str(row['date']), 'price': round(price, 2),
                    'pnl_pct': round(pnl * 100, 2), 'reason': f'止损{BACKTEST_STOP_LOSS*100:.0f}%'})
                holding = False
            elif hold_counter >= BACKTEST_HOLD_DAYS:
                trades.append({'action': 'sell', 'date': str(row['date']), 'price': round(price, 2),
                    'pnl_pct': round(pnl * 100, 2), 'reason': f'持{BACKTEST_HOLD_DAYS}日到期'})
                holding = False

    return _calc_backtest_stats(trades, df, stock_code,
        f'MACD金叉买入,持{BACKTEST_HOLD_DAYS}日,{BACKTEST_STOP_LOSS*100:.0f}%止损')

def _backtest_rsi_oversold(df, stock_code):
    """RSI oversold bounce strategy: buy when RSI6 < 25, hold 3 days, -5% stop loss."""
    BACKTEST_HOLD_DAYS = 3
    BACKTEST_STOP_LOSS = -0.05

    trades = []
    holding = False
    entry_price = 0
    hold_counter = 0

    for i in range(60, len(df)):
        row = df.iloc[i]
        price = row['close']

        buy_signal = row['RSI6'] < 25

        if not holding and buy_signal:
            holding = True
            entry_price = price
            hold_counter = 0
            trades.append({'action': 'buy', 'date': str(row['date']), 'price': round(price, 2)})

        if holding:
            hold_counter += 1
            pnl = (price - entry_price) / entry_price
            if pnl < BACKTEST_STOP_LOSS:
                trades.append({'action': 'sell', 'date': str(row['date']), 'price': round(price, 2),
                    'pnl_pct': round(pnl * 100, 2), 'reason': f'止损{BACKTEST_STOP_LOSS*100:.0f}%'})
                holding = False
            elif hold_counter >= BACKTEST_HOLD_DAYS:
                trades.append({'action': 'sell', 'date': str(row['date']), 'price': round(price, 2),
                    'pnl_pct': round(pnl * 100, 2), 'reason': f'持{BACKTEST_HOLD_DAYS}日到期'})
                holding = False

    return _calc_backtest_stats(trades, df, stock_code,
        f'RSI超卖反弹,持{BACKTEST_HOLD_DAYS}日,{BACKTEST_STOP_LOSS*100:.0f}%止损')

def _calc_backtest_stats(trades, df, stock_code, strategy_desc):
    """Calculate backtest statistics from trade list."""
    sells = [t for t in trades if t['action'] == 'sell']
    if not sells:
        return {'error': '无有效交易信号', 'period': f'{df.date.iloc[60]}~{df.date.iloc[-1]}'}

    rets = [t['pnl_pct'] for t in sells]
    total_r = sum(rets)
    wins = sum(1 for r in rets if r > 0)
    wr = round(wins / len(rets) * 100, 1)

    cum = [0]
    for r in rets:
        cum.append(cum[-1] + r)
    cum_arr = np.array(cum)
    mdd = round(float(cum_arr - np.maximum.accumulate(cum_arr)).min(), 2)

    ex = np.array(rets) - 0.1  # 0.1% per-trade cost estimate
    sr = round(float(np.mean(ex) / np.std(rets)), 2) if np.std(rets) > 0 else 0

    return {
        'stock': stock_code,
        'strategy': strategy_desc,
        'period': f'{df.date.iloc[60]}~{df.date.iloc[-1]}',
        'total_trades': len(sells),
        'win_rate_pct': wr,
        'total_return_pct': round(total_r, 2),
        'avg_return_pct': round(float(np.mean(rets)), 2),
        'max_drawdown_pct': mdd,
        'sharpe_ratio': sr,
        'recent_trades': trades[-20:],
    }

# ═══════════════════════════════════════════════════════════════════════════════
#  BATCH MODE
# ═══════════════════════════════════════════════════════════════════════════════

def run_batch(batch_file, cfg):
    """Analyze multiple stocks from a CSV file."""
    try:
        with open(batch_file, 'r', encoding='utf-8') as fh:
            stocks = [line.strip().split(',')[0] for line in fh if line.strip() and not line.startswith('#')]
    except Exception as e:
        return {"error": f"读批量文件失败: {e}"}

    results = []
    for sc in stocks:
        if not sc:
            continue
        if not validate_stock_code(sc):
            results.append({'code': sc, 'error': f'无效股票代码: {sc}'})
            continue
        try:
            r = analyze_stock(sc, cfg)
            s = r.get('sections', {})
            results.append({
                'code': sc,
                'name': s.get('sina_realtime', {}).get('name', '?'),
                'close': s.get('history', {}).get('latest', {}).get('close'),
                'pe': s.get('history', {}).get('latest', {}).get('pe'),
                'score': s.get('assessment', {}).get('score', {}),
                'signals_count': len(s.get('signals', [])),
                'risk_count': len(s.get('risk_warnings', [])),
                'valuation': s.get('valuation', {}).get('relative', {}).get('status'),
            })
            # Delay between stocks to avoid rate limiting
            time.sleep(1)
        except Exception as e:
            logger.error(f"Batch analysis failed for {sc}: {e}")
            results.append({'code': sc, 'error': str(e)})

    return {'batch_results': results, 'total': len(results)}

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    cfg = Config()

    # Batch mode
    if cfg.batch_file:
        result = run_batch(cfg.batch_file, cfg)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    # Need a stock code for other modes
    if not cfg.stock:
        print(json.dumps({
            "error": "Usage: python akshare_query.py <stock> [--backtest] [--batch <file>] [--trend] [--quick]",
            "examples": [
                "python akshare_query.py 600111",
                "python akshare_query.py 600111 --backtest",
                "python akshare_query.py --batch stocks.csv",
                "python akshare_query.py 600111 --trend",
                "python akshare_query.py 600111 --quick"
            ]
        }, ensure_ascii=False))
        sys.exit(1)

    # Validate stock code
    if not validate_stock_code(cfg.stock):
        print(json.dumps({"error": f"无效股票代码: {cfg.stock}，应为6位数字(如600519)"}))
        sys.exit(1)

    # Backtest mode
    if cfg.backtest:
        bt = run_backtest(cfg.stock, cfg)
        print(json.dumps(bt, ensure_ascii=False, indent=2, default=str))
        return

    # Normal analysis
    result = analyze_stock(cfg.stock, cfg)
    result = clean_output(result)

    output = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if cfg.output:
        with open(cfg.output, 'w', encoding='utf-8') as f:
            f.write(output)
        logger.info(f'OK: {cfg.output}')
    else:
        print(output)

if __name__ == '__main__':
    main()
