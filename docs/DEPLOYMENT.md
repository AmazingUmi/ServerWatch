# 使用 env 配置部署

部署入口沿用 JobFlow 的使用方式：复制 `scripts/new_node.env.example`，编辑配置，然后执行 `scripts/deploy-serverwatch.sh`。脚本在本地构建单文件程序，通过 SSH 安装到指定账号，并按配置启用后台采集。

部署与采集都使用 SSH 登录账号，不额外创建运行账号，不修改 SSH 授权。服务器需要 Linux、Python 3.10+ 和 `tar`；启用后台服务还需要 systemd。客户端需要 Bash、Python 3.10+ 和 OpenSSH。使用 root 以外的账号时，安装目录须可写；自动启用 linger 还需要免交互 sudo。

## 首次部署

先复制配置模板，再填写目标服务器。实际 env 配置已被 Git 忽略；`cp -n` 保留已有文件，不覆盖现有设置。

以下命令在本地 Mac 的项目目录执行：

```bash
cd '/path/to/ServerWatch'
cp -n scripts/new_node.env.example scripts/new_node.env
chmod 600 scripts/new_node.env
```

编辑 `scripts/new_node.env`。下面以 root 账号为例，请将文档示例地址 `192.0.2.10` 替换为自己的服务器地址；其余参数可保留默认值：

```dotenv
NODE_IP=192.0.2.10
ROOT_USER=root
ROOT_SSH=
SSH_PORT=22
SSH_KEY=

INSTALL_PREFIX=
STATE_FILE=
ENABLE_SERVICE=true
ENABLE_LINGER=true

INTERVAL=2
WINDOW=60
TOP=5
GPU=auto
GPU_INTERVAL=5
```

先检查配置和目标环境，再执行安装：

```bash
# 只在本地显示部署计划，不连接服务器
bash scripts/deploy-serverwatch.sh --dry-run

# 连接服务器做环境预检，不安装程序、不启用服务
bash scripts/deploy-serverwatch.sh --check

# 执行配置中的安装与服务操作
bash scripts/deploy-serverwatch.sh
```

默认配置会安装 CLI、启用 `serverwatch.service` 用户服务，并为该账号开启 linger，使后台采集在 SSH 断开后继续运行。若只想安装 CLI，将 `ENABLE_SERVICE=false`；此时 `ENABLE_LINGER` 不生效。

`--dry-run` 只读取并校验配置，包括已填写私钥路径是否存在，不构建程序、不连接 SSH。`--check` 会构建临时包，在服务器的 `/tmp` 临时目录中预检，结束后清理；它不写入安装目录，不改变服务或 linger 状态。

脚本使用非交互 SSH 并校验已有主机密钥。首次使用目标机器时，先通过普通 SSH 核对主机指纹、完成认证，确保该账号能正常登录。脚本不自动接受未知主机密钥，也不会安装或上传私钥。

## 配置字段

配置使用字面值 `KEY=value`，支持空行与整行 `#` 注释。**不加引号，不使用行尾注释，不展开 shell 变量或命令。** 未知字段、重复字段和无效数值会直接报错。真实配置文件已加入 Git 忽略规则；模板仍可提交。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `NODE_IP` | 必填目标 | IPv4、主机名或 SSH 别名；与 `ROOT_USER` 配合 |
| `ROOT_USER` | `root` | SSH 登录、安装和运行采集器的账号；也可以是现有普通账号 |
| `ROOT_SSH` | 空 | 可替代前两项，填写 SSH 别名或 `user@hostname`；使用它时清空 `NODE_IP`、`ROOT_USER` |
| `SSH_PORT` | 空（模板填写 `22`） | 空值或省略时沿用 SSH 配置的端口；填写数值时显式覆盖 |
| `SSH_KEY` | 空 | 客户端私钥路径；支持本机 `~/`，相对路径以 env 文件目录为基准，留空沿用 SSH 配置或 agent |
| `INSTALL_PREFIX` | 空 | 远端安装前缀；空值使用登录账号的 `~/.local` |
| `STATE_FILE` | 空 | 状态文件绝对路径；空值使用 `/run/user/UID/serverwatch/status.json` |
| `ENABLE_SERVICE` | `true` | 是否安装、启用并启动 systemd 用户服务 |
| `ENABLE_LINGER` | `true` | 是否为服务账号开启 linger；仅启用服务时生效 |
| `INTERVAL` | `2` | CPU／内存采样间隔，秒 |
| `WINDOW` | `60` | 近期任务统计窗口，秒 |
| `TOP` | `5` | 各类排行保留数量，1～100 |
| `GPU` | `auto` | `auto`、`nvidia`、`amd` 或 `none` |
| `GPU_INTERVAL` | `5` | GPU 采样间隔，秒 |

时间参数范围为 0.1～3600 秒；`WINDOW` 必须不小于 `INTERVAL`，且 `WINDOW / INTERVAL` 不超过 3600。`INSTALL_PREFIX`、`STATE_FILE` 的非空值应为远端绝对路径，使用字母、数字、点、下划线、连字符与斜杠，不使用空格、`~` 或变量。

ServerWatch 的 `SSH_KEY` 是**客户端登录私钥**；JobFlow 的 `KEY` 则用于向运行账号授权公钥，二者用途不同。ServerWatch 不接受 JobFlow 的 `KEY`、`JOBFLOW_USER` 等专属字段。

如使用 SSH 别名：

```dotenv
NODE_IP=
ROOT_USER=
ROOT_SSH=my-vps
SSH_PORT=
SSH_KEY=
```

