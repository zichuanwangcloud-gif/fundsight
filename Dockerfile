# 盈见 FundSight —— 容器镜像
#
# 核心运行时零第三方依赖,仅需 Python 标准库,因此镜像只基于 python:slim,
# 不执行任何 pip install。data/ 通过卷持久化,全量列表/持仓不随容器销毁丢失。
FROM python:3.10-slim

WORKDIR /app

# 仅拷贝运行所需的后端与前端(.dockerignore 已排除 .git/tests/docs 等)
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/

# 时区设为上海,保证 datetime('now','localtime') 与 gztime 口径一致
ENV TZ=Asia/Shanghai
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["python3", "-m", "backend.app"]
