# Windows / VMware 本地 Linux

项目用 Ubuntu 24.04 LTS 云镜像创建独立虚拟机，所有文件在仓库 `.local/vmware/`，不会修改已有虚拟机或主机网络。默认 2 vCPU、1024 MB 内存、16 GB 稀疏磁盘和 NAT。镜像固定为官方 20261017 build，下载后核对官方 SHA256SUMS。

## 创建与启动

在仓库根目录 PowerShell 执行（需要 VMware Workstation 17、Python 3.11+）：

```powershell
python -m pip install --target .local/vmware/python-deps pycdlib==1.14.0 paramiko==4.0.0
python scripts/vmware_create.py --start
```

默认使用 Ubuntu 官方软件源。国内网络如果访问官方源较慢，可在创建时明确选择已知 HTTPS 镜像：

```powershell
python scripts/vmware_create.py --apt-mirror https://mirrors.ustc.edu.cn/ubuntu --start
```

镜像仅改变包下载地址，仍使用 Ubuntu 原有仓库签名密钥验证。创建脚本跳过包描述翻译下载以减少首次安装流量。

第一次启动会通过 cloud-init 安装 `build-essential`、`cmake`、`gdb`、`python3`、`python3-venv` 和 `open-vm-tools`，需要联网。初始化完成后：

```powershell
python scripts/vmware_ssh.py
python scripts/vmware_ssh.py --upload 'cd /home/edge/project && make -C linux test all && EDGE_SERVER="$PWD/linux/build/edge_scheduler" python3 -m unittest discover -s tests -v'
```

同时预装 ESP-IDF 的宿主构建依赖：Ninja、Flex、Bison、gperf、pip、libffi、OpenSSL 和 libusb。`/var/lib/edge-linux-ready` 只在开发工具验证成功后创建；`vmware_ssh.py` 默认命令先检查这个标记，缺少标记时才等待 cloud-init，然后检查 GCC、CMake 和 Python。对本次已经修复的 VM，直接运行 `python scripts/vmware_ssh.py` 即可，不受 cloud-init 历史错误影响。首次安装期间可执行 `python scripts/vmware_ssh.py 'sudo tail -n 20 /var/log/cloud-init-output.log'` 查看进度。

如果 VMware Tools 尚未就绪，可以从 VMware 控制台或 `.local/vmware/edge-linux/serial.log` 查看 DHCP 地址，使用 `--host 192.168.x.x`。SSH 用户为 `edge`，只启用密钥登录，私钥在 `.local/vmware/edge-linux/id_rsa`；首次连接记录该 VM 的主机密钥。此账户拥有免密码 sudo，仅用于本地开发。不要将 `.local/`、私钥或虚拟磁盘加入 Git。

`--upload` 自动创建远端工程目录，并跳过 `.git/`、`.local/`、`build/`、`build-*/`、结果目录和可能含 Wi-Fi 密码的 `sdkconfig`。如需下载实验产物：

```powershell
python scripts/vmware_ssh.py --download /home/edge/project/results/comparison/report.md results/comparison/report.md 'true'
```

下载目标必须位于本地项目目录内。上传按文件覆盖同名源文件，不会删除远端已有文件；切换或删除源文件后，编译前运行 `make -C linux clean`。

日后启动/关闭：

```powershell
$vmrun = 'C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe'
$vmx = (Resolve-Path '.local/vmware/edge-linux/edge-linux.vmx').Path
& $vmrun -T ws start $vmx nogui
& $vmrun -T ws stop $vmx soft
```

也可以在 VMware Workstation 中打开上述 `.vmx`。创建脚本拒绝覆盖已有 VM；如需重建，先自行保留需要的虚拟机数据，再选择新的目录或手动处理旧目录。

## ESP32 联网

默认 NAT 便于本机开发与联网安装依赖，ESP32 无法直接连接这个私有 NAT 地址。硬件联调时在 VMware 中把该 VM 的网卡切换为桥接，让 VM 和 ESP32 连接同一局域网，再把 VM 的局域网 IP 写入固件配置。本次本地验证不需要修改主机防火墙或 NAT 端口转发。

官方来源：[Ubuntu Noble 镜像](https://cloud-images.ubuntu.com/noble/20261017/)、[cloud-init NoCloud](https://cloudinit.readthedocs.io/en/latest/reference/datasources/nocloud.html)。

已验证镜像 SHA256：`9a1e3959c9f4ad9d4752101fd4ff9a43191fb7d72e61fa20e033f9fd235ce794`。Ubuntu daily 镜像可能有保留期限；保留 `.local/vmware/images/` 中已校验的原始镜像即可再次离线创建磁盘（guest 软件包安装仍需联网）。

## 本次环境记录（2026-10-23，上海时间）

本次虚拟机最初使用官方 HTTP archive，`apt update` 连续大索引重试。VM 内对同一 Packages.xz 读取前 256 KiB，官方源耗时 27.32 秒，USTC HTTPS 1.87 秒，TUNA HTTPS 2.38 秒。随后在这台新 VM 内将原始源备份为 `/etc/apt/sources.list.d/ubuntu.sources.original`，切换至 USTC HTTPS，并终止尚处于索引更新阶段的 apt 进程；没有中断 dpkg，也没有修改宿主或其他 VM。

该次中断保留在 `cloud-init status --long` 的历史错误中，随后串行手动执行 `apt-get update` 和上面的包安装清单进行恢复。当前环境是否就绪以 `/var/lib/edge-linux-ready` 和工具实际版本检查为准，不将 cloud-init 的最终日志文本视为安装成功。详细本地记录在 `.local/vmware/provisioning-record.md`。
