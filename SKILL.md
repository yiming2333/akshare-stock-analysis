# AKShare Stock Analysis Skill — v2.0.0

## 版本历史
| 版本 | 日期 | 变更 |
|------|------|------|
| v2.0.0 | 2026-05-02 | **重大重构**: ① 移除主观trade_suggestion→客观signals+risk_warnings ② 新增--backtest回测模式 ③ 新增估值定价模块(相对估值+简易DCF) ④ 新增波动率/VaR风险度量 ⑤ 数据持久化(SQLite)+--trend趋势输出 ⑥ --batch批量分析 ⑦ 重试机制+缓存分层 ⑧ 事件预警增强 |
| v1.2.0 | 2026-05-01 | 7项Bug修复: 新浪PE/PB/Peer分组/Capital转亿/PEG基数保护等 |
| v1.1.2 | 2026-05-01 | 三个Bug修复: PEG EPS口径/Peer fallback/资金流向字段补齐 |
| v1.1.1 | 2026-04-30 | PEG Q1实时YoY; 资金流向字段扩展; 北向/融资列序修复 |
| v1.1.0 | 2026-04-29 | 全面重构: 双源融合/PEG/杜邦/现金流/同业排名/背离/评分/预测 |
| v1.0.0 | 2026-04-28 | 初始版本: 四维个股诊断引擎

四维深度个股诊断引擎：基本面 + 消息面 + 资金面 + 技术面，输出 JSON 结构化报告。

## Data Sources

| 维度 | 数据源 | 内容 |
|------|--------|------|
| 基本面 | pytdx (通达信) + Baostock | 股本结构、总资产/净资产/BVPS、利润表、增长率、杜邦分析、PE/PB/PS、PEG、PE/PB历史分位数、现金流质量(CFO/NP/OR)、同行横向对比排名 |
| 消息面 | AKShare (东财新闻) | 最新公告、新闻 |
| 资金面 | AKShare (东财dataCenter) | 日频主力/超大单/大单/中单/小单净流入、北向资金、融资融券、股东户数 |
| 技术面 | Baostock历史K线 + 内置计算 | MA(5/10/20/30/60/120)、MACD(6,13,5)、KDJ(6,3,3)、RSI(6/12/24)、WR(10)、BOLL(20,2)、ADX(14)、ATR(14)、量价关系、多周期趋势 |

## 新增功能 (v2.0)

### 客观信号 (signals)
替代原主观 trade_suggestion，输出客观触发的技术/资金/基本面信号：
- 技术信号: MACD金叉、KDJ超买/超卖、均线多头/空头排列、RSI超买/超卖、BOLL突破
- 资金信号: 主力连续5日净流入/流出、聪明钱底背离
- 北向信号: 北向资金5日大幅流入/流出
- 基本面信号: ROE优秀、净利润高增长、PEG深度低估

### 风险预警 (risk_warnings)
- 解禁风险: 未来60天大额解禁预警
- 融资过热: 融资余额占流通市值>8%
- 估值峰值: PE处于90%+历史分位
- PEG高估: PEG>3
- 现金流风险: 经营现金流为负
- 筹码分散: 股东户数大增
- 高Beta/高回撤风险

### 回测模式 (--backtest)
```bash
python scripts/akshare_query.py 600111 --backtest
```
按MACD金叉买入、持5日卖出、-8%止损的规则，对过去1-3年历史数据回测。
输出: 累计收益率、胜率、最大回撤、夏普比率、具体交易记录。

### 估值定价模块
- **相对估值**: 基于同行业PE/PB中位数，计算当前股价的低估/合理/高估区间
- **简易DCF**: 基于经营现金流，三场景(保守/基准/乐观)估算内在价值及上涨空间

输出格式示例：
```json
"valuation": {
  "relative": {
    "pe": 70.02, "pb": 7.49,
    "pe_peer_median": 45.3, "pe_peer_pct": 85.0,
    "fair_price_pe": 34.5, "price_to_fair_pe": 1.54,
    "status": "高估"
  },
  "dcf": {
    "scenarios": {
      "conservative": {"intrinsic_value": 4.67, "upside_pct": -91.2},
      "base": {"intrinsic_value": 5.2, "upside_pct": -90.2},
      "optimistic": {"intrinsic_value": 6.37, "upside_pct": -88.0}
    }
  }
}
```

### 波动率与风险度量
- 年化波动率(20日/60日)
- VaR (历史模拟法, 5%分位数)
- 最大回撤(过去1年)

### 数据持久化 + 趋势 (--trend)
```bash
python scripts/akshare_query.py 600111 --trend
```
使用 SQLite (`snapshots.db`) 存储每日快照，输出过去20日趋势序列和趋势摘要。

### 批量分析 (--batch)
```bash
python scripts/akshare_query.py --batch stocks.csv
```
一次性分析多只股票，输出汇总对比表。

### 重试机制
- 所有AKShare调用内置指数退避重试(3次, 1s/2s/4s)
- 缓存分层: 财报/估值24h, 资金流向10min, 行情30s

## Quick Reference

