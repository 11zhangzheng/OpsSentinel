from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


ROOT = Path(r"D:\Desktop\OpsSentinel")
SOURCE = Path(r"D:\Desktop\求职简历.docx")
WORK = ROOT / ".resume_work"
OUTPUT = ROOT / "张峥_求职简历_科研版.docx"
PHOTO = WORK / "photo.jpg"

CN_FONT = "Microsoft YaHei"
EN_FONT = "Arial"
BLACK = RGBColor(0x1F, 0x23, 0x2B)
MUTED = RGBColor(0x55, 0x5B, 0x66)
BLUE = "1677B8"
LIGHT = "F2F4F7"


def set_run_font(run, size: float, *, bold: bool = False, color=BLACK, italic: bool = False):
    run.font.name = EN_FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), EN_FONT)
    rfonts.set(qn("w:hAnsi"), EN_FONT)
    rfonts.set(qn("w:eastAsia"), CN_FONT)
    rfonts.set(qn("w:cs"), EN_FONT)


def set_paragraph_spacing(paragraph, *, before=0, after=0, line=1.0):
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.line_spacing = line


def clear_cell_borders(cell):
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tc_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = borders.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            borders.append(node)
        node.set(qn("w:val"), "nil")


def set_cell_margins(cell, top=0, start=0, bottom=0, end=0):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for tag, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{tag}"))
        if node is None:
            node = OxmlElement(f"w:{tag}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_cell_width(cell, width_cm: float):
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.first_child_found_in("w:tcW")
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(int(width_cm / 2.54 * 1440)))
    tc_w.set(qn("w:type"), "dxa")


def keep(paragraph, *, next_one=False):
    p_pr = paragraph._p.get_or_add_pPr()
    keep_lines = OxmlElement("w:keepLines")
    p_pr.append(keep_lines)
    if next_one:
        keep_next = OxmlElement("w:keepNext")
        p_pr.append(keep_next)


def set_right_tab(paragraph, position_cm: float):
    p_pr = paragraph._p.get_or_add_pPr()
    tabs = p_pr.find(qn("w:tabs"))
    if tabs is None:
        tabs = OxmlElement("w:tabs")
        p_pr.append(tabs)
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "right")
    tab.set(qn("w:pos"), str(int(position_cm / 2.54 * 1440)))
    tabs.append(tab)


def set_paragraph_shading(paragraph, fill: str):
    p_pr = paragraph._p.get_or_add_pPr()
    shd = p_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        p_pr.append(shd)
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)


def set_left_border(paragraph, color: str):
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = p_pr.find(qn("w:pBdr"))
    if p_bdr is None:
        p_bdr = OxmlElement("w:pBdr")
        p_pr.append(p_bdr)
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), "18")
    left.set(qn("w:space"), "7")
    left.set(qn("w:color"), color)
    p_bdr.append(left)


def section_heading(doc: Document, text: str):
    p = doc.add_paragraph()
    set_paragraph_spacing(p, before=4.0, after=3.0, line=1.0)
    set_paragraph_shading(p, LIGHT)
    set_left_border(p, BLUE)
    p.paragraph_format.left_indent = Pt(7)
    p.paragraph_format.right_indent = Pt(2)
    run = p.add_run(text)
    set_run_font(run, 12.2, bold=True, color=BLACK)
    keep(p, next_one=True)
    return p


def add_tabbed_line(doc: Document, left: str, right: str, *, size=10.0, bold=True, after=0.6):
    p = doc.add_paragraph()
    set_paragraph_spacing(p, after=after, line=1.02)
    set_right_tab(p, 18.1)
    r1 = p.add_run(left)
    set_run_font(r1, size, bold=bold)
    p.add_run("\t")
    r2 = p.add_run(right)
    set_run_font(r2, size - 0.2, bold=False, color=MUTED)
    keep(p, next_one=True)
    return p


def add_detail(doc: Document, text: str, *, size=9.2, after=1.5, color=MUTED):
    p = doc.add_paragraph()
    set_paragraph_spacing(p, after=after, line=1.06)
    r = p.add_run(text)
    set_run_font(r, size, color=color)
    keep(p)
    return p


def add_project_title(doc: Document, name: str, description: str):
    p = doc.add_paragraph()
    set_paragraph_spacing(p, before=0.5, after=0.8, line=1.0)
    r = p.add_run(name)
    set_run_font(r, 10.7, bold=True)
    r = p.add_run(f"  |  {description}")
    set_run_font(r, 10.0, bold=True, color=MUTED)
    keep(p, next_one=True)
    return p


