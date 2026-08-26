# AgentBC 双机 Git 工作规范

> 生效日期：2026-08-27  
> 适用范围：AgentBC Mac mini 开发机、MacBook 发布机，以及 Codex、Claude、Hermes 协作分支  
> 权威公开基线：GitHub `main`  
> 权威私有集成分支：`private/integration`

## 1. 目标

本规范固定 AgentBC 的双机职责、远端命名、分支集合、worktree 生命周期和发布路径，避免再次出现：

- 把“Mac mini 禁止向公开 `main` push”误实现成禁止本地 fetch/pull；
- 为同一提交创建 `private-integration`、`heads/private/integration`、`internal/*` 等别名引用；
- 已完成的任务分支、测试分支或 worktree 长期残留；
- Git 客户端打开不同仓库时，把公开工作仓库和私有裸仓库误认为同一个引用空间；
- 清理 worktree 时误删仍需保留的 integration 或 Agent 分支。

## 2. 双机职责

### 2.1 Mac mini：开发与集成主机

Mac mini 负责：

- 从公开 GitHub fetch/pull `main` 和 tags；
- 运行 integration、三个 Executor 分支、测试、Runner 和 AgentBC 任务；
- 审阅 Agent 结果并合入 `private/integration`；
- 仅向 MacBook 私有裸仓库推送 `private/integration` 与 `agent/*`；
- 构建候选包和执行开发期验证。

Mac mini 禁止：

- 向 GitHub `main`、任何公开分支或 tag push；
- 创建 GitHub Release、上传 PyPI 或执行正式 Homebrew 发布；
- force-push、删除 MacBook 私有远端分支或覆盖已发布 tag；
- 使用 `reference-transaction` 等本地引用钩子阻止 fetch、pull、tag 获取或本地 `main` 快进。

Mac mini 的保护只能放在 `pre-push`：它检查目标 remote 和待推送 ref，不得拦截本地引用更新。

### 2.2 MacBook：发布与公开写入主机

MacBook 负责：

- 接收 Mac mini 推送到私有裸仓库的 integration/Agent 分支；
- 在公开工作仓库中审阅、测试和准备正式发布；
- 向 GitHub `main`、tags 和 Release 执行唯一合法的公开写入；
- 执行 PyPI、Homebrew Formula/bottle 与最终双机发布验收。

MacBook 的公开工作仓库和私有裸仓库是两个不同 Git 仓库：

```text
公开工作仓库：/Users/rowaywang/Documents/Work/Agent-Bridge-Connect/agent-bridge-connect
私有裸仓库：  /Users/rowaywang/Documents/Work/Agent-Bridge-Connect/agent-bridge-connect.git
```

公开工作仓库使用固定 remote：

- `origin`：GitHub 公共仓库；
- `private`：同机私有裸仓库。

`private` 的 fetch refspec 只允许 `private/integration` 和 `agent/*`，不得获取私有裸仓库的
`main` 形成第二个可见 main。Mac mini 的 `origin` 使用相同的私有 refspec；Mac mini 的
`public` 才是公开 `main` 与 tags 的读取来源。

不得为同一私有仓库重复创建 `internal`、`private-origin`、`agentbc-release`、`flow-contract`
或其他一次性 remote。

## 3. 唯一允许的长期分支

长期分支只允许以下五条：

```text
main
private/integration
agent/claude
agent/codex
agent/hermes
```

语义固定：

- `main`：公开、已发布或待发布的唯一产品历史；
- `private/integration`：私有开发集成和验收主线，必须以最新公开 `main` 为祖先；
- `agent/*`：三个 Executor 的稳定协作入口，空闲时必须与 integration 同步或明确落后，不能承载
  已经合入但未清理的独立历史。

禁止私建引用分支，包括但不限于：

```text
private-integration
heads/private/integration
remotes/private/integration
internal/private-integration
private-origin/private-integration
flow-contract/*
agentbc-release/*
```

远端跟踪引用必须由固定 remote 自动生成，禁止用普通 branch 模拟 `refs/remotes/*`。

## 4. 日常同步流程

### 4.1 Mac mini 同步公开基线

```bash
git fetch public --prune --tags
git switch main
git merge --ff-only public/main
git switch private/integration
git merge --ff-only main
```

规则：

- fetch/pull 完全允许；禁止的是向公开 remote push；
- `main` 只能 fast-forward 到 `public/main`，不得在 Mac mini 上产生本地 main 提交；
- integration 与 main 若发生分叉，立即停止，先审计提交关系，禁止 merge/rebase 猜测修复；
- 同步前后均运行 `git status --short --branch` 和 `git worktree list`。

### 4.2 integration 或 Agent 分支缺失时恢复

