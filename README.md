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

## Docker 部署（推荐，常驻）

一键启动一个后台常驻、崩溃自愈的服务：

```bash
docker compose up -d --build   # 构建并后台启动
docker compose logs -f         # 查看日志（含全量列表同步进度）
docker compose down            # 停止（data/ 卷保留，自选持仓不丢）
```

启动后浏览器访问 `http://<本机IP>:8000`（同机可用 `http://localhost:8000`）。

- **首启自拉全量**：若基金列表仍是初始的 15 只种子数据，启动时会在后台
  自动从东财拉取全市场约 2.7 万只基金写入搜索库（拉取失败不影响服务，
  种子数据继续可用）。之后每 7 天自动刷新一次。
- **数据持久化**：SQLite 库通过 `./data` 卷挂载到宿主机，容器重建不丢数据。
  ⚠️ 若不用 compose 而是 `docker run`，务必挂载数据目录（`-v $PWD/data:/app/data`）
  或用 `FUNDSIGHT_DB` 指定持久路径，否则容器销毁后自选/账号全部丢失。
- **零第三方依赖**：镜像基于 `python:3.10-slim`，不执行任何 `pip install`。

## 用户体系（登录 + 数据隔离）

自选/持仓按登录用户隔离，登录后你的自选一直为你保留。

- **注册/登录**：首次打开显示登录门控，用「用户名 + 密码」自助注册即可。
  密码用 `hashlib.pbkdf2_hmac` 加盐哈希存储，绝不明文落库（`backend/auth.py`）。
- **会话**：登录签发的 token 存 `session` 表（非内存），服务重启不掉登录；
  token 放 HttpOnly Cookie。默认有效期 30 天。
- **隔离**：所有 `holding` 读写都带 `user_id`，越权访问他人自选不生效。
- **存量迁移**：升级前那份「全局共享」的自选（`user_id=0`）会自动迁移给
  **首个注册的账号**。
- **HTTPS 部署**：Cookie 目前未加 `Secure`（方便本地 http）；对外走 HTTPS 时
  应在 `backend/app.py` 的 `_session_cookie_header` 追加 `; Secure`。

> 可用 `FUNDSIGHT_DB=/abs/path/fundsight.db` 覆盖数据库落盘位置。


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
- `tests/test_auth.py` —— 密码哈希、账号、会话、存量迁移、自选数据隔离/越权防护

测试使用临时数据库文件或内存 SQLite，不会污染真实的
`data/fundsight.db`。
