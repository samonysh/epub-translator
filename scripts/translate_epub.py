#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translate_epub.py — EPUB 英→中 段落级双语对照翻译器

用法：
    python translate_epub.py <input.epub> <output.epub> [--concurrency 64]
                              [--batch-tokens 12000] [--resume]

行为：
    - 解包 EPUB，遍历所有 (x)html 章节
    - 跳过：<pre>/<code>/<math>/<svg>/<script>/<style>、公式段、装饰段
    - 翻译：正文在原节点紧下方插入同类型中文节点；标题和目录在同一行追加中文
    - 表格：在原 <table> 下方追加一份整表翻译版（class="translated-zh table-zh"）
    - 批量 JSON 翻译协议：多条原文组装为 [{"id":"8位base36","text":"..."}] 一次请求，
      模型返回 [{"id":"...","zh":"..."}]；摊薄 system_prompt 开销并大幅减少请求数
    - 批次大小按 token 预算动态调节（--batch-tokens，输入侧估算，默认 12000；
      输出约与输入同量级，单请求总量远小于 128K 上下文）
    - 并行调用 OpenAI 兼容 API，带缓存与断点续传
    - 注入 translated.css，重新打包为合规 EPUB

依赖：pip install openai beautifulsoup4 lxml
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, NavigableString, Tag
from openai import OpenAI

# ============ 配置加载 ============
# 优先级：命令行 --config > 环境变量 > 同目录 / 上级目录的 config.json > config.example.json
#
# 关键字段（来自 config.json）：
#   model_name   : OpenAI 兼容模型名，如 "deepseek-v4-flash" / "gpt-4o-mini"
#   api_key      : API key（可由环境变量 API_KEY 覆盖）
#   base_url     : OpenAI 兼容服务的根地址（不含 /chat/completions），可由环境变量 BASE_URL 覆盖
#   extra_body   : dict, 传给 OpenAI SDK 的 extra_body（DeepSeek 关闭思考: {"thinking":{"type":"disabled"}}）
#   system_prompt: 翻译用系统提示词
#
# 注意：API key 等敏感字段不应直接出现在脚本里。请使用同目录的 config.json
# （或 config.example.json）配置，或者通过环境变量传入。

# ============ 批量翻译协议 ============
# 请求（user text）：[{"id":"00000000","text":"原文1"},{"id":"00000001","text":"原文2"}, ...]
# 响应（要求模型）：[{"id":"00000000","zh":"译文1"},{"id":"00000001","zh":"译文2"}, ...]
#
# id 方案：批内顺序计数器编码为 8 位 base36（00000000 / 00000001 / ... / 0000000z / 00000010）。
# 选顺序码而非随机串的原因：
#   1) 零碰撞、零状态，天然与输入顺序对齐；
#   2) LLM 回显顺序短码的出错率远低于随机串；
#   3) 8 位 base36 提供 36^8 ≈ 2.8 万亿空间，足够短不占 token。
DEFAULT_SYSTEM_PROMPT = (
    "你是一个英译中批量翻译引擎。用户消息是一个 JSON 数组，每个元素形如 "
    '{"id":"<8位ID>","text":"<待翻译的英文原文>"}。\n'
    "你的任务：把每个元素的 text 翻译成简体中文，然后只输出一个 JSON 数组作为回答，"
    '每个元素形如 {"id":"<对应的原ID>","zh":"<中文译文>"}。\n'
    "硬性规则：\n"
    "1. 只输出 JSON 数组本身，不要输出任何解释、前后缀或 markdown 代码块标记。\n"
    "2. id 必须与输入完全一致，顺序与输入一致，条目数与输入相同；不得遗漏、合并或拆分任何条目。\n"
    "3. 译文准确、自然、符合中文技术写作习惯，不要逐字硬译；标题类短句保持标题语气。\n"
    "4. 保留所有 ⟦KEEP_0⟧ ⟦KEEP_1⟧ 之类的占位符原样不变，不要翻译、删除或改写它们。\n"
    "5. 保留专有名词、API 名、类名、函数名、产品名的英文原文（必要时可加中文解释）。\n"
    "6. 保留 Markdown / HTML 标记（如 **bold**、<em>）不变。\n"
    '7. 译文中的换行写成 \\n 转义，确保输出始终是合法 JSON。'
)

# ---- 批量分档参数 ----
B36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
ITEM_JSON_OVERHEAD_TOKENS = 14   # {"id":"xxxxxxxx","text":""} 的 JSON 包装开销（估算）
MAX_ITEMS_PER_BATCH = 128        # 单批条目上限：防止模型回显超长 id 列表时漂移
MIN_BATCH_TOKENS = 600           # 单批最小预算：自适应收缩时的下限，避免碎片请求


def _default_config_path() -> Path:
    """按优先级查找 config.json：脚本同目录 → assets 上一级 → 当前工作目录。"""
    here = Path(__file__).resolve().parent
    candidates = [
        here / "config.json",
        here.parent / "config.json",
        Path.cwd() / "config.json",
        here / "config.example.json",
        here.parent / "config.example.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "未找到 config.json。请在 skill 目录下创建 config.json（参考 config.example.json）。"
    )


def load_config(path: Optional[Path] = None) -> Dict[str, object]:
    """加载配置文件并允许环境变量覆盖。"""
    cfg_path = Path(path) if path else _default_config_path()
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 去掉以 _ 开头的注释字段
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}

    # 环境变量覆盖（最高优先级，便于在 CI / 临时调试时切换）
    if os.environ.get("MODEL_NAME"):
        cfg["model_name"] = os.environ["MODEL_NAME"]
    if os.environ.get("API_KEY"):
        cfg["api_key"] = os.environ["API_KEY"]
    if os.environ.get("BASE_URL"):
        cfg["base_url"] = os.environ["BASE_URL"]
    if os.environ.get("SYSTEM_PROMPT"):
        cfg["system_prompt"] = os.environ["SYSTEM_PROMPT"]
    if os.environ.get("BATCH_TOKENS"):
        cfg["batch_tokens"] = int(os.environ["BATCH_TOKENS"])

    # 兜底默认值
    cfg.setdefault("model_name", "deepseek-v4-flash")
    cfg.setdefault("extra_body", {})
    cfg.setdefault("batch_tokens", 12000)
    cfg.setdefault("system_prompt", DEFAULT_SYSTEM_PROMPT)

    for k in ("api_key", "base_url", "model_name"):
        if not cfg.get(k):
            raise ValueError(
                f"配置缺少必填项: {k}。请在 config.json 中设置，或通过环境变量 {k.upper()} 提供。"
            )

    # 规范化 base_url：去掉末尾的 /chat/completions 与多余斜杠
    bu = str(cfg["base_url"]).strip().rstrip("/")
    if bu.endswith("/chat/completions"):
        bu = bu[: -len("/chat/completions")].rstrip("/")
    cfg["base_url"] = bu

    return cfg


