# 本地验证记录 — 2026-10-23

## 环境

- Windows 宿主上的新建 VMware Workstation 虚拟机，文件位于 `.local/vmware/edge-linux/`。
- Ubuntu 24.04.5，Linux 6.8.0-147-generic，glibc 2.39；2 vCPU、1 GB RAM、16 GB 稀疏磁盘。
- GCC 13.3.0、Make 4.3、CMake 3.28.3、Python 3.12.3。
- 本次 guest IP 为 `192.168.182.144`，账号 `edge`，工程 `/home/edge/project`。DHCP 地址以后可能变化，SSH 脚本可通过 VMware Tools 查询。
- Ubuntu 官方源网络慢；新 VM 内备份后改 USTC HTTPS，依赖手动安装完成。cloud-init 保留首次中断的历史 error，`/var/lib/edge-linux-ready` 及实际编译验证证明工具链可用。

## 已实际通过

| 项目 | 结果 |
|---|---|
| Linux C 严格编译 | `-std=c11 -Wall -Wextra -Werror -pedantic` 通过 |
| C 核心单测 | JSON、数值计算、FIFO/priority、增量分帧全部通过 |
| 固件可移植 C 编码器 | CMake + CTest，1/1 通过 |
| ESP-IDF 完整 ESP32 固件 | v5.5.6 + Xtensa GCC 14.2.0，应用与 bootloader 实际编译通过；应用 767568 字节 |
| Python 单测与真实 C 服务集成 | 15/15 通过，无 skip，约 2.59 秒 |
| ASan + UBSan C 核心 | 通过，无检测错误 |
| ASan + UBSan 真实服务集成 | 同一完整测试集 15/15 通过，约 4.72 秒 |
| 对照实验 | 两策略各 3 次，共 192 个任务完成，无拒绝 |
| 只读助手 | 实际读取报告并引用两类任务数值，缺报告时不下结论 |
| 图表 | 从真实报告导出 PNG/SVG，已检查渲染 |

集成测试实际覆盖：固件最大帧（8192 样本、最大序号、32 字节 ID）、每字节拆分、多帧合并、跨连接恢复、非法 JSON、NaN、超长帧、断线残帧、队满拒绝、非抢占优先级及同级稳定、SIGTERM 排空、禁止覆盖旧 CSV、非法参数、状态文件写失败。

完整固件使用官方 ESP-IDF 提交 `880a203ae603d08a94d06aa01e0d6761958e5e9e` 的必要组件快照，在 VM 中串行完成 742 个构建步骤；反汇编确认 `app_main` 的 Wi-Fi、TCP 连接、编码和发送路径实际链接进 ELF。仓库保存了 [构建日志](evidence/2026-10-23/firmware/build-default.log)、[反汇编](evidence/2026-10-23/firmware/app_main-disassembly.txt) 和 SHA256 [manifest](evidence/2026-10-23/firmware/manifest.json)。应用、bootloader、分区表、ELF 和默认 `sdkconfig` 保留在本机 `.local/firmware-build/`。应用 SHA256 为 `42119d9db83c71ebc4526db5b6797b39c4053e6ac623be7454de4c3a951c61eb`。

这份固件的 Wi-Fi SSID/密码为空，仅用于验证本地构建；上板前必须配置并重新编译。未执行任何烧录命令。

## 对照实测

数据来源为**合成数据**。每次输入 24 个普通任务（8192 样本）和 8 个紧急任务（64 样本），普通任务先形成积压；队列容量 64、窗口 8、passes=2000、单 worker。回放和服务固定在 guest CPU 0。两种策略使用同一个可执行文件和输入文件 SHA256，交替顺序运行。

| 策略 | 类型 | 完成 | 拒绝 | 等待 P95 ms | 完成 P95 ms |
|---|---|---:|---:|---:|---:|
| FIFO | 紧急 | 24 | 0 | 1054.768 | 1055.125 |
| priority | 紧急 | 24 | 0 | 57.733 | 58.082 |
| FIFO | 普通 | 72 | 0 | 969.852 | 1015.291 |
| priority | 普通 | 72 | 0 | 1026.914 | 1071.233 |

这是三次任务合并后的 nearest-rank P95，并非每次 P95 的平均值。本轮优先级调度减少了紧急任务的等待，同时普通任务的汇总 P95 上升。各次仍有波动，例如第三次 priority 普通任务 P95 低于同次 FIFO；不能把汇总取舍理解为每个任务必然变慢。

各次最大发送迟到约 5.98–9.63 ms。计划到达间隔相同，但真实到达不可能完全一致；guest CPU 固定也不能消除宿主负载和虚拟化影响。不作统计显著性、吞吐量提升或硬实时保证。

输入 SHA256：`c544f627861eb1f7ec154c17a6672c3a86550771202b6abc0e6a5d469097a155`。
实测 Linux 可执行文件 SHA256：`21a88335ee9786e12069f595907f0cb9233f78b3cc06712f89043d4e132d46cc`。

仓库中的 [证据快照](evidence/2026-10-23/README.md) 保存每次 `tasks.csv`、`metadata.json`、`status.json`；汇总为 [report.md](evidence/2026-10-23/comparison/report.md)、[report.json](evidence/2026-10-23/comparison/report.json)、[comparison.png](evidence/2026-10-23/comparison/comparison.png)、[comparison.svg](evidence/2026-10-23/comparison/comparison.svg)。本机 `results/comparison/` 还保留 `server.log`，打包备份为 `results/comparison-results.tar.gz`，输入为 `results/workload.jsonl`。后续运行产生的 `results/` 仍默认不加入 Git。

## 尚未验收

- 未连接/烧录 ESP32；Wi-Fi 断线恢复、真实网络丢帧、板上内存与看门狗需接板验证。
- 未录制硬件演示视频。
- 以上结果来自本机 VM；GitHub CI 的独立结果见仓库 [Actions](https://github.com/BJTURylynn/esp32-linux-edge-scheduler/actions)，不能将本地验证与 CI 状态混同。

## 重现

```bash
make test
make sanitize
EDGE_SERVER="$PWD/linux/build-sanitize/edge_scheduler" \
EDGE_FRAME_TEST="$PWD/build/firmware-host/test_frame" \
python3 -m unittest discover -s tests -v
python3 -m tools.replay generate results/workload.jsonl
python3 -m tools.experiment --workload results/workload.jsonl \
  --output results/comparison-new --repeats 3 --capacity 64 --passes 2000 --cpu 0
```

不覆盖已有对照结果。当前 CPU 0 可用，换环境时先查询允许的 affinity。
