# -*- coding: utf-8 -*-
"""用户体系鉴权工具 —— 纯标准库，零依赖。

密码用 hashlib.pbkdf2_hmac(sha256) 加随机盐哈希存储，绝不明文落库；
会话 token 存 session 表（非内存），服务重启不掉登录。
"""
import hashlib
import hmac
import secrets
import threading
import time

from backend.models.db import get_conn

_PBKDF2_ROUNDS = 100_000


class UsernameTaken(Exception):
    """注册时用户名已被占用。"""


def hash_password(pwd, salt=None):
    """返回 (hash_hex, salt_hex)。salt 为空时随机生成 16 字节盐。"""
    if salt is None:
        salt = secrets.token_bytes(16)
    elif isinstance(salt, str):
        salt = bytes.fromhex(salt)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return dk.hex(), salt.hex()


def verify_password(pwd, salt_hex, hash_hex):
    """常数时间比较，防时序侧信道。"""
    calc, _ = hash_password(pwd, salt_hex)
    return hmac.compare_digest(calc, hash_hex)


def create_user(username, password):
    """创建账号 → 返回 user_id。用户名占用抛 UsernameTaken。

    首个用户注册后，继承历史「全局共享」自选（user_id=0）——见迁移约定。
    """
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("用户名和密码不能为空")
    h, salt = hash_password(password)
    conn = get_conn()
    try:
        if conn.execute("SELECT 1 FROM user WHERE username=?", (username,)).fetchone():
            raise UsernameTaken(username)
        cur = conn.execute(
            "INSERT INTO user(username,pwd_hash,pwd_salt,created_at) "
            "VALUES (?,?,?,datetime('now','localtime'))",
            (username, h, salt),
        )
        uid = cur.lastrowid
        # 首个账号：把存量全局自选（user_id=0）迁移给它
        n = conn.execute("SELECT COUNT(*) AS n FROM user").fetchone()["n"]
        if n == 1:
            conn.execute("UPDATE holding SET user_id=? WHERE user_id=0", (uid,))
        conn.commit()
        return uid
    finally:
        conn.close()


def authenticate(username, password):
    """校验账号密码 → user_id | None。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id,pwd_hash,pwd_salt FROM user WHERE username=?",
            ((username or "").strip(),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if verify_password(password, row["pwd_salt"], row["pwd_hash"]):
        return row["id"]
    return None


def create_session(user_id, ttl_days=30):
    """签发会话 token 并落库，返回 token。"""
    token = secrets.token_urlsafe(32)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO session(token,user_id,created_at,expires_at) "
            "VALUES (?,?,datetime('now','localtime'),datetime('now','localtime',?))",
            (token, user_id, f"+{int(ttl_days)} days"),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def get_user_by_token(token):
    """有效且未过期的 token → user_id；否则 None。"""
    if not token:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT user_id FROM session "
            "WHERE token=? AND expires_at > datetime('now','localtime')",
            (token,),
        ).fetchone()
    finally:
        conn.close()
    return row["user_id"] if row else None


def get_username(user_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT username FROM user WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    return row["username"] if row else None


def delete_session(token):
    if not token:
        return
    conn = get_conn()
    try:
        conn.execute("DELETE FROM session WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()


def purge_expired_sessions():
    """删除所有已过期 session(expires_at <= now)。返回删除条数。

    防 session 表无限膨胀:登录签发的 token 默认 30 天有效,过期后不再
    被使用(get_user_by_token 已判过期),但行不会自动消失。由 scheduler
    日更清理。
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM session WHERE expires_at <= datetime('now','localtime')"
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ======================== M10B 鉴权加固 ========================

# 接口限流:自用级宽松阈值,60 次/分钟(按 user_id + 端点 隔离)。
RATE_LIMIT = 60
RATE_WINDOW_SEC = 60
# 内存计数为主(快),rate_limit_state 表落盘兜底(防重启重置)。
# key=(user_id, endpoint) -> [window_epoch, count]
_rate_cache = {}


