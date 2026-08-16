"""Typed chat tool schemas for native function-calling.

Each tool is a pydantic args schema wrapped as a langchain StructuredTool;
llm.bind_tools(get_chat_tools()) makes the model emit structured tool_calls
enforced by JSON Schema at the API level. The implementations live in
agents/tools_impl.py — this module only declares the typed surface.

Atomic tools: search_papers / deep_read / ask_papers / research_map /
reading_path / write_review / check_structure / check_format /
export_manuscript. Skill layer surface: use_skill loads instruction
skills; citation_export / export_report are executable skills.
No X/Y/Z counts, no flash/pro modes.
"""
from __future__ import annotations

from typing import Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class SearchPapersArgs(BaseModel):
    """多源检索学术论文（OpenAlex/arXiv/Crossref/Europe PMC/DOAJ），语义重排后自适应分出核心集与候选集。检索完成前会逐篇探测 OA PDF 是否可访问（只读文件头，不下载全文），并给每篇标注 fulltext_status: available/unavailable/unknown；用户问“哪些能看全文”时直接使用这些标记回答，不要为清点全文状态调用 deep_read。会话已有论文而用户只是点名其中某篇（给了 DOI/标题/“这篇”）时不要调用，直接用 deep_read/ask_papers。"""
    topic: str = Field(description="研究主题或问题。必填。")
    conception: str = Field(default="",
        description="可选：用户的研究构想/角度/预期贡献，用于引导查询拆解。")
    language: Literal["both", "zh", "en"] = Field(default="both",
        description="检索语言偏好：both | zh | en。")


class DeepReadArgs(BaseModel):
    """对核心集论文或用户上传的 PDF/DOCX/图片做结构化深读。网络论文默认对每篇都尝试获取合法 OA 全文并做全文级深读；取不到全文的自动回退摘要级并在结果中明确列出，绝不把摘要冒充全文。上传附件按需启动布局/OCR/VLM，多次读取复用缓存。"""
    paper_ids: list[str] = Field(default_factory=list,
        description="可选：只读指定 paper_id 的论文（核心集或候选集均可）；空列表 = 整个核心集。用户直接给出 DOI 时原样传入，不要先 search_papers 定位。")
    attachment_ids: list[str] = Field(default_factory=list,
        description="可选：按需深读当前会话上传附件的 id（PDF/DOCX/PNG/JPG/WebP）。可与 paper_ids 同时使用。")
    focus: str = Field(default="",
        description="可选：本次深读要获取的信息重点（如「实验数据集与指标数值」）。网络论文无论 focus 是否为空都会尝试全文；focus 会作为上传附件按需理解的侧重点。")


class AskPapersArgs(BaseModel):
    """基于本会话论文与上传文件回答问题（RAG，带引用）。图/表/公式问题会按需理解相关上传 PDF/DOCX/图片；限定附件时传 attachment_id，限定论文时传 paper_id，两者不要同时传。"""
    query: str = Field(description="要回答的问题（自包含，不含「这篇/那篇」等指代词）。必填。")
    paper_id: str = Field(default="",
        description="可选：限定某篇网络论文的 paper_id（核心集或候选集均可，支持 DOI 别名）；与 attachment_id 互斥。用户已给出 DOI 时直接传入，不要先 search_papers。")
    attachment_id: str = Field(default="",
        description="可选：限定当前会话某个上传附件的 id；与 paper_id 互斥。")
    top_k: int = Field(default=5, ge=1, le=10,
        description="检索段落数，1-10。")


class ResearchMapArgs(BaseModel):
    """生成研究地图：主题聚类 + 时间脉络 + 领域脉络 + 论文谱系图数据。需先 search_papers。无参数。"""
    pass


class ReadingPathArgs(BaseModel):
    """推荐阅读路径（奠基→桥梁→前沿，每篇附理由）。需先有核心集论文。无参数。"""
    pass


class WriteReviewArgs(BaseModel):
    """撰写文献综述（按主题簇组织，引用经防幻觉校验）。建议先 deep_read。无参数。"""
    pass


class UseSkillArgs(BaseModel):
    """加载一个技能的完整工作流指令（系统 prompt「技能」小节列出了可用技能及其触发场景）。用户请求命中某个技能的触发场景时调用；加载后严格按返回的指令组合基础工具执行。未命中任何技能时不要调用。"""
    name: str = Field(description="技能名，必须来自系统 prompt 技能列表（如 compare_papers）。必填。")
    focus: str = Field(default="",
        description="可选：用户本次请求的侧重点（如「只对比方法和实验」），会注入技能指令用于裁剪执行范围。")