# 全局配置（在 main() 里完成初始化；模块导入时延迟加载）
CONFIG: Dict[str, object] = {}

# ============ 跳过翻译的标签与正则 ============
SKIP_TAGS = {"pre", "code", "math", "svg", "script", "style", "noscript", "kbd", "samp", "tt"}
TRANSLATE_TAGS = {
    "p", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
    "caption", "figcaption", "dd", "dt",
}
# Springer/出版社把"段落"用 <div class="Para"> 标记，把"代码块"用 <div class="ProgramCode"> 标记。
# 也兼容 ePub3/各出版社常见的代码块 class 名。
TRANSLATE_DIV_CLASSES = {"Para"}
SKIP_DIV_CLASSES = {
    # Springer 代码/公式
    "ProgramCode", "LineGroup", "FixedLine",
    "EmphasisFontCategoryNonProportional",  # 等宽字体（行内代码）
    # O'Reilly / Apress / 通用
    "code", "Code", "code-block", "CodeBlock", "listing", "Listing",
    "informalexample", "programlisting", "screen", "literallayout",
    # MathJax / 公式相关
    "MathJax", "math", "equation", "Equation", "EquationContent",
    # 索引条目不翻译；目录链接由 collect_units_in_html(..., include_toc_links=True) 单独处理。
    "TocEntry", "TocItem", "TocPageNumber", "TocChapter",
    "TocSection1", "TocSection2", "TocBack",
    "PrimaryIE", "SecondaryIE", "TertiaryIE",
    "LohEntry", "LohItem", "LohPageNumber",
    "Occurrence", "IndexPageNumbers",
    # 图注容器：由内部 <p>/<div> 单独翻译，整体不翻避免重复
    "Caption", "CaptionContent", "Figure", "MediaObject",
}

# 段落内需要保护的"片段"：行内代码、公式、URL
PLACEHOLDER_RE = re.compile(
    r"(`[^`\n]+`"          # `inline code`
    r"|\$\$[^$]+\$\$"      # $$ block math $$
    r"|\$[^$\n]+\$"        # $ inline math $
    r"|\\\([^)]+\\\)"      # \( inline math \)
    r"|\\\[[^\]]+\\\]"     # \[ block math \]
    r"|https?://\S+"       # URLs
    r")"
)

# 一个段落"看起来只是公式 / 数字 / 符号"，跳过翻译
PURE_NON_TEXT_RE = re.compile(
    r"^[\s\d\W_]*$|^\$[^$]+\$$|^\\\([^)]+\\\)$|^\\\[[^\]]+\\\]$"
)

CACHE: Dict[str, str] = {}
CACHE_PATH: Optional[Path] = None
CACHE_LOCK = threading.Lock()


# ============ OpenAI 兼容客户端 ============
CLIENT: Optional[OpenAI] = None


def make_client() -> OpenAI:
    """根据 CONFIG 构造 OpenAI 兼容客户端（懒加载）。"""
    if not CONFIG:
        raise RuntimeError("CONFIG 未初始化，请先调用 load_config()。")
    return OpenAI(
        api_key=str(CONFIG["api_key"]),
        base_url=str(CONFIG["base_url"]),
    )


def _client() -> OpenAI:
    global CLIENT
    if CLIENT is None:
        CLIENT = make_client()
    return CLIENT


def call_llm_once(user_content: str) -> str:
    """单次 LLM 调用（不重试），失败抛异常。user_content 为批量 JSON 数组文本。"""
    kwargs = dict(
        model=str(CONFIG["model_name"]),
        messages=[
            {"role": "system", "content": str(CONFIG.get("system_prompt", ""))},
            {"role": "user", "content": user_content},
        ],
        stream=False,
    )
    extra_body = CONFIG.get("extra_body") or {}
    if extra_body:
        kwargs["extra_body"] = extra_body
    resp = _client().chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def batch_id(i: int) -> str:
    """批内顺序索引 -> 8 位 base36 编码（00000000, 00000001, ...）。"""
    n = i
    out = ""
    while n:
        out = B36_DIGITS[n % 36] + out
        n //= 36
    return out.rjust(8, "0")


def est_tokens(s: str) -> float:
    """粗估 token 数：中文约 0.7 token/字，其它文字约 3.5 字符/token。仅用于分批预算。"""
    cjk = 0
    for c in s:
        if "\u4e00" <= c <= "\u9fff":
            cjk += 1
    return cjk * 0.7 + (len(s) - cjk) / 3.5


def parse_llm_json_array(raw: str) -> Optional[List[dict]]:
    """健壮解析模型返回：剥 markdown 围栏 -> 直接 parse -> 兜底截取最外层 [ ... ]。

    也兼容被对象包裹的返回：{"items":[...]} / {"data":[...]} / {"translations":[...]}。
    解析失败返回 None（由上层重试）。
    """
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s).strip()

    def _try_load(text: str) -> Optional[list]:
        try:
            v = json.loads(text)
        except Exception:
            return None
        if isinstance(v, dict):
            for key in ("items", "data", "translations", "list", "results"):
                if isinstance(v.get(key), list):
                    return v[key]
            return None
        return v if isinstance(v, list) else None

    arr = _try_load(s)
    if arr is None:
        lpos = s.find("[")
        rpos = s.rfind("]")
        if lpos == -1 or rpos <= lpos:
            return None
        arr = _try_load(s[lpos: rpos + 1])
    if arr is None:
        return None
    return [el for el in arr if isinstance(el, dict)]


def translate_items_json(items: List[Dict[str, str]],
                         max_retry: int = 5,
                         label: str = "") -> Dict[str, str]:
    """按批量 JSON 协议翻译一组 {"id","text"} 条目，返回 {id: 中文译文}。

    可靠性策略：
      - 网络错误 / 空响应 / 非法 JSON / 条目缺失 -> 整批指数退避重试；
      - 重试耗尽后返回已成功校验的部分结果（缺失条目由上层单条兜底）。
    校验规则：id 必须在本批集合内、zh 非空、重复 id 取首个。
    """
    payload = json.dumps(items, ensure_ascii=False)
    valid_ids = {it["id"] for it in items}
    backoff = 2.0
    got: Dict[str, str] = {}
    last_err: object = "unknown"

    for attempt in range(max_retry):
        try:
            raw = call_llm_once(payload)
            arr = parse_llm_json_array(raw) if raw else None
            if arr is None:
                last_err = "empty/invalid JSON response" if not raw else "invalid JSON response"
            else:
                got = {}
                for el in arr:
                    iid = str(el.get("id", ""))
                    zh = el.get("zh")
                    if not isinstance(zh, str):
                        zh = str(el.get("translation") or el.get("text") or "")
                    zh = zh.strip()
                    if iid in valid_ids and zh and iid not in got:
                        got[iid] = zh
                if len(got) == len(valid_ids):
                    return got
                last_err = f"missing {len(valid_ids) - len(got)}/{len(valid_ids)} items"
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt < max_retry - 1:
            time.sleep(backoff)
            backoff *= 2

    prefix = f"[{label}] " if label else ""
    if got:
        print(f"  [WARN] {prefix}批次部分成功 {len(got)}/{len(valid_ids)}，缺失条目将逐条兜底: {last_err}",
              file=sys.stderr)
    else:
        print(f"  [WARN] {prefix}批次翻译失败: {last_err}", file=sys.stderr)
    return got


