# ServerWatch

轻量的 Linux 服务器状态 CLI：在一个终端页面查看 CPU、内存、GPU，以及当前和最近一段时间内资源占用最多的进程。

Python 3.10+，**零第三方 Python 运行依赖**。直接读取 Linux `/proc` 和 GPU 驱动提供的指标；可交互运行，也可通过 systemd 用户服务持续采集。没有 HTTP 端口、数据库或前端构建。

从本地 Mac 通过 env 配置部署，见 **[部署脚本说明](docs/DEPLOYMENT.md)**；手动安装、日常查看、后台服务和卸载见 [安装与使用](docs/INSTALL.md)。已完成的 Linux 实机检查见 [验证记录](docs/VALIDATION.md)。

## 快速开始

沿用 JobFlow 的 env 配置方式，从本机部署：

复制 env 模板并填写自己的服务器信息。实际配置不会提交到 Git；`cp -n` 保留已有配置。

```bash
git clone https://github.com/AmazingUmi/ServerWatch.git
cd ServerWatch
cp -n scripts/new_node.env.example scripts/new_node.env
# 编辑 scripts/new_node.env，填写 SSH 目标和采集参数
bash scripts/deploy-serverwatch.sh --dry-run
bash scripts/deploy-serverwatch.sh --check
bash scripts/deploy-serverwatch.sh

# 后续升级源码或修改配置
bash scripts/deploy-serverwatch.sh --upgrade
```

默认配置会安装 CLI、启用 systemd 用户服务并开启 linger；只安装 CLI 时设置 `ENABLE_SERVICE=false`。`--dry-run` 仅显示计划，`--check` 只做环境预检。详细字段与多节点配置见 [部署脚本说明](docs/DEPLOYMENT.md)。

在 Linux 服务器上进入项目目录：

```sh
# 不安装，直接运行
python3 -m serverwatch once
python3 -m serverwatch watch

# 安装成一个可执行文件，默认 ~/.local/bin/serverwatch
sh scripts/install.sh
export PATH="$HOME/.local/bin:$PATH"

serverwatch                  # 终端中实时查看；输出重定向时采集单次快照
serverwatch once --json      # JSON，方便脚本集成
serverwatch watch --top 10 --interval 2 --window 120
serverwatch watch --panel gpu # 直接进入 GPU 详情
```

默认进入简洁总览，用数字键进入二级详情：`0` 总览、`1` CPU、`2` 内存、`3` GPU、`4` 最近任务；`Esc` 返回总览，`q` 或 `Ctrl+C` 退出。页面根据终端大小裁剪内容，使用 `↑`／`↓` 或 `k`／`j` 滚动；键盘操作仅在交互终端中启用。

主面板将 CPU、内存、GPU 放在带细分割线的三个紧凑分区中，下方保留最多 5 个近期 CPU 占用最高的任务；窄终端自动改为纵向排列。二级面板沿用同样的指标卡片、分区标题和细分割线，分别展示负载／逐核状态、可用内存／swap、每张 GPU 状态、近期任务统计。所有页面直接显示百分比与数值，不再显示重复的文本进度条；完整统计口径见下文。多 GPU 总览在全部设备都提供相应指标时显示最高利用率和显存合计，否则对应指标标为 N/A。

默认每 2 秒采样一次 CPU／内存，每 5 秒更新一次 GPU，显示前 5 个进程，最近任务统计窗口为 60 秒。单次快照也需要两次 CPU 采样，因此默认等待一个采样间隔。

单文件部署可用：

```sh
python3 scripts/build.py                  # 生成 dist/serverwatch.pyz
python3 dist/serverwatch.pyz --help
sh scripts/install.sh --prefix /your/path # 安装到 /your/path/bin/serverwatch
```

生成的文件仍依赖服务器上的 Python 3.10+。macOS 可构建和运行模拟数据测试，但状态采集面向 Linux；推荐 SSH 登录服务器后使用。

