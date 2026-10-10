# Docker WebUI 同源接线与宿主服务管理

2026-09-28；GUI-105-003-05 的 Mac 自动验收子项。Linux/Windows 人工格仍待验，不关闭三宿主父项。

## 接线

合并 `compose.yaml`、本机运行时覆盖和 `compose.webui.yaml`。WebUI 使用主线同一镜像与 `data` 命名卷，只读挂载到 `/data/workspace`；root 为 `/data/workspace/record`，reports 为 `/data/workspace/tasks/report`。未挂载 config/profiles/runtime/Docker socket，不注入 Executor 凭据。只读根文件系统、非 root、cap_drop ALL、no-new-privileges。

WebUI 使用前台 `serve`，容器内部8080，宿主固定仅 `127.0.0.1:18080`。严格 Host/Origin 和 bearer token 校验保留。私有启动链接仅写 `/run/webui/launch-url` tmpfs 文件，0600；普通状态与容器日志不输出 token。

`deploy/webui/compose.yaml` 是旧宿主 bind 示例；当前 Docker 主线使用新增 `deploy/docker/compose.webui.yaml`，不能混用两者。

## 安装与命令

在已安装本轮 wheel 的宿主 Python 环境，显式建立双容器绑定：

```sh
python -m agent_bridge_connect.docker_host install \
  --prefix /path/to/empty/host \
  --skill-home /path/to/isolated/home \
  --container agentbc-mac-flow-runner-1 \
  --webui-container agentbc-mac-flow-webui-1
/path/to/empty/host/bin/agentbc webui start
/path/to/empty/host/bin/agentbc webui status
/path/to/empty/host/bin/agentbc webui close
```

不带子命令等同 start；`start --no-open` 仅打印私有链接。start重复使用当前链接，重启生成新token；close幂等，只停止确切绑定的WebUI，Runner不变。容器重建/端口/数据源变更要求显式重新绑定，不能按同名自动控制其他实例。Runner已停机时仍可管理原WebUI。

绑定校验同一Compose project的webui服务、同一只读数据源、单一loopback端口与只读root；拒绝额外host/profile挂载。端口冲突报错，不自动换端口或杀占用进程。命令不会安装LaunchAgent、启动宿主Runner或回退原生WebUI。

Compose参数应保存在安装部署自己的env文件（不含token）：

```dotenv
AGENTBC_WEBUI_IMAGE=sha256:eeaa10f3847aaaac9e296229cd3a4b0ce8f3eb2f99377c9b3d7612bbf07e1775
AGENTBC_WEBUI_PORT=18080
AGENTBC_WEBUI_RESTART=no
```

常驻使用Compose明确配置的restart策略；本轮隔离验收为no，未更改用户正式setup偏好。Docker版交互setup与升级/回滚仍属安装生命周期收口，不将本条接线替代为完整安装验收。

## 实际证据

- 镜像与Runner一致：`sha256:eeaa10f3847aaaac9e296229cd3a4b0ce8f3eb2f99377c9b3d7612bbf07e1775`。
- 隔离宿主入口：`/private/tmp/agentbc-webui-host/install/bin/agentbc`；页面origin `http://127.0.0.1:18080/`，私有token不写入本文。
- API：无token 401、错误Host/Origin 403、POST不可写；ANDN-001 snapshot/report成功，报告Marker valid=yes。
- 原生浏览器：进行中空态、已完成7项、失败/待恢复6项；ANDN-001显示completed、19s和步骤文字；报告链接创建独立blob页面。浏览器工具禁止读取blob地址，正文正确性由同一HTTP报告接口核验；此限制不冒充报告页视觉人工验收。
- 独立WebUI暂停14秒：界面显示request_timeout并保留选中详情；恢复后自动回到新鲜快照，选中任务未跳转。
- 生命周期：重复start复用链接；重复close成功；占用18080时启动失败且端口不变；释放后并发冷start均成功且链接一致；重启token轮换。Runner ID/PID/StartedAt不变。
- 浏览、API与启停前后，整个静态record/report树的内容SHA256和mtime_ns完全一致。没有业务任务被关闭/取消。

## 版本边界

按老板确认，OpenCode原生审批列入1.06a `EXEC-106-OPENCODE-APPROVAL`，1.05a停止继续优化。pending计时与实时记录/对话同样仍在1.06a；本轮只做WebUI Docker接线。

Mac人工联验、Linux/Windows、运行中真实任务终态迁移、最终同候选四Executor、升级回滚及正式本机替换仍分别待验。当前服务留运行供人工查看，正式本机CLI/skill未替换，未提交/发布。

### 当前 Mac 数据身份

- 宿主：macOS 15.7.4 / arm64。
- Task：ANDN-001。
- source_id：`e89ae84c31874964add442159c2b9a93`。
- snapshot_revision：`05666e03581fb2134811b000fd2c08793da73efe2c743f0afd9f421ee410fc94`。
- 只读比对包含202个任务/报告文件，内容与mtime完全一致。
- 宿主wheel SHA256：`e3500a62712133613f8b866f46364fd67a6ded7a983af23066b4fd2202dc2a55`；版本字符串仍为开发基线1.0.4a1，不当作正式发布版本。

### 回归收口

- 首轮完整2245项、217.706秒、OK。
- 加入Runner停机时WebUI仍可独立管理的修正及回归后，最终完整2246项、363.237秒、OK：`/private/tmp/webui-docker-final-full.log`。
- 前端18项通过；最终wheel构建含类型检查与Vite；Ruff、working-tree边界、测试结构75文件、现有index发布边界和diff check通过。
- 页面与服务保持运行供老板人工验收；Mac自动验证通过不替代三宿主人工结论。
