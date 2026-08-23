"""Registered prompts for the evidence-bounded literature-review pipeline.

The review writer deliberately receives two evidence levels only:
``abstract`` for network papers and ``uploaded_fulltext`` for user files.
Every prompt names the evidence boundary so a model cannot silently promote a
network record to full-text knowledge.
"""
from __future__ import annotations

from core.prompts.registry import register


UPLOAD_CHUNK_PROMPT = """你正在为用户上传论文建立全文综述缓存。
证据 id：{evidence_id}
文件名：{filename}
分块序号：{chunk_index}
章节：{section}

分块原文：
{chunk_text}

只归纳该分块实际出现的信息，按“研究问题/方法或材料/主要发现/限制与未来工作/与前后论证的关系”组织为紧凑中文段落。没有出现的项目写“未报告”，不得补写数字、因果关系、章节内容或外部知识。保留能区分该分块的具体术语。只输出归纳正文。"""

OUTLINE_PLANNING_PROMPT = """你是文献综述的提纲规划器。主题：{topic}
用户关注点：{focus}
目标语言：{language}
期望篇幅（中文字符）：{target_length}

下面是本次唯一允许使用的证据。每条以稳定 id 开头，level=abstract 表示网络论文只有有效摘要，level=uploaded_fulltext 表示用户上传文件的可解析全文。不得引入列表外论文、标题推断或模型常识；没有证据的内容写入“证据不足”，不要补写。
{evidence}

请规划一个可执行的综述提纲，使用 JSON：
{{"themes":[{{"id":"theme-1","title":"主题名","evidence_ids":["稳定id"],"question":"本主题要比较的问题"}}],"scope_note":"证据边界说明","comparison_axes":["方法","发现","演进","局限"]}}
主题数量根据材料量自适应（2-6 个），确保每个有效证据至少归入一个主题。只输出 JSON。"""

THEME_DRAFTING_PROMPT = """你正在撰写综述中的一个分主题：{theme_title}
主题问题：{theme_question}
研究主题：{topic}
用户关注点：{focus}
目标篇幅约 {target_budget} 个中文字符。

只可使用下列证据：
{evidence}

写成真正的综合分析而不是逐篇摘要拼接。必须包含：代表工作、方法差异、主要发现、时间/方法演进或相互关系、共同局限与冲突。每个可核查事实在句末用稳定 id 标记为 [stable_id]。level=abstract 的网络论文只能支持摘要明确说出的研究问题、方法概况和发现；精确数字、实验细节、章节结构和未出现在摘要中的限制必须明确写“摘要证据不足，请上传原文”。level=uploaded_fulltext 的断言可使用全文，但要避免把不同文件混为一谈。不得使用其他论文或常识。只输出分主题正文，不要标题前缀。"""

SYNTHESIS_PROMPT = """你是综述综合分析作者。研究主题：{topic}
用户关注点：{focus}

已完成的分主题草稿（其中的稳定 id 已在证据表中核验）：
{theme_drafts}

证据表：
{evidence}

请写“方法、发现与发展脉络的综合比较”以及“主要争议、共同局限与研究空白”“未来研究方向”三部分的正文。必须跨主题比较，不得简单重复段落；争议和空白只能来自证据中明确的差异、限制或缺口，推断要标注为推断。所有事实继续使用 [stable_id]，只使用证据表中的 id。摘要不足的地方要明确提示上传原文。"""

REVISION_PROMPT = """你是最终综述编辑。请在不添加任何新事实的前提下修订下面的 Markdown 综述。
主题：{topic}
证据表：
{evidence}

草稿：
{draft}

硬性要求：
1. 保留并补齐以下八个一级结构（标题必须逐字出现）：
# 引言
# 综述范围与证据基础
# 分类框架与分主题综述
# 方法、发现与发展脉络的综合比较
# 主要争议、共同局限与研究空白
# 未来研究方向
# 结论
# 纳入文献与证据层级说明
2. 不得新造论文、数字、实验细节或引用；只保留证据表中的 [stable_id]。
3. 网络论文只能按摘要级证据表述，上传文件才可作全文级表述。
4. 主题段落要有比较、演进和局限，不得逐篇摘要罗列。
5. 保持具体、清晰、适合直接导出；目标约 {target_length} 个中文字符，但材料不足时宁可密度优先，不机械注水。
只输出完整 Markdown 正文。"""

EXPANSION_PROMPT = """你是文献综述扩写编辑。当前 Markdown 综述明显短于材料覆盖所需篇幅，请在不添加新事实、不重复注水的前提下，扩展跨文献比较、方法差异、发展关系、争议、共同局限和研究空白。
主题：{topic}
目标约 {target_length} 个中文字符；当前约 {current_length} 个字符。

唯一允许使用的证据：
{evidence}

当前综述：
{draft}

必须保留八个既定一级标题及其顺序。所有可核查事实使用证据表中的 [stable_id]；网络论文仍只能支持摘要明确内容，上传论文才可支持全文级细节。不得引用未纳入证据，不得为了长度制造信息。只输出完整 Markdown。"""

# Compatibility exports for callers/tests that still import the old names.
SECTION_WRITING_PROMPT = THEME_DRAFTING_PROMPT
INTRODUCTION_PROMPT = THEME_DRAFTING_PROMPT
CROSS_TOPIC_PROMPT = SYNTHESIS_PROMPT
CONCLUSION_PROMPT = SYNTHESIS_PROMPT
SELF_REVIEW_PROMPT = REVISION_PROMPT
REWRITE_PROMPT = REVISION_PROMPT
REVIEW_AND_REVISE_PROMPT = REVISION_PROMPT

register("review.upload_chunk", 1, UPLOAD_CHUNK_PROMPT)
register("review.outline", 3, OUTLINE_PLANNING_PROMPT)
register("review.theme", 3, THEME_DRAFTING_PROMPT)
register("review.synthesis", 3, SYNTHESIS_PROMPT)
register("review.revision", 3, REVISION_PROMPT)
register("review.expansion", 1, EXPANSION_PROMPT)
