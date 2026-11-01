# S3 检索设计问答与设计思维

> 记录 S3（语义召回 + 精排 + 容错）落地过程中的几个关键概念辨析与设计原则，供后续回顾。
> 相关：验收报告 [2026-11-01-s3-acceptance-report.md](_build_log/2026-11-01-s3-acceptance-report.md)、
> 交互图 [output/s3-pipeline.html](../output/s3-pipeline.html)、
> 完整流程脚本 [backend/scripts/verify_s3_full.py](../backend/scripts/verify_s3_full.py)。

## 一、四个核心概念辨析（Q&A）

### Q1：S2 的「打分」和 S3 的「召回」有什么区别？

- **打分是一个机制，召回是一个决策。** 打分是「给每个候选算一个语义分」；召回是「决定用哪条路把候选捞回来」。前者是后者里的**一个分支**。
- 打分 = embedding + 余弦，对**全部候选**算分，无失败处理。
- 召回 = 三选一降级链（`exact_id_lookup → embedding → keyword_2gram`），并上报 `recall_strategy`。
- 打分没消失：它降级成召回里的「embedding」分支。

流程区别：

```
S2：打分 → 过滤 → 排序 → 去重 → 截断 → 组装      （6 段，单一打分机制）
S3：召回 → 过滤 → 精排 → 去重 → 截断 → 组装      （6 段，召回决策 + 精排）
      ↑ exact_id / embedding / keyword_2gram        ↑ 新增
```

三个具体变化：①「打分」→「召回」（从机制升为决策）；②「排序」被吸收进召回（embedding 分支天然排好序）；③新增「精排」。

### Q2：S3 是不是对「打分」和「精排」环节的有限替代？

- **一半对**：S3 的改动确实「有限」，且全部落在**排序层**；过滤/去重/截断/组装四个阶段原样继承 S2，一行没改。
- **要修正**：准确说不是「替代」，是「加固 + 补缺」——
  - 打分 → 召回 = **加固**（embedding 余弦核心没换，只是外面包了一层降级链 + 加了 exact_id 快路径）。
  - 精排 = **补缺**（S2 根本没有精排，S3 才从无到有）。
- 更准的说法：S3 把「一段排序」补成了「两段排序」（粗召回 + 细精排）。

### Q3：「排序」和「精排」的区别是什么？

- **排序 = 按已有的分重排，不产生新分。** `sorted(list, key=score)`。免费、确定、不会失败、无依赖。
- **精排 = 用一个更细的模型重新算分，再按新分重排。** cross-encoder 推理。贵（只对 top-K）、可失败（所以有 `rerank_status`）。
- **区分标准就一条：这个环节产不产生「新分数」。**

```
排序前：A(0.90) B(0.80) C(0.75)      ← 召回(embedding)给的分
排序后：A(0.90) B(0.80) C(0.75)      ← 分数没变，只换了顺序

精排前：A(0.90) B(0.80) C(0.75)      ← 召回给的分
精排中：cross-encoder 重算 → A(0.85) B(0.92) C(0.60)
精排后：B(0.92) A(0.85) C(0.60)      ← 分数变了，B 反超 A
```

### Q4：为什么召回和精排设计成「有限支持」？遇到什么情况会失效？

**设计原因（为什么有限）：**

1. **外部依赖不可靠** —— 过滤/去重是纯 Python 算结构化字段，永远可靠；embedding 靠远程 HTTP（网络+端点+key），rerank 靠本地模型（首次要下载），都会挂。
2. **成本不对称** —— 精排只对 top-K 打分（`search_rerank_top_k=30`）不对全量；embedding 有超时预算（`openai_embedding_timeout_seconds`）。
3. **降级 > 失败** —— 宁可交出「词面兜底的结果」或「没精排的召回序」，也不该因为语义层挂了就 500。

**失效情况：**

| 环节 | 失效原因 | 触发 → 降级 |
|---|---|---|
| 召回(embedding) | 地址改错 | 连接异常 → keyword_2gram（S3-2） |
| 召回(embedding) | 向量服务超时 | `httpx.TimeoutException` → keyword_2gram（S3-6） |
| 召回(embedding) | key 缺失/失效 | 401 → `raise_for_status()` → keyword_2gram |
| 召回(embedding) | 端点无 `/embeddings` | DeepSeek 默认 base_url 就没有 → keyword_2gram |
| 精排(cross-encoder) | 模型首次下载失败 | 无网/HF 不通 → 保留召回序 + `rerank_status=failed` |
| 精排(cross-encoder) | ONNX 推理报错 | 模型损坏/不兼容 → `failed` |
| 精排(cross-encoder) | 根本没配 reranker | `None` → `disabled`（S3-3） |

## 二、设计原则（提炼）

1. **语义层尽力而为，结构层永远保证**：硬约束（过滤/去重）是硬保证，语义（召回/精排）是软增强、可降级。这是采购 agent 的生命线——结构化约束出错致命，语义相关性出错只是「排序差点」。
2. **降级链每层独立降，不挂死**：embedding 挂→keyword_2gram；精排挂→保留召回序；全挂→空结果 + `empty_reason`。每一档是「更差但可用」，不是「报错」。
3. **两段排序分工**：粗召回保命中（双塔 embedding，快而粗），细精排保精度（cross-encoder，慢而细）。一个捞全集、一个捞 top-K。
4. **可观测是降级的根**：`recall_strategy`（走哪条路）、`diagnostics.rerank_status`（精排成没成）、`diagnostics.latency_ms`（多慢）——决策/失败/成本三件事都必须可见。
5. **keyword_2gram 是独立备胎，不是劣化版 embedding**：字符 bigram 用「字面」信息，embedding 用「语义」信息，二者互补——这正是「买家的话和标题对不上」时字面兜底还能捞回东西的原因。

## 三、降级矩阵

| 场景 | 行为 | 结果 |
|---|---|---|
| 全好 | embedding 召回 + cross-encoder 精排 | 满血 |
| 召回挂 | keyword_2gram 召回 + 精排 | 少了语义召回（字面兜底） |
| 精排挂 | embedding 召回 + 无精排 | 少了精排质量（保留召回序） |
| 全挂 | 空 + `empty_reason` | 明确说明，不抛异常 |

## 四、一句话总结

S3 = S2 的硬约束不变，把「打分」升维成带降级链的「召回」，补上缺失的「精排」，并给整条链加了可观测——**语义层尽力而为、结构层永远保证**。