```bash
# 标准诊断
python scripts/akshare_query.py 600111

# 快速模式 (跳过新闻/资金流/北向 — 减少API调用)
python scripts/akshare_query.py 600111 --quick

# 回测验证
python scripts/akshare_query.py 600111 --backtest

# 批量分析
python scripts/akshare_query.py --batch stocks.csv

# 趋势追踪 (含SQLite持久化)
python scripts/akshare_query.py 600111 --trend

# JSON输出到文件
python scripts/akshare_query.py 600111 --output result.json
```

## Complete Workflow

当用户要求「诊断个股」「分析X股票」时：

### Step 1: 拉取数据
```bash
python scripts/akshare_query.py <stock_code>
```

### Step 2: 解析JSON & 合成报告

JSON 包含以下 sections:
```json
{
  "capital":       {股本结构, 总资产, 净资产, BVPS, 营收, 净利润, 市值},
  "fundamentals":  {ROE, 利润率, EPS, 增长, PEG, PE/PB分位, 现金流, 杜邦, Beta},
  "technical":     {MA, MACD, KDJ, RSI, WR, BOLL, ADX, ATR},
  "trends":        {多周期涨跌幅},
  "volume_price":  {量价关系, 量比},
  "fund_flow":     {10日资金流, 背离预警},
  "northbound":    {北向资金},
  "margin":        {融资融券},
  "shareholders":  {股东户数},
  "analyst":       {研报追踪/评级变动},
  "lockup":        {解禁预警},
  "signals":       {客观信号数组 ✨NEW},
  "risk_warnings": {风险预警数组 ✨NEW},
  "valuation":     {相对估值+DCF ✨NEW},
  "risk_metrics":  {波动率/VaR/最大回撤 ✨NEW},
  "assessment":    {评分, 预测, 关键价位}
}
```

## 技术指标计算方法

| 指标 | 参数 | 公式 |
|------|------|------|
| MACD | (6,13,5) | EMA(6)-EMA(13)=DIF, EMA(DIF,5)=DEA, HIST=2*(DIF-DEA) |
| KDJ | (6,3,3) | RSV=(C-L6)/(H6-L6)*100, K=EMA(RSV,2), D=EMA(K,2), J=3K-2D |
| RSI | (6,12,24) | RS=AvgGain/AvgLoss, RSI=100-100/(1+RS) |
| WR | (10) | (H10-C)/(H10-L10)*100 |
| BOLL | (20,2) | MID=MA20, UP/DN=MID±2*STD(20) |

## 背离检测规则

| 类型 | 条件 | 信号 |
|------|------|------|
| 资金背离(熊) | 今日涨幅>0 且 主力净流入<0 | 高风险：散户拉升，机构出货 |
| 资金背离(牛) | 今日跌幅>0 且 主力净流入>0 | 低吸信号：散户恐慌，机构吸筹 |
| 聪明钱背离 | 主力连续3日流入 + 股价下跌 | 底背离散户割肉，机构接筹 |
| MACD顶背离 | 价格创新高 + MACD柱下降 | 顶背离，上涨动能衰竭 |
| MACD底背离 | 价格创新低 + MACD柱上升 | 底背离，反转信号 |

## 依赖安装

```bash
pip install baostock pytdx akshare pandas numpy requests
```

## 缓存机制

| 数据层 | TTL | 说明 |
|--------|-----|------|
| K线行情 | 30s | 实时行情缓存极短 |
| 资金流向 | 10min | 日频数据，适度缓存 |
| 北向资金 | 30min | 延迟较大，用缓存 |
| 同行PE/PB | 24h | 财报数据变化慢 |

## 评分体系

| 打分来源 | 规则 |
|---------|------|
| MA均线 | 5+条多头排列 +1 |
| MACD | 金叉/DIF>DEA +1 |
| KDJ | K>D +1; J>100 超买 -1/+1w |
| RSI | 30-70 正常区间 +1; 否则 +1w |
| WR | <20 超买 -1/+1w |
| BOLL | >80%位置 -1/+1w |
| ADX | >25强趋势: PDI>MDI +1, 否则 -1 |
| ROE | >8% +1 |
| 净利增速 | >10% +1; >50% +2 |
| 现金流 | strong +1; weak -1 |
| PEG | >3 扣2分; >2扣1; <0.5 +1 |
| PE分位数 | ≥90% 扣2分; ≥80% 扣1/+1w; <20% +1 |
| 同行排名 | Top2 +1; 倒2 -1 |
| 主力资金 | 股价涨+主力卖 扣2/+2w; 主力买 +1 |
| 北向资金 | >5亿 +2; >1亿 +1; <-5亿 -2; <-1亿 -1 |
| 融资 | 高杠杆 -1/+1w; 低杠杆 +1 |
| 筹码 | 集中 +1; 分散 -1 |
| 研报 | 买入占比≥80% +2; ≥60% +1; <20% -2 |
| 解禁 | >10% -2/+1w; >5% -1 |
| 舆情 | 正面关键词>负面 +1~2; 相反 -1~2 |

## 注意事项

- 免费数据源有频率限制，避免短时间内大量查询
- 通达信数据依赖第三方服务器，非交易时段可能无实时数据
- AKShare 资金流数据依赖东财接口，非交易时段无当日数据
- 本技能不提供投资建议，所有信号和预测均为基于公开数据的客观分析
- DCF估值基于经营现金流估算，仅供参考，实际价值受多重因素影响
- 简易DCF使用固定WACC=10%、永续增长率=3%，精确度有限
