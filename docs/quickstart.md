# 一键演示与项目学习入口

项目已有两个任务源：ESP32-C3经Wi-Fi/TCP，Arduino Uno经USB串口，汇入同一个WSL Linux C队列。C工作线程执行滑动平均和RMS；FIFO与优先级模式决定下一个任务先处理谁，已经执行的任务不被抢占。数据均为synthetic合成测试数据。

## 本机准备

1. 两板接USB：本次Uno=COM10，ESP32-C3=COM11。换USB接口可能改变编号。
2. 电脑和ESP32同连Shino 2.4GHz热点。电脑使用ESP32C3-Shino配置；原来的同名WPA3配置与兼容模式WPA2不匹配时，请在系统中重新连接。
3. ESP32已烧录EdgeWiFi，Arduino已烧录EdgeDevice；不要打开占用COM10的串口监视器。
4. 打开PowerShell，进入项目：

```powershell
cd D:\Projects\esp32-linux-edge-scheduler
$python = 'D:\Codex\esp32-linux-edge-scheduler\tools\device-venv\Scripts\python.exe'
```

新电脑需要Linux的GCC/Make/Python以及Windows Python可选串口依赖：`python -m pip install -r requirements-device.txt`。Windows使用WSL发行版Ubuntu-24.04，可通过参数更换名称；原生Linux也能运行演示入口。

## ESP32重启后的配置

重新上电会丢失内存里的Wi-Fi凭据。需要先执行（目录必须全新）：

```powershell
& $python -m tools.wifi_configure --port COM11 --ssid Shino --output D:/Codex/esp32-linux-edge-scheduler/outputs/device-config-next
```

在隐藏提示中输入热点密码。凭据不写入源码或报告。记下 `WIFI_READY ip=...` 的实际IP，下面使用它；地址可能变化。若设备一直在线，已配置过且IP不变，可跳过本步骤。

## 一条命令运行完整项目演示

```powershell
& $python -m tools.project_demo --esp-host 172.20.10.14 --uno-port COM10 --output D:/Codex/esp32-linux-edge-scheduler/outputs/my-project-demo
```

输出目录最后一级使用英文、数字、下划线或短横线；目录必须全新。重复可以用my-project-demo-v2。不要手动预先启动C服务，本入口会选择空闲端口，并拥有这次服务的生命周期。

这条命令会：

1. 在WSL本机Linux文件系统创建新的 `$HOME/edge-results/my-project-demo`，编译并启动C服务。
2. 两个接入线程同时拉取ESP32 TCP数据、接收Arduino串口帧，各20个。
3. 等待Linux处理完40个任务，正常停止自己启动的C进程，把结果复制到D盘输出。
4. 核对两设备各帧的device_id+seq、样本与mean/RMS，审计优先级选择顺序。
5. 保存 `result.json`、`report.md`、`service/tasks.csv`、`service/status.json`、`capture/*/frames.jsonl` 和各阶段日志。

默认capacity64、window8、passes1000000、priority，一个工作线程。较多的passes重复执行真实计算以便出现排队，没有sleep伪造算力。日常轻量功能验证可以加 `--passes 1`，但计算很快时不一定观察到优先级效果。`--policy fifo`可运行FIFO；参数相同但实时到达不同，不能将两次随意运行直接称为公平性能提升。

命令失败或Ctrl+C时会清理自己启动的C服务，保存失败阶段和日志，不停止别人的服务、不修改防火墙。协议控制兼容Windows CRLF与Linux LF。已有输出拒绝覆盖，避免把旧结果当新验收。

## 如何判断是真的通过

先看 `result.json` 的success，再看verification.verified和audit.policy_ok。`priority_behavior_observed`表示本轮确实出现了紧急任务优先取出的可核对证据；policy_ok=true但behavior=false只能表示没有观察到违规，不能声称有性能改善。

forwarded表示TCP发送完成，不是任务完成确认。最终以Linux CSV和数值验证为准。计时从Linux完整帧接收开始，不包含生成、USB和无线传输前半程，不是端到端延迟。

## 已有公平对照

[双设备实测证据](evidence/dual-device-scheduler/README.md)保存同40帧数据、另设2ms合成到达间隔、各策略三轮的回放。urgent等待P95为170.845→8.117ms，normal为159.699→163.057ms，六轮均40完成/0拒绝。优先级改善紧急等待，也可能延后普通任务，不代表提高吞吐或满足硬实时。

现有只读助手可解释该报告：

```powershell
& $python -m tools.agent ask '为什么紧急任务快了，普通任务却更慢？' --report docs/evidence/dual-device-scheduler/comparison/report.json
```

默认规则模式，不需模型Key、不产生模型调用；可选模型入口见agent.md。

## 建议你按这个顺序读代码

- `linux/include/edge.h`：Task、Queue与Server数据结构。
- `linux/src/core.c`：严格解析、有界队列、两种pop策略及计算。
- `linux/src/main.c`：TCP接收、互斥锁/条件变量、工作线程、状态与CSV。
- `firmware/esp32c3-arduino/EdgeWiFi/EdgeWiFi.ino`：设备Wi-Fi/TCP与合成任务。
- `tools/serial_bridge.py`、`wifi_bridge.py`：有界分帧与转发，不重放不确定数据。
- `tools/project_demo.py`、`demo_service.py`：跨Windows/WSL的生命周期和失败清理。
- `tools/dual_verify.py`、`schedule_audit.py`：如何用证据确认正确性和调度行为。

当前完成边界是一个Linux计算节点、两个任务源、两种应用层队列策略及只读解释。真实Wi-Fi断线恢复、断电自动接入、长期运行、多Linux节点分配和录像尚需单独验收。
