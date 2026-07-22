# -*- coding: utf-8 -*-
"""数据库初始化 + 建表 + 种子数据。

SQLite 单文件，位于 data/fundsight.db。
表：fund_list（搜索用全量列表）/ fund_quote（行情缓存）/ holding（自选+持仓+预期）
    / user（账号）/ session（登录会话）
"""
import os
import sqlite3

_DEFAULT_DB_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "fundsight.db")
)
# 部署可用 FUNDSIGHT_DB 指定持久化路径；未设置则落在仓库 data/ 下。
DB_PATH = os.path.abspath(os.environ.get("FUNDSIGHT_DB") or _DEFAULT_DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS fund_list (
    fund_code   TEXT PRIMARY KEY,
    name        TEXT,
    pinyin      TEXT,
    fund_type   TEXT,
    synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS fund_quote (
    fund_code   TEXT PRIMARY KEY,
    name        TEXT,
    dwjz        REAL,   -- 昨日单位净值
    gsz         REAL,   -- 盘中估算净值
    gszzl       REAL,   -- 盘中估算涨跌幅 %
    gztime      TEXT,   -- 估值时间
    nav         REAL,   -- 最新官方单位净值（收盘回填，与 dwjz/估值分离）
    nav_date    TEXT,   -- 官方净值日期（收盘后回填）
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS holding (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER DEFAULT 0,
    fund_code     TEXT NOT NULL,
    hold_amount   REAL,   -- 当前持仓金额
    cost_amount   REAL,   -- 买入成本（可选）
    target_rate   REAL,   -- 预期收益率 %
    target_price  REAL,   -- 目标净值
    stop_profit   REAL,   -- 止盈线 %
    stop_loss     REAL,   -- 止损线 %
    trailing_stop_pct REAL,  -- 移动止盈回撤 %(PRD-07,从 peak_nav 回撤触发)
    peak_nav    REAL,   -- 持仓期最高净值(scheduler 日更,只增不减)
    created_at    TEXT
);

-- 历史净值序列：走势图用，抓取层日更写入，业务层只读
CREATE TABLE IF NOT EXISTS fund_nav_history (
    fund_code         TEXT NOT NULL,
    nav_date         TEXT NOT NULL,   -- YYYY-MM-DD
    nav              REAL,            -- 单位净值（分红日会断崖跳跌）
    equity_return    REAL,            -- 单位净值口径当日涨跌幅 %（分红日假大跌）
    nav_adj          REAL,            -- 累计净值（后复权，分红日不跳变；PRD-02）
    equity_return_adj REAL,           -- 复权口径当日涨跌幅 %（消除分红假大跌；PRD-02）
    PRIMARY KEY (fund_code, nav_date)
);

-- 基金基本面：详情页用（经理/规模/近期收益率/费率），抓取层低频写入，业务层只读（M8-B）
CREATE TABLE IF NOT EXISTS fund_profile (
    fund_code  TEXT PRIMARY KEY,
    name       TEXT,   -- 基金全称（fS_name）
    manager    TEXT,   -- 现任基金经理
    scale      REAL,   -- 最新规模（亿元）
    rate       TEXT,   -- 管理费率（原样保留字符串，如 "1.50%"）
    syl_1n     REAL,   -- pingzhongdata 原字段：近1年收益率 %
    syl_3y     REAL,   -- pingzhongdata 原字段：近3年收益率 %
    syl_6y     REAL,   -- pingzhongdata 原字段：成立以来收益率 %
    syl_1y     REAL,   -- pingzhongdata 原字段：近1月收益率 %
    asset_alloc_stock REAL,  -- 最新一期股票占净比 %（PRD-05，Data_assetAllocation）
    asset_alloc_bond  REAL,  -- 最新一期债券占净比 %
    asset_alloc_cash  REAL,  -- 最新一期现金占净比 %
    holder_inst       REAL,  -- 最新一期机构持有比例 %（Data_holderStructure）
    holder_retail     REAL,  -- 最新一期个人持有比例 %
    peer_percentile   REAL,  -- 最新同类百分位 %（PRD-06，Data_rateInSimilarPersent，越大越靠前）
    peer_rank          INTEGER,  -- 最新同类排名（Data_rateInSimilarType 最新 y）
    peer_total         INTEGER,  -- 同类总数（Data_rateInSimilarType 最新 sc）
    updated_at TEXT
);

-- 用户体系：账号 + 会话。密码用 pbkdf2 加盐哈希（见 backend/auth.py），零第三方依赖。
CREATE TABLE IF NOT EXISTS user (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT UNIQUE NOT NULL,
    pwd_hash   TEXT NOT NULL,   -- pbkdf2_hmac(sha256) 十六进制
    pwd_salt   TEXT NOT NULL,   -- 每用户随机盐（十六进制）
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS session (
    token      TEXT PRIMARY KEY,   -- secrets.token_urlsafe
    user_id    INTEGER NOT NULL,
    created_at TEXT,
    expires_at TEXT                 -- datetime，过期即失效
);

-- 搜索索引：全量同步后 fund_list 达 2.7 万行，为名称/拼音匹配与排序兜底
CREATE INDEX IF NOT EXISTS idx_fund_list_name ON fund_list(name);
CREATE INDEX IF NOT EXISTS idx_fund_list_pinyin ON fund_list(pinyin);
-- 自选按用户隔离，加索引加速 WHERE user_id=? 过滤
CREATE INDEX IF NOT EXISTS idx_holding_user ON holding(user_id);

-- 交易流水：买卖记录，持仓由 compute_position() 对流水加权推导，不单独存持仓表
CREATE TABLE IF NOT EXISTS fund_transaction (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER DEFAULT 0,
    fund_code   TEXT NOT NULL,
    action      TEXT,    -- buy | sell
    shares      REAL,
    price       REAL,
    amount      REAL,
    trade_date  TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_fund_transaction_user_code
    ON fund_transaction(user_id, fund_code);

-- 抓取任务执行记录:可观测性用,各 _safe_* 包装器写入,业务层只读(M9-A)
CREATE TABLE IF NOT EXISTS task_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name   TEXT NOT NULL,    -- fund_list_sync | nav_refresh | quote_refresh | history_refresh | profile_refresh
    started_at  TEXT,             -- datetime('now','localtime')
    finished_at TEXT,
    duration_ms INTEGER,          -- 耗时(毫秒)
    status      TEXT,             -- ok | fail
    affected    INTEGER,          -- 成功处理的条数(各 sync/refresh 的返回值)
    error       TEXT              -- 失败时 "ExcType: msg",成功为 NULL
);
CREATE INDEX IF NOT EXISTS idx_task_run_name_time ON task_run(task_name, started_at);

-- 站内通知:后台巡检(净值断点等)发现异常时,推送给相关持仓用户(M9-D)。
-- kind = nav_gap | stop_profit | stop_loss;read_at 为 NULL 表示未读。
CREATE TABLE IF NOT EXISTS notification (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    fund_code   TEXT,
    kind        TEXT NOT NULL,    -- nav_gap | stop_profit | stop_loss
    message     TEXT,
    created_at  TEXT,
    read_at     TEXT              -- datetime('now','localtime'),NULL=未读
);
CREATE INDEX IF NOT EXISTS idx_notification_user_read ON notification(user_id, read_at);

-- M10B 鉴权加固:登录审计 + 接口限流状态。
-- login_audit:记录登录成功/失败(user_id/ip/ua/ok/created_at),按 user_id 隔离只读。
-- rate_limit_state:限流计数落盘兜底,防重启重置(内存计数为主)。各窗口结束由
-- start_rate_limit_cleanup 日更清理,防膨胀。
CREATE TABLE IF NOT EXISTS login_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,              -- 失败且未知用户时 NULL
    ip          TEXT,
    ua          TEXT,
    ok          INTEGER NOT NULL,     -- 1 成功 / 0 失败
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_audit_user ON login_audit(user_id, id);
CREATE TABLE IF NOT EXISTS rate_limit_state (
    user_id      INTEGER NOT NULL,
    endpoint     TEXT NOT NULL,
    window_start TEXT NOT NULL,       -- 窗口起点(epoch 整数的文本),便于清理时比较
    count        INTEGER NOT NULL,
    PRIMARY KEY (user_id, endpoint, window_start)
);

-- 盘中估值时序:每基金每采样时刻一行,按 quote_date 自然分区。
-- fund_quote 只存最新快照(画不出折线),本表追加今日逐点采样供详情页画
-- 「今日实时涨幅」折线。今日数据保留到次日被新 quote_date 覆盖;7 天前
-- 旧数据由 scheduler.start_tick_purge 清理防膨胀。
CREATE TABLE IF NOT EXISTS fund_quote_tick (
    fund_code   TEXT NOT NULL,
    quote_date  TEXT NOT NULL,   -- YYYY-MM-DD(交易日,本地采样日期)
    quote_time  TEXT NOT NULL,   -- HH:MM:SS 本地采样时刻
    gsz         REAL,             -- 盘中估算净值
    gszzl       REAL,             -- 盘中估算涨跌幅 %(折线纵轴)
    dwjz        REAL,             -- 昨日单位净值
    gztime      TEXT,             -- 数据源原始估值时间
    PRIMARY KEY (fund_code, quote_date, quote_time)
);
CREATE INDEX IF NOT EXISTS idx_quote_tick_date ON fund_quote_tick(quote_date);

-- 定投计划:用户设定每月/周定投,到点站内提醒(PRD-04 P1)
CREATE TABLE IF NOT EXISTS dca_plan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    fund_code   TEXT NOT NULL,
    per_amount  REAL NOT NULL,
    freq        TEXT NOT NULL,        -- monthly | biweekly | weekly
    invest_day  INTEGER NOT NULL,     -- 每月几号(1-28)或每周几(0-6)
    next_date   TEXT NOT NULL,        -- 下次触发日(YYYY-MM-DD)
    active      INTEGER DEFAULT 1,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_dca_plan_user ON dca_plan(user_id, active);

-- 大盘指数快照:上证/深证/创业板/沪深300 等,抓取层低频写入,业务层只读(P1a)。
-- 供首页/市场/持仓页顶部「大盘指数条」一眼看行情。盘中 60s 刷新、收盘定格最新收盘价。
CREATE TABLE IF NOT EXISTS market_index (
    code        TEXT PRIMARY KEY,   -- 指数代码(如 000001 上证指数)
    name        TEXT,
    price       REAL,               -- 最新点位
    change      REAL,               -- 涨跌额
    change_pct  REAL,               -- 涨跌幅 %
    updated_at  TEXT
);

-- 基金排行榜:6 大类 × 5 区间 的 topN 榜单,抓取层日更写入,业务层只读(P1b)。
-- 对标天天基金「基金排行」逛入口。每(period,category)组先删后插,rank 为榜内名次。
CREATE TABLE IF NOT EXISTS fund_rank (
    period      TEXT NOT NULL,      -- 1m|3m|6m|1y|ytd(榜单排序区间)
    category    TEXT NOT NULL,      -- all|gp|hh|zs|zq|qdii(基金大类)
    rank        INTEGER NOT NULL,   -- 榜内名次(1 起)
    fund_code   TEXT NOT NULL,
    name        TEXT,
    nav_date    TEXT,
    nav         REAL,               -- 单位净值
    r_1m        REAL,               -- 近1月收益 %
    r_3m        REAL,               -- 近3月收益 %
    r_6m        REAL,               -- 近6月收益 %
    r_1y        REAL,               -- 近1年收益 %
    r_ytd       REAL,               -- 今年来收益 %
    updated_at  TEXT,
    PRIMARY KEY (period, category, fund_code)
);
CREATE INDEX IF NOT EXISTS idx_fund_rank_lookup ON fund_rank(period, category, rank);

-- 基金重仓股 Top10:详情页 F10 深度用,抓取层日更/首访兜底写入,业务层只读(P2)。
-- 数据源 F10 jjcc(季度持仓明细),每基金先删后插最新一期 Top10。
CREATE TABLE IF NOT EXISTS fund_holding_stock (
    fund_code     TEXT NOT NULL,
    rank          INTEGER NOT NULL,   -- 持仓名次(1 起)
    stock_code    TEXT NOT NULL,
    stock_name    TEXT,
    weight        REAL,               -- 占净值比例 %
    report_period TEXT,               -- 报告期,如 "2026年2季度"
    updated_at    TEXT,
    PRIMARY KEY (fund_code, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_holding_stock_fund ON fund_holding_stock(fund_code, rank);
"""

# 种子数据：开发期用，部署后由 fund_list_sync.py 拉全量覆盖
# (代码, 名称, 拼音首字母, 类型)
SEED_FUNDS = [
    ("020608", "南方中证机器人ETF发起联接C", "nfzzjqrETFfqljC", "指数"),
    ("005827", "易方达蓝筹精选混合", "yfdlcjxhh", "混合"),
    ("000001", "华夏成长混合", "hxczhh", "混合"),
    ("161725", "招商中证白酒指数", "zszzbjzs", "指数"),
    ("110022", "易方达消费行业股票", "yfdxfhygp", "股票"),
    ("003096", "中欧医疗健康混合C", "zoyljkhhC", "混合"),
    ("001594", "天弘中证银行ETF联接C", "thzzyhETFljC", "指数"),
    ("012348", "天弘恒生科技指数C", "thhskjzsC", "QDII"),
    ("270042", "广发纳斯达克100指数", "gfnsdk100zs", "QDII"),
    ("161005", "富国天惠成长混合", "fgthczhh", "混合"),
    ("519674", "银河创新成长混合", "yhcxczhh", "混合"),
    ("008888", "华夏国证半导体芯片ETF联接C", "hxgzbdtxpETFljC", "指数"),
    ("001102", "前海开源国家比较优势混合", "qhkygjbjysHH", "混合"),
    ("400015", "东方新能源汽车混合", "dfxnyqchh", "混合"),
    ("002190", "农银新能源主题", "nyxnyzt", "股票"),
]


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn):
    """幂等迁移：为已存在的旧库补齐新增列（SQLite 无 ADD COLUMN IF NOT EXISTS）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fund_quote)")}
    if "nav" not in cols:
        conn.execute("ALTER TABLE fund_quote ADD COLUMN nav REAL")

    # fund_nav_history 加列：equity_return（当日涨跌幅 %，涨跌柱用）——M8-B
    hist_cols = {r[1] for r in conn.execute("PRAGMA table_info(fund_nav_history)")}
    if "equity_return" not in hist_cols:
        conn.execute("ALTER TABLE fund_nav_history ADD COLUMN equity_return REAL")
    # PRD-02 分红复权：累计净值（后复权）+ 复权涨跌幅，消除分红日假大跌
    if "nav_adj" not in hist_cols:
        conn.execute("ALTER TABLE fund_nav_history ADD COLUMN nav_adj REAL")
    if "equity_return_adj" not in hist_cols:
        conn.execute("ALTER TABLE fund_nav_history ADD COLUMN equity_return_adj REAL")

    # PRD-05 基本面深化：资产配置(股/债/现金占净比) + 持有人结构(机构/个人)
    prof_cols = {r[1] for r in conn.execute("PRAGMA table_info(fund_profile)")}
    for col in ("asset_alloc_stock", "asset_alloc_bond", "asset_alloc_cash",
               "holder_inst", "holder_retail"):
        if col not in prof_cols:
            conn.execute(f"ALTER TABLE fund_profile ADD COLUMN {col} REAL")
    # PRD-06 同类百分位 + 排名 + 总数
    if "peer_percentile" not in prof_cols:
        conn.execute("ALTER TABLE fund_profile ADD COLUMN peer_percentile REAL")
    if "peer_rank" not in prof_cols:
        conn.execute("ALTER TABLE fund_profile ADD COLUMN peer_rank INTEGER")
    if "peer_total" not in prof_cols:
        conn.execute("ALTER TABLE fund_profile ADD COLUMN peer_total INTEGER")
    # PRD-07 移动止盈:holding 加 trailing_stop_pct / peak_nav
    hold_cols = {r[1] for r in conn.execute("PRAGMA table_info(holding)")}
    if "trailing_stop_pct" not in hold_cols:
        conn.execute("ALTER TABLE holding ADD COLUMN trailing_stop_pct REAL")
    if "peak_nav" not in hold_cols:
        conn.execute("ALTER TABLE holding ADD COLUMN peak_nav REAL")


def init_db(with_seed=True):
    conn = get_conn()
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    if with_seed:
        cur = conn.execute("SELECT COUNT(*) AS n FROM fund_list")
        if cur.fetchone()["n"] == 0:
            conn.executemany(
                "INSERT INTO fund_list(fund_code,name,pinyin,fund_type,synced_at) "
                "VALUES (?,?,?,?,datetime('now','localtime'))",
                SEED_FUNDS,
            )
            print(f"已写入 {len(SEED_FUNDS)} 只种子基金")
    conn.commit()
    conn.close()
    print(f"数据库就绪: {DB_PATH}")


if __name__ == "__main__":
    init_db()