# ============ 占位符保护 ============
def protect(text: str) -> Tuple[str, List[str]]:
    keeps: List[str] = []

    def _sub(m: re.Match) -> str:
        keeps.append(m.group(0))
        return f"⟦KEEP_{len(keeps) - 1}⟧"

    masked = PLACEHOLDER_RE.sub(_sub, text)
    return masked, keeps


def restore(text: str, keeps: List[str]) -> str:
    for i, k in enumerate(keeps):
        text = text.replace(f"⟦KEEP_{i}⟧", k)
    return text


# ============ 批量翻译（带缓存） ============
def split_batches(texts: List[str], token_budget: float) -> List[List[str]]:
    """按预估 token 预算贪心分批；同时受 MAX_ITEMS_PER_BATCH 条目数上限约束。

    超过预算的单条长文本（如超长段落）独立成批，不强制切分（保持段落完整性）。
    """
    batches: List[List[str]] = []
    cur: List[str] = []
    cur_tokens = 0.0
    for t in texts:
        cost = est_tokens(t) + ITEM_JSON_OVERHEAD_TOKENS
        if cur and (cur_tokens + cost > token_budget or len(cur) >= MAX_ITEMS_PER_BATCH):
            batches.append(cur)
            cur, cur_tokens = [], 0.0
        cur.append(t)
        cur_tokens += cost
    if cur:
        batches.append(cur)
    return batches


def run_batch(texts: List[str], label: str = "") -> Dict[str, str]:
    """把一批原文打包为 JSON 数组请求翻译，返回 {原文: 译文}。

    流程：占位符保护 -> 组装 [{"id","text"}] -> 批量调用 ->
          解析校验 -> 还原占位符 -> 缺失条目单条兜底（同一协议的单元素数组）。
    """
    items: List[Dict[str, str]] = []
    item_by_id: Dict[str, Dict[str, str]] = {}
    meta: Dict[str, Tuple[str, List[str]]] = {}   # id -> (原文, keeps)
    for i, t in enumerate(texts):
        masked, keeps = protect(t)
        iid = batch_id(i)
        it = {"id": iid, "text": masked}
        items.append(it)
        item_by_id[iid] = it
        meta[iid] = (t, keeps)

    got = translate_items_json(items, label=label)

    results: Dict[str, str] = {}
    missing_ids: List[str] = []
    for iid, (orig, keeps) in meta.items():
        zh_masked = got.get(iid)
        if zh_masked:
            results[orig] = restore(zh_masked, keeps).strip()
        else:
            missing_ids.append(iid)

    # 兜底：批量响应中缺失的条目逐条重试（单元素数组，仍是同一协议）
    for iid in missing_ids:
        single = translate_items_json([item_by_id[iid]], max_retry=3, label=f"{label}#{iid}")
        zh_masked = single.get(iid, "")
        orig, keeps = meta[iid]
        results[orig] = restore(zh_masked, keeps).strip() if zh_masked else ""
    return results


def flush_cache() -> None:
    if CACHE_PATH is None:
        return
    with CACHE_LOCK:
        snapshot = dict(CACHE)  # 拷贝快照避免在写盘期间发生 dict resize
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def load_cache(resume: bool) -> None:
    global CACHE
    if CACHE_PATH and resume and CACHE_PATH.exists():
        try:
            CACHE = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            print(f"  [INFO] 命中缓存条目: {len(CACHE)}")
        except Exception as e:
            print(f"  [WARN] 加载缓存失败: {e}", file=sys.stderr)
            CACHE = {}


# ============ HTML 解析与翻译单元收集 ============
def _classes_of(node: Tag) -> List[str]:
    c = node.get("class", [])
    if isinstance(c, str):
        return c.split()
    return list(c or [])


def _classes_of_value(value: object) -> List[str]:
    """Normalise BeautifulSoup's class callback value (str or list)."""
    if isinstance(value, str):
        return value.split()
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _has_inline_translation(node: Tag) -> bool:
    return node.find(
        class_=lambda classes: "translated-zh" in _classes_of_value(classes)
    ) is not None


def is_skippable_node(node: Tag) -> bool:
    """节点本身或祖先含跳过标签 / 跳过 class 则跳过。"""
    cur = node
    while cur is not None and isinstance(cur, Tag):
        name = (cur.name or "").lower()
        if name in SKIP_TAGS:
            return True
        if name == "div":
            for c in _classes_of(cur):
                if c in SKIP_DIV_CLASSES:
                    return True
        # span/em 上的等宽类也算行内代码
        if name in ("span", "em", "i", "b", "strong"):
            for c in _classes_of(cur):
                if c in SKIP_DIV_CLASSES:
                    return True
        cur = cur.parent
    return False


def node_pure_text(node: Tag) -> str:
    """提取节点的纯文本，自动跳过 skippable 子树（代码块/公式/img/...）。"""
    parts: List[str] = []
    for desc in node.descendants:
        if isinstance(desc, NavigableString):
            parent = desc.parent
            if parent is not None and is_skippable_node(parent):
                continue
            s = str(desc)
            if s.strip():
                parts.append(s)
    return " ".join(" ".join(parts).split())


INLINE_KEEP_TOKEN_RE = re.compile(r"⟦KEEP_(\d+)⟧")


def _is_inline_code_node(node: Tag, root: Tag) -> bool:
    """Return whether *node* is code embedded in a translatable text node.

    ``is_skippable_node`` deliberately treats every ``<code>`` element as
    non-translatable. That is correct for blocks, but it used to make a
    ``<code>`` descendant disappear from the Chinese companion paragraph.
    """
    if node is root or (node.name or "").lower() not in {"code", "kbd", "samp", "tt"}:
        return False
    ancestor = node.parent
    while ancestor is not None and ancestor is not root:
        if isinstance(ancestor, Tag) and is_skippable_node(ancestor):
            return False
        ancestor = ancestor.parent
    return node.find_parent("pre") is None


