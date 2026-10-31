# memory-core

[English](README.md) | **中文**

给 AI 伙伴用的长期记忆核心：数据存在本地，不绑定任何框架。

> 状态：**预览版（M2）**。存储、混合回忆、导出格式、用大模型整理记忆、MCP 服务、AstrBot 适配器、每日日记与自我认知、记忆的新陈代谢（淡忘、合并、彻底遗忘）都已可用并有测试。接口可能还会变。

## 为什么做

换了聊天框架、换了大模型、换了向量模型之后，AI 伙伴应该还是"它"。多数记忆插件把数据存成插件私有的格式，很多还把向量当成记忆本身。但向量只属于某一个嵌入模型，换模型就作废了。

memory-core 的几条原则：

1. **文字才是记忆，向量只是索引。** 原始经历和可读的记忆永久保存；向量随时可以用任何模型重算（`memory-core reindex`）。
2. **原始经历只追加、不修改。** 其他一切（记忆、日记、画像、索引）都由它推导而来，并记录来源。因此可以重建、审计，也可以顺着来源链彻底删除。
3. **不绑定框架。** 核心是 Python 库（MCP 服务马上就有），各聊天框架通过很薄的适配器接入。
4. **不绑定模型。** 嵌入模型可以用任何兼容 OpenAI 接口的服务（Ollama、vLLM、LM Studio、OpenAI……）。
5. **本地优先。** 一个 SQLite 文件加一个文件夹。没有遥测，除了你配置的模型接口，不发起任何网络请求。
6. **可迁移。** 有版本号的纯文本导出格式，叫"灵魂包"，适合用 git 管理（[SPEC.md](SPEC.md)）。
7. **按作用域保护隐私。** 每条记忆都有可见范围，由代码强制执行：一个群里发生的事，不会在另一个群里说出来。

## 概念

| | 是什么 | 能否重建 |
|---|---|---|
| **经历 Episode** | 原始经历：收到或发出的消息、看到的东西（网页、视频）、做过的事 | 不能，它是唯一真源 |
| **记忆 Memory** | 推导出的一句话记忆：事实、事件、关系、反思、日记、喜好、词汇（梗）、风格 | 能，从经历重新整理 |
| **实体 Entity** | 人、群、地点、作品……可以有多个别名（账号、昵称） | — |
| **身份 Identity** | Markdown 写的"核心记忆"：`self.md`、`owner.md`、`now.md` | 手工维护 |
| **索引** | 全文索引（SQLite FTS5）和向量，按模型区分 | 随时可以重建 |

**作用域**：`public`（哪里都能说）、`self`（它自己的感受）、`owner`（只在和主人私聊时）、`space:<场所>`（只在那个场所）。

## 快速开始

```bash
pip install -e ".[zh]"          # zh = jieba，中文全文检索更准（可选）
memory-core init ./soul
memory-core record ./soul --space web --source browser --kind observation --content "看了一个讲原神的视频"
memory-core remember ./soul --scope public --kind fact --text "原神是一款开放世界游戏"
memory-core recall ./soul "原神" --space web
memory-core export ./soul ./soul-bundle      # 导出纯文本备份
```

语义检索：在本机运行嵌入模型，例如 `ollama pull bge-m3`，然后在 `soul/config.toml` 里写：

```toml
[embedding]
provider = "openai-compatible"
base_url = "http://127.0.0.1:11434/v1"
model = "bge-m3"
```

再运行 `memory-core reindex ./soul`。

## 整理、MCP、适配器

- **整理**：在 `soul/config.toml` 里配置 `[llm]`（任何兼容 OpenAI 接口的模型，Key 放在环境变量里，用 `api_key_env` 指定），然后运行 `memory-core consolidate ./soul`。模型看到新消息和已有的相关记忆后，提出新增、更新（旧记忆保留并标记为已取代）或不动。**模型只负责提议，可见范围由代码决定**：关于人的记忆只留在学到它的那个场所（或主人私聊），只有不涉及任何人的内容才能公开。**公开场所**（`[spaces] public`，默认 `["web"]`，例如用 [surfing-the-web](https://github.com/JikunLiu101/surfing-the-web) 自己上网看到的）里看到的内容会公开，从它自己的想法整理出的记忆只给主人。
- **MCP 服务**：`memory-core mcp ./soul`（只读加 `--read-only`），Claude Code 等任何 MCP 客户端都能共用同一份记忆。
- **AstrBot 适配器**：见 [`adapters/astrbot`](adapters/astrbot/README.md)。AstrBot 4.24.2 起，后台「插件 → memory-core → 视图 → memory」是一个只读面板：总览、记忆（来自哪几句话、每个版本）、召回调试（每项得分、在哪条路线排第几，可对比两个地方会想起什么，调试不会让记忆变强）、图谱（记忆是关于谁、什么的）。

路线图、致谢和 Python 用法见[英文版](README.md)。

## 许可证

[Apache-2.0](LICENSE)
