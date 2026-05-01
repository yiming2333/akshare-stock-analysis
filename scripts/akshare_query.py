#!/usr/bin/env python3
"""
StockDeepScan v7 — 个股四维深度诊断引擎
=============================================
Data sources:
  pytdx       → 实时行情 + 财务数据 (通达信)
  Baostock    → 历史K线(含换手率/PE/PB) + 财报(利润/增长/杜邦)
  AKShare     → 资金流向(东财dataCenter) + 个股新闻 + 公告
  新浪财经    → 实时行情备选 (hq.sinajs.cn)

Usage:
  python akshare_query.py <stock_code> [--no-fund-flow] [--no-news]
  python akshare_query.py 600111
"""

import sys, json, os, time
sys.stdout.reconfigure(encoding='utf-8')

try:
    import baostock as bs
    import pandas as pd
    import numpy as np
    from pytdx.hq import TdxHq_API
    import requests
except ImportError as e:
    print(json.dumps({"error": f"Missing dependency: {e}. Run: pip install baostock pytdx akshare pandas numpy requests"}))
    sys.exit(1)

# ======== CONFIG ========
STOCK = sys.argv[1] if len(sys.argv) > 1 else '600111'
NO_FUND_FLOW = '--no-fund-flow' in sys.argv
NO_NEWS = '--no-news' in sys.argv
OUTPUT_FILE = None
for i, a in enumerate(sys.argv):
    if a == '--output' and i + 1 < len(sys.argv):
        OUTPUT_FILE = sys.argv[i + 1]
        break
CACHE_DIR = os.path.join(os.path.dirname(__file__), '..', '.cache')
os.makedirs(CACHE_DIR, exist_ok=True)

# Market detection
_MARKET = 'sz' if STOCK.startswith(('0','3')) else 'sh'
_TDX_MARKET = 0 if _MARKET == 'sz' else 1  # pytdx: 0=SZ, 1=SH
_SINA_PREFIX = 'sz' if _MARKET == 'sz' else 'sh'

# Peer groups for industry comparison (matched by stock code)
PEER_GROUPS = {
    '稀土永磁':   ['600111','000831','600392','600259','000970','600549','300748','300224'],
    '白酒':       ['600519','000858','000568','002304','600809','603369','600702'],
    '光伏':       ['601012','688599','600438','002459','688390','300274','002129'],
    '锂电池':     ['300750','002594','002460','600516','300014','603799','002709'],
    '半导体':     ['688981','002371','603986','688012','300782','600703','002049'],
    '券商':       ['600030','300059','000776','601688','601211','600999','002797'],
    '银行':       ['600036','601398','601288','600900','601328','000001','002142'],
    '保险':       ['601318','601628','601336','601601'],
    '光模块/AI算力': ['300308','300502','300394','688498','000988','300570','688205'],
    'CXO/医药':   ['603259','300759','300347','002821','688202','300122','000661'],
    '存储芯片/NAND': ['001309','600171','603986','300688','002049','300474','688234','300042'],
}

TDX_IP = '180.153.18.170'
TDX_PORT = 7709

# ======== HELPERS ========
def safe_num(v, default=0):
    try: return float(v)
    except: return default

def pc(v, fmt=".2f"):
    if v is None: return "N/A"
    try: return f"{v:{fmt}}"
    except: return "N/A"

# ======== DATA FETCHING ========
result = {"stock": STOCK, "status": "ok", "sections": {}}

