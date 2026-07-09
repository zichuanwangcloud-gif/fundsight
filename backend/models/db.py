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
    created_at    TEXT
);

-- 历史净值序列：走势图用，抓取层日更写入，业务层只读
CREATE TABLE IF NOT EXISTS fund_nav_history (
    fund_code  TEXT NOT NULL,
    nav_date   TEXT NOT NULL,   -- YYYY-MM-DD
    nav        REAL,            -- 单位净值
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