先获取固定远端：

```bash
git fetch public --prune --tags
git fetch origin --prune
```

恢复顺序：

1. 优先从 `origin/private/integration` 恢复 integration；
2. 只有远端也不存在、且确认开始全新开发周期时，才从 `public/main` 新建 integration；
3. Agent 分支优先从对应 `origin/agent/*` 恢复；远端不存在时才从 integration 新建；
4. 恢复后必须验证 `public/main` 是 integration 的祖先，并检查 Agent 分支相对 integration 的
   ahead/behind；
5. 不得为绕过 branch/worktree 占用另建拼写变体或引用别名。

integration worktree 被删除但分支仍在时，只恢复 worktree：

```bash
git worktree prune
git worktree add /Users/wangroway/hermes-team/codex/AgentBC_Temp/agent-worktrees/integration private/integration
```

### 4.3 Agent 开发

- 派发前将目标 `agent/*` fast-forward 到 integration；
- Agent worktree 按任务需要创建，不以新分支名称替代固定 `agent/*`；
- Executor 不执行 push、merge 或 rebase；控制端负责验收与集成；
- 合入前检查 task/report/callback/RunLease、HEAD、diff、文件所有权和测试证据；
- 合入后将对应 Agent 分支重新同步到 integration；没有待执行任务时可以移除 Agent worktree，
  但不得因此删除长期 Agent 分支。

## 5. 临时分支和 worktree

原则上直接使用固定 Agent 分支，不创建 topic branch。确实因 release 隔离、故障注入或并行冲突
必须创建临时分支时：

- 名称必须带可追踪 Task ID 或明确用途，不得伪装成 remote/ref 命名空间；
- 创建前记录来源 commit、负责人、过期条件和对应 worktree；
- 不得用临时分支保存唯一验收证据，证据必须进入 task report、integration commit 或正式发布资产；
- 完成后先合入 `private/integration` 并验证提交可达；
- 合入 integration 后立即移除关联 worktree，并删除本地和私有远端临时分支；
- 测试故障注入 worktree 必须在测试结束时恢复或删除，不得跨版本保留；
- 删除前检查脏状态、未跟踪文件、唯一提交和运行进程，禁止直接 `--force` 丢弃未知内容。

每轮集成结束后的目标状态：

```text
长期分支：main、private/integration、agent/claude、agent/codex、agent/hermes
常驻 worktree：main、integration
临时分支/孤立 remote refs：0
临时测试 worktree：0
```

## 6. 合入和同步

标准顺序：

1. 验收目标 Agent 分支；
2. 在 integration 控制端按依赖顺序合入；
3. 运行定向测试、全量门禁和 `git diff --check`；
4. 将三个 Agent 分支 fast-forward 到 integration；
5. 仅向 MacBook 私有 `origin` 推送 integration 与 Agent 分支；
6. 删除已经完成的临时分支/worktree；
7. 复核两机 refs、worktrees、remote 和工作树状态。

Mac mini 的合法 push 范围仅为：

```text
refs/heads/private/integration
refs/heads/agent/claude
refs/heads/agent/codex
refs/heads/agent/hermes
```

删除私有远端分支、非 fast-forward 更新、公开 main/tag push 一律停止并由用户确认。

## 7. 发布流程

1. Mac mini 完成 integration 验收并推送私有分支到 MacBook；
2. MacBook 从固定 `private` remote 获取 integration，不创建一次性 remote 或引用别名；
3. MacBook 在公开工作仓库复验差异、测试、构建与 provenance；
4. 只有 MacBook 可以更新 GitHub `main`、创建不可变 tag/Release、上传 PyPI 和发布 Homebrew；
5. 发布后两机 fetch 最新公开 main/tags；Mac mini 只执行 fast-forward pull；
6. 新开发周期从新的公开 main 基线继续，旧 release/topic 引用立即删除。

## 8. 每次操作检查单

执行前：

- 当前机器角色正确；
- 当前仓库路径、remote URL、branch、HEAD 和工作树正确；
- 没有 active AgentBC 任务或仍占用目标 worktree 的进程；
- 目标操作属于 fetch/pull、私有 push 或公开发布中的哪一种已明确。

执行后：

- `git status --short --branch` 干净；
- `git worktree list --porcelain` 不含 prunable 或未知路径；
- `git for-each-ref` 只包含规范长期分支和有效 remote-tracking refs；
- integration 以公开 main 为祖先，Agent 分支与 integration 的 ahead/behind 符合预期；
- Mac mini fetch/pull 成功，公开 push 仍由 `pre-push` 拒绝；
- 临时分支、worktree、故障注入和孤立引用均已清零。
