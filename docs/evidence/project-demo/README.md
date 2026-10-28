# 一键流程实物验收

此目录保留Windows一条命令实际驱动两块开发板、WSL C服务、计算验算及调度审计的结果。

入口：`python -m tools.project_demo --esp-host <实际ESP32 IPv4> --uno-port <Uno串口> --output <新输出目录>`。

[结果](result.json)：success=true；Uno20、ESP32-C3 20任务，全部完成、0拒绝、最大数值误差0。priority规则审计通过，实际观察到8次紧急优先选择，同优先级FIFO、无单线程计算重叠。

[报告](report.md)、[C结果](service/tasks.csv)、[最终状态](service/status.json)、[监督日志](supervisor.log)、[双设备捕获](capture/summary.json)和[文件哈希](manifest.json)一并保存。C服务自动选择端口、在Linux原生文件系统运行，并在正常收尾后归档到D盘。最终running=false，未遗留该轮监听服务。

首次Windows端到端尝试曾因控制行CRLF被Linux当成未知命令而失败，虽然当时40个计算已完成，整体仍正确标为失败。修复为接受CRLF/LF后新增对应真实C生命周期测试，此目录是重新执行通过的第二轮，不改写第一轮失败。本机失败证据仍保留在D盘outputs/project-demo。

全部样本为synthetic。Windows TCP转发是明确的链路节点；ESP32任务走Wi-Fi、Uno走USB。结果不能当作真实传感器、Wi-Fi吞吐上限、硬实时或断电恢复证明。默认不调用模型，无API Key；目录不包含Wi-Fi配置凭据、串口密码输入或OS保存的网络配置。
