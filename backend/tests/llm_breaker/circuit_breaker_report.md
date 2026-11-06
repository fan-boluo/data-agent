# LLM 熔断 / 恢复验证

- 正常请求耗时：13.53s
- 熔断触发：第 2 个故障请求后全部 breaker OPEN
- 全熔断期耗时：1.59s（对比正常 13.53s，快速失败应低一个量级）
- 恢复后状态：closed×1 / open×2
- 全链已恢复模型：qwen3.8-max-0902, glm-5.3, deepseek-v4.1-flash（3/3）

| 阶段 | 结果 | 耗时(s) | 熔断器状态 | 备注 |
|---|---|---|---|---|
| 预热（正常） | 成功 | 13.53 | closed×3 | - |
| 故障请求 #1 | 失败 | 6.56 | closed×3 | {"session_id":"cbtestsess-cbtest-41a12955","request_id":"cbt |
| 故障请求 #2 | 失败 | 6.78 | open×3 | {"session_id":"cbtestsess-cbtest-e5e700c8","request_id":"cbt |
| 全熔断（快速失败） | 失败 | 1.59 | open×3 | {"session_id":"cbtestsess-cbtest-b8ee242d","request_id":"cbt |
| 恢复探测 #1 | 成功 | 2.75 | half_open×1 / open×2 | - |
| 恢复探测 #2 | 成功 | 2.78 | closed×1 / open×2 | - |
| 恢复探测 #3 | 成功 | 2.70 | closed×1 / open×2 | - |
| 恢复探测 #4 | 成功 | 2.11 | closed×1 / open×2 | - |
| 恢复探测 #5 | 成功 | 2.03 | closed×1 / open×2 | - |
| 恢复探测 #6 | 成功 | 2.55 | closed×1 / open×2 | - |
| 恢复探测 #7 | 成功 | 2.83 | closed×1 / open×2 | - |
| 恢复探测 #8 | 成功 | 2.53 | closed×1 / open×2 | - |
| 恢复探测 #9 | 成功 | 3.58 | closed×1 / open×2 | - |
| 恢复探测 #10 | 成功 | 2.05 | closed×1 / open×2 | - |
| 推进 1-1 | 成功 | 5.73 | closed×1 / half_open×1 / open×1 | - |
| 推进 1-2 | 成功 | 2.80 | closed×1 / open×2 | - |
| 推进 2-1 | 成功 | 2.56 | closed×1 / half_open×1 / open×1 | - |
| 推进 2-2 | 成功 | 2.52 | closed×1 / open×2 | - |