def add_stack(doc: Document, text: str):
    p = doc.add_paragraph()
    set_paragraph_spacing(p, after=1.1, line=1.0)
    lead = p.add_run("技术栈  ")
    set_run_font(lead, 8.7, bold=True, color=MUTED)
    body = p.add_run(text)
    set_run_font(body, 8.7, color=MUTED)
    keep(p, next_one=True)
    return p


def add_bullet(doc: Document, text: str, *, size=9.05, after=0.9):
    p = doc.add_paragraph()
    set_paragraph_spacing(p, after=after, line=1.08)
    p.paragraph_format.left_indent = Cm(0.48)
    p.paragraph_format.first_line_indent = Cm(-0.34)
    bullet = p.add_run("• ")
    set_run_font(bullet, size, bold=True, color=RGBColor(0x16, 0x77, 0xB8))
    body = p.add_run(text)
    set_run_font(body, size, color=BLACK)
    keep(p)
    return p


def add_skill(doc: Document, label: str, text: str, *, after=0.7):
    p = doc.add_paragraph()
    set_paragraph_spacing(p, after=after, line=1.06)
    p.paragraph_format.left_indent = Cm(0.48)
    p.paragraph_format.first_line_indent = Cm(-0.34)
    bullet = p.add_run("• ")
    set_run_font(bullet, 9.0, bold=True, color=RGBColor(0x16, 0x77, 0xB8))
    lead = p.add_run(label)
    set_run_font(lead, 9.0, bold=True)
    body = p.add_run(text)
    set_run_font(body, 9.0)
    keep(p)
    return p


def extract_photo():
    WORK.mkdir(parents=True, exist_ok=True)
    with ZipFile(SOURCE) as archive:
        PHOTO.write_bytes(archive.read("word/media/image1.jpeg"))


