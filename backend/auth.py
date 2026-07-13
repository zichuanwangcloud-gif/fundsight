# -*- coding: utf-8 -*-
"""用户体系鉴权工具 —— 纯标准库，零依赖。

密码用 hashlib.pbkdf2_hmac(sha256) 加随机盐哈希存储，绝不明文落库；
会话 token 存 session 表（非内存），服务重启不掉登录。
"""
import hashlib
import hmac
import secrets

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