class CitationExportArgs(BaseModel):
    """把本会话论文导出为参考文献引用（默认核心集；DOI 经 Crossref 自动补全卷期页）。用户要求"导出参考文献/BibTeX/引用格式/国标/GB/T/写论文的引用条目"时调用。"""
    format: Literal["bibtex", "gbt7714"] = Field(default="bibtex",
        description="引用格式：bibtex（LaTeX）| gbt7714（国标 GB/T 7714 顺序编码制，中文毕业论文常用）。用户提到国标/毕业论文/中文格式时用 gbt7714。")
    paper_ids: list[str] = Field(default_factory=list,
        description="可选：只导出指定 paper_id 的论文；空列表 = 核心集。")


class ExportReportArgs(BaseModel):
    """把研究成果导出为可下载的 markdown 文件（研究地图报告/文献综述）。用户要求"导出报告/下载综述/保存研究地图/给我文件"时调用。返回文件下载链接。"""
    kind: Literal["research_map", "write_review", "all"] = Field(default="all",
        description="导出内容：research_map（需已生成研究地图）| write_review（需已写综述）| all（默认，有什么导什么）。")


class CheckStructureArgs(BaseModel):
    """对会话中用户上传的论文草稿做结构体检（纯代码确定性解析，零模型消耗）：章节树、IMRaD 完整性（缺失章节）、章节比例失衡、摘要长度、引用卫生（文内引用 vs 参考文献列表）、图表引用统计。用户上传了自己的论文/草稿并要求"复查结构/检查章节完整性/结构优化建议/看看组织是否合理"时调用；draft_review 技能流程也要求先调用它拿体检事实。需要会话中有上传附件。"""
    attachment: str = Field(default="",
        description="可选：要检查的附件 id 或文件名片段；空 = 最近上传的附件。")


class CheckFormatArgs(BaseModel):
    """对会话中上传的论文草稿（pdf/docx/tex/txt/md）做格式检查（纯代码确定性解析，零模型消耗）：图表编号连续性与正文引用、引用风格混用、参考文献 GB/T 7714 规范度、关键词数量、标题编号断号；LaTeX 源额外检查 \\cite/\\ref/参考文献块配对。用户要求"检查格式/按学校或期刊的格式要求核对/排版规范检查"时调用；用户给出具体格式要求文本时传入 spec 逐条对照。需要会话中有上传附件。"""
    attachment: str = Field(default="",
        description="可选：要检查的附件 id 或文件名片段；空 = 最近上传的附件。")
    spec: str = Field(default="",
        description="可选：用户给出的格式要求原文（如『摘要不超过300字、关键词3-5个、参考文献按GB/T 7714』），系统逐条自动对照；字体/页边距等排版项会列入人工核对清单。")


class ExportManuscriptArgs(BaseModel):
    """把写作产物（论文初稿/润色改写稿/格式修改清单等 markdown 文本）导出为可下载文件（docx/tex/md）。用户要求"导出初稿/给我 docx/转成 LaTeX/下载修改稿"时调用。内容必须为已完成的最终文本，不要边写边导。"""
    title: str = Field(description="文档标题（用于文件名与文内标题）。必填。")
    content: str = Field(description="要导出的 markdown 全文。必填。")
    format: Literal["docx", "tex", "md"] = Field(default="docx",
        description="导出格式：docx（Word，默认）| tex（LaTeX 源，ctexart 中文可编译）| md。")


class IntegritySweepArgs(BaseModel):
    """对会话论文做可靠性质检（纯官方 API 查询，零模型消耗）：逐篇检查是否被撤稿、是否有勘误或关切声明、arXiv 预印本是否已有正式发表版。用户要求"检查引用可靠性/有没有撤稿的论文/投稿前质检/查勘误状态"时调用；写完综述或导出投稿稿前建议跑一遍。需先 search_papers。"""
    paper_ids: list[str] = Field(default_factory=list,
        description="可选：只查指定 paper_id 的论文；空列表 = 核心集 ∪ 候选集全部。")


class BibImportArgs(BaseModel):
    """把用户上传的 BibTeX(.bib) 文献库（Zotero/EndNote/Mendeley 导出）导入会话候选集，与 citation_export 构成双向互通。逐条解析题录，有 DOI 的经 Crossref 自动补全卷期页，去重后并入候选集（不污染核心集）。用户要求"导入我的文献库/导入 .bib/把 Zotero 导出的文献加进来/导入这些参考文献"时调用。需要会话中已上传 .bib 附件。"""
    attachment: str = Field(default="",
        description="可选：已上传 .bib 附件的 id 或文件名片段；空 = 最近上传的 .bib 附件。")


class ExhibitIndexArgs(BaseModel):
    """列出当前会话论文或上传 PDF/DOCX/图片里的图、表、公式。上传附件会按需解析且复用缓存；无参数时覆盖可用论文及上传附件。"""
    paper_ids: list[str] = Field(default_factory=list,
        description="可选：只列出指定网络论文。")
    attachment_ids: list[str] = Field(default_factory=list,
        description="可选：只列出指定上传附件；空且 paper_ids 也空时覆盖当前会话全部附件。")


