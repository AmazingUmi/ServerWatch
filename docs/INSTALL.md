# ServerWatch 安装与使用

可优先使用 [env 部署脚本](DEPLOYMENT.md)：复制配置模板、填写目标服务器，然后执行部署；支持预检、升级和仅安装 CLI。本文保留逐步手动安装与使用方法。Linux 实机验证范围见 [验证记录](VALIDATION.md)。

以下示例使用占位路径和文档示例地址，请替换为自己的项目目录与服务器信息：

- 本地项目：`/path/to/ServerWatch`
- 服务器：`192.0.2.10`
- SSH 账号：`root`
- 安装位置：`/root/.local/bin/serverwatch`

服务器需要 Linux 和 Python 3.10+；本地需要 Python 3.10+、`ssh` 和 `scp`。不需要 pip、Node.js 或额外 Python 包。先按第一节安装，直接查看即可；只有需要持续积累“最近任务”时才启用后台服务。

## 1. 构建并安装

**在本地 Mac 终端执行：**

```sh
cd '/path/to/ServerWatch'

# 构建单文件程序
python3 scripts/build.py

# 检查服务器 Python
ssh root@192.0.2.10 'python3 --version'

# 上传到临时文件名，完成后原子替换正式文件
ssh root@192.0.2.10 'mkdir -p /root/.local/bin'
scp dist/serverwatch.pyz root@192.0.2.10:/root/.local/bin/serverwatch.new
ssh root@192.0.2.10 'chmod 755 /root/.local/bin/serverwatch.new && mv -f /root/.local/bin/serverwatch.new /root/.local/bin/serverwatch'

# 确认安装
ssh root@192.0.2.10 '/root/.local/bin/serverwatch --version'
```

这里仅安装可执行文件，不启用服务。构建产物是包含源码的 Python zipapp，可从 macOS 复制到 Linux；运行时使用服务器上的 `python3`。

## 2. 直接查看

**在本地 Mac 终端执行：**

```sh
ssh -t root@192.0.2.10 '/root/.local/bin/serverwatch watch'
```

`-t` 分配交互终端，支持二级页面和快捷键。默认首页将 CPU、内存、GPU 分成三个带细分割线的紧凑区域，下方最多显示 5 个近期 CPU 占用最高的任务。宽终端横向排列资源区，窄终端改为纵向排列。二级面板也使用统一的指标卡片、分区和细分割线，展示负载、swap、逐核状态、GPU 温度／功率及近期统计；各页直接显示数值和百分比，不使用重复的文本进度条。完整统计口径见第四节。

| 按键 | 功能 |
| --- | --- |
| `0` / `Esc` | 返回总览 |
| `1` | CPU 详情、逐核状态、CPU 进程排行 |
| `2` | 内存详情和 RSS 进程排行 |
| `3` | GPU 详情和 NVIDIA 计算进程显存排行 |
| `4` | 最近任务：累计 CPU 时间、窗口平均使用率、内存峰值 |
| `↑` / `k` | 向上滚动 |
| `↓` / `j` | 向下滚动 |
| `q` / `Ctrl+C` | 退出 |

页面适配终端大小，窗口较小时可滚动查看；`--no-color` 只关闭颜色，保留快捷键。

其他常用命令，仍在**本地 Mac**执行：

```sh
# 直接打开 CPU 详情，排行保留 10 个进程，统计最近 120 秒
ssh -t root@192.0.2.10 '/root/.local/bin/serverwatch watch --panel cpu --top 10 --window 120'

# 单次查看内存排行，随后退出
ssh root@192.0.2.10 '/root/.local/bin/serverwatch once --panel memory'

# 单次完整 JSON 快照，保存到本地当前目录
ssh root@192.0.2.10 '/root/.local/bin/serverwatch once --json' > serverwatch-snapshot.json

# 无 GPU 的服务器可以关闭 GPU 查询
ssh -t root@192.0.2.10 '/root/.local/bin/serverwatch watch --gpu none'
```

直接 `watch` 会在当前会话内启动采集器，退出后停止；再次打开时“最近任务”从头积累。`once` 默认等待约 2 秒取得 CPU 差值，有 GPU 查询时可能稍长。它不能恢复过去 60 秒的活动。

若已经通过 `ssh root@192.0.2.10` 登录，直接使用 `/root/.local/bin/serverwatch watch` 即可。也可在**服务器**当前 shell 中设置：

```sh
export PATH="$HOME/.local/bin:$PATH"
serverwatch watch
```

如需以后登录继续生效，将该 `export` 行加入服务器对应 shell 的启动文件，例如 Bash 的 `~/.profile`；不设置 PATH 也可以始终使用完整路径。

## 3. 可选：持续采集的后台服务