def inline_code_nodes(node: Tag) -> List[Tag]:
    """Collect inline-code nodes in document order, excluding code blocks."""
    return [tag for tag in node.find_all(["code", "kbd", "samp", "tt"])
            if _is_inline_code_node(tag, node)]


def node_translation_text(node: Tag) -> str:
    """Extract prose while preserving inline code as model-visible KEEP tokens."""
    code_index = {id(tag): i for i, tag in enumerate(inline_code_nodes(node))}
    parts: List[str] = []
    emitted: set[int] = set()
    for desc in node.descendants:
        if not isinstance(desc, NavigableString):
            continue
        parent = desc.parent
        code_parent = next(
            (ancestor for ancestor in ([parent] + list(parent.parents))
             if isinstance(ancestor, Tag) and id(ancestor) in code_index),
            None,
        ) if parent is not None else None
        if code_parent is not None:
            code_id = id(code_parent)
            if code_id not in emitted:
                parts.append(f"⟦KEEP_{code_index[code_id]}⟧")
                emitted.add(code_id)
            continue
        if parent is not None and is_skippable_node(parent):
            continue
        text = str(desc)
        if text.strip():
            parts.append(text)
    return " ".join(" ".join(parts).split())


def set_translated_contents(soup: BeautifulSoup, target: Tag, zh: str, source: Tag) -> None:
    """Populate a Chinese node, restoring inline-code placeholders as HTML."""
    codes = inline_code_nodes(source)
    target.clear()
    cursor = 0
    restored: set[int] = set()
    for match in INLINE_KEEP_TOKEN_RE.finditer(zh):
        if match.start() > cursor:
            target.append(NavigableString(zh[cursor:match.start()]))
        index = int(match.group(1))
        if index < len(codes):
            target.append(copy.deepcopy(codes[index]))
            restored.add(index)
        else:
            target.append(NavigableString(match.group(0)))
        cursor = match.end()
    if cursor < len(zh):
        target.append(NavigableString(zh[cursor:]))
    # A compliant model keeps every token in place.  If a provider drops one,
    # preserve the code rather than silently losing it from the Chinese text.
    for index, code in enumerate(codes):
        if index not in restored:
            target.append(NavigableString(" "))
            target.append(copy.deepcopy(code))


def _add_class(node: Tag, class_name: str) -> None:
    node["class"] = list(dict.fromkeys(_classes_of(node) + [class_name]))


def _is_formula_image(image: Tag) -> bool:
    """Keep formula images out of illustration sizing/deduplication rules."""
    if image.find_parent(["math", "svg"]) is not None:
        return True
    classes = set(_classes_of(image))
    if {"formula-img", "math-img", "math-formula"} & classes:
        return True
    alt = (image.get("alt", "") or "").strip()
    return bool(re.search(r"\\[A-Za-z]+|[{}_^]|[∑∫√∞≤≥≈≠]", alt))


def normalise_images_for_reading(soup: BeautifulSoup, seen_sources: set[str]) -> int:
    """Show each ordinary image resource once and assign conservative display classes.

    EPUB conversion pipelines commonly duplicate an illustration in consecutive
    containers or in a translated table clone.  A resource seen earlier in the
    book is removed; formula images are deliberately excluded.
    """
    removed = 0
    for image in list(soup.find_all("img")):
        if _is_formula_image(image):
            _add_class(image, "formula-img")
            continue
        src = (image.get("src", "") or "").strip()
        if not src:
            continue
        key = src.split("#", 1)[0]
        if key in seen_sources:
            parent = image.parent
            image.decompose()
            # Avoid leaving empty visual paragraphs/figures after deduplication.
            if isinstance(parent, Tag) and parent.name in {"p", "figure", "div"}:
                if not parent.get_text("", strip=True) and not parent.find("img"):
                    parent.decompose()
            removed += 1
            continue
        seen_sources.add(key)
        parent = image.parent
        parent_text = parent.get_text(" ", strip=True) if isinstance(parent, Tag) else ""
        # alt text should not make an otherwise standalone image look inline.
        if parent_text and parent_text != (image.get("alt", "") or "").strip():
            _add_class(image, "epub-inline-image")
        else:
            _add_class(image, "epub-image")
            if isinstance(parent, Tag) and parent.name in {"p", "figure", "div"}:
                _add_class(parent, "epub-image-container")
    return removed


def node_text_excluding_block_children(node: Tag) -> str:
    """提取节点文本，但排除内部已经是"独立段落级"的子节点（避免重复翻译）。

    用于 div.Para 这种容器：如果内部嵌套 <p>/<figure>/<figcaption>/<h*>，
    那些将由各自的逻辑单独翻译；外层 div 只翻译散落在外的散文文本。
    """
    block_children_tags = TRANSLATE_TAGS | {"figure", "table", "ul", "ol"}
    parts: List[str] = []
    for desc in node.descendants:
        if isinstance(desc, NavigableString):
            parent = desc.parent
            # 跳过 skippable 子树
            anc = parent
            in_block_child = False
            while anc is not None and anc is not node and isinstance(anc, Tag):
                if is_skippable_node(anc):
                    in_block_child = True
                    break
                if anc.name in block_children_tags:
                    in_block_child = True
                    break
                anc = anc.parent
            if in_block_child:
                continue
            s = str(desc)
            if s.strip():
                parts.append(s)
    return " ".join(" ".join(parts).split())


def should_translate_text(text: str) -> bool:
    if not text or len(text.strip()) < 2:
        return False
    if PURE_NON_TEXT_RE.match(text):
        return False
    # 已经全部是 CJK 字符 → 跳过
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    if cjk > 0 and cjk / max(len(text), 1) > 0.5:
        return False
    return True