# --- 0. pytdx: real-time + financials ---
try:
    api = TdxHq_API()
    if not api.connect(TDX_IP, TDX_PORT):
        raise ConnectionError(f"pytdx connect to {TDX_IP}:{TDX_PORT} failed")
    fin = api.get_finance_info(_TDX_MARKET, STOCK)
    api.disconnect()
    raw_ts = fin.get('zongguben', 0)
    raw_nc = fin.get('jingzichan', 0)
    result['sections']['capital'] = {
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
except Exception as e:
    result['sections']['capital'] = {"error": str(e)}

# --- 0.5 新浪财经: real-time backup ---
try:
    headers = {'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'}
    r = requests.get(f'http://hq.sinajs.cn/list={_SINA_PREFIX}{STOCK}', headers=headers, timeout=10)
    if r.status_code == 200:
        data = r.text.split('"')[1].split(',')
        if len(data) >= 32:
            sina = {
                'name': data[0],
                'open': safe_num(data[1]),
                'prev_close': safe_num(data[2]),
                'price': safe_num(data[3]),
                'high': safe_num(data[4]),
                'low': safe_num(data[5]),
                'volume_shares': int(safe_num(data[8])),
                'amount_yuan': safe_num(data[9]),
                'date': data[30],
                'time': data[31],
            }
            # 五档买卖
            for i, name in [(10,'bid1_vol'),(11,'bid1'),(12,'bid2_vol'),(13,'bid2'),
                            (14,'bid3_vol'),(15,'bid3'),(16,'bid4_vol'),(17,'bid4'),
                            (18,'bid5_vol'),(19,'bid5')]:
                sina[name] = safe_num(data[i])
            for i, name in [(20,'ask1_vol'),(21,'ask1'),(22,'ask2_vol'),(23,'ask2'),
                            (24,'ask3_vol'),(25,'ask3'),(26,'ask4_vol'),(27,'ask4'),
                            (28,'ask5_vol'),(29,'ask5')]:
                sina[name] = safe_num(data[i])
            # PE/PB (hq.sinajs.cn extended format)
            if len(data) >= 34:
                sina['pe'] = safe_num(data[32])
                sina['pb'] = safe_num(data[33])
            result['sections']['sina_realtime'] = sina
except Exception as e:
    result['sections']['sina_realtime'] = {"error": str(e)}

# --- 1. Baostock: history + fundamentals ---
try:
    bs.login()

    # K-line
    rs = bs.query_history_k_data_plus(
        f'{_MARKET}.{STOCK}',
        'date,open,high,low,close,volume,amount,turn,pctChg,peTTM,pbMRQ',
        start_date='2025-08-01', end_date='2026-05-01',
        frequency='d'
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

    # Profit
    rs2 = bs.query_profit_data(f'{_MARKET}.{STOCK}', 2025, 4)
    profit = {}
    while (rs2.error_code == '0') & rs2.next():
        row = rs2.get_row_data()
        profit = {'roe': safe_num(row[3]), 'net_margin': safe_num(row[4]),
                   'gross_margin': safe_num(row[5]), 'net_profit': safe_num(row[6]),
                   'eps_ttm': safe_num(row[7]), 'revenue_ttm': safe_num(row[8])}
        break

    # Growth (annual 2025 Q4, quarterly format)
    # Quarterly format indices: [3]=YOYProfit, [4]=YOYNI, [5]=YOYEPS, [6]=YOYEquity
    rs3 = bs.query_growth_data(f'{_MARKET}.{STOCK}', 2025, 4)
    growth = {}
    while (rs3.error_code == '0') & rs3.next():
        row = rs3.get_row_data()
        growth = {
            'yoy_np': safe_num(row[4]),      # YOYNI: 归属净利润同比
            'yoy_equity': safe_num(row[6]),  # YOYEquity: 净资产同比
        }
        break

    # Q1 2026 real-time YoY: compute from actual profit amounts
    # We use EPS (归属净利润口径) row[7] instead of net_profit (总净利润) row[6]
    # for a more accurate PEG calculation. Baostock profit_data:
    #   [3]=ROE [4]=净利率 [5]=毛利率 [6]=总净利润 [7]=EPS(归属净利润) [8]=营业收入
    # EPS * total_shares = 归属净利润 (more accurate for PEG)
    q1_yoy_np = None
    try:
        rq = bs.query_profit_data(f'{_MARKET}.{STOCK}', 2026, 1)
        q1_2026_eps = 0; q1_2026_np = 0
        while (rq.error_code == '0') & rq.next():
            row = rq.get_row_data()
            q1_2026_eps = safe_num(row[7])
            q1_2026_np = safe_num(row[6])
            break
        rq2 = bs.query_profit_data(f'{_MARKET}.{STOCK}', 2025, 1)
        q1_2025_eps = 0; q1_2025_np = 0
        while (rq2.error_code == '0') & rq2.next():
            row = rq2.get_row_data()
            q1_2025_eps = safe_num(row[7])
            q1_2025_np = safe_num(row[6])
            break
        # Prefer EPS-based (归属净利润口径), fallback to total net profit
        if q1_2025_eps > 0.001 and q1_2026_eps > 0:
            q1_yoy_np = round((q1_2026_eps - q1_2025_eps) / q1_2025_eps, 4)
        elif q1_2025_np > 10000 and q1_2026_np > 0:
            q1_yoy_np = round((q1_2026_np - q1_2025_np) / q1_2025_np, 4)
        if q1_yoy_np:
            growth['yoy_np_q1'] = q1_yoy_np
            growth['q1_2026_net_profit_yi'] = round(q1_2026_np / 1e8, 2)
            growth['q1_2026_eps'] = q1_2026_eps
    except Exception as e:
        pass  # keep yoy_np from annual as fallback

    # Cash flow (Baostock returns ratios, not amounts — more diagnostic)
    rs4 = bs.query_cash_flow_data(f'{_MARKET}.{STOCK}', 2025, 4)
    cashflow = {}
    while (rs4.error_code == '0') & rs4.next():
        row = rs4.get_row_data()
        cfo_np = safe_num(row[8])   # 经营现金流/净利润 (利润含金量)
        cfo_or = safe_num(row[7])   # 经营现金流/营业收入
        ebit_int = safe_num(row[6]) # EBIT/利息 (偿债能力)
        if cfo_np >= 1.0:
            cfo_verdict = '利润含金量高💪'
        elif cfo_np >= 0.5:
            cfo_verdict = '利润含金量正常'
        elif cfo_np > 0:
            cfo_verdict = '利润多为账面数字⚠️'
        else:
            cfo_verdict = '账面盈利实为烧钱🔴'
        cashflow = {
            'cfo_to_np': round(cfo_np, 2),
            'cfo_to_revenue': round(cfo_or * 100, 1),
            'ebit_to_interest': round(ebit_int, 1),
            'cfo_verdict': cfo_verdict,
            'cash_quality': 'strong' if cfo_np >= 0.8 else ('normal' if cfo_np >= 0.5 else 'weak')
        }
        break

    # Peer comparison (batch query selected industry peers)
    peers_result = []; peer_group_name = None
    for gname, plist in PEER_GROUPS.items():
        if STOCK in plist:
            peer_group_name = gname
            for peer_code in plist:
                if peer_code == STOCK: continue
                pm = 'sh' if peer_code.startswith('6') else 'sz'
                try:
                    rp = bs.query_profit_data(f'{pm}.{peer_code}', 2025, 4)
                    while (rp.error_code == '0') & rp.next():
                        row = rp.get_row_data()
                        peers_result.append({
                            'code': peer_code,
                            'roe': round(safe_num(row[3])*100, 2),
                            'net_margin': round(safe_num(row[4])*100, 2),
                            'eps': round(safe_num(row[7]), 3),
                            'revenue_yi': round(safe_num(row[8])/1e8, 2),
                        })
                        break
                except Exception:
                    pass
            break
    # Sort by ROE descending for ranking
    peers_result.sort(key=lambda x: x['roe'], reverse=True)
    for i, p in enumerate(peers_result):
        p['rank'] = i + 1
    # Find self rank in peers
    self_roe = profit.get('roe', 0) * 100
    self_rank = sum(1 for p in peers_result if p['roe'] > self_roe) + 1 if peers_result else None
    result['sections']['peers'] = {
        'group': peer_group_name or '未匹配行业分组',
        'peer_count': len(peers_result),
        'self_rank': self_rank,
        'self_roe_pct': round(self_roe, 2),
        'peers': peers_result,
        'peer_group_lookup_ok': peer_group_name is not None
    }

    # Beta: stock vs CSI300 (000300.SH) 180-day rolling
    beta_val = None; beta_label = 'N/A'
    try:
        ri = bs.query_history_k_data_plus(
            'sh.000300',
            'date,close,pctChg',
            start_date='2025-08-01', end_date='2026-05-01',
            frequency='d'
        )
        idx_rows = []
        while (ri.error_code == '0') & ri.next():
            idx_rows.append(ri.get_row_data())
        if idx_rows:
            idx_df = pd.DataFrame(idx_rows, columns=['date','close','pctChg'])
            idx_df['pctChg'] = pd.to_numeric(idx_df['pctChg'], errors='coerce')
            # Align dates
            stock_ret = df.set_index('date')['pctChg']
            idx_ret = idx_df.set_index('date')['pctChg']
            common = stock_ret.index.intersection(idx_ret.index)
            if len(common) > 30:
                s_ret = stock_ret.loc[common]; i_ret = idx_ret.loc[common]
                cov = np.cov(s_ret, i_ret)[0][1]
                var = np.var(i_ret)
                beta_val = round(cov / var, 3) if var > 0.001 else 1.0
                if beta_val > 1.5: beta_label = '高波动💥'
                elif beta_val > 1.2: beta_label = '偏高波动⚡'
                elif beta_val > 0.8: beta_label = '与市场同步'
                elif beta_val > 0.5: beta_label = '偏低波动🛡️'
                else: beta_label = '低波动💤'
    except Exception:
        pass
    result['sections']['beta'] = {'value': beta_val, 'label': beta_label}

    bs.logout()

    # Derive mktcap from sina realtime (now available — placed after sina block)
    if 'sina_realtime' in result['sections'] and result['sections']['sina_realtime'].get('price'):
        px = result['sections']['sina_realtime']['price']
        result['sections']['capital']['mktcap_yi'] = round(raw_ts * px / 1e8, 2)

    result['sections']['history'] = {
        'rows': len(rows),
        'start': str(df['date'].iloc[0]),
        'end': str(df['date'].iloc[-1]),
        'latest': {
            'date': str(df['date'].iloc[-1]),
            'close': float(df['close'].iloc[-1]),
            'pctChg': float(df['pctChg'].iloc[-1]),
            'volume': float(df['volume'].iloc[-1]),
            'turn': float(df['turn'].iloc[-1]),
            'pe': float(df['peTTM'].iloc[-1]) if pd.notna(df['peTTM'].iloc[-1]) else None,
            'pb': float(df['pbMRQ'].iloc[-1]) if pd.notna(df['pbMRQ'].iloc[-1]) else None,
        }
    }
    result['sections']['fundamentals'] = {**profit, **growth, **cashflow}

    # Sanity check peTTM/pbMRQ: if latest differs >50% from prev day, use prev
    hl = result['sections']['history']['latest']
    pe_raw = hl.get('pe')
    pb_raw = hl.get('pb')
    if len(df) > 2:
        pe_prev = float(df['peTTM'].iloc[-2]) if pd.notna(df['peTTM'].iloc[-2]) else None
        pb_prev = float(df['pbMRQ'].iloc[-2]) if pd.notna(df['pbMRQ'].iloc[-2]) else None
        if pe_raw and pe_prev and abs(pe_raw - pe_prev) / max(abs(pe_prev), 1) > 0.5:
            hl['pe'] = pe_prev; hl['pe_raw_anomaly'] = pe_raw
        if pb_raw and pb_prev and abs(pb_raw - pb_prev) / max(abs(pb_prev), 1) > 0.5:
            hl['pb'] = pb_prev; hl['pb_raw_anomaly'] = pb_raw

    # PEG: PE / (growth * 100), <1=undervalued, >2=expensive
    # Use latest quarter YoY for PEG (reflects current earnings momentum)
    f = result['sections']['fundamentals']
    pe_val = hl.get('pe') or 0
    g_np = f.get('yoy_np_q1') or f.get('yoy_np', 0)  # Q1优先，年报兜底
    if pe_val and g_np and g_np > 0.01:
        # Cap PEG growth rate: if YoY > 500%, likely base effect distortion.
        # Still report it accurately, but compute a conservative PEG too.
        g_peg = min(g_np, 3.0)  # cap at 300% for PEG to avoid base-effect nonsense
        peg_raw = round(pe_val / (g_np * 100), 2)
        peg_capped = round(pe_val / (g_peg * 100), 2)
        if pe_val < 1 or g_np > 10:  # PE<1 or growth>1000% — data anomaly
            peg_show = peg_capped
        else:
            peg_show = peg_raw
        f['peg'] = peg_show
        f['peg_raw'] = peg_raw
        f['peg_growth_used'] = round(g_peg if g_np > 3.0 else g_np, 4)
        f['peg_verdict'] = '低估' if peg_show < 0.8 else ('合理偏低' if peg_show < 1.2 else ('合理偏高' if peg_show < 2.0 else '高估'))
    else:
        f['peg'] = None
        f['peg_verdict'] = 'N/A'

    # Historical PE/PB percentile (from existing 180-day K-line, zero extra API)
    pe_hist = df['peTTM'].dropna()
    if len(pe_hist) > 10 and pe_val:
        pe_pct_val = round((pe_hist < pe_val).sum() / len(pe_hist) * 100, 1)
        f['pe_hist_pct'] = pe_pct_val
        if pe_pct_val >= 90: f['pe_hist_verdict'] = '历史极高位🔴'
        elif pe_pct_val >= 80: f['pe_hist_verdict'] = '历史高位⚠️'
        elif pe_pct_val >= 60: f['pe_hist_verdict'] = '偏高水平'
        elif pe_pct_val >= 40: f['pe_hist_verdict'] = '历史中位区'
        elif pe_pct_val >= 20: f['pe_hist_verdict'] = '偏低水平'
        else: f['pe_hist_verdict'] = '历史低位区✅'
    else:
        f['pe_hist_pct'] = None; f['pe_hist_verdict'] = 'N/A'

    pb_hist = df['pbMRQ'].dropna()
    pb_val = result['sections']['history']['latest'].get('pb')
    if len(pb_hist) > 10 and pb_val:
        pb_pct_val = round((pb_hist < pb_val).sum() / len(pb_hist) * 100, 1)
        f['pb_hist_pct'] = pb_pct_val
        if pb_pct_val >= 80: f['pb_hist_verdict'] = '历史高位⚠️'
        elif pb_pct_val >= 60: f['pb_hist_verdict'] = '偏高水平'
        elif pb_pct_val >= 40: f['pb_hist_verdict'] = '历史中位区'
        elif pb_pct_val >= 20: f['pb_hist_verdict'] = '偏低水平'
        else: f['pb_hist_verdict'] = '历史低位区✅'
    else:
        f['pb_hist_pct'] = None; f['pb_hist_verdict'] = 'N/A'

    # --- ALL TECHNICAL INDICATORS ---
    close = df['close']; high = df['high']; low = df['low']; vol = df['volume']
    l = len(df) - 1

    # MA
    for n in [5,10,20,30,60,120]:
        df[f'MA{n}'] = close.rolling(n).mean()

    # MACD (6,13,5)
    e1=close.ewm(span=6,adjust=False).mean(); e2=close.ewm(span=13,adjust=False).mean()
    df['DIF']=e1-e2; df['DEA']=df['DIF'].ewm(span=5,adjust=False).mean()
    df['MACDH']=2*(df['DIF']-df['DEA'])

    # KDJ (6,3,3)
    L=low.rolling(6,min_periods=6).min(); H=high.rolling(6,min_periods=6).max()
    rsv=(close-L)/(H-L)*100
    df['K']=rsv.ewm(com=2,adjust=False).mean(); df['D']=df['K'].ewm(com=2,adjust=False).mean()
    df['J']=3*df['K']-2*df['D']

    # RSI (6,12,24)
    for n in [6,12,24]:
        d=close.diff(); g_=d.where(d>0,0).rolling(n).mean(); l__=(-d.where(d<0,0)).rolling(n).mean()
        rs_=g_/l__; df[f'RSI{n}']=100-100/(1+rs_)

    # WR (10)
    h10=high.rolling(10,min_periods=10).max(); l10=low.rolling(10,min_periods=10).min()
    df['WR']=(h10-close)/(h10-l10)*100

    # BOLL (20,2)
    df['B_MID']=close.rolling(20).mean(); s2=close.rolling(20).std()
    df['B_UP']=df['B_MID']+2*s2; df['B_DN']=df['B_MID']-2*s2

    # Volume
    df['VMA5']=vol.rolling(5).mean(); df['VRATIO']=vol/df['VMA5']

    # ATR(14) & ADX(14) — trend strength meter
    tr_ = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    df['ATR14'] = tr_.rolling(14).mean()
    pdm_ = high.diff(); ndm_ = -low.diff()
    pdm_[pdm_ < 0] = 0; ndm_[ndm_ < 0] = 0
    pdm_[pdm_ < ndm_] = 0; ndm_[ndm_ < pdm_] = 0
    atr14_ = df['ATR14'].replace(0, np.nan)
    df['PDI'] = 100 * pdm_.rolling(14).mean() / atr14_
    df['MDI'] = 100 * ndm_.rolling(14).mean() / atr14_
    dx_ = 100 * (df['PDI'] - df['MDI']).abs() / (df['PDI'] + df['MDI'])
    df['ADX'] = dx_.rolling(14).mean()

    last = df.iloc[-1]
    c_now = last['close']

    # MA system
    mas = {}
    for n in [5,10,20,30,60,120]:
        mv = last[f'MA{n}']
        mas[f'MA{n}'] = {'value': float(mv), 'relation': 'above' if c_now > mv else 'below'}
    bull_count = sum(1 for m in mas.values() if m['relation'] == 'above')

    result['sections']['technical'] = {
        'ma_system': {'lines': mas, 'bullish_count': bull_count, 'total': 6},
        'macd': {
            'DIF': float(last['DIF']), 'DEA': float(last['DEA']),
            'HIST': float(last['MACDH']), 'direction': 'bull' if last['MACDH']>0 else 'bear'
        },
        'kdj': {'K': float(last['K']), 'D': float(last['D']), 'J': float(last['J']),
                'status': 'overbought' if last['J']>100 else ('oversold' if last['J']<0 else 'normal')},
        'rsi': {f'RSI{n}': float(last[f'RSI{n}']) for n in [6,12,24]},
        'wr': {'value': float(last['WR']),
               'status': 'overbought' if last['WR']<20 else ('oversold' if last['WR']>80 else 'neutral')},
        'boll': {
            'upper': float(last['B_UP']), 'mid': float(last['B_MID']),
            'lower': float(last['B_DN']),
            'position_pct': float((c_now - last['B_DN']) / (last['B_UP'] - last['B_DN']) * 100),
            'width_pct': float((last['B_UP'] - last['B_DN']) / last['B_MID'] * 100),
        },
        'adx': {
            'adx': round(float(last['ADX']), 1), 'pdi': round(float(last['PDI']), 1),
            'mdi': round(float(last['MDI']), 1),
            'trend_strength': 'strong' if last['ADX'] > 25 else ('weak' if last['ADX'] < 20 else 'moderate'),
            'direction': 'bullish' if last['PDI'] > last['MDI'] else 'bearish'
        },
        'atr': {
            'value': round(float(last['ATR14']), 2),
            'pct_of_price': round(float(last['ATR14'] / c_now * 100), 2),
            'daily_range_est': f"{round(c_now - last['ATR14'], 2)} ~ {round(c_now + last['ATR14'], 2)}"
        },
    }

    # Multi-period trend
    result['sections']['trends'] = {}
    for period, label in [(5,'short'), (10,'short'), (20,'short_mid'), (30,'mid'), (60,'mid_long'), (120,'long')]:
        start = max(0, l-period+1)
        seg = df.iloc[start:l+1]
        if len(seg) >= 2:
            chg = (seg['close'].iloc[-1]-seg['close'].iloc[0])/seg['close'].iloc[0]*100
            result['sections']['trends'][label] = {
                'period_days': period, 'change_pct': round(chg, 2),
                'high': float(seg['high'].max()), 'low': float(seg['low'].min()),
                'direction': 'up' if chg>0 else 'down'
            }

    # Volume-price
    rec = df.tail(5)
    result['sections']['volume_price'] = {
        'today_vol_w': float(rec['volume'].iloc[-1]/10000),
        'prev4d_avg_vol_w': float(rec['volume'].iloc[:-1].mean()/10000),
        'vol_ratio': float(rec['volume'].iloc[-1]/rec['volume'].iloc[:-1].mean()),
        'recent_5d': [
            {'date': str(rec['date'].iloc[i]), 'close': float(rec['close'].iloc[i]),
             'pctChg': float(rec['pctChg'].iloc[i]), 'volume_w': float(rec['volume'].iloc[i]/10000)}
            for i in range(len(rec))
        ]
    }

except Exception as e:
    result['sections']['history'] = {"error": str(e)}

# --- 2. AKShare: fund flow ---
if not NO_FUND_FLOW:
    try:
        import akshare as ak
        ff = ak.stock_individual_fund_flow(stock=STOCK, market=_MARKET)
        ff_tail = ff.tail(10)
        flows = []
        for i in range(len(ff_tail)):
            r = ff_tail.iloc[i]
            flows.append({
                'date': str(r.iloc[0]),
                'close': float(r.iloc[1]),
                'pctChg': float(r.iloc[2]),
                'main_net_yi': round(float(r.iloc[3])/1e8, 2),
                'main_pct': round(float(r.iloc[4]), 2),
                'xl_net_yi': round(float(r.iloc[5])/1e8, 2),
                'xl_pct': round(float(r.iloc[6]), 2),
                'lg_net_yi': round(float(r.iloc[7])/1e8, 2),
                'lg_pct': round(float(r.iloc[8]), 2),
                'md_net_yi': round(float(r.iloc[9])/1e8, 2),
                'md_pct': round(float(r.iloc[10]), 2),
                'sm_net_yi': round(float(r.iloc[11])/1e8, 2) or 0.0,
                'sm_pct': round(float(r.iloc[12]), 2) or 0.0,
            })

        result['sections']['fund_flow'] = {
            'recent_10d': flows,
            'today': flows[-1] if flows else None,
            'last5d_total_main_yi': round(sum(f['main_net_yi'] for f in flows[-5:]), 2),
        }

        # Alert for divergence
        if flows:
            tf = flows[-1]
            if tf['main_net_yi'] < 0 and tf['pctChg'] > 0:
                result['sections']['fund_flow']['alert'] = {
                    'type': 'bearish_divergence',
                    'msg': f"Price UP (+{tf['pctChg']}%) but Main Force SELLING ({tf['main_net_yi']}Y)",
                    'risk': 'high'
                }
    except Exception as e:
        result['sections']['fund_flow'] = {"error": str(e)}

# --- 3. AKShare: news + announcements ---
if not NO_NEWS:
    try:
        import akshare as ak
        news_items = []

        # 3a. 东方财富个股新闻
        try:
            df_news = ak.stock_news_em(symbol=STOCK)
            for i in range(min(10, len(df_news))):
                row = df_news.iloc[i]
                news_items.append({
                    'source': '东方财富',
                    'title': str(row.iloc[1]) if len(row) > 1 else '',
                    'content': str(row.iloc[2])[:200] if len(row) > 2 else '',
                    'time': str(row.iloc[3]) if len(row) > 3 else '',
                    'url': str(row.iloc[5]) if len(row) > 5 else '',
                    'type': 'news'
                })
        except Exception as e:
            news_items.append({'source': '东方财富', 'error': str(e), 'type': 'error'})

        # 3b. 主流财经新闻
        try:
            df_main = ak.stock_news_main_cx()
            # Filter for relevant ones (simple keyword match on title)
            relevant = df_main[df_main.iloc[:, 0].astype(str).str.contains(
                f'{STOCK}|AI|算力|光模块|CPO|光通信|数据中心', case=False, na=False
            )]
            for i in range(min(5, len(relevant))):
                row = relevant.iloc[i]
                news_items.append({
                    'source': '主流财经',
                    'title': str(row.iloc[0]) if len(row) > 0 else '',
                    'time': str(row.iloc[1]) if len(row) > 1 else '',
                    'type': 'macro_news'
                })
        except Exception:
            pass  # 非核心，静默跳过

        result['sections']['news'] = {
            'items': news_items,
            'total': len(news_items),
            'latest_date': news_items[0]['time'] if news_items else None
        }

    except Exception as e:
        result['sections']['news'] = {"error": str(e), "items": []}

# ======== ADDITIONAL AKSHARE FETCHES ========
# --- 4. 北向资金 ---
try:
    import akshare as ak
    from datetime import datetime, timedelta
    nb = ak.stock_hsgt_individual_em(symbol=STOCK)
    if nb is not None and len(nb) > 0:
        nb = nb.sort_values(nb.columns[0])
        nb_last_date = str(nb.iloc[-1, 0])
        nb_data_age = (datetime.now() - datetime.strptime(nb_last_date[:10], '%Y-%m-%d')).days
        nb_tail = nb.tail(10)
        nb_data = []
        for i in range(len(nb_tail)):
            r = nb_tail.iloc[i]
            nb_data.append({
                'date': str(r.iloc[0])[:10],
                'close': round(float(r.iloc[1]), 2),
                'pctChg': round(float(r.iloc[2]), 2),
                'hold_value_yi': round(float(r.iloc[4])/1e8, 2),
                'hold_pct_a': round(float(r.iloc[5]), 3),
                'value_chg_yi': round(float(r.iloc[8])/1e8, 2),
            })
        nb_today = nb_data[-1]
        nb_5d_val = round(sum(x['value_chg_yi'] for x in nb_data[-5:]), 2)
        nb_10d_val = round(sum(x['value_chg_yi'] for x in nb_data[-10:]), 2)
        if nb_data_age > 90:
            nb_verdict = f'数据过时({nb_data_age}天)⚠️ 可能已不在沪深港通'
        elif nb_5d_val > 5: nb_verdict = '大幅流入📈📈'
        elif nb_5d_val > 1: nb_verdict = '持续流入📈'
        elif nb_5d_val > -1: nb_verdict = '小幅进出'
        elif nb_5d_val > -5: nb_verdict = '持续流出📉'
        else: nb_verdict = '大幅流出📉📉'
        result['sections']['northbound'] = {
            'recent_10d': nb_data, 'today': nb_today,
            'last5d_net_yi': nb_5d_val, 'last10d_net_yi': nb_10d_val,
            'latest_hold_pct': nb_today.get('hold_pct_a'),
            'data_freshness_days': nb_data_age,
            'verdict': nb_verdict
        }
except Exception as e:
    result['sections']['northbound'] = {"error": str(e)}

# --- 5. 融资融券 ---
try:
    import akshare as ak
    # SSE/SZSE margin returns ALL stocks for a date; filter for ours
    if _MARKET == 'sh':
        mg = ak.stock_margin_detail_sse(date='20260430')
        # cols: 信用交易日期, 标的证券代码, 标的证券简称, 融资余额, 融资买入额, 融资偿还额, 融券余量, 融券卖出量, 融券偿还量
        mg = mg[mg.iloc[:, 1].astype(str) == STOCK]
    else:
        mg = ak.stock_margin_detail_szse(date='20260430')
        mg = mg[mg.iloc[:, 1].astype(str) == STOCK]
    if mg is not None and len(mg) > 0:
        r = mg.iloc[0]
        result['sections']['margin'] = {
            'date': str(r.iloc[0]),
            'rz_balance_yi': round(float(r.iloc[3])/1e8, 2),
            'rz_buy_yi': round(float(r.iloc[4])/1e8, 2),
            'rz_repay_yi': round(float(r.iloc[5])/1e8, 2),
            'rq_shares_wan': round(float(r.iloc[6])/1e4, 2),
            'rq_sell_wan': round(float(r.iloc[7])/1e4, 2),
        }
        # Balance trend: high rz_balance vs market cap?
        cap = result['sections'].get('capital', {})
        tot = cap.get('total_shares', 0) or 0
        rs = result['sections']
        if rs.get('sina_realtime', {}).get('price'):
            mkt_cap = tot * rs['sina_realtime']['price'] / 1e8
        elif rs.get('history', {}).get('latest', {}).get('close'):
            mkt_cap = tot * rs['history']['latest']['close'] / 1e8
        else:
            mkt_cap = 0
        rz_ratio = round(float(r.iloc[3]) / 1e8 / mkt_cap * 100, 2) if mkt_cap > 0 else 0
        result['sections']['margin']['rz_to_mktcap_pct'] = rz_ratio
        result['sections']['margin']['rz_verdict'] = '高杠杆⚠️' if rz_ratio > 5 else ('适中' if rz_ratio > 2 else '低杠杆')
except Exception as e:
    result['sections']['margin'] = {"error": str(e)}

# --- 6. 股东户数 ---
try:
    import akshare as ak
    # stock_zh_a_gdhs returns shareholder count for ALL A-stocks at a reporting date
    # Try latest quarter dates (Q1 2026: 20260331, Q4 2025: 20251231)
    for dt in ['20260331','20251231','20250930']:
        try:
            sh = ak.stock_zh_a_gdhs(symbol=dt)
            if sh is not None and len(sh) > 0:
                r = sh[sh.iloc[:, 0].astype(str) == STOCK]
                if len(r) > 0:
                    row = r.iloc[0]
                    # cols: 代码, 名称, 最新价, 涨跌幅, 股东数量-总计, 股东数量-上次, 变动, 变动幅度, ...
                    result['sections']['shareholders'] = {
                        'report_date': dt,
                        'holders': int(float(row.iloc[4])),
                        'prev_holders': int(float(row.iloc[5])),
                        'change': int(float(row.iloc[6])),
                        'change_pct': round(float(row.iloc[7]), 2),
                    }
                    if result['sections']['shareholders']['change'] < 0:
                        result['sections']['shareholders']['trend'] = '筹码集中🟢'
                    elif result['sections']['shareholders']['change'] / max(result['sections']['shareholders']['prev_holders'], 1) > 0.1:
                        result['sections']['shareholders']['trend'] = '筹码分散🔴'
                    else:
                        result['sections']['shareholders']['trend'] = '基本稳定'
                    break
        except Exception:
            continue
except Exception as e:
    result['sections']['shareholders'] = {"error": str(e)}

# --- 7. 研报追踪 ---
try:
    import akshare as ak
    rp = ak.stock_research_report_em(symbol=STOCK)
    if rp is not None and len(rp) > 0:
        rp_items = []
        for i in range(min(8, len(rp))):
            row = rp.iloc[i]
            # Confirmed positions (headers may be off by 1 in some AKShare versions):
            # 0=idx 1=code 2=name 3=title 4=rating 5=org ... 14=date 15=pdf_link
            rp_items.append({
                'date': str(row.iloc[14]) if len(row) > 14 else '',
                'title': str(row.iloc[3])[:80] if len(row) > 3 else '',
                'org': str(row.iloc[5]) if len(row) > 5 else '',
                'rating': str(row.iloc[4]) if len(row) > 4 else '',
            })
        # Scoring
        buy_count = sum(1 for x in rp_items if any(kw in x.get('rating','') for kw in ['买入','增持','强烈推荐','推荐']))
        if len(rp_items) > 0:
            rp_score = '强烈看多' if buy_count >= 6 else ('偏多' if buy_count >= 4 else ('中性' if buy_count >= 2 else '偏空'))
        else:
            rp_score = 'N/A'
        result['sections']['analyst'] = {
            'reports': rp_items, 'total': len(rp_items),
            'buy_pct': round(buy_count/len(rp_items)*100, 1) if rp_items else 0,
            'verdict': rp_score
        }
except Exception as e:
    result['sections']['analyst'] = {"error": str(e)}

# --- 8. 限售解禁 ---
try:
    import akshare as ak
    # stock_restricted_release_detail_em: all upcoming lockup releases, filter by stock
    lj = ak.stock_restricted_release_detail_em(start_date='20260501', end_date='20260731')
    if lj is not None and len(lj) > 0:
        # cols: 序号,股票代码,股票简称,解禁时间,解禁股数,解禁前日收盘价,实际解禁数量,实际解禁市值,占流通市值比例,...
        f = lj[lj.iloc[:, 1].astype(str) == STOCK]
        if len(f) > 0:
            lj_future = []
            for i in range(min(5, len(f))):
                row = f.iloc[i]
                lj_future.append({
                    'date': str(row.iloc[3]),
                    'shares_wan': round(float(row.iloc[6])/1e4, 2) if len(row) > 6 else 0,
                    'ratio_pct': round(float(row.iloc[8]), 2) if len(row) > 8 else 0,
                    'amount_yi': round(float(row.iloc[7])/1e8, 2) if len(row) > 7 else 0,
                })
            lj_verdict = '近期有解禁压力⚠️' if lj_future[0].get('ratio_pct', 0) > 5 else ('轻量解禁' if lj_future else '无近期解禁✅')
            result['sections']['lockup'] = {'upcoming': lj_future, 'verdict': lj_verdict}
        else:
            result['sections']['lockup'] = {'upcoming': [], 'verdict': '无近期解禁✅'}
except Exception as e:
    result['sections']['lockup'] = {"error": str(e)}

# ======== SCORING & FORECAST ========
s = result['sections']
pos = neg = warn = 0

if 'technical' in s:
    t = s['technical']
    if t['ma_system']['bullish_count'] >= 5: pos += 1

    if t.get('macd',{}).get('direction') == 'bull': pos += 1
    if t.get('kdj',{}).get('K',0) > t.get('kdj',{}).get('D',0): pos += 1
    rsi12 = t.get('rsi',{}).get('RSI12', 50)
    if 30 < rsi12 < 70: pos += 1
    else: warn += 1

    if t.get('kdj',{}).get('J', 0) > 100: neg += 1; warn += 1
    if t.get('wr',{}).get('value', 50) < 20: neg += 1; warn += 1
    if t.get('boll',{}).get('position_pct', 50) > 80: neg += 1; warn += 1

    # ADX — trend strength
    adx = t.get('adx', {})
    if adx.get('trend_strength') == 'strong':
        if adx.get('direction') == 'bullish': pos += 1
        else: neg += 1

if 'fundamentals' in s:
    f = s['fundamentals']
    if f.get('roe', 0) > 0.08: pos += 1
    # Growth scoring: use best available metric (Q1 > annual)
    g = f.get('yoy_np_q1') or f.get('yoy_np', 0)
    if g > 0.1: pos += 1
    if g > 0.5: pos += 2  # explosive growth (>50% YoY)
    # Cash quality: profit backed by real cash?
    if f.get('cash_quality') == 'strong': pos += 1
    elif f.get('cash_quality') == 'weak': neg += 1
    # PEG-aware valuation (replaces raw PE cutoff)
    peg = f.get('peg')
    if peg is not None:
        if peg > 3: neg += 2
        elif peg > 2: neg += 1
        elif peg < 0.5: pos += 1  # deep value
    else:
        pe = s.get('history',{}).get('latest',{}).get('pe', 0) or 0
        if pe > 60: neg += 1

    # Historical PE/PB percentile (cheap or expensive vs own history)
    pe_pct_val = f.get('pe_hist_pct')
    if pe_pct_val is not None:
        if pe_pct_val >= 90: neg += 2
        elif pe_pct_val >= 80: neg += 1; warn += 1
        elif pe_pct_val < 20: pos += 1

    # Peer ranking bonus/penalty
    pr = s.get('peers', {}).get('self_rank')
    pcnt = s.get('peers', {}).get('peer_count', 0)
    if pr and pcnt >= 3:
        if pr <= 2: pos += 1  # Top 2 in industry
        elif pr >= pcnt - 1: neg += 1  # Bottom 2

if 'fund_flow' in s and s['fund_flow'].get('today'):
    tf = s['fund_flow']['today']
    tf_main = tf.get('main_net_yi', 0)
    tf_pct = tf.get('pctChg', 0)
    if tf_main < 0 and tf_pct > 0: neg += 2; warn += 2
    elif tf_main > 0: pos += 1

# Northbound flow (skip scoring if data is stale)
if 'northbound' in s and s['northbound'].get('today'):
    nb = s['northbound']
    if nb.get('data_freshness_days', 999) < 90:
        nb_net = nb.get('last5d_net_yi', 0)
        if nb_net > 5: pos += 2
        elif nb_net > 1: pos += 1
        elif nb_net < -5: neg += 2
        elif nb_net < -1: neg += 1

# Margin trend
if 'margin' in s and s['margin'].get('rz_verdict'):
    mt = s['margin']['rz_verdict']
    if '高杠杆' in mt: warn += 1; neg += 1
    elif '低杠杆' in mt: pos += 1

# Shareholder concentration
if 'shareholders' in s and s['shareholders'].get('trend'):
    sht = s['shareholders']['trend']
    if '集中' in sht: pos += 1
    elif '分散' in sht: neg += 1

# Analyst consensus
if 'analyst' in s and s['analyst'].get('reports'):
    ra = s['analyst']
    bp = ra.get('buy_pct', 50)
    if bp >= 80: pos += 2
    elif bp >= 60: pos += 1
    elif bp < 20: neg += 2

# Lockup risk
if 'lockup' in s and s['lockup'].get('upcoming'):
    lk = s['lockup']['upcoming']
    if lk and lk[0].get('ratio_pct', 0) > 10: neg += 2; warn += 1
    elif lk and lk[0].get('ratio_pct', 0) > 5: neg += 1

# Beta risk
if 'beta' in s and s['beta'].get('value'):
    bv = s['beta']['value']
    f = s.get('fundamentals', {})
    f['beta'] = bv
    f['beta_label'] = s['beta'].get('label', 'N/A')

# News sentiment — weighted scoring (not just keyword count)
if 'news' in s and s['news'].get('items'):
    news_text = ' '.join([n.get('title','') + n.get('content','') for n in s['news']['items'][:5]])
    # Weighted keywords: some words carry much more signal
    pos_weighted = {
        '业绩大增': 3, '超预期': 3, '涨停': 2, '突破': 2, '创新高': 2,
        '回购': 2, '增持': 2, '新进': 2, '举牌': 2, '中标': 1, '订单': 1, '签约': 1,
        '量产': 2, '放量': 2, '利好': 1, '增长': 1, '分红': 1, '送转': 1, '入选': 1,
        '获批': 1, '首款': 1, '首家': 1, '大单': 2, '调高': 2, '升级': 1, '推荐': 1,
        '金股': 1, '高位': -1, '拒绝': -1, '终止': -2
    }
    neg_weighted = {
        '亏损': 3, '暴雷': 4, '立案': 4, '违规': 3, '退市': 5,
        '减持': 2, '调查': 3, '诉讼': 2, '冻结': 3, '质押': 2,
        '预警': 2, '降级': 2, '下调': 2, '巨亏': 4, '停产': 3,
        '商誉': 2, '减值': 3, '下跌': 1, '降价': 1, '裁员': 2,
        '产能过剩': 2, '强制': 3, '出清': 2, '处罚': 3, '警示函': 2,
        '监管函': 3, '问询函': 2, 'ST': 4, '被立案': 5
    }
    pos_score = sum(w for kw, w in pos_weighted.items() if kw in news_text)
    neg_score = sum(w for kw, w in neg_weighted.items() if kw in news_text)
    s['news']['sentiment_score'] = {'positive': pos_score, 'negative': neg_score, 'net': pos_score - neg_score}
    if pos_score - neg_score >= 5:
        pos += 2; s['news']['sentiment_verdict'] = '强烈偏多🔥'
    elif pos_score - neg_score >= 2:
        pos += 1; s['news']['sentiment_verdict'] = '偏多✅'
    elif neg_score - pos_score >= 5:
        neg += 2; s['news']['sentiment_verdict'] = '强烈偏空🔴'
    elif neg_score - pos_score >= 2:
        neg += 1; s['news']['sentiment_verdict'] = '偏空⚠️'
    else:
        s['news']['sentiment_verdict'] = '中性'

# --- Forecast ---
def make_forecast(pos, neg, warn, trends, fundamentals, fund_flow):
    fc = {}

    # Short-term
    if warn >= 4:
        fc['short'] = {
            'direction': 'bearish', 'confidence': 'high',
            'summary': 'Overbought + divergence. High probability pullback within 3-5 days.',
            'scenarios': {
                'bear': 'Pullback to MA20 (~49-50) or MA60 (~51)',
                'base': 'Consolidate between MA20(49.7) and current(53)',
                'bull': 'If volume sustains > 2x avg, test BOLL upper then 55-56'
            }
        }
    elif warn >= 2:
        fc['short'] = {
            'direction': 'neutral_bearish', 'confidence': 'medium',
            'summary': 'Overbought signals present. Upside limited, consolidation likely.',
            'scenarios': {
                'bear': 'Fade to 50-51 zone',
                'base': 'Rangebound 51-54',
                'bull': 'Extended rally to 55-56 on strong volume'
            }
        }
    elif pos > neg + 2:
        fc['short'] = {
            'direction': 'bullish', 'confidence': 'medium',
            'summary': 'Strong momentum across multiple timeframes.',
            'scenarios': {
                'bear': 'Mild pullback to MA20',
                'base': 'Test BOLL upper',
                'bull': 'Breakout toward next resistance'
            }
        }
    else:
        fc['short'] = {
            'direction': 'neutral', 'confidence': 'low',
            'summary': 'Mixed signals. Wait for clearer direction.',
            'scenarios': {'bear':'Test support','base':'Rangebound','bull':'Break resistance'}
        }

    # Mid-term
    mid_chg = trends.get('mid', {}).get('change_pct', 0) if trends else 0
    if pos > neg + 1 and mid_chg > 0:
        fc['mid'] = {
            'direction': 'bullish',
            'summary': f'+{mid_chg}% in 30d — trend reversal confirmed, MA20/30 as support',
            'target_zone': '56-60', 'stop_zone': '46 (below BOLL lower)'
        }
    elif pos > neg:
        fc['mid'] = {
            'direction': 'neutral_bullish',
            'summary': 'Gradual recovery, watch MA60 as key support',
            'target_zone': '55-58', 'stop_zone': '48'
        }
    elif warn >= 2:
        fc['mid'] = {
            'direction': 'neutral',
            'summary': 'Overbought risk of mean reversion. 46-48 support zone.',
            'target_zone': '50-55', 'stop_zone': '44'
        }
    else:
        fc['mid'] = {
            'direction': 'neutral',
            'summary': 'Wait for earnings/volume confirmation.',
            'target_zone': '48-58', 'stop_zone': '44'
        }

    # Long-term
    yoy_rev = fundamentals.get('yoy_rev', 0)
    yoy_np = fundamentals.get('yoy_np', 0)
    if yoy_rev > 0.15 and yoy_np > 0.3:
        fc['long'] = {
            'direction': 'bullish',
            'summary': 'Strong earnings growth supports valuation. 63.57 (ATH) is key resistance.',
            'target_zone': '60-70', 'stop_zone': '44'
        }
    elif yoy_np > 0.1:
        fc['long'] = {
            'direction': 'neutral_bullish',
            'summary': 'Earnings improving but PE elevated. Tied to rare earth pricing + policy.',
            'target_zone': '55-65', 'stop_zone': '44'
        }
    else:
        fc['long'] = {
            'direction': 'neutral',
            'summary': 'Wait for sustained earnings growth confirmation.',
            'target_zone': '50-60', 'stop_zone': '43'
        }

    return fc

# --- Trade suggestion ---
def make_trade_suggestion(pos, neg, warn, fc):
    trade = {
        'score': {'bullish': pos, 'bearish': neg, 'warnings': warn},
        'disclaimer': '以下仅为风险敞口评估，不构成买卖建议。请结合自身情况独立决策。'
    }

    st = fc['short']['direction']
    mt = fc['mid']['direction']
    lt = fc['long']['direction']

    if warn >= 4:
        trade['short_term'] = {
            'action': '谨慎 / 减仓',
            'reason': '多指标超买 + 主力出货信号，短期回调概率高',
            'advice': '不追高，已有持仓者可考虑部分减仓锁定利润'
        }
        trade['mid_term'] = {
            'action': '观望 / 等回调',
            'reason': '短期过热后大概率均值回归',
            'advice': '等待回调至MA20或MA60附近再评估入场'
        }
        trade['long_term'] = {
            'action': '关注 / 逢低布局',
            'reason': f'长期趋势:[{lt}]，基本面改善中',
            'advice': '稀土行业长期逻辑存在，但当前估值偏高。等待回调后分批建仓更优'
        }
        trade['position_guidance'] = {
            '激进型': '≤20%仓位，严格止损MA60下方',
            '稳健型': '≤10%仓位，等回调确认支撑',
            '保守型': '观望，等待日线回调10%+再评估'
        }
    elif warn >= 2:
        trade['short_term'] = {
            'action': '谨慎持有',
            'reason': '部分指标超买，短期存在回调风险',
            'advice': '持有者可设止盈，未入场者等待回调'
        }
        trade['position_guidance'] = {
            '激进型': '≤30%仓位', '稳健型': '≤15%仓位', '保守型': '观望'
        }
    elif pos > neg + 2:
        trade['short_term'] = {
            'action': '适度参与',
            'reason': '技术面强势，多周期共振向上',
            'advice': '回调至MA10/MA20附近可轻仓试多，止损MA60下方'
        }
        trade['position_guidance'] = {
            '激进型': '≤40%仓位', '稳健型': '≤25%仓位', '保守型': '≤15%仓位'
        }
    else:
        trade['short_term'] = {
            'action': '观望',
            'reason': '多空信号混杂，方向不明',
            'advice': '等待放量突破或回调企稳信号'
        }
        trade['position_guidance'] = {
            '激进型': '≤20%仓位', '稳健型': '观望', '保守型': '观望'
        }

    return trade

# Build assessment if we have data
c_now = None
if 'history' in s and 'latest' in s['history']:
    c_now = s['history']['latest']['close']
    last_close_ref = c_now
elif 'sina_realtime' in s and 'price' in s['sina_realtime']:
    c_now = s['sina_realtime']['price']
    last_close_ref = c_now

fc = make_forecast(pos, neg, warn,
                   result['sections'].get('trends', {}),
                   result['sections'].get('fundamentals', {}),
                   result['sections'].get('fund_flow', {}))
trade = make_trade_suggestion(pos, neg, warn, fc)

# Key levels
kl = {'resistance_r2': 63.57, 'resistance_r1': 56.00}
if c_now:
    kl['current'] = round(c_now, 2)
if 'history' in s and 'latest' in s['history']:
    kl['resistance'] = round(s['history']['latest']['close'] * 1.05, 2)
if 'technical' in s:
    t = s['technical']
    kl['resistance_boll_upper'] = round(t['boll']['upper'], 2)
    kl['boll_position_pct'] = round(t['boll']['position_pct'], 1)
    for n in ['MA60', 'MA20', 'MA30']:
        if n in t['ma_system']['lines']:
            kl[f'support_{n}'] = round(t['ma_system']['lines'][n]['value'], 2)
    kl['support_boll_lower'] = round(t['boll']['lower'], 2)
kl['support_psyc'] = 48.00

result['sections']['assessment'] = {
    'score': {'positive': pos, 'negative': neg, 'warnings': warn},
    'forecast': fc,
    'trade_suggestion': trade,
    'key_levels': kl,
}

# ======== OUTPUT ========
# Cleanup: replace -0.0 with 0.0 globally (float precision artifacts)
def _clean_neg_zero(obj):
    if isinstance(obj, float):
        return obj if obj != 0.0 else 0.0  # preserves -0.0 → 0.0
    if isinstance(obj, dict):
        return {k: _clean_neg_zero(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_neg_zero(v) for v in obj]
    return obj
result = _clean_neg_zero(result)
output = json.dumps(result, ensure_ascii=False, indent=2, default=str)
if OUTPUT_FILE:
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(output)
    print(f'OK: written to {OUTPUT_FILE}', file=sys.stderr)
else:
    print(output)
