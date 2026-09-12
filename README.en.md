# LocalPilot

**Local AI coding from ordinary ChatGPT web Chat.**

Read a local project, load its skills, edit files, run tests, and inspect the actual results through a private MCP connection. LocalPilot runs on your Mac and uses the model available in your existing Chat. It does not start Codex, switch to Work, or initiate additional model-generation API calls.

[中文 / full guide](README.md) · [Tunnel setup](docs/TUNNEL.zh-CN.md) · [Configuration](docs/USAGE.zh-CN.md)

![LocalPilot task panel with demo data](docs/images/task-panel.png)

The screenshot renders the real component with labeled public demo data. It is not a fabricated ChatGPT conversation.

Features include file search/read/write/patching, shell jobs, local skill discovery, image reading and pixel transforms, generated-file saving, Chrome navigation and screenshots, optional local MCP bridging, and persistent plans with evidence-based completion checks.

“Free” means no LocalPilot software usage fee or additional model API usage initiated by this project. You still need eligible ChatGPT features and Tunnel/developer-mode access. Chat uses tokens and remains subject to account limits and billing. There is no unlimited-usage claim or measured universal token-savings percentage. [Official usage guidance](https://learn.chatgpt.com/docs/enterprise/chatgpt-work-usage-and-cost)

Tested on macOS with Python 3.12 and tunnel-client 0.0.14. Each user deploys their own Agent and private Tunnel; the repository does not expose the author's machine or provide a published public-store plugin. Follow the Chinese setup guide for the complete install, workspace association, runtime key, Chat connection and verification steps.

The task controller persists state and rejects premature completion. It cannot install a native Stop hook into ordinary Chat or guarantee uninterrupted hour-long execution. Basic image transforms and native generative editing are separate capabilities.

Independent project, not affiliated with OpenAI. No open-source license has been selected for this repository; dependencies retain their own licenses.