def build():
    extract_photo()
    doc = Document()
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(1.05)
    section.bottom_margin = Cm(1.00)
    section.left_margin = Cm(1.25)
    section.right_margin = Cm(1.25)
    section.header_distance = Cm(0.4)
    section.footer_distance = Cm(0.4)

    normal = doc.styles["Normal"]
    normal.font.name = EN_FONT
    normal.font.size = Pt(9.2)
    normal.font.color.rgb = BLACK
    normal._element.rPr.rFonts.set(qn("w:ascii"), EN_FONT)
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), EN_FONT)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), CN_FONT)

    title_style = doc.styles["Title"]
    title_style.font.name = EN_FONT
    title_style.font.size = Pt(23)
    title_style.font.bold = True
    title_style.font.color.rgb = BLACK
    title_style._element.rPr.rFonts.set(qn("w:ascii"), EN_FONT)
    title_style._element.rPr.rFonts.set(qn("w:hAnsi"), EN_FONT)
    title_style._element.rPr.rFonts.set(qn("w:eastAsia"), CN_FONT)
    title_ppr = title_style._element.get_or_add_pPr()
    title_border = title_ppr.find(qn("w:pBdr"))
    if title_border is not None:
        title_ppr.remove(title_border)

    doc.core_properties.title = "张峥 求职简历"
    doc.core_properties.subject = "大模型应用 Agent 开发与 Python 后端开发岗位"
    doc.core_properties.author = "张峥"
    doc.core_properties.keywords = "LLM Agent Python FastAPI Docker"

    # Stable header: no floating text boxes, only ordinary text and an inline photo.
    table = doc.add_table(rows=1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    left, right = table.rows[0].cells
    set_cell_width(left, 15.85)
    set_cell_width(right, 2.65)
    for cell in (left, right):
        clear_cell_borders(cell)
        set_cell_margins(cell, top=0, start=0, bottom=0, end=0)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

    p = left.paragraphs[0]
    p.style = doc.styles["Title"]
    set_paragraph_spacing(p, after=0.5, line=1.0)
    run = p.add_run("张峥")
    set_run_font(run, 23, bold=True)

    p = left.add_paragraph()
    set_paragraph_spacing(p, after=2.0, line=1.0)
    run = p.add_run("大模型应用 / Agent 开发工程师  |  Python 后端开发工程师")
    set_run_font(run, 10.8, bold=True)

    p = left.add_paragraph()
    set_paragraph_spacing(p, after=2.2, line=1.0)
    run = p.add_run("159-3782-0499  |  2026140877@bupt.edu.cn  |  日常实习，可随时到岗")
    set_run_font(run, 9.4, color=MUTED)

    p = left.add_paragraph()
    set_paragraph_spacing(p, after=0, line=1.07)
    run = p.add_run(
        "北京邮电大学计算机学院硕士研究生，聚焦 LLM Agent 与 Python 后端；具备从 Agent 工作流、"
        "状态持久化到容器化验证和 Linux 服务部署的端到端项目实践。"
    )
    set_run_font(run, 9.15, color=BLACK)

    p = right.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    set_paragraph_spacing(p, after=0, line=1.0)
    p.add_run().add_picture(str(PHOTO), width=Cm(2.35))

    section_heading(doc, "教育背景")
    add_tabbed_line(doc, "北京邮电大学  ·  计算机学院  ·  硕士研究生", "2026.09 - 2029.06")
    add_detail(doc, "研究方向：LLM / Multimodal Agent、长上下文推理、Agent Memory、Retrieval 与 Routing", after=1.1)
    add_tabbed_line(doc, "河南大学（双一流建设高校）  ·  软件工程（卓越班）", "2022.09 - 2026.06")
    add_detail(doc, "GPA 3.58 / 4.00，排名前 6%；国家级一等奖 1 项，省级一等奖 2 项", after=1.0)

    section_heading(doc, "科研经历")
    add_project_title(doc, "Gist-Residual", "长视频多模态问答中的成本感知记忆路由")
    add_bullet(doc, "围绕 Gist → Residual → Raw Visual 多级视觉保真度，研究轻量 Memory Router 在回答准确率与推理成本之间的自适应选择；设计问题无关 Residual 缓存、标签隔离与视频级数据划分，降低跨问题复用和评测泄漏风险。", size=8.85, after=0.7)
    add_bullet(doc, "构建包含 Production Authority、数据溯源、缓存隔离、实测成本与预注册门禁的实验链路；100 题部分数据集 Pilot 中，Oracle 准确率为 38%，较最佳固定策略 23% 提供 15 个百分点的潜在提升空间，缺失观测为 0。", size=8.85, after=0.7)
    add_bullet(doc, "通过稳定性、精确搜索与观察充分性诊断定位 Beam 搜索和视觉证据链缺陷；验证“统一增加采样帧”无法有效恢复失败样例，据此推进融合时间戳、OCR、字幕或 ASR、音频事件的多模态证据编译方案。", size=8.85, after=1.0)

    section_heading(doc, "项目经历")
    add_project_title(doc, "gh-assistant", "GitHub 软件工程 Agent Harness")
    add_stack(doc, "Python · LLM Tool Calling · Anthropic / OpenAI-compatible API · GitHub REST API · Docker · Git Worktree · SQLite")
    add_bullet(doc, "面向 GitHub Issue 设计并实现 SWE Agent Harness，贯通任务获取、代码检索、方案规划、补丁生成、自动验证、失败修复、独立审查、人工审批与 Draft PR 创建。")
    add_bullet(doc, "设计 provider-neutral 的 Message / ToolCall / ToolResult 协议，兼容 Anthropic 与 OpenAI-compatible 模型；按 Planning、Implementation、Review 阶段动态收敛可用工具与权限。")
    add_bullet(doc, "使用 Git Worktree 与受限 Docker Executor 隔离任务和测试；基于 SQLite 持久化运行、审批与工具调用，支持中断恢复和审计，并以 scripted model、mock GitHub、临时仓库及 hidden tests 构建确定性评测。", after=1.2)

    add_project_title(doc, "OpsSentinel", "服务监控与受控自动恢复平台")
    add_stack(doc, "Python · FastAPI · asyncio · SQLite · httpx · Docker Compose · systemd · pytest")
    add_bullet(doc, "实现持续巡检、事故去重、诊断、审批或自动处置、连续探针验证的状态机；动作返回成功后仍需新鲜业务探针确认，并持久化动作前后证据与事故时间线。")
    add_bullet(doc, "支持 HTTP 契约探测与 Linux Host Agent、资源预警和维护窗口；将恢复限制为四类固定动作，通过控制器策略与 Agent 允许列表实施两层授权，并以 operation ID、执行前复检和结果不明不重放约束外部副作用。")
    add_bullet(doc, "在 Ubuntu 22.04 完成 systemd 常驻安装、历史迁移、四类故障演练和控制器进程恢复验证；Linux 全套测试 132 项通过。", after=1.0)

    section_heading(doc, "专业技能")
    add_skill(doc, "LLM / Agent：", "熟悉 Transformer、Self-Attention、KV Cache、ReAct、Planning、Tool Calling、Memory、RAG 与长上下文；具备 Agent Evaluation、Failure Analysis 和 Context Management 实践。")
    add_skill(doc, "工程开发：", "熟悉 Python、FastAPI、asyncio、SQLite、Linux、Git 与 Docker；具备 REST API、异步调用、状态机、服务调试、自动化测试和 systemd 部署经验。")
    add_skill(doc, "基础与英语：", "掌握常用数据结构与算法、操作系统、计算机网络和数据库基础；CET-4 601，CET-6 531。", after=0)

    # Prevent Word from adding extra blank space after the final paragraph.
    for paragraph in doc.paragraphs:
        paragraph.paragraph_format.widow_control = True

    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    build()
