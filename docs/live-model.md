# 真实大模型接入与只读工具演示

已使用用户选择的 `coding-glm-5.3-flash-free` 在AIHubMix官方兼容接口完成真实工具调用验证。服务地址：`https://aihubmix.com/v1`，官方模型页：https://aihubmix.com/model/coding-glm-5.3-flash-free 。请求路由保持指定的-free ID，服务响应中的上游model字段为glm-5.3-flash。

## 运行

在项目根目录执行：

```powershell
$python = 'D:\Codex\esp32-linux-edge-scheduler\tools\device-venv\Scripts\python.exe'
& $python -m tools.agent_live --output D:/Codex/esp32-linux-edge-scheduler/outputs/llm-next
```

入口会隐藏提示输入API Key，或从本进程的 `EDGE_AGENT_API_KEY` 读取；不写入源码、输出或GitHub。每次选择全新输出目录，重复可使用llm-next-v2。这个入口显式使用选定模型，原 `tools.agent` 的默认离线规则行为保持不变。

带自定义问题：

```powershell
& $python -m tools.agent_live '先查报告，再用200字说明紧急等待下降是否意味着吞吐提升。' --output D:/Codex/esp32-linux-edge-scheduler/outputs/llm-question
```

默认读取仓库已有的历史实物状态和双设备回放报告；它们是已完成实验的快照，不表示当前进程在线。使用自己的本次结果时，显式传 `--status <状态文件> --report <对照报告>`。

## 本次真实验收

- 返回mode=llm，reason=null，success=true，不是规则fallback。
- 两次真实HTTP请求均为200。
- 首次响应finish_reason=tool_calls，模型请求query_status和read_report；程序校验空参数后读取固定文件并返回筛选后的字段。
- 第二次响应finish_reason=stop，给出完整解释。
- 两工具均available=true，保存了API响应ID、模型ID、token计数、耗时及输入证据SHA256。
- 最终两次请求元数据计数合计2301 tokens，不包括首轮探测。
- 模型引用了紧急等待P95 170.845→8.117ms、普通159.699→163.057ms，解释了非抢占与等待取舍，明确不能据此证明吞吐提高或硬实时。

本次没有让模型控制调度：仍只有query_status/read_report两个无参数只读工具。工具路径由程序指定，模型看不到任意主机文件或命令权限。C计算及两块板的程序不依赖模型。

## 完整性与失败检查

不能以CLI退出0或答案含正确数字证明接入。实测入口同时检查mode、reason、模型请求数、成功工具轨迹、HTTP响应及结束原因，失败返回非零并保留诊断。

首轮现有入口能完成工具闭环，但长回答尾部不完整。新增有界输出预算和length/content_filter拒绝，实测入口默认1600 tokens，允许64..4096；本次又以简短问题重测并核实stop结束。没有添加不兼容的关闭thinking设置。

核心 `tools.agent` 继续默认800，可以显式使用 `--max-output-tokens 1600`。请求仍有5轮默认预算、单次60秒实测超时、256KiB响应上限，不自动重试HTTP失败，不切换到付费模型。配置一致仍有系统噪声；提示词禁止无SLO情况下判断普通延后“代价可控”或宣称适合生产。

## 证据

本机：D:/Codex/esp32-linux-edge-scheduler/outputs/llm-integration/live-validation/result.json、answer.md、report.md。
仓库：docs/evidence/live-llm/，只包含脱敏后的回答、白名单API元数据、证据哈希和说明，不包含密钥、Authorization或reasoning_content。

自动测试使用模拟响应，不消耗真实API；本次真人授权的网络验证与自动测试独立记录。模型回答仍需核对程序统计摘要，不能把一次解释正确视为消除了幻觉。
