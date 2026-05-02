# AKShare Stock Analysis Skill

> **基于Python + AKShare的A股智能分析工具**，提供多维度个股诊断与量化信号生成。

## 项目简介

AKShare Stock Analysis Skill 是一个专为 **A 股市场** 设计的个股诊断系统，整合基本面、技术面、资金面、消息面数据，生成 **结构化 JSON 输出**，便于 **AI Agent 调用** 和 **量化策略集成**。

核心升级 v8.0 已从“主观建议”转向“客观信号”，并新增回测、估值预警、趋势持久化、批量分析等模块。

---

## 核心功能矩阵

### 1. 四维诊断引擎 v8.0

| 维度 | 数据源提供方 | 核心指标 |
|---|---|---|
| 📊 基本面 | pytdx、Baostock、新浪财经 | 股本结构、杜邦分解、ROE、利润表、PE/PB/PS/PEG（基数保护）、现金流质量（CFO/NP/OR）、历史PE/PB百分位 |
| 💰 资金面 | AKShare（东财） | 主力/超大单/大单/中单/小单净流入、融资融券余额、**北向资金持股变化** |
| 📈 技术面 | Baostock + 内置计算 | MA(5/10/20/30/60/120)、MACD、KDJ、RSI、WR、BOLL、ADX、ATR、量价关系、多周期涨跌幅 |
| 📰 事件驱动 | AKShare + SQLite | 未来30天限售解禁预警、大股东减持监控、券商评级变动跟踪 |

### 2. 新增金融工程模块

| 模块 | 说明 |
|---|---|
| **估值定价** | 同行业PE/PB百位排名、基于DFC+增长率假定的公允价值敏感性表格 |
| **技术信号** | 移除了主观 `suggestion`，改为客观信号数组（如MACD金叉/死叉、RSI超买/超卖、主力底背离） |
| **回测引擎** | 支持按自定义信号规则（`macd_golden`类）回测单股或批量股票（1-3年），输出累计收益率、胜率、最大回撤（MaxDD） |
| **风险指标** | 历史波动率（20d/60d）、95% 日常VaR、1年最大回撤 |
| **趋势持久化** | 将每日快照(PE/PB/主力流向/北向)写入 SQLite (`snapshots.db`)，生成60日指标序列（`--trend`开关） |

### 3. 其他重要特性

| 特性 | 说明 |
|---|---|
| **同行对比** | 内置12个行业 Peer 分组，输出同组内 ROE、营收等多指标横向排名 |
| **缓存分层** | 财报、行业 PE 百分位 24h 缓存；资金流向 10 分钟缓存；实时行情不缓存或短缓存 |
| **批量分析** | `--batch stocks.csv` 一次性分析多只票，**生成汇总对比表** |
| **通联降级** | Baostock PE/PB >50% 偏离时自动 fallback 至新浪备选源；pytdx/AKShare 接口自带 **指数退避重试** 3次 |

---

## 技术架构

```
命令行参数
    │
    ▼
┌─────────────────────────────┐
│       akshare_query.py       │
│   StockDeepScan v8 引擎      │
├─────────────────────────────┤
│  • argparse 参数解析         │
│  • detect_market 代码前缀    │
│  • 重试装饰器 @retry          │
└─────────────────────────────┘
    │
    ├── 实时行情：pytdx + 新浪
    ├── 历史K线：Baostock
    ├── 财务数据：Baostock + 新浪
    ├── 资金流向：AKShare（东财）
    ├── 北向数据：stock_hsgt_individual_em
    ├── 融资融券：stock_margin_detail_sse/szse
    ├── 行业Peer：PEER_GROUPS + Baostock
    ├── 事件预警：SQLite + 本地信号枚举
    ├── 估值模块：同行情占比 + 简易DCF
    ├── 技术布告板：内置TA-Lib类计算
    ├── 消息面：stock_news_em
    ├── 批量操作：CSV导入 + Pandas合并
    │
    └── 持久化：SQLite（snapshots.db） + 每日缓存
```

---

## 安装与使用

### 安装依赖

```bash
pip install baostock pytdx akshare pandas numpy requests
```

### 运行命令

```bash
python akshare_query.py 600519                    # 单股四维诊断
python akshare_query.py 600519 --quick            # 仅行情+基本指标，省时间
python akshare_query.py 000858 --no-fund-flow     # 跳过资金流向
python akshare_query.py 300750 --no-news          # 跳过新闻采集
python akshare_query.py 002594 --trend            # 输出该股票60日PE/PB曲线
python akshare_query.py --backtest --rules macd_golden  # 跑回测
python akshare_query.py --batch stocks.csv        # 批量分析，CSV首列为stock_code
python akshare_query.py 600519 --output report.json    # 结果输出到JSON文件
```

### `stocks.csv` 示例

```csv
stock_code
600519
000858
300750
002594
```

---

## 配置文件（`PEER_GROUPS`）

```python
PEER_GROUPS = {
    '半导体': ['688981','002371','603986','688012','300782','600703','002049'],
    '存储芯片/NAND': ['001309','600171','603986','300688','002049','300474','688234','300042'],
    '白酒': ['600519','000858','000568','002304','600809','603369','600702'],
    '锂电池': ['300750','002594','002460','600516','300014','603799','002709'],
    '券商': ['600030','300059','000776','601688','601211','600999','002797'],
    # ... 其他行业
}
```

> 可直接编辑 `PEER_GROUPS` 字典更新分组。

---

## 八步风控及核心原理说明（金融专用）

1. **多点熔断**：多数据源时序交叉，任一数据源失效不影响整体产出。
2. **客观信号取代主观结论**：移除 `suggestion`，使用 `signals` 字典，规避投顾合规风险。
3. **风险透明化**：暴露 `risk_warning` 字段（含解禁/减持/评级极速下滑预警）。
4. **估值约束**：PE/PB 超50%偏差自动拒绝并 reload 备用源；PEG 设基期保护 cap=300%。
5. **工程回测**：回测功能验证一个客观信号在历史上的是否产生超额收益。
6. **趋势持久化**：SQLite 存放短期指标序，可判断“北向连续5日流入”等趋势性信号。
7. **降级可用**：任一数据源（pytdx / AKShare / Baostock / 新浪）整源挂掉仍能提供不完整结论。
8. **缓存分层**：财报 & 行业排名 24h 缓存，资金流向 10min 缓存，避免封 IP。

---

## 已知问题（后续优化）

- AKShare 数据源本身可能因上游网站改版而失效；建议锁定 AKShare 版本并定期测试。
- 部分 Baostock 数据可能有延迟或缺失。
- 消息面目前仅提供基础新闻采集，尚未集成NLP情感打分。
- 回测模块只支持内置信号规则，尚未开放复杂的自定义策略组合。

---