def collect_units_in_html(soup: BeautifulSoup, *, include_toc_links: bool = False) -> Tuple[List[Tag], Dict[int, str]]:
    """收集需要翻译的段落级节点。

    返回 (units, text_overrides)：
    - units：待翻译节点列表
    - text_overrides：dict[id(node) -> str]，对某些节点（如 div.Para 内含子段落的容器），
      要用"剥除子段落后的散文文本"作为翻译输入，而非默认的 node_pure_text。

    幂等性保证：
    - 跳过任何 class 已含 `translated-zh` 的节点。
    - 跳过任何已经有"紧邻 translated-zh 兄弟"的源节点。
    """
    units: List[Tag] = []
    text_overrides: Dict[int, str] = {}
    seen_ids: set = set()

    def _already_translated(node: Tag) -> bool:
        if "translated-zh" in _classes_of(node):
            return True
        # 标题和目录译文内嵌在原节点中，而非作为相邻兄弟。
        if _has_inline_translation(node):
            return True
        nxt = node.find_next_sibling()
        if nxt is not None and isinstance(nxt, Tag) and "translated-zh" in _classes_of(nxt):
            return True
        return False

    # 判定：节点是否含会被独立翻译的块级子节点（若是，则本节点不整体翻译，避免双中文）
    BLOCK_FOR_DEDUP = TRANSLATE_TAGS | {"figure", "table", "ul", "ol", "figcaption"}

    def _has_translatable_block_child(tag: Tag) -> bool:
        # 直接子元素中是否有任何一个会被标准段落逻辑单独翻译
        for child in tag.find_all(TRANSLATE_TAGS, recursive=False):
            if is_skippable_node(child):
                continue
            t = node_pure_text(child)
            if should_translate_text(t):
                return True
        # figcaption / .Caption 容器：内部通常有 <p class="SimplePara">，不要整体翻译
        for child in tag.find_all(["p", "div"], recursive=False):
            if is_skippable_node(child):
                continue
            t = node_pure_text(child)
            if should_translate_text(t):
                return True
        return False

    # A navigation document is structurally a list of links.  Translate only
    # those labels, otherwise both <li> and its <a> would receive a translation.
    if include_toc_links:
        for tag in soup.find_all("a", href=True):
            if tag.find_parent(list(SKIP_TAGS)) is not None or _already_translated(tag):
                continue
            text = " ".join(tag.get_text(" ", strip=True).split())
            if should_translate_text(text) and id(tag) not in seen_ids:
                seen_ids.add(id(tag))
                units.append(tag)
        return units, text_overrides

    # 1. 标准段落级标签
    for tag in soup.find_all(TRANSLATE_TAGS):
        if is_skippable_node(tag):
            continue
        if _already_translated(tag):
            continue
        if tag.name == "li" and tag.find(["p", "blockquote"]):
            continue
        if tag.find_parent("table") is not None:
            continue
        # figcaption / 含独立可翻译块子节点的容器，跳过整体翻译（子节点会单独翻）
        if tag.name == "figcaption" or _has_translatable_block_child(tag):
            continue
        text = node_translation_text(tag)
        if should_translate_text(text):
            if id(tag) not in seen_ids:
                seen_ids.add(id(tag))
                units.append(tag)

    # 2. 出版社特有的 <div class="Para"> 等"段落 div"
    for tag in soup.find_all("div"):
        if is_skippable_node(tag):
            continue
        if _already_translated(tag):
            continue
        classes = _classes_of(tag)
        if not any(c in TRANSLATE_DIV_CLASSES for c in classes):
            continue

        # 内部含 figure/p/h*/li/ul/ol 等"块级子段落"时：
        # - 内部的子段落由各自的逻辑独立翻译
        # - 外层 div 仅翻译"剥除这些子段落之后"剩下的散文，避免重复
        has_block_child = bool(tag.find(TRANSLATE_TAGS | {"figure", "table", "ul", "ol"}))
        if has_block_child:
            residual = node_text_excluding_block_children(tag)
            if should_translate_text(residual) and id(tag) not in seen_ids:
                # 注意：因为 div 内部有子节点，append 译文兄弟也可能改变文档结构，
                # 但 BeautifulSoup 的 insert_after 是在 parent 内的同级位置插入，安全。
                seen_ids.add(id(tag))
                text_overrides[id(tag)] = residual
                units.append(tag)
            continue

        text = node_translation_text(tag)
        if should_translate_text(text) and id(tag) not in seen_ids:
            seen_ids.add(id(tag))
            units.append(tag)

    return units, text_overrides


def collect_tables(soup: BeautifulSoup) -> List[Tag]:
    return [t for t in soup.find_all("table") if not is_skippable_node(t)]


def collect_table_cell_texts(table: Tag) -> List[str]:
    texts: List[str] = []
    for cell in table.find_all(["th", "td"]):
        if is_skippable_node(cell):
            continue
        t = node_translation_text(cell)
        if should_translate_text(t):
            texts.append(t)
    return texts


# ============ 并行翻译 ============
def translate_batch(unique_texts: List[str], concurrency: int,
                    batch_tokens: Optional[int] = None) -> Dict[str, str]:
    """批量并行翻译调度：分批 -> 批级线程池 -> 写缓存。

    批次大小动态调节（参考模型 128K 输入/输出上下文）：
      - 基准：单批输入侧预估 token 预算（默认 12000；输出约与输入同量级，
        单请求总量 << 128K，留足安全余量，可经 --batch-tokens / config 调大）；
      - 自适应收缩：若按预算分出的批数 < 并发数 x2，则缩小预算（下限
        MIN_BATCH_TOKENS），让线程池保持满载 -- 既摊薄 system_prompt 开销，
        又不牺牲并行度。
    """
    todo = [t for t in unique_texts if t not in CACHE]
    if not todo:
        return {t: CACHE.get(t, "") for t in unique_texts}

    total = len(todo)
    budget = float(batch_tokens or CONFIG.get("batch_tokens") or 12000)
    total_tokens = sum(est_tokens(t) + ITEM_JSON_OVERHEAD_TOKENS for t in todo)
    # 自适应收缩：保证批数至少为并发数的 2 倍
    if total_tokens / budget < concurrency * 2:
        budget = max(float(MIN_BATCH_TOKENS), total_tokens / (concurrency * 2))

    batches = split_batches(todo, budget)
    # 自适应 flush 间隔：总数大时拉大间隔，避免频繁写大 json 阻塞线程切换
    flush_every = max(50, total // 20)
    print(f"  [INFO] 待翻译 {total} 条 -> {len(batches)} 批 "
          f"(单批≈{int(budget)} tokens, 均值 {total // max(len(batches), 1)} 条/批, "
          f"并发 {concurrency}, flush 每 {flush_every} 条) ...")

    done = 0
    items_since_flush = 0
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(run_batch, b, f"B{i:03d}"): b
                   for i, b in enumerate(batches)}
        for fu in as_completed(futures):
            b = futures[fu]
            try:
                res = fu.result()
            except Exception as e:  # noqa: BLE001
                print(f"  [ERR] 批次异常({len(b)} 条): {e}", file=sys.stderr)
                res = {t: "" for t in b}
            with CACHE_LOCK:
                for t, zh in res.items():
                    CACHE[t] = zh
            done += len(b)
            items_since_flush += len(b)
            if items_since_flush >= flush_every or done >= total:
                elapsed = time.time() - t_start
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                print(f"    ... {done}/{total}  {rate:.1f}/条·s  ETA {eta:.0f}s")
                flush_cache()
                items_since_flush = 0
    flush_cache()
    return {t: CACHE.get(t, "") for t in unique_texts}