后台采集器持续保留滚动窗口；退出查看界面不会停止采集。它写入本机最新 JSON 快照，不开放网络端口、不持续追加历史文件。

### 上传服务模板

**在本地 Mac 终端执行：**

```sh
cd '/path/to/ServerWatch'
ssh root@192.0.2.10 'mkdir -p /root/.config/systemd/user'
scp packaging/serverwatch.service root@192.0.2.10:/root/.config/systemd/user/serverwatch.service
ssh root@192.0.2.10
```

### 启用用户服务

接下来均在**服务器终端**执行：

```sh
systemctl --user daemon-reload
systemctl --user enable --now serverwatch.service
systemctl --user status serverwatch.service --no-pager
```

如需退出 SSH 后以及重启后仍持续采集，手动为该账号启用 linger：

```sh
loginctl enable-linger root
loginctl show-user root -p Linger
```

如果 `systemctl --user` 提示无法连接用户总线，按后面的故障排查处理，再重试启用服务。

### 查看服务已经积累的状态

模板将状态写入 systemd 用户运行目录；此处 root 的路径为 `/run/user/0/serverwatch/status.json`。

**在服务器终端执行：**

```sh
/root/.local/bin/serverwatch status --state /run/user/0/serverwatch/status.json
/root/.local/bin/serverwatch watch --state /run/user/0/serverwatch/status.json
```

正常登录会设置 `XDG_RUNTIME_DIR`，也可使用 `--state "$XDG_RUNTIME_DIR/serverwatch/status.json"`，或直接执行 `serverwatch status` 读取默认位置。

**以后从本地 Mac 打开同一份后台状态：**

```sh
ssh -t root@192.0.2.10 '/root/.local/bin/serverwatch watch --state /run/user/0/serverwatch/status.json'
```

这里必须带 `--state`，才是读取后台服务；不带该参数的 `watch` 会另起一个独立采集器。退出缓存查看界面不会停止后台服务。服务重启后滚动历史重新积累，服务停止后运行目录可能被清理。

### 调整后台采样参数

**在服务器终端执行：**

```sh
systemctl --user edit serverwatch.service
```

写入以下覆盖配置，例如每 3 秒采集、统计最近 120 秒、保留前 10 个进程：

```ini
[Service]
ExecStart=
ExecStart=%h/.local/bin/serverwatch serve --state %t/serverwatch/status.json --interval 3 --window 120 --top 10 --gpu-interval 10
```

保存后执行：

```sh
systemctl --user daemon-reload
systemctl --user restart serverwatch.service
```

`--top`、`--window` 和 GPU 采集选项应设置在 `serve` 端。`watch --state ... --interval 3` 只调整界面读取缓存的频率，不改变服务的采样设置。

