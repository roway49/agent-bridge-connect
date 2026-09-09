# PERM-104-002 / FNJN-001 最终收口证据

日期：2026-09-09
分支：`agent/codex`
工作树：`/Users/wangroway/hermes-team/codex/AgentBC_Temp/agent-worktrees/codex`

本文件只记录本任务在 Artifact root 内产生的实现/回归证据，不替代 AgentBC Core execution report。

## Step 1：分支与历史保护

执行结果：

```text
HEAD = a02d451f7c4dc426915a4c889bc990d928ca1868
private/integration = 1cbf67a02a7091969fd851bf28a09d5b2425a9fd
git merge-base --is-ancestor private/integration HEAD  => 0
git merge-base --is-ancestor 363e304 HEAD              => 0
git merge-base --is-ancestor c252592 HEAD              => 0
git rev-list --left-right --count private/integration...HEAD => 0 6
```

`HEAD` 是 `a02d451` merge commit，保留 `private/integration@1cbf67a`；
`363e304`（PERM-104-002 closure）与 `c252592`（native permission identity）均可达。
`git diff --stat private/integration HEAD` 只包含 PERM-104 相关实现/测试文件；对
`session_cleanup.py`、`codex_session_cleanup.py`、`terminal_delivery.py`、
`terminal_delivery_coordinator.py`、`session.py`、`e2e_session_supervisor.py`、`runner.py`
及 SESSION-104-001 relay/cleanup 测试的 targeted `git diff --name-status` 无输出，证明当前树未重新引入
旧 Desktop relay、cleanup 或 terminal-delivery 行为。`git diff --check private/integration HEAD` 通过。
本步骤未 reset、rebase、switch、push，也未修改 `private/integration`。

## Step 2：Plan D context-free acceptance

现有生产实现未发现需要修改的已证实缺口；本任务只补齐缺失的确定性最终回归矩阵测试，未加入生产安全策略。
直接验证保持以下不变量：

- explicit `full` 使用已有 Executor-native full 路径，确定性测试不产生 approval dialog；
- inherit/safe 的 approve 路径只接受一个可信 native request，并在同一官方 session 下完成 full continuation；
- 不引入新的 Seatbelt、PathPlan restriction、version allowlist、permission category matcher 或额外 safety policy；
- 同一已生效 concrete full 不重复请求 full、grant、dialog 或 continuation。

新增文件：`tests/test_perm104_002_final_matrix.py`。测试使用官方 session receipt、稳定 fingerprint、
确定性 fake native event 和 `DialogNotifier` spy；不执行真实破坏性动作、不替控制器批准真实权限弹窗。

## Step 3：三 Executor 最终矩阵

新增测试命令：

```bash
PYTHONPATH=src PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m unittest -v tests.test_perm104_002_final_matrix
```

结果：`Ran 4 tests ... OK`。

覆盖项：

| Executor | full handoff/retry 保持 full | approve 同 session continuation | deny/timeout 无 continuation | duplicate/replay/out-of-order 幂等 |
| --- | --- | --- | --- | --- |
| Codex | PASS | PASS | PASS | PASS |
| Claude | PASS | PASS | PASS | PASS |
| Hermes | PASS | PASS | PASS | PASS |

矩阵断言每个事件序列最多一个 input、notification/dialog、decision、grant、worker 和 continuation；
保留 exact official session identity 与现有 executor-native authority。Deny 和 timeout 均无 continuation。

## Step 4：测试与质量门禁

所有命令均在项目根执行，均使用 `PYTHONPATH=src` 与 `PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache`。focused 命令原文如下：

```bash
PYTHONPATH=src PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m unittest -v tests.test_perm104_plan_d tests.test_perm104_002_permission_runtime tests.test_perm104_002_production_routing tests.test_perm104_002_production_wiring tests.test_perm104_002_r3 tests.test_perm104_002_sdk_transport tests.test_perm104_002_session_tool_rules tests.test_perm104_002_transport tests.test_perm104_002_v2_broker tests.test_permission_modes tests.test_permission_registry tests.test_phase3_runner_arguments tests.test_phase3_session_lifecycle
PYTHONPATH=src PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m unittest discover -s tests -p 'test_perm104*.py'
PYTHONPATH=src PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m unittest discover -s tests -p 'test_permission*.py'
PYTHONPATH=src PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m unittest -v tests.test_runner tests.test_runner_spool_isolation tests.test_phase2_runner_policy tests.test_phase3_runner_arguments tests.test_phase3_session_lifecycle tests.test_phase6_runner_adapter_authorization tests.test_perm104_001_hermes_longrun
PYTHONPATH=src PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m unittest -v tests.test_flow104_002_terminal_delivery tests.test_flow104_002_fault_injection tests.test_perm104_003_input_terminal_arbitration
PYTHONPATH=src PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m unittest -v tests.test_session104_desktop_archive tests.test_session_cleanup_codex_v2 tests.test_session_104_collaboration tests.test_phase5_codex_hermes_cleanup tests.test_phase5_cleanup_contract tests.test_auxiliary_sessions tests.test_permission_canary_regression
PYTHONPATH=src PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache .venv/bin/python -m unittest discover -s tests -p 'test*.py'
ruff check .
PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache PYTHONPATH=src .venv/bin/python -m compileall -q src tests
git diff --check
```

结果汇总：

| 命令/范围 | 结果 |
| --- | --- |
| `python -m unittest ...` 13 个既有 focused PERM/permission/Runner/session 模块 | 263 passed |
| `python -m unittest discover -s tests -p 'test_perm104*.py'` | 275 passed, 3 skipped |
| `python -m unittest discover -s tests -p 'test_permission*.py'` | 74 passed |
| `python -m unittest ...` Runner/session 集合 | 127 passed, 14 skipped |
| `python -m unittest ...` terminal-delivery 集合 | 70 passed |
| `python -m unittest ...` SESSION-104-001 relay/cleanup 集合 | 133 passed |
| `python -m unittest discover -s tests -p 'test*.py'` | 1881 passed, 17 skipped；`OK`；370.732s |
| `ruff check .` | `All checks passed!` |
| `PYTHONPYCACHEPREFIX=/tmp/agentbc-pycache PYTHONPATH=src .venv/bin/python -m compileall -q src tests` | exit 0 |
| `git diff --check` | exit 0 |

package build 最终命令（从 `/tmp` 执行，避免项目内 `build/` 目录遮蔽 build module）：

```bash
/opt/homebrew/opt/python@3.14/bin/python3.14 -m pip wheel \
  /Users/wangroway/hermes-team/codex/AgentBC_Temp/agent-worktrees/codex \
  --no-deps --no-build-isolation --wheel-dir /tmp/fnjn-package
```

结果：成功创建 `agentbc-1.0.3a2-py3-none-any.whl`；
SHA-256 `ea661a07fa10503294feb92906d8adfca0ef6cb31c78620d26c287fa78ff41c4`。
此前 `.venv` 缺少可用 pip/build 模块、系统 Python setuptools 版本不足，以及项目根 `build/` 目录遮蔽
module 的尝试均未被当作成功；最终标准 setuptools wheel 已成功，失败尝试产生的 `UNKNOWN.egg-info`
已清理且未进入提交。

## Step 5：清单、提交与工作树

已更新 `AGENTBC_1.0.4A_DEVELOPMENT_CHECKLIST.md`，仅写入上述直接证据支持的 PERM-104-002 收口状态；
最终矩阵、清单和本证据文件已在本地提交 `2996bf9`（`test(permission): close PERM-104-002 final matrix`）中保存；
不 push，不修改 `private/integration`。最终工作树洁净性在回调前再次复核。
