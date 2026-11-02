# 参考来源与取舍

学习时间:2026-11-02。克隆到 `/tmp/resume-refs` 阅读,未复制源码,只吸收思路并按本 skill 需要重写。

## srbhr/Resume-Matcher

读过:`apps/backend/app/services/ats.py`、`writing_quality.py`、`bullet_scoring.py`、`text_match.py`、`ats_parse/checks.py`。

采用:

- 加权的多分项评分,无法评估的分项记 None,权重重新分配。
- 章节完整性 + 联系方式检查。
- 日期格式一致性(统计主导格式占比),并扩展到中文 `2021年3月`、`2021.03`。
- 写作规则:职责词开头、第一人称、过长要点、无数字、同一经历重复开头。惩罚分值沿用其"硬 40 / 软 10 / 无数字 5"的层次,但中文的"负责"太常见,统一降为 20。
- 整词关键词匹配,中文用子串匹配。
- 要点相关度打分"只打分,不改写"的原则:评估与改写分开。

不采用:

- 关键词和标题匹配的主体依赖其 LLM 抽取的 JD 关键词;这里改为由助手抽取后通过 `--keywords` 传入,语义匹配交给 Jev 的 relevance / must_have_met。
- summary 章节要求:中文简历常无个人简介,所以只作为信息,不计入完整性。
- 版面类 ATS 检查(多栏、表格、文本框、页眉页脚):需要 PDF 版面信息,纯文本拿不到。

## xitanggg/open-resume

读过:`parse-resume-from-pdf` 目录结构(read-pdf、group-text-items-into-lines、group-lines-into-sections、feature-scoring-system)。

采用的思路:简历必须先能被还原成"行 -> 章节"的结构,后续评分才有意义;因此本 skill 把"提取不出文本/乱码"当成硬错误,而不是低分。

不采用:其基于特征打分的字段抽取(姓名、学校等结构化抽取),与评分目标无关。

## OmkarPathak/pyresparser

仅克隆,未深入阅读。其定位是基于 spaCy / NLTK 的字段抽取(姓名、技能、学历),与"评分"目标不同,未采用。

## TypeSafe AI Jev

- 端点 `POST https://api.typesafe.ai/v1/systemone`,请求体 `model / state / questions`,`Authorization: Bearer <key>`。
- 三种问题:`noul`(是否,0-1 概率)、`choice`(选项概率)、`score`(有序等级,带 `probabilities`)。
- 错误码:401 密钥错误,403 缺少密钥,400 未知模型或 state 过大,422 字段缺失,429 限流,529 过载。本客户端只对 429 / 529 重试,其余直接抛错。
- 模型固定为 `jev-1.13.0`,避免别名 `jev-latest` 悄悄变化;可用 `JEV_MODEL` 或 `--model` 覆盖。
- 2026-11-02 已用兼容网关实测:`/v1/systemone` + `jev-1.13.0` 的响应结构基本一致;偏差只有一处,`noul` 题不接受 `criteria`(见 rubric.md)。
- 以上细节来自第三方整理页面(jevaiguide.com、autojev.ai、typesafeai.org),这些站点声明与 TypeSafe AI 无隶属关系;官方文档在 docs.typesafe.ai。首次接入请用真实密钥跑一次并核对响应结构,客户端对不符合预期的响应会抛错而不是猜测。