## 显示内容

| 区域 | 含义 |
| --- | --- |
| CPU | 整机 CPU 使用率、逻辑核数、负载均值 |
| 内存 | 物理内存总量、可用量、已用量，以及 swap |
| GPU | 每张卡的利用率、显存、温度和功率；不可用的指标留空或标为不可用 |
| 当前 CPU 排名 | 最近采样间隔内占用 CPU 最多的进程 |
| 当前内存排名 | 按当前驻留内存 RSS 排序的进程 |
| 最近任务 | 统计窗口内累计 CPU 时间最多的进程，包括窗口内已退出、且曾被采样到的进程 |
| GPU 进程 | NVIDIA 计算进程的显存占用排名 |

这里的“任务”按 **PID + 启动时间** 区分，不是调度器任务或项目聚合。不会把重用同一个 PID 的不同进程混在一起。

- **CPU 口径**：整机使用率最大为 100%；进程使用率以单个逻辑核为 100%，多线程进程可能超过 100%。CPU 使用率来自连续采样的差值，不是进程整个生命周期的平均值。
- **内存口径**：已用内存为总量减 `MemAvailable`，不把可回收缓存全部当作已占用；进程 RSS 包含共享页，不应简单相加解释为整机内存。
- **最近任务**：只覆盖采集器实际运行和观测到的时间；启动时逐渐积累，重启后重新统计。采样间隔内创建又退出的短进程可能完全不可见。
- **GPU 进程口径**：按显存占用排序，不能解释为单进程 GPU 算力利用率。GPU 和 CPU 的刷新节奏可以不同。

## CLI

```text
serverwatch watch     持续采样并刷新终端
serverwatch once      采样一次并退出
serverwatch serve     持续采样，将最新快照写入本地状态文件
serverwatch status    读取后台采集器的最新快照
```

`watch`、`once`、`serve` 的采集选项：

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `--interval SECONDS` | `2` | CPU／内存采样间隔 |
| `--gpu-interval SECONDS` | `5` | GPU 采样间隔 |
| `--window SECONDS` | `60` | 最近任务统计窗口 |
| `--top N` | `5` | 各排行榜显示数量 |
| `--gpu auto\|nvidia\|amd\|none` | `auto` | GPU 采集后端；`none` 完全关闭 GPU 查询 |
| `--json` | 关闭 | `once` 输出 JSON；`watch` 输出每行一个快照的 NDJSON |
| `--no-color` | 关闭 | 禁用终端颜色 |
| `--panel overview\|cpu\|memory\|gpu\|tasks` | `overview` | 初始页面；单次文本快照也可选择详情 |

`--no-color` 不影响交互终端中的快捷键。JSON 始终包含完整快照，不按当前页面裁剪。

时间选项支持 `0.1`～`3600` 秒，排行榜数量支持 `1`～`100`。窗口必须不小于采样间隔，且 `window / interval` 最多为 `3600`，避免无限增长的历史采样缓存；长窗口或高频采样仍会增加内存占用。

`serve` 和 `status` 可指定 `--state PATH`。默认使用 `$XDG_RUNTIME_DIR/serverwatch/status.json`；未设置运行目录时，使用 `$XDG_CACHE_HOME/serverwatch/status.json`，再回退到 `~/.cache/serverwatch/status.json`。`watch --state PATH` 切换为读取已有服务快照，不启动第二个采集器。

```sh
serverwatch serve --state "$HOME/.cache/serverwatch/status.json"
# 另一个终端中读取，无需重新采集
serverwatch status --state "$HOME/.cache/serverwatch/status.json"
serverwatch status --state "$HOME/.cache/serverwatch/status.json" --json
serverwatch watch --state "$HOME/.cache/serverwatch/status.json"
```

