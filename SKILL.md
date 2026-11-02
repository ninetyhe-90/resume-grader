---
name: resume-grader
description: 给简历打分并判定 优秀/良/差。用确定性规则检查(联系方式、章节、日期格式、量化成果、措辞、JD 关键词)加 TypeSafe Jev 决策模型做语义判断(成果证据、职责深度、清晰度、岗位匹配、夸大/时间线矛盾风险),按应用侧阈值和闸门给出最终等级与改进建议。当用户要求评估简历、给简历打分、筛简历、判断简历好坏、resume score、grade resume、screen CV,或提供简历文件(pdf/docx/md/txt/json)并想知道它是优秀、良还是差时使用。
---

# resume-grader

给一份简历输出 **优秀 / 良 / 差** 三档结论、分项分数、触发的闸门和可执行的改进建议。

分工(沿用 Jev 的设计原则:模型只做有限选项的判断,阈值与动作归应用):

- `scripts/resume_features.py`: 确定性检查,无 LLM,可复现。
- `scripts/jev_client.py`: 调用 Jev。Jev 返回 noul / choice / score 的概率,不生成文字。
- `scripts/score_resume.py`: 合成分数、套用闸门、划分等级、输出报告。

## 工作流

1. 确认输入。简历路径(`.pdf .docx .md .txt .json`)。PDF 依赖 `pdftotext`(`brew install poppler`)。扫描件需先 OCR,脚本检测到乱码会直接报错。
2. 如果用户给了岗位 JD:存成文本文件传 `--jd`;同时由助手从 JD 里抽出 3 到 10 个必备关键词传 `--keywords`(逗号分隔)。关键词不要凭空添加,只取 JD 明确写出的硬技能。
3. 运行:

   ```bash
   python3 -B scripts/score_resume.py <resume> [--jd jd.txt] [--keywords "Python,Kafka"] [--json]
   ```

   需要环境变量 `JEV_API_KEY`(也接受 `TYPESAFE_API_KEY`)。网关地址用 `JEV_BASE_URL`(自动拼 `/v1/systemone`)或 `JEV_ENDPOINT`(完整 URL),默认官方 `https://api.typesafe.ai/v1/systemone`。非本机的 http 地址必须额外设置 `JEV_ALLOW_HTTP=1`,因为密钥会明文传输。密钥只放环境变量,不要写进文件、命令行参数或对话。
4. 向用户汇报:等级、综合分、触发的上限(caps)、flags、分项表、建议。`needs_human_review` 为 yes 时明确说明"边界/不确定,建议人工复核",不要把边界结果当成确定结论。
5. 用户要求改简历时,基于报告里的建议改写,但只重述简历里已有的事实,不得编造数字、公司或技能。

## 退出码与失败处理

- 退出码 2 表示失败,原因在 stderr。常见:未设置 `TYPESAFE_API_KEY`、Jev 返回 401/403/422/429/529、返回结构不合法、简历超过 15000 字符、文本乱码。
- 失败时如实告诉用户原因,不要自行估算等级冒充结果。配置类错误(密钥、地址、http 开关、401/404 等)的排查表见 [README.md](README.md) 的"排错"一节,把对应一行告诉用户。
- 没有 API key 或用户明确不想外发简历时,才使用 `--no-jev`。该模式只检查格式与写作规范,报告会标注 `Judge: NONE`,等级最高只到"良",且必须对用户说明这不是完整评估。

## 隐私

简历含个人信息。默认在发送给 Jev 之前把邮箱和手机号替换为 `[EMAIL]` `[PHONE]`(姓名、公司名不会被遮蔽)。`--no-redact` 仅在用户明确同意时使用。第三方服务会收到简历正文,首次使用前向用户说明一次。

## 等级规则(概要)

- 综合分 = 0.65 x Jev 质量分 + 0.35 x 规则分 - 风险扣分。
- 优秀 >= 80,良 >= 60,其余为差。阈值可用 `--excellent` `--good` 调整。
- 上限闸门会压低等级,例如缺联系方式、无经历章节、时间线矛盾概率 >= 0.7、JD 必备项未满足。
- 综合分距阈值 3 分以内、Jev 总评置信度 < 0.6、或 Jev 总评与综合等级不一致时,标记需要人工复核。

完整公式、权重、题目设计和校准方法见 [references/rubric.md](references/rubric.md)。借鉴的开源项目与取舍见 [references/sources.md](references/sources.md)。

## 校准

默认阈值是起点,不是真值。用户有已标注的简历样本(优秀/良/差)时,先批量跑 `--json`,对比 `composite` 与人工标签,再调整 `--excellent` `--good`。没有样本之前,不要对用户声称准确率。

## 自检

修改脚本后运行 `python3 -B scripts/selftest.py`(离线,不需要密钥,不会发起网络请求)。