# ============ 写回 HTML ============
def insert_translation_node(soup: BeautifulSoup, node: Tag, zh: str) -> None:
    if not zh:
        return
    # 标题和目录采用“英文 · 中文”同一行，目录仍保持同一个可点击链接。
    if node.name in {"h1", "h2", "h3", "h4", "h5", "h6", "a"}:
        if _has_inline_translation(node):
            return
        separator = soup.new_tag("span")
        separator["class"] = ["translated-separator"]
        separator["aria-hidden"] = "true"
        separator.string = " · "
        inline = soup.new_tag("span")
        inline["class"] = ["translated-zh", "translated-inline"]
        inline["lang"] = "zh-CN"
        set_translated_contents(soup, inline, zh, node)
        node.append(separator)
        node.append(inline)
        return
    # 幂等性：避免在已经有 translated-zh 紧邻兄弟的情况下重复插入
    nxt = node.find_next_sibling()
    if nxt is not None and isinstance(nxt, Tag) and "translated-zh" in _classes_of(nxt):
        return
    new_tag = soup.new_tag(node.name)
    # 去重 class（避免 translated-zh translated-zh）
    classes = list(dict.fromkeys(_classes_of(node) + ["translated-zh"]))
    new_tag["class"] = classes
    new_tag["lang"] = "zh-CN"
    set_translated_contents(soup, new_tag, zh, node)
    node.insert_after(new_tag)


def translate_table_node(soup: BeautifulSoup, table: Tag) -> None:
    # 幂等性：如果下一个兄弟已经是 translated-zh 的翻译表，跳过
    nxt = table.find_next_sibling()
    if nxt is not None and isinstance(nxt, Tag):
        ncls = _classes_of(nxt)
        if "translated-zh" in ncls and "table-zh" in ncls:
            return
    cloned = copy.deepcopy(table)
    # The Chinese table is a textual counterpart; retain illustrations only in
    # the original table so an EPUB never renders the same image twice here.
    for image in cloned.find_all("img"):
        image.decompose()
    changed = False
    original_cells = table.find_all(["th", "td"])
    cloned_cells = cloned.find_all(["th", "td"])
    for source_cell, cell in zip(original_cells, cloned_cells):
        if is_skippable_node(source_cell):
            continue
        text = node_translation_text(source_cell)
        if not should_translate_text(text):
            continue
        zh = CACHE.get(text, "")
        if zh:
            set_translated_contents(soup, cell, zh, source_cell)
            changed = True
    if changed:
        classes = list(dict.fromkeys(_classes_of(cloned) + ["translated-zh", "table-zh"]))
        cloned["class"] = classes
        table.insert_after(cloned)


# ============ EPUB 打包/解包 ============
def unpack_epub(epub_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub_path, "r") as zf:
        zf.extractall(dest)


def pack_epub(src_dir: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    # mimetype 必须 STORED 且为第一个条目
    with zipfile.ZipFile(out_path, "w") as zf:
        mt = src_dir / "mimetype"
        if mt.exists():
            zi = zipfile.ZipInfo("mimetype")
            zi.compress_type = zipfile.ZIP_STORED
            zf.writestr(zi, mt.read_bytes())
        for root, _, files in os.walk(src_dir):
            for name in files:
                p = Path(root) / name
                rel = p.relative_to(src_dir).as_posix()
                if rel == "mimetype":
                    continue
                zf.write(p, rel, compress_type=zipfile.ZIP_DEFLATED)


def read_epub_title(extract_dir: Path) -> str:
    """Return the first OPF dc:title without depending on an XML extension."""
    for opf_path in extract_dir.rglob("*.opf"):
        try:
            root = ET.parse(opf_path).getroot()
        except ET.ParseError:
            continue
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] == "title":
                title = " ".join((node.text or "").split())
                if title:
                    return title
    return ""


def safe_epub_filename(title: str, fallback_stem: str) -> str:
    """Make a translated book title usable as a Windows EPUB filename."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    # Leave enough room for the .epub suffix and normal filesystem metadata.
    return (cleaned or fallback_stem).strip()[:120].rstrip(" .") or "translated-book"


# ============ CSS 注入 ============
TRANSLATED_CSS = """
/* === Bilingual reader stylesheet: portable, calm, and reader-theme-safe === */
html, body, section, article, div, p, span, h1, h2, h3, h4, h5, h6,
li, td, th, blockquote, figure, figcaption, caption {
    color: #000000 !important;
    background-color: transparent !important;
    font-family: "LXGW WenKai", "LXGW WenKai Screen", "LXGW WenKai GB",
                 "霞鹜文楷", "Source Han Serif SC", "Noto Serif CJK SC",
                 "Songti SC", "STSong", "SimSun", serif !important;
    font-size: 1em;
    line-height: 1.75;
}
body { margin: 0; }
p, li, blockquote, dd, dt, figcaption, caption {
    margin-top: 0;
    margin-bottom: 0.8em;
}
/* English source and Chinese translation deliberately share font, size and spacing. */
.translated-zh {
    color: #000000 !important;
    font: inherit !important;
    line-height: inherit !important;
    background: transparent !important;
    border: 0 !important;
    border-radius: 0;
    padding: 0 !important;
    margin-top: 0.18em;
    margin-bottom: 0.8em;
    font-weight: normal;
    text-decoration: none;
}
/* Headings and EPUB3 TOC entries: English first, Chinese immediately after it. */
.translated-inline {
    display: inline;
    margin: 0 !important;
    white-space: normal;
}
.translated-separator {
    display: inline;
    margin: 0 0.18em;
    color: #666666 !important;
    font-weight: normal;
}
h1, h2, h3, h4, h5, h6 { margin-bottom: 0.8em; }
nav a .translated-inline, .translated-inline { color: inherit !important; }