`status --max-age 15` 默认将超过 15 秒的快照判为过期，并返回非零退出码。`watch --state PATH --max-age 15` 持续显示快照，并在过期时标记 `STALE`。若将采样间隔设得较长，应相应增大此阈值。缓存查看模式的 `--interval` 控制读取频率；`--top`、`--window`、GPU 选项应设置在 `serve` 端。状态文件原子替换、权限为 `0600`，仅保存最新快照；滚动窗口在采集进程内存中维护，不持续追加日志。

## 后台运行

安装后，可选择启用仓库提供的 **systemd 用户服务**：

```sh
mkdir -p "$HOME/.config/systemd/user"
cp packaging/serverwatch.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now serverwatch

serverwatch status
journalctl --user -u serverwatch -n 30
```

模板使用 `~/.local/bin/serverwatch` 和 systemd 用户运行目录。自定义安装位置需修改 `ExecStart`。服务退出后运行目录通常会被清理，`status` 会提示没有快照。

若希望退出 SSH 会话后用户服务仍运行，需要服务器已为该账号启用 linger；按服务器管理方式配置，可由管理员执行 `loginctl enable-linger USERNAME`。手动安装脚本 `scripts/install.sh` 不会启用服务或 linger；env 部署脚本按 `ENABLE_SERVICE` 和 `ENABLE_LINGER` 配置执行。

```sh
systemctl --user disable --now serverwatch
```

## GPU 与权限

- **NVIDIA**：使用驱动随附的 `nvidia-smi`，无需安装 Python GPU 包。读取设备状态和计算进程显存；驱动超时或不支持的字段会显示警告，CPU／内存采集继续。
- **AMD**：读取 amdgpu 驱动的 sysfs 状态，包括驱动实际提供的利用率、显存、温度和功率。当前不提供 AMD 单进程显存排行。
- **无 GPU**：CPU／内存功能可独立使用；可加 `--gpu none` 避免查询。
- **可见性**：仅能看到当前账号和 `/proc` 挂载策略允许读取的进程。`hidepid`、容器隔离或 GPU 驱动限制会影响结果。容器内的 CPU／内存读数不保证对应容器 cgroup 配额，应优先在宿主机运行。
- **读取范围**：仅采集进程名称及资源元数据，不读取进程命令行参数、环境变量或文件内容。通常不需要 root；权限不足会降低可观测范围。

采集成本随进程数、采样频率和 GPU 驱动查询耗时变化。没有预设的 CPU／内存开销承诺；对大型节点可提高 `--interval`、`--gpu-interval`，或关闭 GPU 查询。

## 开发与验证

```sh
python3 -B -m unittest discover -s tests -v
python3 scripts/build.py
python3 scripts/benchmark.py --processes 1000
```

测试使用可控的 `/proc`、sysfs 和 GPU 命令输出验证采样与异常处理，也已在 Linux 服务器完成实际运行检查，具体环境、结果和限制见 [验证记录](docs/VALIDATION.md)。GitHub Actions 配置了 Python 3.10／3.13 的单元测试、真实 `/proc` 快照和单文件安装检查，运行结果见 [Actions](https://github.com/AmazingUmi/ServerWatch/actions)。真实 GPU 驱动和硬件仍需对应设备验收。

`benchmark.py` 使用本地普通文件模拟进程，测量采集器和滚动统计的耗时及进程峰值 RSS，用于本机回归比较。它不包含 GPU 查询、终端绘制或状态文件写入，结果不能代替真实 Linux 服务器的开销测量。默认模拟 60 秒窗口、2 秒间隔，先填满统计窗口，再测量 40 次采样，不实际等待采样间隔。

指标接口参考：[Linux `/proc`](https://www.kernel.org/doc/html/latest/filesystems/proc.html)、[NVIDIA `nvidia-smi`](https://docs.nvidia.com/deploy/nvidia-smi/index.html)、[AMD GPU 状态接口](https://docs.kernel.org/gpu/amdgpu/thermal.html)。
