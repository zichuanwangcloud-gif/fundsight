# 盈见 FundSight

> 预见收益 —— 一个自用的网页版基金看板

天天基金给你看**现状**，盈见让你看**现状 vs 你的预期**。
录入自选基金、持仓和预期目标，每天一眼看到：当日涨幅、真实盈亏、离目标还差多少。

## 状态

- 数据源已实测通过（2026-07-02，编号 020608），立项判断 **GO**
- 当前阶段：M0 数据 spike ✅ 完成 → 准备进入 M1 单人版

## 文档

- [立项文档](docs/01-立项文档.md) —— 定位、MVP、风险、里程碑
- [数据源调研](docs/02-数据源调研.md) —— 为什么用 fundgz + AKShare
- [架构设计](docs/03-架构设计.md) —— 分层、数据模型、计算逻辑、合规边界

## 快速验证数据链路

```bash
python3 scripts/fund_spike.py
```

会拉取一只真实基金，打印当日涨幅、盘中估值，并演示盈亏计算。

## 技术选型

Python（数据 + API）· SQLite（缓存 + 存储）· 单页前端 · 先本地跑

## 合规提醒

数据来自非公开接口，**仅自用私享、低频、不商业化**。公开/收费前须重新评估合规。
# fundsight

## 本地运行 / 测试

### 依赖

核心运行时**零依赖**，仅使用 Python 3 标准库（`http.server` / `sqlite3` /
`urllib` / `json` / `ssl`）。可选依赖（如收盘净值回填用的 `akshare`）见
`requirements.txt` 中的说明注释。

### 启动服务

```bash
./scripts/run.sh            # 默认监听 8000 端口
PORT=9000 ./scripts/run.sh  # 通过环境变量覆盖端口
```

首次启动会自动建表并写入 15 只种子基金到 `data/fundsight.db`，随后打开
浏览器访问 `http://localhost:8000` 即可看到前端页面。

### 运行测试

```bash
./scripts/test.sh
# 等价于：
python3 -m unittest discover -s tests -v
```

测试位于 `tests/`，全部基于标准库 `unittest`：

- `tests/test_db.py` —— 建表、种子数据、`get_conn` 查询
- `tests/test_fundgz.py` —— `_f()` 数字转换边界、离线样本报文解析
  （不发起真实网络请求）、`refresh_quotes` 写入逻辑
- `tests/test_calc.py` —— 今日浮动盈亏 / 距目标净值 的核心计算约定

测试使用临时数据库文件或内存 SQLite，不会污染真实的
`data/fundsight.db`。
