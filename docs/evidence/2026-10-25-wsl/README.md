# 2026-10-25 WSL 本机补验

在用户电脑新安装的 Ubuntu 24.04 / WSL 2 中，验证此前 PR #1 的本地源码快照。Ubuntu 的虚拟磁盘存放在 D 盘；项目构建与实验在 Linux 原生文件系统中执行。仅修正绘图脚本把环境写死为 VMware 的标题，调度、通信和 Agent 实现未改动，未覆盖 2026-10-23 的 VMware 历史证据。

系统与工具版本、被测源码逐文件 SHA256 见 [manifest.json](manifest.json)。测试日志中的目录是当次运行位置，后续可以从自己的仓库目录复现。

## 补齐结果

| 检查 | 结果 | 原始证据 |
| --- | --- | --- |
| C11 严格编译、C 核心测试 | 通过；包含 `-Wall -Wextra -Werror -pedantic` | [test.log](test.log) |
| 可移植固件编码器 CTest | 1/1 通过 | [test.log](test.log) |
| Python 与真实 C 服务测试 | 42/42 通过，0 跳过 | [test.log](test.log) |
| ASan/UBSan C 核心测试 | 通过 | [sanitize-build.log](sanitize-build.log) |
| ASan/UBSan 下完整测试 | 42/42 通过，0 跳过 | [sanitize-tests.log](sanitize-tests.log) |
| FIFO/priority 对照实验 | 每种策略 3 次，共 192 个任务，全部完成、0 拒绝 | [报告](comparison/report.md)、[实验日志](experiment.log) |
| 离线助手读取新报告 | 正确读取新状态和测量值，`model_calls=0` | [agent-offline.json](agent-offline.json) |

此前在 Windows 跳过的 9 项已在本机真正执行：1 项固件编码器跨端测试、4 项服务集成测试和 4 项生命周期测试。涵盖分包/粘包、重连、非法报文隔离、Python 数值参考对照、优先级且不抢占、队列过载、排空退出、输出文件保护及状态写入失败。

## 本次调度测量

输入是明确标注的**合成数据**，每次 24 个普通任务及 8 个紧急任务。两种策略使用相同输入与参数、容量 64、窗口 8、计算 2000 passes、一个工作线程，固定在同一允许 CPU 上；第二组颠倒策略顺序。具体配置、接收时刻及发送迟到见各运行的 `metadata.json` 和 `tasks.csv`。

| 类型 | FIFO 等待 P95（ms） | priority 等待 P95（ms） |
| --- | ---: | ---: |
| 紧急 | 530.318 | 9.378 |
| 普通 | 483.976 | 496.282 |

P95 为三次运行完成任务合并后的 nearest-rank 分位数，每次结果见 [完整报告](comparison/report.md)。优先级降低本次紧急任务等待的同时，普通任务等待有所增加；运行中的任务不会被抢占。该结论只描述本机这组负载，不能推断开发板性能、硬实时保证或整体吞吐提高。

[输入](workload.jsonl)、[结构化报告](comparison/report.json) 和六次运行的原始数据均随本目录保存。Agent 读取的状态是已结束实验的快照，不是常驻服务在线证明。

图表：[PNG](comparison/comparison.png)、[SVG](comparison/comparison.svg)。图中散点显示单次运行的 P95，不是置信区间。

## 复现

在 Ubuntu 的项目根目录执行，输出目录必须未使用过，以免覆盖历史证据：

```bash
make test
make sanitize
EDGE_SERVER="$PWD/linux/build-sanitize/edge_scheduler" \
EDGE_FRAME_TEST="$PWD/build/firmware-host/test_frame" \
python3 -m unittest discover -s tests -v

python3 -m tools.replay generate results/wsl-recheck/workload.jsonl
cpu=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
python3 -m tools.experiment \
  --workload results/wsl-recheck/workload.jsonl \
  --output results/wsl-recheck/comparison --cpu "$cpu"

python3 -m tools.agent ask "两种策略的等待时间有什么差异？" \
  --backend rules \
  --status results/wsl-recheck/comparison/run-03-priority/status.json \
  --report results/wsl-recheck/comparison/report.json --json
```

## 仍需实物或其他环境验证

本次只构建固件的可移植主机编码器，未在本机安装完整 ESP-IDF 或重做完整固件构建。此前完整 ESP-IDF 构建已在 PR #1 的 GitHub Actions 成功。未烧录 ESP32，未验证真实 Wi-Fi 连接/重连；TCP 重连测试来自电脑模拟客户端。未调用真实大模型 API，也未产生模型费用。
