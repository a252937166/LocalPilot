# 演示页面与截图

README 中两张 PNG 由仓库的实际 `agent/task_panel.html` 在 Chrome 中渲染并截图，使用 `scripts/serve_demo.py` 提供的固定演示数据。不会调用本机文件、shell、ChatGPT 或模型 API。示例标签、路径和回执均为公开展示用数据；它们不充当真实 Chat 验收证据。

```bash
.venv/bin/python scripts/serve_demo.py
```

在浏览器访问 `http://127.0.0.1:8765`，可以展开目标、按步骤筛选、展开执行回执。终端 Ctrl+C 停止服务。只监听回环地址，不提供本机控制接口。

`task-panel.png` 展示计划、验收与任务状态；`execution-receipt.png` 展示展开的执行输出。真实协议、浏览器与 Chat 验证范围在 [验证说明](VERIFICATION.zh-CN.md) 单独记录。