def check_rate_limit(user_id, endpoint, limit=RATE_LIMIT, window_sec=RATE_WINDOW_SEC):
    """是否放行本次请求。超限返回 False(由调用方回 429)。

    内存计数为主,每个窗口(默认 60 秒)内累计;同窗口内若内存丢失(如重启),
    从 rate_limit_state 表取较大值兜底,避免重启即重置。
    """
    if user_id is None:
        return True
    window = int(time.time() // window_sec)
    key = (user_id, endpoint)
    cached = _rate_cache.get(key)
    if cached and cached[0] == window:
        count = cached[1] + 1
    else:
        count = 1  # 新窗口重置
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT count FROM rate_limit_state "
            "WHERE user_id=? AND endpoint=? AND window_start=?",
            (user_id, endpoint, str(window)),
        ).fetchone()
        db_count = row["count"] if row else 0
        # 取内存与库的较大值 +1,保证不因单源丢失而放行超额
        count = max(count, db_count + 1)
        conn.execute(
            "INSERT OR REPLACE INTO rate_limit_state(user_id,endpoint,window_start,count) "
            "VALUES(?,?,?,?)",
            (user_id, endpoint, str(window), count),
        )
        conn.commit()
    finally:
        conn.close()
    _rate_cache[key] = [window, count]
    return count <= limit


def revoke_user_sessions(user_id):
    """吊销该用户**所有**存量 session(改密/登出时调用)。返回删除条数。

    之前登出只删当前 token;M10B 起,改密或登出使该用户全设备下线。
    """
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM session WHERE user_id=?", (user_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_user_id(username):
    """按用户名查 user_id(登录失败审计用:未知用户返回 None)。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM user WHERE username=?", ((username or "").strip(),)
        ).fetchone()
    finally:
        conn.close()
    return row["id"] if row else None


def set_password(user_id, new_password):
    """直接重设密码(已通过身份校验后调用)。"""
    if not new_password:
        raise ValueError("密码不能为空")
    h, salt = hash_password(new_password)
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE user SET pwd_hash=?,pwd_salt=? WHERE id=?",
            (h, salt, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def change_password(user_id, old_password, new_password):
    """改密:校验旧密码 → 重设 → 吊销该用户所有存量 session。

    返回 True 成功;旧密码不匹配返回 False(不改不吊销)。当前设备由调用方
    重新签发 session 保持登录。
    """
    if not new_password:
        return False
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT pwd_hash,pwd_salt FROM user WHERE id=?", (user_id,)
        ).fetchone()
        if not row or not verify_password(old_password, row["pwd_salt"], row["pwd_hash"]):
            return False
        h, salt = hash_password(new_password)
        conn.execute(
            "UPDATE user SET pwd_hash=?,pwd_salt=? WHERE id=?",
            (h, salt, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    # 改密成功 → 该用户所有存量 session 失效(B2)
    revoke_user_sessions(user_id)
    return True


def record_login_audit(user_id, ip, ua, ok):
    """落 login_audit:成功/失败均记录(user_id 未知时为 NULL)。"""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO login_audit(user_id,ip,ua,ok,created_at) "
            "VALUES(?,?,?,?,datetime('now','localtime'))",
            (user_id, ip, ua, 1 if ok else 0),
        )
        conn.commit()
    finally:
        conn.close()


def purge_stale_rate_limit(window_sec=RATE_WINDOW_SEC):
    """清理已结束窗口的 rate_limit_state 行,防膨胀。返回删除条数。"""
    now_window = int(time.time() // window_sec)
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM rate_limit_state WHERE CAST(window_start AS INTEGER) < ?",
            (now_window,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def start_rate_limit_cleanup(interval_hours=24, run_now=True):
    """启动限流状态清理 daemon(日更),返回该线程。

    rate_limit_state 每个窗口一行,窗口结束后即过期;日更清理防表膨胀。
    全程吞异常,失败仅静默(不影响主服务)。
    """
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            try:
                purge_stale_rate_limit()
            except Exception:
                pass
        while True:
            time.sleep(interval)
            try:
                purge_stale_rate_limit()
            except Exception:
                pass

    t = threading.Thread(target=_loop, name="rate-limit-cleanup", daemon=True)
    t.start()
    return t