## 4. 参数和统计口径

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--panel overview/cpu/memory/gpu/tasks` | `overview` | 初始页面；实际命令中选择其中一个名称 |
| `--top N` | `5` | 每种排行保留的进程数量；范围 1～100 |
| `--interval SECONDS` | `2` | CPU／内存采样间隔 |
| `--window SECONDS` | `60` | 最近任务滚动统计窗口 |
| `--gpu-interval SECONDS` | `5` | GPU 更新间隔 |
| `--gpu auto/nvidia/amd/none` | `auto` | GPU 后端；实际命令中选择其中一个名称 |
| `--max-age SECONDS` | `15` | `status` 和缓存 `watch` 的过期阈值 |
| `--json` | 关闭 | 完整 JSON；持续查看时为每行一个快照的 NDJSON |

时间参数支持 0.1～3600 秒；窗口必须大于等于采样间隔，`window / interval` 不能超过 3600。更长的窗口、更短的间隔和更多进程会增加开销。

- 整机 CPU 使用率最大为 100%；进程 CPU 以一个逻辑核为 100%，多线程进程可能超过 100%。
- 内存已用量为总量减可用量；进程按 RSS 排序，共享内存可能计入多个进程，不能简单求和当作系统占用。
- “任务”指进程，以 PID 和启动时间区分。最近任务按观测到的累计 CPU 时间排序；退出的进程在窗口内保留，采样之间创建又退出的短进程可能遗漏。
- NVIDIA 通过 `nvidia-smi` 采集设备状态和计算进程显存；单进程排行是显存占用，不是 GPU 算力利用率。
- AMD 通过 amdgpu sysfs 采集可用的设备指标，当前没有 AMD 单进程显存排行。GPU 缺失、字段不支持或查询失败不会停止 CPU／内存监控。
- `/proc` 权限和容器隔离会限制可见进程；建议在宿主机运行，容器内读数不保证对应容器配额。

## 5. 更新

**在本地 Mac 终端执行：**

```sh
cd '/path/to/ServerWatch'
python3 scripts/build.py
scp dist/serverwatch.pyz root@192.0.2.10:/root/.local/bin/serverwatch.new
ssh root@192.0.2.10 'chmod 755 /root/.local/bin/serverwatch.new && mv -f /root/.local/bin/serverwatch.new /root/.local/bin/serverwatch'
ssh root@192.0.2.10 '/root/.local/bin/serverwatch --version'
```

先上传 `.new` 再 `mv`，避免覆盖正在运行的程序文件。直接运行的 `watch` 退出再打开即可使用新版本；启用了后台服务时，还需在**服务器终端**执行：

```sh
systemctl --user restart serverwatch.service
systemctl --user status serverwatch.service --no-pager
```

如果更新了服务模板，再按第三节上传模板并执行 `daemon-reload`。重启采集器会清空其内存中的滚动历史。

## 6. 停止和卸载

只停止后台服务，**在服务器终端执行**：

```sh
systemctl --user stop serverwatch.service
```

取消自启并卸载已安装的服务和程序。先确认没有部署脚本正在运行，再在服务器终端执行：

```sh
systemctl --user disable --now serverwatch.service
rm -f /root/.config/systemd/user/serverwatch.service
rm -f /root/.config/systemd/user/serverwatch.service.d/override.conf
rmdir /root/.config/systemd/user/serverwatch.service.d 2>/dev/null || true
systemctl --user daemon-reload
rm -f /root/.local/bin/serverwatch /root/.local/bin/serverwatch.new
rm -f /root/.local/share/serverwatch/deployment.json /root/.local/share/serverwatch/deploy.lock
rmdir /root/.local/share/serverwatch 2>/dev/null || true
```

若未安装后台服务，只需删除程序文件及部署脚本生成的 `deployment.json`、`deploy.lock`；手动安装没有部署记录时，`rm -f` 会跳过缺失文件。关闭仍在运行的独立 `watch` 会话即可停止它们。自定义 `INSTALL_PREFIX` 时，程序和部署记录分别在该前缀的 `bin` 与 `share/serverwatch` 下；用户名不为 root 时也要使用对应账号的路径。

自定义 `--state` 产生的状态文件和同名 `.lock` 可在采集器停止后按实际路径删除。不要为卸载 ServerWatch 自动关闭 linger，因为同账号的其他用户服务可能依赖它。服务转为仅 CLI 时，除了删除用户单元，还必须移除部署记录；具体步骤和冲突处理见 [部署脚本说明](DEPLOYMENT.md#重复部署升级与恢复)。

## 7. 故障排查

| 现象 | 处理 |
| --- | --- |
| `live collection requires Linux /proc` | 在 Linux 服务器上采集；Mac 只用于构建或读取已有缓存。 |
| `python3` 找不到或版本过低 | 在服务器执行 `python3 --version`；安装／选择 Python 3.10+，确保它位于服务和 shell 的 PATH 中。 |
| `serverwatch: command not found` | 使用 `/root/.local/bin/serverwatch`，或按第二节设置 PATH。 |
| 界面不能切换页面 | SSH 使用 `-t`；重定向输出、`--json` 或非交互输入不会启用快捷键。 |
| GPU 为 N/A／没有受支持的 GPU | 确认硬件与驱动是否存在；NVIDIA 可单独检查 `nvidia-smi`。CPU／内存仍可用，无 GPU 时可设置 `--gpu none`。 |
| 看不到某些进程 | 检查账号权限、`/proc` 的 `hidepid` 策略和容器隔离；排行仅覆盖可读取的进程。 |
| `No such file` 状态文件不存在 | 确认服务正在运行，并使用其真实 `--state` 路径；默认路径会受 `XDG_RUNTIME_DIR` 影响。 |
| 界面显示 `STALE` 或 `status` 退出码为 2 | 查看服务日志；若主动设置较长采样间隔，适当增大 `--max-age`，例如 `--max-age 60`。 |
| `another ServerWatch service is already writing...` | 同一个状态路径只能有一个写入者；使用已有服务，或为另一个采集器指定不同路径。 |

查看后台日志，**在服务器终端执行**：

```sh
journalctl --user -u serverwatch.service -n 50 --no-pager
```

若 root 用户服务提示 `Failed to connect to bus`，并且确实准备使用常驻用户服务，可在**服务器 root 终端**执行：

```sh
loginctl enable-linger root
systemctl start user@0.service
export XDG_RUNTIME_DIR=/run/user/0
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus
systemctl --user daemon-reload
systemctl --user enable --now serverwatch.service
```

以上命令适用于使用 systemd 的目标服务器。它们是手动安装选项，不是 ServerWatch 可执行文件的自动行为。