`ROOT_SSH` 为别名时，登录账号由 SSH 配置决定。上述空端口会保留别名的 `Port` 设置；需要覆盖时再填写 `SSH_PORT`。

## 多台服务器与本机安装

每台服务器保留一份 env 配置：

```bash
cp -n scripts/new_node.env.example scripts/gpu_node.env
# 编辑 scripts/gpu_node.env
bash scripts/deploy-serverwatch.sh --env scripts/gpu_node.env --dry-run
bash scripts/deploy-serverwatch.sh --env scripts/gpu_node.env --check
bash scripts/deploy-serverwatch.sh --env scripts/gpu_node.env
```

源码与配置已经在 Linux 节点上时，可直接安装到当前账号：

```bash
bash scripts/deploy-serverwatch.sh --local --env scripts/new_node.env --check
bash scripts/deploy-serverwatch.sh --local --env scripts/new_node.env
```

`--local` 不建立 SSH 连接，SSH 目标字段不用于选择运行账号。不要为了本机模式直接加 `sudo`，除非确实打算将 CLI 和用户服务安装到 root 账号。

## 部署后查看

部署成功后，脚本会输出实际程序位置、状态文件路径和可复制的查看命令。使用上述 root 默认配置时：

```bash
# 打开独立实时采集界面
ssh -t root@192.0.2.10 '/root/.local/bin/serverwatch watch'

# 查看后台采集器已经积累的近期状态
ssh -t root@192.0.2.10 '/root/.local/bin/serverwatch watch --state /run/user/0/serverwatch/status.json'

# 单次读取后台状态
ssh root@192.0.2.10 '/root/.local/bin/serverwatch status --state /run/user/0/serverwatch/status.json'

# 检查后台服务与日志
ssh root@192.0.2.10 'systemctl --user status serverwatch.service --no-pager'
ssh root@192.0.2.10 'journalctl --user -u serverwatch.service -n 30 --no-pager'
```

自定义路径、端口或采样间隔时，优先使用脚本输出的命令；较长采样间隔需要相应的 `--max-age`。不带 `--state` 的 `watch` 会创建独立采集器，退出后停止；带 `--state` 的界面只读后台缓存。

## 重复部署、升级与恢复

相同源码和配置再次执行且不带 `--upgrade` 时，脚本校验已安装文件、服务启用／运行状态和新鲜缓存，不重启正常运行的采集器。如果文件一致但服务状态不符合配置，脚本会报错并提示以 `--upgrade` 修复。源码或采样配置有变化时，同样需要明确使用 `--upgrade`：

```bash
# 编辑 env，或更新本地源码后
bash scripts/deploy-serverwatch.sh --dry-run --upgrade
bash scripts/deploy-serverwatch.sh --upgrade
```

程序和服务配置通过临时文件原子替换。`--upgrade` 会重启已配置的采集服务，**即使源码与配置没有变化**，内存中的近期历史也会重新积累。若目标路径已有未由部署脚本管理的 ServerWatch 文件，也需要检查后使用 `--upgrade` 接管；脚本不会静默覆盖。

脚本捕获到部署失败时，会尝试恢复原程序、配置和服务启停状态；若恢复不完整，会报告需要检查的项目。回滚依赖脚本能够执行错误处理，强制结束进程或主机断电后应检查实际安装状态。启用服务时会检查运行状态和新生成的状态快照，以脚本最终结果为准。

`ENABLE_SERVICE=false` 是仅安装 CLI 的选项，不应当作停止现有服务的开关。从服务模式改为仅 CLI，须先停止并禁用服务，移除用户单元与部署记录 `INSTALL_PREFIX/share/serverwatch/deployment.json`，然后再执行部署；若保留现有程序文件，下一次使用 `--upgrade`。完整清理步骤见 [停止和卸载](INSTALL.md#6-停止和卸载)。

默认部署记录位于 `~/.local/share/serverwatch/deployment.json`，并通过同目录的 `deploy.lock` 避免并发部署。卸载时应在服务和部署过程都已停止后清理这些文件；自定义 `INSTALL_PREFIX` 时使用对应路径。更改前缀不会自动删除旧前缀下的程序和部署记录。

## 常见部署问题

| 提示 | 处理 |
| --- | --- |
| `existing installation differs` | 确认目标文件和配置后使用 `--upgrade`；相同内容日常检查不加该参数。 |
| `installation exists but service settings differ` | 检查服务是否被停止、禁用或 linger 改动；需要按 env 恢复时使用 `--upgrade`。 |
| `service-to-CLI conversion is manual` | 停止／禁用服务，删除用户单元和部署记录，再按上一节切换模式。 |
| `service overrides exist` / `existing ... overrides` | 先用 `systemctl --user cat serverwatch.service` 检查覆盖配置，将采样参数迁入 env，移除对应覆盖目录后再部署。`--upgrade` 不会绕过此检查。 |
| `another ServerWatch service exists` / `another ... unit is already loaded` | 检查已有的系统级、全局用户或运行时单元；处理同名单元冲突后再部署，不要同时运行两套同名服务。 |
| `serverwatch.service is masked` | 确认要启用后手动取消 mask，再部署。 |
| `another ServerWatch deployment is running` | 等当前部署退出后再试；不要删除一个仍被使用的锁文件。 |

手动逐步安装、日常快捷键、统计口径与卸载方法继续见 [安装与使用](INSTALL.md)。部署脚本只提供 env 驱动的安装流程，不增加 Web 服务或新的运行时依赖。
