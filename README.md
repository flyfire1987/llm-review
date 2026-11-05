# 大模型综述 · LLM Review

一套**大模型技术综述网站**，覆盖预训练 / 后训练 / 推理 / 架构四大板块，共 19 个技术点模块。

每天凌晨 03:00 自动从 arXiv、Hacker News、36氪 同步最新论文与新闻，**命中技术点关键词的内容会自动合并进对应模块页**，让综述随领域进展持续更新。

## 技术栈

- Python 3 + Flask
- 内容以 JSON 存储（`content/`），无数据库依赖
- 原生 JavaScript + CSS，无前端框架

## 目录结构

```
llm-review/
├── app.py              # Flask 入口，路由与页面渲染
├── store.py            # 内容读写、去重索引、模块页合并
├── sync_news.py        # 每日动态同步（抓取→打分→翻译→写入→合并）
├── content/            # 19 个技术点模块 + 阶段总览页
│   └── news/           # 每日动态（自动生成，不进版本库）
├── data/
│   ├── nav.json        # 导航树
│   ├── topics.json     # 技术点配置 + 关键词路由表
│   └── seen.json       # 去重索引（运行时状态）
├── templates/          # Jinja2 模板
├── static/             # CSS / JS
└── restart.sh          # 重启脚本
```

## 运行

```bash
./restart.sh                      # 启动（端口 8090）
curl http://127.0.0.1:8090/        # 首页
curl http://127.0.0.1:8090/news    # 每日动态
```

## 每日动态同步

```bash
python3 sync_news.py                # 执行一次
python3 sync_news.py --dry-run      # 只抓取打分，不写文件
python3 sync_news.py --limit 5      # 限制条数
```

**工作流程**：抓取数据源 → 关键词路由打分 → 去重 → 中译 → 写入当日 JSON → 合并进模块页。

**打分规则**：命中技术点关键词得分，长词权重更高；强相关（命中 ≥2 分）额外加分并触发模块页合并；**未命中任何技术点的内容一律丢弃**，避免无关论文占用名额。

**数据源**：

| 源 | 方式 | 内容 |
|---|---|---|
| arXiv | API | cs.CL / cs.LG / cs.AI / cs.DC 最新论文 |
| Hacker News | opencli | 英文社区讨论 |
| 36氪 | opencli | 国内科技新闻 |

翻译使用免费 MyMemory API，无需密钥；`sync_news.py` 内的翻译器按 `LLM API → argostranslate → MyMemory` 顺序降级。

## 配置

`data/topics.json` 顶部为全局配置：

| 键 | 含义 | 默认 |
|---|---|---|
| `_daily_max` | 每日收录条数 | 10 |
| `_merge_threshold` | 合并进模块页的命中阈值 | 2 |
| `_merge_max_per_page` | 单页最多合并条数 | — |
| `_retention_days` | 动态保留天数 | 30 |

各技术点的 `keywords` 决定路由准确度，**新增关键词时优先用长词**（长词权重更高、误命中更少）。

## 定时任务

```
cron 0 3 * * * @ Asia/Shanghai
→ cd llm-review && python3 sync_news.py
```

由 OpenClaw cron 管理。
