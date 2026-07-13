# 盈见 FundSight —— 协作规范

## Git 规范

### 🚫 所有改码任务必须新建 worktree——严禁动主仓库

- **铁律**：凡是修改代码/配置/文档的任务，**必须在新建的 git worktree 中完成**，决不允许在主仓库（`/opt/fundsight` 主工作树）里改任何文件。
- **决不允许**在主仓库执行 `git checkout <分支>` 切换分支、`git reset`、`git stash` 覆盖、`git restore` 等会改动主工作树的操作——主仓库永远停在固定基线（默认 `main` / origin 最新），只读、只 pull。
- 之所以如此：主仓库是共享基线，多线路并行时若有人切分支会把他人未提交的改动覆盖或污染（已发生过：M10B 改动被并行线路 checkout 覆盖丢失，M10C 未跟踪文件污染主工作树导致测试套件假红）。
- 落地姿势：
  1. 开工前 `git fetch origin main` 拉最新基线。
  2. 用 `EnterWorktree`（或 `git worktree add`）从 `origin/main` 切出独立 worktree + 功能分支（如 `feat/xxx`、`fix/xxx`）。
  3. 在 worktree 内改码、自测、提交，`push` 该功能分支。
  4. 开 PR，评审通过后合并回 `main`；worktree 用完即清理。
- **唯一例外**：纯只读操作（`git status` / `git log` / `grep` / 跑测试观察）可在主仓库进行，但**绝不出** `git checkout` / 改文件。

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

- **所有**改码任务一律在独立 worktree 完成（见上方铁律），不限于"并行"场景——单人单任务也必须建 worktree，杜绝污染主仓库。
- 拆分并行任务时，保证各 worktree 修改的**文件集不相交**，避免合并冲突。
- 每个 worktree 完成后先自测、提交到独立分支，经 review 再合并；用完用 `ExitWorktree` 或 `git worktree remove` 清理。

## 项目约定

- **零第三方依赖优先**：后端用 Python 标准库（`http.server` + `sqlite3`）；akshare 等为可选依赖，缺失时须优雅降级不崩。
- **抓取层是唯一对外接口**：外部数据请求收敛在 `backend/datasource/`，业务层只读 SQLite 缓存，绝不高频轮询。
- **合规红线**：数据源仅自用私享、低频、不商业化；公开/收费前须重新评估合规。

## 运行 / 测试

- 启动：`bash scripts/run.sh`（或 `python3 -m backend.app`，默认端口 8000，`PORT` 可覆盖）。
- 测试：`bash scripts/test.sh`（或 `python3 -m unittest discover -s tests`）。