class ExplainElementArgs(BaseModel):
    """查看当前会话论文或上传附件中某个图/表/公式的多模态详细解读。上传元素 id 形如 `upload:<attachment_id>::figure::1`；调用前必须确保该附件属于当前会话。"""
    element_id: str = Field(description="要解读的元素 id，形如 `{paper_id}::figure::3`（来自 deep_read 的 elements 或 exhibit_index）。必填。")
    paper_id: str = Field(default="",
        description="可选：元素所属论文的 paper_id（用于校验归属）。空时从 element_id 自动解析。")


class FieldCensusArgs(BaseModel):
    """对会话研究领域做宏观计量普查（OpenAlex 聚合，确定性统计 + 1 句画像）：近 15 年年度发文趋势、高产学/机构/期刊 top10。用户要求"这个领域每年发文趋势/谁最高产/主要发在哪些期刊/领域统计/领域画像"时调用。与 research_map 的区别：research_map 整理你手里这批论文的关系结构，field_census 看整个领域的宏观画像。需先 search_papers。无参数。"""
    pass


def _stub(**_kwargs) -> None:
    """No-op; real execution is in agents/tools_impl.py."""
    return None


def get_chat_tools() -> list[StructuredTool]:
    """Return the typed chat tools for llm.bind_tools()."""
    return [
        StructuredTool.from_function(_stub, name="search_papers", description=SearchPapersArgs.__doc__, args_schema=SearchPapersArgs),
        StructuredTool.from_function(_stub, name="deep_read", description=DeepReadArgs.__doc__, args_schema=DeepReadArgs),
        StructuredTool.from_function(_stub, name="ask_papers", description=AskPapersArgs.__doc__, args_schema=AskPapersArgs),
        StructuredTool.from_function(_stub, name="research_map", description=ResearchMapArgs.__doc__, args_schema=ResearchMapArgs),
        StructuredTool.from_function(_stub, name="reading_path", description=ReadingPathArgs.__doc__, args_schema=ReadingPathArgs),
        StructuredTool.from_function(_stub, name="write_review", description=WriteReviewArgs.__doc__, args_schema=WriteReviewArgs),
        StructuredTool.from_function(_stub, name="use_skill", description=UseSkillArgs.__doc__, args_schema=UseSkillArgs),
        StructuredTool.from_function(_stub, name="citation_export", description=CitationExportArgs.__doc__, args_schema=CitationExportArgs),
        StructuredTool.from_function(_stub, name="export_report", description=ExportReportArgs.__doc__, args_schema=ExportReportArgs),
        StructuredTool.from_function(_stub, name="check_structure", description=CheckStructureArgs.__doc__, args_schema=CheckStructureArgs),
        StructuredTool.from_function(_stub, name="check_format", description=CheckFormatArgs.__doc__, args_schema=CheckFormatArgs),
        StructuredTool.from_function(_stub, name="export_manuscript", description=ExportManuscriptArgs.__doc__, args_schema=ExportManuscriptArgs),
        StructuredTool.from_function(_stub, name="integrity_sweep", description=IntegritySweepArgs.__doc__, args_schema=IntegritySweepArgs),
        StructuredTool.from_function(_stub, name="bib_import", description=BibImportArgs.__doc__, args_schema=BibImportArgs),
        StructuredTool.from_function(_stub, name="exhibit_index", description=ExhibitIndexArgs.__doc__, args_schema=ExhibitIndexArgs),
        StructuredTool.from_function(_stub, name="explain_element", description=ExplainElementArgs.__doc__, args_schema=ExplainElementArgs),
        StructuredTool.from_function(_stub, name="field_census", description=FieldCensusArgs.__doc__, args_schema=FieldCensusArgs),
    ]


TOOL_NAMES: frozenset[str] = frozenset(t.name for t in get_chat_tools())

# Args schema by tool name, used by tools_impl for validation.
ARGS_SCHEMAS: dict[str, type[BaseModel]] = {
    "search_papers": SearchPapersArgs,
    "deep_read": DeepReadArgs,
    "ask_papers": AskPapersArgs,
    "research_map": ResearchMapArgs,
    "reading_path": ReadingPathArgs,
    "write_review": WriteReviewArgs,
    "use_skill": UseSkillArgs,
    "citation_export": CitationExportArgs,
    "export_report": ExportReportArgs,
    "check_structure": CheckStructureArgs,
    "check_format": CheckFormatArgs,
    "export_manuscript": ExportManuscriptArgs,
    "integrity_sweep": IntegritySweepArgs,
    "bib_import": BibImportArgs,
    "exhibit_index": ExhibitIndexArgs,
    "explain_element": ExplainElementArgs,
    "field_census": FieldCensusArgs,
}
