# 发布流程

中文 | [English](RELEASE_PROCESS.md)

本清单用于准备和发布 AgentBC，避免用一次本地 smoke、可变分支或重复资产名冒充正式发布。
当前发布候选为 **1.0.4A**，其不可变发布映射为：

```text
Release 名称： AgentBC 1.0.4A
产品标签：     v1.0.4A
Python 包：    1.0.4a1
```

## 1. 冻结发布提交

- 完成 CHANGELOG，将 `Unreleased` 替换为实际发布日期；
- 从当前公开 `main` 创建干净的 `release/*` 分支；私有 integration 提交及其任何祖先都不得成为
  公开候选的父提交；
- 审阅候选的完整文件树，不能只审阅最后一次 commit diff；
- 在任何网络写入前执行
  `python3 scripts/check_repository_boundary.py --source revision --revision HEAD` 与
  `python3 scripts/check_public_release.py`，两项都必须通过；
- 确认 `pyproject.toml` 与 `agent_bridge_connect.__version__` 都是 `1.0.4a1`；
- 确认公开远端不存在 `v1.0.4A`，PyPI 也不存在 `agentbc==1.0.4a1` 文件。已发布标签和
  包文件不可覆盖。

## 2. 执行发布矩阵

以 release-check workflow 的 Python 3.10、3.11、3.14 矩阵为权威门禁。每个 job 都执行
源码测试、Ruff、compileall、Shell 语法、构建、Twine、发行文件名校验、manifest、wheel
安装和纯包 smoke。

打标签前还必须完成 1.0.4A 清单中的发布专属人工门禁：双机 Update/Homebrew、成功与失败
终态 session cleanup、clean install/restore 和真实 Executor 检查。Runner 健康或一次 smoke
不能替代其他门禁。

## 3. 构建本地候选包

在干净 checkout 中使用隔离输出目录。构建前生成 build identity，使 wheel 与 sdist 绑定
精确源码提交：

```bash
python3 scripts/build_provenance.py print-package-version
python3 scripts/build_provenance.py print-product-version
python3 scripts/build_provenance.py generate-build-info --build-source release-candidate
python3 -m build
python3 -m twine check dist/*.whl dist/*.tar.gz
python3 scripts/build_provenance.py validate-dists
python3 scripts/build_provenance.py generate-manifest
```

逐项核对 `dist/release-manifest.json` 中的 SHA-256；在全新虚拟环境安装 wheel，执行
`agentbc --version` 和纯包 smoke。候选资产可以丢弃；源码提交变化后不得继续上传旧候选。

从同一提交单独构建 macOS local-alpha 候选包：

```bash
./scripts/build_local_alpha_bundle.sh /tmp/agentbc-v1.0.4A-release
shasum -a 256 -c /tmp/agentbc-v1.0.4A-release/agentbc-v1.0.4A-macos-local-alpha.tar.gz.sha256
```

至少解压一次，并在包内执行 `shasum -a 256 -c SHA256SUMS`。压缩包、压缩包 checksum、
`install-agentbc-alpha.sh` 和 `uninstall-agentbc-alpha.sh` 是 GitHub Release 必备资产；PyPI
workflow 不负责构建这些 macOS 资产。

## 4. 打标签并发布

全部本地门禁通过后，只允许推送 `release/*` 候选分支，并创建目标为公开 `main` 的 PR。
禁止直接 push `main`。PR 必须通过根目录边界、公开内容边界和完整发布矩阵，并取得仓库所有者
审阅后才能合并：

```bash
git push public HEAD:refs/heads/release/v1.0.4A
# 所有 required checks 和 owner review 通过后，在 GitHub 合并 PR。
```

只有 PR 合入后，受保护的发布器才能在该公开合并提交上创建不可变 annotated tag
`v1.0.4A`。开发机凭据不得直接 push `main` 或发布 tag。创建 draft Release 前必须再次校验
最终 tag/version/commit 关系。

使用对应 CHANGELOG 内容从 `v1.0.4A` 创建名为 `AgentBC 1.0.4A` 的 **draft GitHub Release**。
发布前先上传并校验：

```text
agentbc-v1.0.4A-macos-local-alpha.tar.gz
agentbc-v1.0.4A-macos-local-alpha.tar.gz.sha256
install-agentbc-alpha.sh
uninstall-agentbc-alpha.sh
```

确认四项 macOS 资产后再发布 draft。发布 Release 会触发
`.github/workflows/publish-pypi.yml`：从 tag 重建、校验 provenance、上传 wheel/sdist/manifest
到 GitHub Release，并通过 Trusted Publishing 仅向 PyPI 发布 wheel/sdist。禁止使用开发机
凭据或未打标签的工作树直接上传 Python 发行包。

## 5. 发布后验证与恢复

发布完成后：

- 对比 GitHub Release、PyPI 资产哈希与 release manifest；
- 在全新环境安装 `agentbc==1.0.4a1`；
- 验证 `agentbc --version`、`agentbc setup --show`、Runner identity、Skill manifest 和
  `agentbc doctor`；
- Apple Silicon 与 Intel 安装路径均通过后再宣布发布完成。

若 tag 已存在但发布 job 失败，应修复 workflow，不得移动或重建 tag。受保护的恢复入口是
手动 workflow dispatch：`release_tag=v1.0.4A`、`publish=true`；它会先 checkout 并校验
既有 tag，再重新构建。已经发布到 PyPI 的同版本文件永远不得覆盖。