/* Code must remain readable at body size, in a clearly bounded mono block. */
pre, pre code, .ProgramCode, .programcode, .programcode1, .CodeBlock,
.code-block, .listing, .Listing, .programlisting, .screen, .literallayout {
    display: block;
    font-family: "LXGW WenKai Mono", "霞鹜文楷等宽", "LXGW WenKai Mono Lite",
                 "Source Code Pro", "JetBrains Mono", Consolas, monospace !important;
    font-size: 1em !important;
    line-height: 1.75 !important;
    color: #000000 !important;
    background: #f8f9fb !important;
    border: 1px solid #aeb7c2 !important;
    border-radius: 4px;
    padding: 0.75em 0.9em !important;
    margin: 1em 0;
    overflow-x: auto;
    white-space: pre-wrap;
    overflow-wrap: break-word;
    tab-size: 4;
    page-break-inside: avoid;
    break-inside: avoid;
}
.ProgramCode *, .programcode *, .programcode1 *, pre *, code {
    font-family: "LXGW WenKai Mono", "霞鹜文楷等宽", Consolas, monospace !important;
    font-size: inherit !important;
    line-height: inherit !important;
}
/* Reader-safe responsive images; do not accidentally turn inline formula images into blocks. */
img { max-width: 100% !important; height: auto !important; vertical-align: middle; }
p > img { display: inline; max-height: 1.3em; width: auto; margin: 0 0.1em; }
/* Ordinary illustrations are deliberately smaller than the reading surface. */
.epub-image-container { text-align: center; }
img.epub-image {
    display: block;
    width: auto !important;
    max-width: 82% !important;
    max-height: 65vh;
    margin: 0.9em auto;
}
img.epub-inline-image { max-width: 1.5em !important; max-height: 1.5em; }
p.formula-block, p.图, p.sgc-11, p.sgc-3 { text-align: center; margin: 0.9em 0; }
p.formula-block > img, p.图 > img, p.sgc-11 > img, p.sgc-3 > img {
    display: inline-block; max-height: none; max-width: 95%; margin: 0;
}
""".lstrip()


def inject_css(work_dir: Path) -> None:
    """把 translated.css 放到与 content.opf 同级的 styles 目录或 OEBPS 根，并写入引用。"""
    # 找到所有 xhtml/html 文件
    html_files = list(work_dir.rglob("*.xhtml")) + list(work_dir.rglob("*.html"))
    if not html_files:
        return

    # 把 css 文件放到第一个 html 同级目录
    base = html_files[0].parent
    css_path = base / "translated.css"
    css_path.write_text(TRANSLATED_CSS, encoding="utf-8")

    for hf in html_files:
        try:
            soup = BeautifulSoup(hf.read_text(encoding="utf-8"), "lxml-xml")
        except Exception:
            soup = BeautifulSoup(hf.read_text(encoding="utf-8"), "html.parser")
        head = soup.find("head")
        if head is None:
            continue
        # 已有则跳过
        already = any(
            (link.get("href", "") or "").endswith("translated.css")
            for link in head.find_all("link")
        )
        if already:
            continue
        rel = os.path.relpath(css_path, hf.parent).replace("\\", "/")
        link = soup.new_tag("link", rel="stylesheet", type="text/css", href=rel)
        head.append(link)
        hf.write_text(str(soup), encoding="utf-8")

    # 把 css 登记到 content.opf
    opfs = list(work_dir.rglob("*.opf"))
    for opf in opfs:
        try:
            soup = BeautifulSoup(opf.read_text(encoding="utf-8"), "lxml-xml")
        except Exception:
            continue
        manifest = soup.find("manifest")
        if manifest is None:
            continue
        if any(
            (it.get("href", "") or "").endswith("translated.css")
            for it in manifest.find_all("item")
        ):
            continue
        rel = os.path.relpath(css_path, opf.parent).replace("\\", "/")
        item = soup.new_tag(
            "item", id="translated-css", href=rel,
        )
        item["media-type"] = "text/css"
        manifest.append(item)
        opf.write_text(str(soup), encoding="utf-8")


# ============ 主流程 ============
def scan_html_file(hf: Path, *, include_toc_links: bool = False,
                   seen_image_sources: Optional[set[str]] = None) -> Tuple[BeautifulSoup, List[Tag], Dict[int, str], List[Tag], List[str]]:
    """扫描单个 html 文件，返回 (soup, para_units, text_overrides, tables, unique_texts)。
    不调用 API，只解析 DOM 与抽取文本，可以多线程并行执行。
    """
    raw = hf.read_text(encoding="utf-8")
    soup = BeautifulSoup(raw, "html.parser")
    normalise_images_for_reading(soup, seen_image_sources if seen_image_sources is not None else set())
    para_units, text_overrides = collect_units_in_html(soup, include_toc_links=include_toc_links)
    tables = collect_tables(soup)
    unique: List[str] = []
    seen = set()
    for n in para_units:
        t = text_overrides.get(id(n)) or node_translation_text(n)
        if t and t not in seen:
            seen.add(t)
            unique.append(t)
    for tb in tables:
        for t in collect_table_cell_texts(tb):
            if t and t not in seen:
                seen.add(t)
                unique.append(t)
    return soup, para_units, text_overrides, tables, unique


def is_navigation_document(path: Path) -> bool:
    """Recognise EPUB3 navigation files even when publishers use custom names."""
    if path.name.lower() in {"navigation.xhtml", "nav.xhtml", "toc.xhtml"}:
        return True
    try:
        sample = path.read_text(encoding="utf-8", errors="ignore")[:20000].lower()
    except OSError:
        return False
    return "<nav" in sample and ("toc" in sample or "table of contents" in sample)


def collect_ncx_texts(ncx_path: Path) -> List[str]:
    """Collect EPUB2 NCX navLabel text so legacy-reader directories are bilingual too."""
    root = ET.parse(ncx_path).getroot()
    texts: List[str] = []
    for label in root.iter():
        if label.tag.rsplit("}", 1)[-1] != "navLabel":
            continue
        node = next((child for child in label if child.tag.rsplit("}", 1)[-1] == "text"), None)
        text = " ".join((node.text or "").split()) if node is not None else ""
        if should_translate_text(text):
            texts.append(text)
    return texts


def writeback_ncx(ncx_path: Path) -> int:
    """Append the Chinese label after the English label in each EPUB2 NCX entry."""
    tree = ET.parse(ncx_path)
    root = tree.getroot()
    changed = 0
    for label in root.iter():
        if label.tag.rsplit("}", 1)[-1] != "navLabel":
            continue
        node = next((child for child in label if child.tag.rsplit("}", 1)[-1] == "text"), None)
        text = " ".join((node.text or "").split()) if node is not None else ""
        if not should_translate_text(text):
            continue
        zh = CACHE.get(text, "")
        if zh and zh not in text:
            node.text = f"{text} {zh}"
            changed += 1
    if changed:
        tree.write(ncx_path, encoding="utf-8", xml_declaration=True)
    return changed


def writeback_html_file(hf: Path, soup: BeautifulSoup, para_units: List[Tag],
                        text_overrides: Dict[int, str], tables: List[Tag]) -> int:
    """把缓存中的译文写回 DOM，并保存文件。返回插入译文数。"""
    inserted = 0
    for n in para_units:
        t = text_overrides.get(id(n)) or node_translation_text(n)
        zh = CACHE.get(t, "")
        if zh:
            # insert_translation_node 内部有幂等检查
            before = id(n.find_next_sibling())
            insert_translation_node(soup, n, zh)
            after = id(n.find_next_sibling())
            if before != after:
                inserted += 1
    for tb in tables:
        translate_table_node(soup, tb)
    hf.write_text(str(soup), encoding="utf-8")
    return inserted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="输入 EPUB 文件")
    ap.add_argument("output", help="输出目录中的任意 EPUB 路径；文件名会自动改为中文书名")
    ap.add_argument("--config", default=None,
                    help="配置文件路径（默认按优先级查找 config.json）")
    ap.add_argument("--concurrency", type=int, default=64,
                    help="并发线程数 (默认 64；急速档 96)")
    ap.add_argument("--batch-tokens", type=int, default=None,
                    help="单批翻译的预估 token 预算（输入侧，默认 12000；"
                         "128K 上下文模型下输出约与输入同量级，总量安全，可按需调大）")
    ap.add_argument("--work-dir", default=None, help="工作目录 (默认在临时目录)")
    ap.add_argument("--resume", action="store_true",
                    help="复用 cache.json 断点续传；幂等：源 EPUB 重新解包，已写过的译文也会被检测跳过")
    args = ap.parse_args()

    # 加载配置（API key / base_url / model_name 等）
    global CONFIG
    try:
        CONFIG = load_config(Path(args.config) if args.config else None)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERR] {e}", file=sys.stderr)
        return 2
    # Never expose even a partial API key in terminal logs.
    print(f"[CONFIG] model={CONFIG['model_name']} base_url={CONFIG['base_url']} key=***")

    t0 = time.time()
    in_path = Path(args.input).resolve()
    requested_output = Path(args.output).resolve()
    if not in_path.exists():
        print(f"[ERR] 输入文件不存在: {in_path}", file=sys.stderr)
        return 1

    work_dir = (
        Path(args.work_dir).resolve()
        if args.work_dir
        else Path.cwd() / f".epub-translate-{in_path.stem}"
    )
    if work_dir.exists() and not args.resume:
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    global CACHE_PATH
    CACHE_PATH = work_dir / "cache.json"
    load_cache(args.resume)

    extract_dir = work_dir / "extract"
    # 幂等关键：--resume 模式下也强制重新解包源 EPUB，避免在已含译文的 DOM 上重复插入
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    unpack_epub(in_path, extract_dir)
    print(f"[OK] 已解包到 {extract_dir}")
    book_title = read_epub_title(extract_dir)
    if book_title:
        print(f"[INFO] OPF 书名: {book_title}")

    html_files = sorted(
        list(extract_dir.rglob("*.xhtml")) + list(extract_dir.rglob("*.html"))
    )
    # EPUB3 目录链接及 EPUB2 NCX 标签都是读者可见导航文字。
    ncx_files = sorted(extract_dir.rglob("*.ncx"))
    print(f"[OK] 发现 {len(html_files)} 个 XHTML/HTML 文件和 {len(ncx_files)} 个 NCX 目录")

    # ---- 阶段 1：全局扫描所有章节，收集全部唯一翻译文本 ----
    print("[PHASE 1] 扫描所有章节，收集唯一文本 ...")
    scan_results: List[Tuple[Path, BeautifulSoup, List[Tag], Dict[int, str], List[Tag], List[str]]] = []
    global_unique: List[str] = []
    global_seen: set = set()
    seen_image_sources: set[str] = set()
    for hf in html_files:
        try:
            soup, paras, overrides, tables, uniques = scan_html_file(
                hf, include_toc_links=is_navigation_document(hf),
                seen_image_sources=seen_image_sources,
            )
        except Exception:
            print(f"[ERR] 扫描 {hf.name} 失败:\n{traceback.format_exc()}", file=sys.stderr)
            continue
        scan_results.append((hf, soup, paras, overrides, tables, uniques))
        for t in uniques:
            if t not in global_seen:
                global_seen.add(t)
                global_unique.append(t)
        print(f"  - {hf.name:<55s} paras={len(paras):4d} tables={len(tables):3d} unique={len(uniques):4d}")
    for ncx in ncx_files:
        try:
            for t in collect_ncx_texts(ncx):
                if t not in global_seen:
                    global_seen.add(t)
                    global_unique.append(t)
        except Exception:
            print(f"[ERR] 扫描 NCX {ncx.name} 失败:\n{traceback.format_exc()}", file=sys.stderr)
    # Metadata is not always repeated in chapter HTML, so translate the OPF
    # title explicitly before choosing the final output filename.
    if book_title and book_title not in global_seen:
        global_seen.add(book_title)
        global_unique.append(book_title)

    total_files = len(scan_results)
    total_unique = len(global_unique)
    todo = [t for t in global_unique if t not in CACHE]
    print(f"[PHASE 1 DONE] 章节={total_files} 全局唯一文本={total_unique} 已缓存={total_unique - len(todo)} 需翻译={len(todo)}")

    # ---- 阶段 2：一次性提交所有文本到全局线程池并行翻译 ----
    if todo:
        print(f"[PHASE 2] 全局批量并行翻译 (并发={args.concurrency}, "
              f"批预算={args.batch_tokens or CONFIG.get('batch_tokens', 12000)} tokens) ...")
        translate_batch(global_unique, concurrency=args.concurrency,
                        batch_tokens=args.batch_tokens)
    else:
        print("[PHASE 2] 全部命中缓存，无需调用 API")

    # ---- 阶段 3：把译文写回所有章节 DOM ----
    print("[PHASE 3] 写回译文并保存章节文件 ...")
    total_inserted = 0
    for hf, soup, paras, overrides, tables, _ in scan_results:
        try:
            inserted = writeback_html_file(hf, soup, paras, overrides, tables)
            total_inserted += inserted
        except Exception:
            print(f"[ERR] 写回 {hf.name} 失败:\n{traceback.format_exc()}", file=sys.stderr)
    for ncx in ncx_files:
        try:
            total_inserted += writeback_ncx(ncx)
        except Exception:
            print(f"[ERR] 写回 NCX {ncx.name} 失败:\n{traceback.format_exc()}", file=sys.stderr)
    print(f"[PHASE 3 DONE] 共写入译文节点 {total_inserted} 个")

    inject_css(extract_dir)
    print("[OK] 已注入 translated.css")

    translated_title = CACHE.get(book_title, "") if book_title else ""
    filename_stem = safe_epub_filename(translated_title, requested_output.stem)
    out_path = requested_output.parent / f"{filename_stem}.epub"
    print(f"[INFO] 输出文件名: {out_path.name}")
    pack_epub(extract_dir, out_path)
    dt = time.time() - t0
    print(f"[DONE] 输出 EPUB: {out_path}  耗时 {dt:.1f}s")
    print("[NEXT] 请调用 epub-reader-optimizer skill 对成品做样式美化。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
