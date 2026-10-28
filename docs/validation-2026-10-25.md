# 增量验证记录 2026-10-25

本次沿用提交 `bd1d85cd26d7dea17e73ab8629c4af2ea406f3d7` 的实现与目录结构。合作者的 VMware 实验、固件构建和证据快照保留原样；原记录见 [2026-10-23 验证](validation.md)。

## 修改内容

- 给原有只读助手增加 `ask` 入口，默认离线；模型接入需要显式配置并选择 `--backend llm`。
- 保持两个无参数工具：状态查询、报告读取。模型只能选择工具，不能选择文件路径或修改服务。
- 添加调用次数、输入/响应大小、参数和证据结构检查；错误、重复调用或预算耗尽后回退到本地解释。
- 模型解释旁附程序生成的证据摘要，复用已读取的快照；发送给模型的数据不包含主机完整路径或原始 metadata。
- 修复 SIGTERM 关闭接收后状态文件仍短暂显示 `accepting:true` 的问题，新增排空阶段的中间状态断言。

## 本机已运行

**更新：下方 Windows 记录保留为历史。安装 WSL 后已在本机 Ubuntu 补齐 Linux 验证：普通构建与 ASan/UBSan 构建均为 42 项全部通过、0 跳过，另完成六次调度实测。新证据及复现步骤见 [WSL 本机补验](evidence/2026-10-25-wsl/README.md)。**

首次验证环境为 Windows、Python 3.12；当时没有合作者的 VMware 虚拟机和 Linux C 编译环境。

| 检查 | 结果与边界 |
| --- | --- |
| Python 测试发现 | 共 42 项，其中 33 项通过、9 项按环境条件跳过 |
| 模型传输测试 | 11 项通过；请求头与参数、URL/重定向、256 KiB 上限、403/超时、异常脱敏与响应结构 |
| 问答及证据测试 | 16 项通过；两个工具、禁止选路径、深层参数拒绝、API 失败不重试、最大次数、重复工具、快照复用、无效报告与离线 CLI |
| 原有纯 Python 工具测试 | 6 项通过 |
| 历史报告离线问答 | 实际读取 `docs/evidence/2026-10-23/comparison/report.json`，模式为 `rules`，模型请求数为 0 |

跳过的 9 项需要真实编译的 Linux 服务及固件主机编码器：1 项固件跨端测试、4 项服务集成、4 项生命周期测试。不能将它们记作本机已通过。

本机测试命令：

```powershell
New-Item -ItemType Directory -Path .local/tmp -Force
$env:TEMP = (Resolve-Path .local/tmp).Path
$env:TMP = $env:TEMP
python -X utf8 -m unittest discover -s tests -v
```

把临时目录放在 `.local/tmp` 是为适配本机沙箱目录权限，不改变产品行为。该目录被 Git 忽略。

## Linux 与硬件验收范围

Linux 严格编译、C 核心、全部 Python/C 集成、ASan/UBSan 及完整 ESP32 固件构建，交由仓库现有 [Ubuntu CI](https://github.com/BJTURylynn/esp32-linux-edge-scheduler/actions) 在提交后运行。实际状态应读取对应提交的 Actions/PR 检查结果，本段不预先将其标记为通过。

未访问真实模型服务、未读取用户已有模型凭证、未消耗模型 API 费用。模拟响应证明的是客户端协议与调度边界，不代表真实供应商或模型的效果已验收。

未连接或烧录 ESP32，没有验证真实 Wi-Fi 重连，也没有重新测量 FIFO/priority 性能。原有 2026-10-23 报告只作为离线问答证据读取，不能算作本次新实验。
