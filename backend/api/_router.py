# -*- coding: utf-8 -*-
"""极简路由基础设施(零依赖)—— 供后续线路(市场/详情/流水)注册新端点。

核心持仓/搜索/鉴权仍由 app.py 直接处理(与用户体系鉴权耦合);此路由表是
新功能模块的扩展点:各线路在自己的 api 模块导出 ROUTES,__init__ 汇总为
ALL_ROUTES,app.py 在核心分发之外尝试匹配。

- Ctx: 封装一次请求的 query / body / 路径参数 / 当前登录用户 user_id。
- match: 支持 {name} 占位的路径匹配,如 /api/fund/{code}。
- dispatch: 按 (method, pattern) 查表;命中调 handler。handler 返回 obj(默认 200)
  或 (code, obj);未命中返回 None(交回 app.py 走核心分发/静态/404)。
  需要登录的 handler 自行判断 ctx.user_id 是否为 None 并返回 401。
"""


class Ctx:
    def __init__(self, query=None, body=None, params=None, user_id=None):
        self.query = query or {}      # parse_qs 结果: {name: [values]}
        self.body = body or {}        # 已解析的 JSON dict
        self.params = params or {}    # 路径参数: {"code": "020608"}
        self.user_id = user_id        # 当前登录用户 id,未登录为 None

    def q(self, name, default=""):
        vals = self.query.get(name)
        return vals[0] if vals else default


def match(pattern, path):
    p_segs = pattern.strip("/").split("/")
    u_segs = path.strip("/").split("/")
    if len(p_segs) != len(u_segs):
        return None
    params = {}
    for p, u in zip(p_segs, u_segs):
        if p.startswith("{") and p.endswith("}"):
            params[p[1:-1]] = u
        elif p != u:
            return None
    return params


def dispatch(routes, method, path, query=None, body=None, user_id=None):
    """命中返回 (code, obj);未命中返回 None。"""
    for m, pattern, handler in routes:
        if m != method:
            continue
        params = match(pattern, path)
        if params is None:
            continue
        result = handler(Ctx(query=query, body=body, params=params, user_id=user_id))
        if isinstance(result, tuple) and len(result) == 2:
            return result
        return (200, result)
    return None
