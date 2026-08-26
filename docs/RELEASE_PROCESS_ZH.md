# 发布流程

中文 | [English](RELEASE_PROCESS.md)

本清单用于准备和发布 AgentBC，避免用一次本地 smoke、可变分支或重复资产名冒充正式发布。
当前发布版本为 **1.0.3A**。由于首个 Alpha 序号已用于 Homebrew bootstrap，最终发布的
不可变映射为：

```text
Release 名称： AgentBC 1.0.3A
产品标签：     v1.0.3A2
Python 包：    1.0.3a2
```

## 1. 冻结发布提交

- 完成 CHANGELOG，将 `Unreleased` 替换为实际发布日期；
- 要求发布分支干净且非 detached，逐文件审阅发布 diff；
- 确认 `pyproject.toml` 与 `agent_bridge_connect.__version__` 都是 `1.0.3a2`；
- 确认公开远端不存在 `v1.0.3A2`，PyPI 也不存在 `agentbc==1.0.3a2` 文件。已发布标签和
  包文件不可覆盖。

## 2. 执行发布矩阵

以 release-check workflow 的 Python 3.10、3.11、3.14 矩阵为权威门禁。每个 job 都执行
源码测试、Ruff、compileall、Shell 语法、构建、Twine、发行文件名校验、manifest、wheel
安装和纯包 smoke。

打标签前还必须完成 1.0.3A 清单中的发布专属人工门禁：双机 Update/Homebrew、成功与失败
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
./scripts/build_local_alpha_bundle.sh /tmp/agentbc-v1.0.3A2-release
shasum -a 256 -c /tmp/agentbc-v1.0.3A2-release/agentbc-v1.0.3A2-macos-local-alpha.tar.gz.sha256
```

至少解压一次，并在包内执行 `shasum -a 256 -c SHA256SUMS`。压缩包、压缩包 checksum、
`install-agentbc-alpha.sh` 和 `uninstall-agentbc-alpha.sh` 是 GitHub Release 必备资产；PyPI
workflow 不负责构建这些 macOS 资产。

## 4. 打标签并发布

全部门禁通过后，在已审阅的发布提交上创建不可变 annotated tag，并校验 tag/version/commit：

```bash
git tag -a v1.0.3A2 -m "AgentBC 1.0.3A"
python3 scripts/build_provenance.py validate --tag v1.0.3A2
git push public <release-branch>
git push public refs/tags/v1.0.3A2
```

使用对应 CHANGELOG 内容从 `v1.0.3A2` 创建名为 `AgentBC 1.0.3A` 的 **draft GitHub Release**。
发布前先上传并校验：

```text
agentbc-v1.0.3A2-macos-local-alpha.tar.gz
agentbc-v1.0.3A2-macos-local-alpha.tar.gz.sha256
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
- 在全新环境安装 `agentbc==1.0.3a2`；
- 验证 `agentbc --version`、`agentbc setup --show`、Runner identity、Skill manifest 和
  `agentbc doctor`；
- Apple Silicon 与 Intel 安装路径均通过后再宣布发布完成。

若 tag 已存在但发布 job 失败，应修复 workflow，不得移动或重建 tag。受保护的恢复入口是
手动 workflow dispatch：`release_tag=v1.0.3A2`、`publish=true`；它会先 checkout 并校验
既有 tag，再重新构建。已经发布到 PyPI 的同版本文件永远不得覆盖。
