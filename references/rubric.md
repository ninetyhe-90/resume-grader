# 评分细则

## 1. 规则分(确定性,0-100)

| 分项 | 权重 | 计算 |
|---|---|---|
| completeness | 0.25 | 联系方式(邮箱或手机)+ 经历/教育/技能三个必需章节,命中数 / 4 |
| writing | 0.30 | 每条经历要点从 100 起扣分取均值:开头是职责词 -20,重复开头(同一经历 3 次以上) -10,过长 -10,第一人称 -10,无数字 -5 |
| metrics | 0.20 | 含数字的要点占比,50% 即满分 |
| date_consistency | 0.10 | 日期区间中占主导格式的比例(`2021.03` / `2021-03` / `2021年3月` / `Mar 2021` / 仅年份) |
| keywords | 0.15 | `--keywords` 命中率,整词匹配,中文子串匹配 |

无法评估的分项(没传关键词、日期少于 2 个、没有要点)记为 n/a,其权重按比例分给其余分项。

中文职责词:主要负责、负责、参与、协助、协同、配合、帮助、辅助、从事。英文:responsible for、helped、assisted、involved in、worked on 等。

日期只统计"区间"(两个日期被连接符隔开,或日期后接 至今/Present),避免把 "2000 users" 当日期。

## 2. Jev 质量分(语义,0-100)

一次请求,同一份 `state = {resume, target_job?}`,问题互相独立:

| 题 | 类型 | 说明 |
|---|---|---|
| impact | score 0-4 | 成果的具体、可度量程度 |
| depth | score 0-4 | 职责与主导程度,按职业阶段相对判断 |
| clarity | score 0-4 | 结构、简洁、一致性 |
| relevance | score 0-4 | 与 JD 的匹配(仅传 `--jd`) |
| verdict | choice | excellent / good / poor 总评 |
| vague_claims | noul | 空泛自评多于证据 |
| inconsistency | noul | 时间线或事实矛盾 |
| unexplained_gap | noul | 超过 6 个月未说明的空窗 |
| must_have_met | noul | JD 必备项均有证据(仅传 `--jd`) |

score 题不取 `score` 字段本身,而是用 `probabilities` 算期望等级,再换算为 0-100,这样两级之间的不确定性不会丢失。

质量分权重:impact 0.25、depth 0.20、clarity 0.10、relevance 0.25、verdict 0.20。没有 JD 时去掉 relevance,其余按比例放大。verdict 取概率加权:excellent 92、good 70、poor 30。

`noul` 题不带 `criteria`:实测 Aegis 网关对 `noul` 的任何 `criteria`(含 `null`、空对象)都返回 400,所以真假含义直接写在 `instructions` 末尾,例如 "Answer true only if ..."。

题目写法遵循 Jev 官方指南:每个等级都写明可观察的表现,不只写 low / high;每个 choice 选项写清触发条件;问题 id 不会展示给模型,完整问题必须写在 `instructions` 里。

## 3. 合成与闸门

```
composite = 0.65 * jev_quality + 0.35 * rules_score - risk_penalty
```

风险扣分(概率 >= 0.7 触发):vague_claims -8,unexplained_gap -5,inconsistency -10。

等级上限(取所有触发项中最低者):

| 触发条件 | 最高等级 |
|---|---|
| 找不到邮箱和手机 | 良 |
| 没有经历/项目章节 | 良 |
| 全文少于 300 字符 | 差 |
| inconsistency >= 0.7 | 良 |
| must_have_met < 0.3 | 良 |
| `--no-jev` 规则模式 | 良 |

阈值:优秀 >= 80,良 >= 60,其余为差。

人工复核标记:综合分距任一阈值不足 3 分;verdict 置信度 < 0.6;Jev 总评与综合等级不一致(哪怕只差一档)。只标记、不改等级:实测一份营销简历综合分 83.5 判优秀,而 Jev 总评为良(0.87),这类情况交给人判断,而不是为了让它"通过"去调阈值。

## 4. 为什么这样分工

- 模型判断归 Jev,但"多少分算优秀""什么情况下封顶""是否放行"归应用代码。Jev 只返回概率,阈值由应用拥有。
- 规则分可复现、可解释,用来托底和提供具体修改点;Jev 负责规则看不懂的语义。
- 缺失或畸形的 Jev 答案一律抛错,不使用"默认中等分"掩盖故障。

## 5. 已知限制

- Jev 可能给出"合法但错误"的答案;公开基准不能代替你自己的样本评估。
- 规则只看文本。PDF 的多栏、表格、页眉页脚、图片等 ATS 版式问题,文本提取后看不到,这类检查需要版面分析,本 skill 未实现。
- 姓名、公司等不会被遮蔽,发送前请确认可以外发。
- 单次请求上限 15000 字符(Jev 官方每请求 64k token,其中 state 与最长问题合计 32k)。
- Jev 控制台新注册曾出现暂停,拿不到密钥时只能用 `--no-jev`,或自建兼容该 API 的服务并设置 `JEV_ENDPOINT`(https,或本机 http)。
