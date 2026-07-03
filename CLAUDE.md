# 盈见 FundSight —— 协作规范

## Git 规范

### 🚫 禁止直接 push main 分支

- **绝不** `git push` 到 `main`（也不允许 `git push origin main`、`--force` 推 main）。
- 所有改动走「功能分支 → PR → 合并」流程：
  1. 从最新 `main` 切功能分支（如 `feat/xxx`、`fix/xxx`）。
  2. 在功能分支上提交，`push` 功能分支。
  3. 开 PR，评审通过后再合并回 `main`。
- 本地把改动 merge 进 `main` 是允许的（用于集成验证），但**合并后的 main 不得被 push**——推送只能通过 PR。
- 如需推送，先确认当前分支不是 `main`：`git rev-parse --abbrev-ref HEAD`。

### 提交信息

- 遵循 Conventional Commits：`feat:` / `fix:` / `test:` / `docs:` / `refactor:` / `chore:`。
- commit message 结尾附：
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

### 多 worktree 并行开发

- 拆分并行任务时，保证各 worktree 修改的**文件集不相交**，避免合并冲突。
- 每个 worktree 完成后先自测、提交到独立分支，经 review 再合并。

## 项目约定

- **零第三方依赖优先**：后端用 Python 标准库（`http.server` + `sqlite3`）；akshare 等为可选依赖，缺失时须优雅降级不崩。
- **抓取层是唯一对外接口**：外部数据请求收敛在 `backend/datasource/`，业务层只读 SQLite 缓存，绝不高频轮询。
- **合规红线**：数据源仅自用私享、低频、不商业化；公开/收费前须重新评估合规。

## 运行 / 测试

- 启动：`bash scripts/run.sh`（或 `python3 -m backend.app`，默认端口 8000，`PORT` 可覆盖）。
- 测试：`bash scripts/test.sh`（或 `python3 -m unittest discover -s tests`）。
