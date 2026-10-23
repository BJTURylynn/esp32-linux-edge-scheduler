# 2026-10-23 本地验收证据快照

这是 VMware Ubuntu 中一次合成负载实验及 ESP32 本地构建的原始证据，采集环境和验收边界见 [validation.md](../../validation.md)。

- [实验报告](comparison/report.md)、[结构化报告](comparison/report.json)、[PNG 图表](comparison/comparison.png)、[SVG 图表](comparison/comparison.svg)。
- `comparison/run-*/` 保存六次运行各自的逐任务 CSV、配置/输入哈希和最终状态。未修改原始测量数值。
- [固件构建日志](firmware/build-default.log)、[app_main 反汇编](firmware/app_main-disassembly.txt)、[源码与产物 SHA256 清单](firmware/manifest.json)。清单中的 BIN/ELF 等大文件保留在本机 `.local/firmware-build/`，不包含在此快照中。

输入由 `python3 -m tools.replay generate results/workload.jsonl` 生成。实验复现步骤见验收记录；快照保留历史结果，后续重跑请使用新的输出目录。

固件清单中的源码 SHA256 对应当次 Windows 工作副本的字节（部分文件为 CRLF）；仓库使用 `.gitattributes` 将源码统一为 LF，跨平台检出后的字节哈希可能不同。证据快照自身保留原始字节，日志和产物哈希仍以当次清单为准。
