"""Bilingual (EN/ZH) internationalization for Paper Agent UI (DESIGN D-037)."""

from __future__ import annotations

_STRINGS: dict[str, dict[str, str]] = {
    # --- App title ---
    "app_title": {"en": "Paper Agent", "zh": "论文智能助手"},
    "app_subtitle": {"en": "Literature Survey & Review", "zh": "文献调研与综述"},

    # --- Sidebar ---
    "sidebar_title": {"en": "Workspace", "zh": "工作区"},
    "sidebar_api_configured": {"en": "API Key configured", "zh": "API 密钥已配置"},
    "sidebar_enter_key": {"en": "Enter your API key to start.", "zh": "请输入 API 密钥开始使用。"},
    "sidebar_progress": {"en": "Progress", "zh": "进度"},
    "sidebar_history": {"en": "History", "zh": "历史记录"},
    "sidebar_no_history": {"en": "No history yet", "zh": "暂无历史记录"},
    "sidebar_search": {"en": "Search", "zh": "搜索"},
    "sidebar_graph": {"en": "Citation Graph", "zh": "引用图谱"},
    "sidebar_core": {"en": "core", "zh": "核心"},
    "sidebar_search_layer": {"en": "search", "zh": "搜索层"},
    "sidebar_ref": {"en": "ref", "zh": "参考"},

    # --- Section 1: Input ---
    "sec1_title": {"en": "1. Research Topic", "zh": "1. 研究主题"},
    "sec1_topic": {"en": "Research topic", "zh": "研究主题"},
    "sec1_topic_ph": {"en": "e.g., Enlightenment rationality and its critics", "zh": "例：启蒙理性及其批判"},
    "sec1_conception": {"en": "Your initial conception (optional)", "zh": "你的初步构想（可选）"},
    "sec1_conception_ph": {"en": "Describe your research idea or angle...", "zh": "描述你的研究思路或角度..."},
    "sec1_language": {"en": "Language", "zh": "语言"},
    "sec1_language_hint": {
        "en": "Controls the language of LLM-generated content (search queries, cluster labels, literature review, etc.)",
        "zh": "控制大模型生成内容的语言（搜索词、聚类标签、文献综述等）"
    },
    "sec1_field": {"en": "Field profile", "zh": "领域配置"},
    "sec1_mode": {"en": "Search mode", "zh": "搜索模式"},
    "sec1_mode_flash": {"en": "Flash (fast, low cost)", "zh": "Flash（快速，低成本）"},
    "sec1_mode_pro": {"en": "Pro (best accuracy, higher cost)", "zh": "Pro（最准确，成本较高）"},
    "sec1_mode_help": {
        "en": "Flash: Python local ranking selects top X papers, LLM scores only those. Faster, cheaper.\n\nPro: ALL found papers get LLM scoring (reads every abstract). Most accurate, higher cost.",
        "zh": "Flash：Python 本地排序选前 X 篇，LLM 只打分这些。更快、更省。\n\nPro：所有搜到的论文都送 LLM 打分（逐篇读摘要）。最准确，成本较高。"
    },
    "sec1_mode_flash_desc": {
        "en": "Flash: Python ranking selects top X, top Y directly become core. No LLM scoring. Fastest, lowest cost.",
        "zh": "Flash：本地排序选前 X 篇，前 Y 篇直接进入核心层。不调 LLM 打分。最快、最省。"
    },
    "sec1_mode_pro_desc": {
        "en": "Pro: LLM reads abstracts of top min(2Y, X) papers, scores each, picks top Y as core. Most accurate, higher cost.",
        "zh": "Pro：LLM 逐篇读摘要打分前 min(2Y, X) 篇，选最优 Y 篇为核心层。最准确，成本较高。"
    },
    "sec1_count_settings": {"en": "Paper count settings", "zh": "论文数量设置"},
    "sec1_x_label": {"en": "Search layer size (X)", "zh": "搜索层大小 (X)"},
    "sec1_x_help": {
        "en": "X = search layer size. Top X papers by Python TF-IDF ranking. Flash: top Y from X directly become core. Pro: LLM scores top min(2Y, X) from X, picks best Y as core.",
        "zh": "X = 搜索层大小。Python TF-IDF 排序前 X 篇。Flash：前 Y 篇直接进入核心层。Pro：LLM 对前 min(2Y, X) 篇打分，选最优 Y 篇为核心层。"
    },
    "sec1_y_label": {"en": "Core layer size (Y <= X)", "zh": "核心层大小 (Y <= X)"},
    "sec1_y_help": {
        "en": "Y = core layer size. These Y papers enter the citation graph. Must be <= X.",
        "zh": "Y = 核心层大小。这 Y 篇论文进入引用图谱。必须 <= X。"
    },
    "sec1_start": {"en": "Start Search", "zh": "开始搜索"},
    "sec1_searching": {"en": "Searching...", "zh": "搜索中..."},

    # --- Section 2: Search Strategy ---
    "sec2_title": {"en": "2. Search Strategy", "zh": "2. 搜索策略"},

    # --- Section 3: Results ---
    "sec3_title": {"en": "3. Results", "zh": "3. 搜索结果"},
    "sec3_core": {"en": "Core Layer", "zh": "核心层"},
    "sec3_search": {"en": "Search Layer", "zh": "搜索层"},
    "sec3_reference": {"en": "Reference Layer", "zh": "参考层"},
    "sec3_papers": {"en": "papers", "zh": "篇"},
    "sec3_no_results": {"en": "No papers found.", "zh": "未找到论文。"},
    "sec3_filter": {"en": "Filter", "zh": "筛选"},
    "sec3_filter_ph": {"en": "type to filter...", "zh": "输入以筛选..."},
    "sec3_source": {"en": "Source", "zh": "来源"},
    "sec3_llm": {"en": "LLM", "zh": "大模型"},

    # --- Section 4: Graph ---
    "sec4_title": {"en": "4. Citation Graph", "zh": "4. 引用图谱"},
    "sec4_no_core": {"en": "No core papers available for graph construction.", "zh": "没有核心层论文可用于构建图谱。"},
    "sec4_build_desc": {
        "en": "Build a semantic similarity graph from core layer papers. TF-IDF edges connect papers by abstract/keyword similarity. Louvain clustering detects topic groups. LLM generates cluster labels, overviews, and edge annotations.",
        "zh": "从核心层论文构建语义相似度图谱。TF-IDF 边按摘要/关键词相似度连接论文。Louvain 聚类检测主题分组。LLM 生成聚类标签、概述和边关系标注。"
    },
    "sec4_build": {"en": "Build Graph", "zh": "构建图谱"},
    "sec4_rebuild": {"en": "Rebuild Graph", "zh": "重建图谱"},
    "sec4_nodes": {"en": "Nodes", "zh": "节点"},
    "sec4_edges": {"en": "Edges", "zh": "边"},
    "sec4_clusters": {"en": "Clusters", "zh": "聚类"},
    "sec4_interactive": {"en": "Interactive Graph", "zh": "交互式图谱"},
    "sec4_graph_hint": {
        "en": "Nodes labeled P1, P2, ... colored by cluster. Node size reflects connections. Edge labels show relationship type. Hover for details. Drag to rearrange. Scroll to zoom.",
        "zh": "节点标记为 P1、P2...，按聚类着色。节点大小反映连接数。边标签显示关系类型。悬停查看详情。拖拽可重新排列。滚轮缩放。"
    },
    "sec4_node_legend": {"en": "Node Legend (P# = paper title)", "zh": "节点对照表（P# = 论文标题）"},
    "sec4_topic_clusters": {"en": "Topic Clusters", "zh": "主题聚类"},
    "sec4_annotated_edges": {"en": "Annotated Edges", "zh": "标注边"},
    "sec4_edges_unit": {"en": "edges", "zh": "条边"},
    "sec4_no_data": {"en": "No graph data available.", "zh": "无图谱数据。"},
    "sec4_no_html": {"en": "Graph HTML file not found", "zh": "图谱 HTML 文件未找到"},

    # --- Reference Layer ---
    "ref_title": {"en": "Reference Layer", "zh": "参考层"},
    "ref_ranked": {"en": "papers, ranked", "zh": "篇，按排名排序"},
    "ref_desc": {"en": "Papers found during search but not included in citation graph.", "zh": "搜索中发现但未纳入引用图谱的论文。"},
    "ref_prev": {"en": "Prev", "zh": "上一页"},
    "ref_next": {"en": "Next", "zh": "下一页"},
    "ref_page": {"en": "Page", "zh": "第"},
    "ref_of": {"en": "of", "zh": "页 / 共"},

    # --- Common ---
    "common_citations": {"en": "Citations", "zh": "引用数"},
    "common_abstract": {"en": "Abstract", "zh": "摘要"},
    "common_keywords": {"en": "Keywords", "zh": "关键词"},
    "common_score": {"en": "score", "zh": "评分"},
    "common_year": {"en": "Year", "zh": "年份"},
    "common_authors": {"en": "Authors", "zh": "作者"},
    "common_title": {"en": "Title", "zh": "标题"},
    "common_links": {"en": "Links", "zh": "链接"},
    "common_venue": {"en": "Venue", "zh": "期刊"},
    "common_searching": {"en": "Searching", "zh": "搜索中"},
    "lang_toggle": {"en": "中文", "zh": "English"},
    "settings_btn": {"en": "Settings", "zh": "设置"},
    "settings_title": {"en": "Settings", "zh": "设置"},
    "settings_language": {"en": "Language", "zh": "语言"},
    "settings_language_hint": {
        "en": "Controls the UI language (titles, labels, buttons). Does not affect LLM output language — use the Language option in Section 1 for that.",
        "zh": "控制界面语言（标题、标签、按钮等）。不影响大模型输出语言 — 请在第 1 部分设置模型输出语言。"
    },
    "settings_api_key": {"en": "API Key", "zh": "API 密钥"},
    "settings_api_key_ph": {"en": "Enter DeepSeek API key...", "zh": "输入 DeepSeek API 密钥..."},
    "settings_api_configured": {"en": "API Key configured", "zh": "API 密钥已配置"},
    "settings_api_model": {"en": "Model", "zh": "模型"},
    "settings_api_not_configured": {"en": "API Key not configured. Click the settings button above to enter your key.", "zh": "API 密钥未配置。点击上方的设置按钮输入密钥。"},
    "settings_lang_en": {"en": "English", "zh": "English"},
    "settings_lang_zh": {"en": "中文", "zh": "中文"},
}


def t(key: str, lang: str = "en") -> str:
    """Translate a key to the given language. Falls back to English then key."""
    entry = _STRINGS.get(key)
    if not entry:
        return key
    return entry.get(lang, entry.get("en", key))
