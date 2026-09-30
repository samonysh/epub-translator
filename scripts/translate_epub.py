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
    - 注入 translated.css，重新打包并使用 EPUBCheck 检查成品

依赖：uv sync --locked（或 pip install -r requirements.txt）；EPUBCheck 还需要系统可用的 Java
"""
from __future__ import annotations

import argparse
import copy
import html
import importlib.util
import json
import os
import re
import shutil
import struct
import sys
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote
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
#   reasoning_mode: "disabled"（默认）或 "low"；翻译不需要长链推理。
#   reasoning_provider: auto（默认）或显式指定供应商，避免兼容网关误判。
#   extra_body   : dict, 传给 OpenAI SDK 的额外供应商参数；不得覆盖 reasoning_mode 的限制。
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
INLINE_CODE_PROMPT_RULE = (
    "text 中出现的 <code data-epub-inline-code=\"N\">...</code> 是正文中的行内代码："
    "必须原样保留该标签、data-epub-inline-code 属性和其中代码文本，并按中文语序放在合适位置；"
    "不要把它替换成占位符、Markdown 反引号或普通文本。"
)

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
    f"6. {INLINE_CODE_PROMPT_RULE}\n"
    "7. 保留其他 Markdown / HTML 标记（如 **bold**、<em>）不变。\n"
    '8. 译文中的换行写成 \\n 转义，确保输出始终是合法 JSON。'
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
    if os.environ.get("REASONING_MODE"):
        cfg["reasoning_mode"] = os.environ["REASONING_MODE"]
    if os.environ.get("REASONING_PROVIDER"):
        cfg["reasoning_provider"] = os.environ["REASONING_PROVIDER"]

    # 兜底默认值
    cfg.setdefault("model_name", "deepseek-v4-flash")
    cfg.setdefault("extra_body", {})
    cfg.setdefault("reasoning_mode", "disabled")
    cfg.setdefault("reasoning_provider", "auto")
    cfg.setdefault("batch_tokens", 12000)
    cfg.setdefault("system_prompt", DEFAULT_SYSTEM_PROMPT)
    # Existing user config.json files may contain the pre-inline-code prompt.
    # Preserve every custom instruction, but always add this protocol rule so
    # the new marker is not mistaken for disposable HTML by the model.
    if "data-epub-inline-code" not in str(cfg["system_prompt"]):
        cfg["system_prompt"] = f"{cfg['system_prompt']}\n{INLINE_CODE_PROMPT_RULE}"

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

    reasoning_mode = str(cfg["reasoning_mode"]).lower().strip()
    if reasoning_mode not in {"disabled", "low"}:
        raise ValueError("reasoning_mode 只能是 disabled 或 low。")
    cfg["reasoning_mode"] = reasoning_mode
    reasoning_provider = str(cfg["reasoning_provider"]).lower().strip()
    if reasoning_provider not in {"auto", "openai", "deepseek", "qwen", "zhipu", "ark", "generic"}:
        raise ValueError(
            "reasoning_provider 只能是 auto、openai、deepseek、qwen、zhipu、ark 或 generic。"
        )
    cfg["reasoning_provider"] = reasoning_provider
    if not isinstance(cfg["extra_body"], dict):
        raise ValueError("extra_body 必须是 JSON 对象。")

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
TRANSLATE_DIV_CLASSES = {"Para", "term", "subterm"}
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

# 行内代码节点：标签形式（<code>/<kbd>/...）与出版社的等宽 span 形式
# （Springer/Apress 用 <span class="EmphasisFontCategoryNonProportional"> 承载
# 行内代码、URL、提交前缀等）。两者都必须以 model-visible marker 的形式交给模型，
# 否则这些片段会在译文句子里留下空洞（例如"例如 和 。"）。
INLINE_CODE_TAGS = {"code", "kbd", "samp", "tt"}
INLINE_CODE_SPAN_TAGS = {"span", "em", "i", "b", "strong"}
INLINE_CODE_SPAN_CLASSES = {"EmphasisFontCategoryNonProportional"}

# 段落内需要保护的片段：Markdown 行内代码、公式、URL。
# EPUB 的 <code> 行内节点会在 node_translation_text() 中转换为模型可见的
# <code data-epub-inline-code="N">...</code>，因此不在这里掩码。
#
# 货币金额也写成 $...$（"$0.5 spent on Opus … $50"），朴素的 \$[^$]+\$ 会把整句
# 英文当成行内公式掩码，模型照抄 ⟦KEEP_n⟧，还原后中文段落里就嵌进大段英文。
# 因此 $...$ 只在"确实是公式"时掩码：开盘 $ 后面不能紧跟数字或空白（排除货币），
# 中间必须出现 LaTeX/运算符号，且闭合 $ 在 120 字符内。
PLACEHOLDER_RE = re.compile(
    r"(`[^`\n]+`"          # `inline code`
    r"|\$\$[^$]+\$\$"      # $$ block math $$
    r"|\$(?![0-9\s])(?=[^$\n]{0,120}\$)[^$\n]*[\\^_{}=+*/][^$\n]*\$"   # $ inline math $
    r"|\\\([^)]+\\\)"      # \( inline math \)
    r"|\\\[[^\]]+\\\]"     # \[ block math \]
    r"|https?://\S+"       # URLs
    r")"
)

INLINE_CODE_MARKER_RE = re.compile(
    r'<code data-epub-inline-code="(\d+)">.*?</code>', re.DOTALL
)

# 写回时需要同时拿到 marker 里的文本，用于判断模型是否"翻译了"这段行内内容。
INLINE_CODE_TEXT_RE = re.compile(
    r'<code data-epub-inline-code="(\d+)">(.*?)</code>', re.DOTALL
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
USAGE_LOCK = threading.Lock()
# 服务端 usage 是成本/配额的唯一可信来源；reasoning_tokens 并非每个兼容服务都会返回。
USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
         "reasoning_tokens": 0, "total_tokens": 0, "usage_reported": False,
         "reasoning_reported": False}


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


def _reasoning_provider() -> str:
    """Infer the documented reasoning-control dialect, with an explicit override."""
    configured = str(CONFIG.get("reasoning_provider", "auto"))
    if configured != "auto":
        return configured
    host = str(CONFIG.get("base_url", "")).lower()
    if "api.openai.com" in host:
        return "openai"
    if "deepseek.com" in host:
        return "deepseek"
    if "dashscope" in host or "qwen" in host:
        return "qwen"
    if "bigmodel.cn" in host:
        return "zhipu"
    if "volcengine" in host or "volces.com" in host:
        return "ark"
    return "generic"


def _openai_reasoning_effort(mode: str) -> Optional[str]:
    """Use the smallest documented OpenAI setting supported by likely model families."""
    model = str(CONFIG.get("model_name", "")).lower()
    # GPT-5.1 and later support none. Earlier reasoning models do not, so
    # retain the documented conservative lower setting rather than failing.
    if re.match(r"gpt-5\.(?:[1-9]|[1-9]\d)", model):
        return "none" if mode == "disabled" else "low"
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        return "low"
    # Non-reasoning models (for example GPT-4.1 / GPT-4o) need no control field.
    return None


def _reasoning_kwargs() -> Dict[str, object]:
    """Generate provider-specific low/disabled reasoning parameters for translation."""
    mode = str(CONFIG["reasoning_mode"])
    provider = _reasoning_provider()
    if provider == "openai":
        effort = _openai_reasoning_effort(mode)
        return {"reasoning_effort": effort} if effort else {}
    if provider == "qwen":
        body: Dict[str, object] = {"enable_thinking": mode != "disabled"}
        if mode == "low":
            body["reasoning_effort"] = "low"
        return {"extra_body": body}
    if provider in {"deepseek", "zhipu", "ark"}:
        if mode == "disabled":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {"extra_body": {"thinking": {"type": "enabled"}}}
    # Unknown OpenAI-compatible gateways do not share a reliable schema. Avoid
    # sending a speculative field that would make translation fail; users can
    # select a documented provider through reasoning_provider.
    return {}


def _record_usage(usage: object) -> None:
    """收集供应商返回的 usage；缺失字段按 0 计，不估算或伪造 token 数据。"""
    def read(obj: object, key: str) -> int:
        value = getattr(obj, key, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(key)
        return int(value or 0)

    details = getattr(usage, "completion_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
    reasoning = read(details, "reasoning_tokens") if details is not None else 0
    with USAGE_LOCK:
        USAGE["calls"] += 1
        USAGE["prompt_tokens"] += read(usage, "prompt_tokens")
        USAGE["completion_tokens"] += read(usage, "completion_tokens")
        USAGE["reasoning_tokens"] += reasoning
        USAGE["total_tokens"] += read(usage, "total_tokens")
        USAGE["usage_reported"] = bool(USAGE["usage_reported"] or usage is not None)
        USAGE["reasoning_reported"] = bool(USAGE["reasoning_reported"] or details is not None)


def print_usage_summary() -> None:
    """输出本次运行的实际 token 用量；不把模型无关的节省估算写成事实。"""
    with USAGE_LOCK:
        snapshot = dict(USAGE)
    if not snapshot["calls"]:
        return
    if not snapshot["usage_reported"]:
        print("[USAGE] 当前服务未返回 usage，无法报告实际 token 用量。")
        return
    print("[USAGE] 调用={calls} 输入={prompt_tokens} 输出={completion_tokens} "
          "总计={total_tokens} reasoning={reasoning_tokens}".format(**snapshot))
    if snapshot["reasoning_reported"]:
        print("[USAGE] reasoning_tokens 由服务端返回；关闭思考时应为 0。"
              "实际节省 = 对照运行的 total_tokens − 本次 total_tokens。")
    else:
        print("[USAGE] 当前服务未返回 reasoning_tokens；已强制关闭/低思考，但无法从 usage 精确拆分其节省量。")


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
    extra_body = dict(CONFIG.get("extra_body") or {})
    # 重建必需字段，避免用户配置借由 extra_body 覆盖思考限制。
    reasoning_kwargs = _reasoning_kwargs()
    if "extra_body" in reasoning_kwargs:
        extra_body.update(reasoning_kwargs["extra_body"])
    else:
        kwargs.update(reasoning_kwargs)
    if extra_body:
        kwargs["extra_body"] = extra_body
    resp = _client().chat.completions.create(**kwargs)
    _record_usage(getattr(resp, "usage", None))
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
    """Mask formulae/URLs without hiding model-visible EPUB inline code."""
    keeps: List[str] = []

    def _sub(m: re.Match) -> str:
        keeps.append(m.group(0))
        return f"⟦KEEP_{len(keeps) - 1}⟧"

    # Do not let the URL rule mask a URL which happens to be code (for example
    # <code>https://example.test</code>).  The whole marker remains visible to
    # the model so it can place it naturally in the Chinese sentence.
    chunks: List[str] = []
    cursor = 0
    for marker in INLINE_CODE_MARKER_RE.finditer(text):
        chunks.append(PLACEHOLDER_RE.sub(_sub, text[cursor:marker.start()]))
        chunks.append(marker.group(0))
        cursor = marker.end()
    chunks.append(PLACEHOLDER_RE.sub(_sub, text[cursor:]))
    return "".join(chunks), keeps


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

    # 兜底：行内代码 marker 必须与原文一一对应。模型偶尔把同一个 marker 用两次、
    # 或漏掉一个，写回时缺失的节点会被追加到句末，中文就会读成「使用 查看…」这种
    # 断句，甚至改变语义（CI/CD → bypassPermissions）。这类条目单条重试。
    for orig, zh in list(results.items()):
        want = sorted(INLINE_CODE_MARKER_RE.findall(orig))
        if not want or sorted(INLINE_CODE_MARKER_RE.findall(zh)) == want:
            continue
        for attempt in range(2):
            iid = batch_id(0)
            masked, keeps = protect(orig)
            single = translate_items_json(
                [{"id": iid, "text": masked}], max_retry=2,
                label=f"{label} marker-retry{attempt + 1}",
            )
            candidate = restore(single.get(iid, ""), keeps).strip()
            if candidate and sorted(INLINE_CODE_MARKER_RE.findall(candidate)) == want:
                results[orig] = candidate
                break
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


def is_skippable_node(node: Tag, stop: Optional[Tag] = None) -> bool:
    """节点本身或祖先含跳过标签 / 跳过 class 则跳过。

    ``stop`` bounds the ancestor walk: when *node* is the root of a translation
    unit, only skippable elements *inside* that unit matter.  Without it a
    caption's own prose paragraph was considered skippable merely because a
    caption container sits above it.
    """
    cur = node
    while cur is not None and isinstance(cur, Tag):
        # The unit root itself is never "skippable": a caption container carries a
        # skip class of its own, yet its text must still be translated.
        if cur is stop:
            break
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


def _is_inline_code_node(node: Tag, root: Tag) -> bool:
    """Return whether *node* is code embedded in a translatable text node.

    ``is_skippable_node`` deliberately treats every ``<code>`` element as
    non-translatable. That is correct for blocks, but it used to make a
    ``<code>`` descendant disappear from the Chinese companion paragraph.
    Publisher monospace spans (``EmphasisFontCategoryNonProportional``) behave
    exactly like inline ``<code>`` and are handled the same way.
    """
    if node is root:
        return False
    name = (node.name or "").lower()
    if name in INLINE_CODE_TAGS:
        pass
    elif name in INLINE_CODE_SPAN_TAGS and any(
        c in INLINE_CODE_SPAN_CLASSES for c in _classes_of(node)
    ):
        pass
    else:
        return False
    ancestor = node.parent
    while ancestor is not None and ancestor is not root:
        if isinstance(ancestor, Tag) and is_skippable_node(ancestor):
            return False
        ancestor = ancestor.parent
    return node.find_parent("pre") is None


def inline_code_nodes(node: Tag) -> List[Tag]:
    """Collect inline-code nodes in document order, excluding code blocks."""
    return [tag for tag in node.find_all(sorted(INLINE_CODE_TAGS | INLINE_CODE_SPAN_TAGS))
            if _is_inline_code_node(tag, node)]


def node_translation_text(node: Tag, *, bounded: bool = False) -> str:
    """Extract prose and expose inline code to the model as stable HTML markers.

    ``bounded=True`` treats *node* as the root of a translation unit: skippable
    ancestors above it are ignored (needed for caption prose, whose container
    carries a skip class of its own).
    """
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
                index = code_index[code_id]
                code_text = html.escape(code_parent.get_text("", strip=False), quote=False)
                parts.append(
                    f'<code data-epub-inline-code="{index}">{code_text}</code>'
                )
                emitted.add(code_id)
            continue
        if parent is not None and is_skippable_node(parent, stop=node if bounded else None):
            continue
        text = str(desc)
        if text.strip():
            parts.append(text)
    return " ".join(" ".join(parts).split())


def set_translated_contents(soup: BeautifulSoup, target: Tag, zh: str, source: Tag,
                            *, visible_codes_only: bool = False) -> None:
    """Populate a Chinese node and restore model-visible inline code markers.

    The marker intentionally carries no source CSS class.  We replace it with
    a deep copy of the original EPUB node, retaining publisher-specific code
    styling while allowing the model to choose its position in the translation.

    ``visible_codes_only`` is used for units whose translation input excluded
    block children (``node_text_excluding_block_children``): inline code living
    inside those children was never shown to the model, so it must not be
    appended as a "missing" token either.  Marker indices still refer to the
    full node list, which is what the model saw.
    """
    codes = inline_code_nodes(source)
    target.clear()
    cursor = 0
    restored: set[int] = set()
    for match in INLINE_CODE_TEXT_RE.finditer(zh):
        if match.start() > cursor:
            target.append(NavigableString(zh[cursor:match.start()]))
        index = int(match.group(1))
        if index < len(codes):
            original = codes[index]
            model_text = html.unescape(match.group(2) or "").strip()
            original_text = html.unescape(original.get_text("", strip=False)).strip()
            if model_text and model_text != original_text:
                # 出版社的等宽 span 有时承载的是散文（例如参考文献标题）。模型把它
                # 译成了中文，此时必须采用译文；照抄原节点会把英文再印一遍。
                target.append(NavigableString(model_text))
            else:
                target.append(copy.deepcopy(original))
            restored.add(index)
        else:
            target.append(NavigableString(match.group(0)))
        cursor = match.end()
    if cursor < len(zh):
        target.append(NavigableString(zh[cursor:]))
    # A compliant model keeps every token in place.  If a provider drops one,
    # preserve the code rather than silently losing it from the Chinese text.
    for index, code in enumerate(codes):
        if index in restored:
            continue
        if visible_codes_only and _code_inside_block_child(code, source):
            continue
        target.append(NavigableString(" "))
        target.append(copy.deepcopy(code))
    normalize_cjk_quotes(target)


def _code_inside_block_child(code: Tag, unit: Tag) -> bool:
    """True when *code* sits inside a block child of *unit* (so it was not part
    of the unit's own translation text)."""
    block_children_tags = TRANSLATE_TAGS | {"figure", "table", "ul", "ol"}
    ancestor = code.parent
    while ancestor is not None and ancestor is not unit and isinstance(ancestor, Tag):
        if is_skippable_node(ancestor) or ancestor.name in block_children_tags:
            return True
        ancestor = ancestor.parent
    return False


def normalize_cjk_quotes(node: Tag) -> int:
    """Use Chinese curly quotes inside a Chinese node.

    The model mixes straight ``"`` with “ ” (one real book: 300 vs 954).  Only
    text nodes with a *balanced* pair of straight quotes are converted, so a
    quote whose partner lives in another element is left untouched, and inline
    code (whose ``data-epub-inline-code`` attribute needs its straight quotes)
    is skipped entirely.
    """
    changed = 0
    def prose_text_nodes() -> List[NavigableString]:
        """重新获取仍在 DOM 中的文本节点，避免修改后继续操作旧引用。"""
        result = []
        for text in node.find_all(string=True):
            parent = text.parent
            if parent is not None and (
                parent.name in ("code", "pre")
                or parent.find_parent(["code", "pre"]) is not None
            ):
                continue
            result.append(text)
        return result

    texts = prose_text_nodes()
    joined = "".join(str(t) for t in texts)

    # 同一节点内统一引号体系：整段没有 “ ” 时，把「」也转成 “ ”，
    # 避免同一本书里两套引号混用（「授人以鱼…」 vs “噪声”）。
    if ("「" in joined or "」" in joined) and "“" not in joined and "”" not in joined:
        for text in texts:
            value = str(text)
            if "「" in value or "」" in value:
                text.replace_with(value.replace("「", "“").replace("」", "”"))
                changed += 1
        texts = prose_text_nodes()
        joined = "".join(str(t) for t in texts)

    # 直引号按"整个节点"配对，而不是按文本节点：一对引号可能被 <code> 分开，
    # 逐节点判断会漏掉（实测残留 22 个 ASCII 引号）。
    if joined.count('"') >= 2 and joined.count('"') % 2 == 0:
        opening = True
        for text in texts:
            value = str(text)
            if '"' not in value:
                continue
            out: List[str] = []
            for char in value:
                if char == '"':
                    out.append("“" if opening else "”")
                    opening = not opening
                else:
                    out.append(char)
            text.replace_with("".join(out))
            changed += 1
    changed += normalize_cjk_spacing(node)
    return changed


# 中文全角标点前不应有空格；提取时用空格拼接文本片段会留下 "排除 ：" 这类空隙。
CJK_BEFORE_PUNCT = "、。，；：？！）〕】》」』”’…—"
CJK_AFTER_PUNCT = "（〔【《「『“‘"


def normalize_cjk_spacing(node: Tag) -> int:
    """Remove spaces that extraction left before/after full-width punctuation."""
    changed = 0
    for text in node.find_all(string=True):
        parent = text.parent
        if parent is not None and (parent.name in ("code", "pre") or parent.find_parent(["code", "pre"]) is not None):
            continue
        value = str(text)
        fixed = re.sub(rf"[ \t]+(?=[{CJK_BEFORE_PUNCT}])", "", value)
        fixed = re.sub(rf"(?<=[{CJK_AFTER_PUNCT}])[ \t]+", "", fixed)
        if fixed != value:
            text.replace_with(fixed)
            changed += 1
    return changed


def _add_class(node: Tag, class_name: str) -> None:
    node["class"] = list(dict.fromkeys(_classes_of(node) + [class_name]))


ILLUSTRATION_CLASS = "epub-image"
GLYPH_CLASS = "epub-inline-image"
FIXED_SIZE_CLASS = "epub-fixed-image"
STYLED_SIZE_CLASS = "epub-styled-image"

# Publisher containers that hold a whole illustration rather than a sentence glyph.
FIGURE_CONTAINER_HINTS = ("figure", "cover", "mediaobject", "imageobject", "illustration")
# Publisher containers that hold a code listing; glyphs inside them must stay inline.
CODE_CONTAINER_CLASSES = {
    "ProgramCode", "programcode", "programcode1", "LineGroup", "FixedLine",
    "CodeBlock", "code-block", "code", "Code", "listing", "Listing",
    "programlisting", "screen", "literallayout",
}
# A resource below this size is a glyph/placeholder, whatever its alt text claims.
BIG_IMAGE_MIN_EDGE = 120
BIG_IMAGE_MIN_AREA = 20000
# Both width= and height= attributes at or above this size are a deliberate
# publisher layout choice (e.g. an 85x85 QR code) and must not be crushed.
AUTHORED_SIZE_MIN = 60
# Text runs: a large glyph inside a sentence (an inline icon) is not promoted to a
# block illustration without caption-like alt text.
TEXT_RUN_TAGS = {
    "p", "li", "td", "th", "dd", "dt", "figcaption", "caption", "span", "a",
    "em", "strong", "i", "b", "sup", "sub", "small",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
# Descriptive alt prose ("A chart compares ...") marks an illustration; LaTeX does not.
MATH_ALT_RE = re.compile(r"\\[A-Za-z]+|[\\{}]")
# Publisher placeholder labels are short like a maths symbol but are not one.
NON_MATH_ALT_LABELS = {
    "pic", "pics", "image", "images", "img", "figure", "fig", "figs", "graphic",
    "photo", "picture", "cover", "logo", "art", "icon", "unnamed", "untitled",
    "placeholder", "spacer", "blank",
}
IMAGE_SIZE_CACHE: Dict[str, Optional[Tuple[int, int]]] = {}


def _sniff_image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """Read intrinsic dimensions from PNG/GIF/JPEG/WEBP headers without decoding."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])  # type: ignore[return-value]
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])  # type: ignore[return-value]
    if data[:2] == b"\xff\xd8":
        index = 2
        while index < len(data) - 9:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return width, height
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            index += 2 + struct.unpack(">H", data[index + 2:index + 4])[0]
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and data[12:16] == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None


def image_pixel_size(src: str, html_dir: Optional[Path]) -> Optional[Tuple[int, int]]:
    """Resolve an <img src> against its HTML file and read the real pixel size."""
    if not src or html_dir is None:
        return None
    target = (html_dir / unquote(src.split("#", 1)[0])).resolve()
    key = str(target)
    if key in IMAGE_SIZE_CACHE:
        return IMAGE_SIZE_CACHE[key]
    size: Optional[Tuple[int, int]] = None
    try:
        with target.open("rb") as handle:
            size = _sniff_image_size(handle.read(4096))
    except OSError:
        size = None
    IMAGE_SIZE_CACHE[key] = size
    return size


def _is_big_image(size: Optional[Tuple[int, int]]) -> bool:
    """Unknown size is treated as large: structure and alt text still decide."""
    if size is None:
        return True
    width, height = size
    return max(width, height) >= BIG_IMAGE_MIN_EDGE or width * height >= BIG_IMAGE_MIN_AREA


def alt_describes_illustration(alt: str) -> bool:
    """True for a figure-caption-like alt, False for maths or short glyph labels."""
    value = " ".join((alt or "").split())
    if len(value) < 40 or len(value.split()) < 6:
        return False
    if MATH_ALT_RE.search(value):
        return False
    letters = sum(1 for char in value if char.isalpha())
    return letters / len(value) >= 0.55


def _ancestor_matches(image: Tag, predicate, levels: int = 4) -> bool:
    node = image.parent
    for _ in range(levels):
        if node is None or not isinstance(node, Tag):
            return False
        if predicate(node):
            return True
        node = node.parent
    return False


def _is_figure_container(node: Tag) -> bool:
    if (node.name or "").lower() == "figure":
        return True
    hay = (" ".join(_classes_of(node)) + " " + (node.get("id") or "")).lower()
    return any(hint in hay for hint in FIGURE_CONTAINER_HINTS)


def _is_code_container(node: Tag) -> bool:
    if (node.name or "").lower() in {"pre", "code"}:
        return True
    return any(cls in CODE_CONTAINER_CLASSES for cls in _classes_of(node))


def _length_attribute(value: object) -> Optional[int]:
    """Parse an HTML width/height attribute (``85``, ``85px``) into pixels."""
    text = str(value or "").strip().lower()
    if text.endswith("px"):
        text = text[:-2].strip()
    return int(text) if text.isdigit() else None


def has_authored_size(image: Tag) -> bool:
    """True when width= and height= both ask for a deliberate, non-tiny size."""
    width = _length_attribute(image.get("width"))
    height = _length_attribute(image.get("height"))
    if width is None or height is None:
        return False
    return width >= AUTHORED_SIZE_MIN and height >= AUTHORED_SIZE_MIN


INLINE_STYLE_SIZE_RE = re.compile(
    r"(?:^|;)\s*(?:width|height)\s*:\s*([0-9]*\.?[0-9]+)\s*(%|px|em|rem|pt)(?![0-9a-z%])",
    re.IGNORECASE,
)


def has_inline_style_size(image: Tag) -> bool:
    """识别出版社写在 style 中的尺寸，避免二维码等图被压成行内字高。"""
    for match in INLINE_STYLE_SIZE_RE.finditer(image.get("style") or ""):
        value, unit = float(match.group(1)), match.group(2).lower()
        if unit == "%" or unit in {"px", "pt"} and value >= AUTHORED_SIZE_MIN:
            return True
        if unit in {"em", "rem"} and value >= 2:
            return True
    return False


def classify_image(image: Tag, size: Optional[Tuple[int, int]] = None) -> str:
    """Decide how an <img> is displayed: block illustration, inline glyph or authored size.

    Structure wins over prose: a figure/cover container always means an
    illustration, because the reading baseline used to clamp everything inside
    such a container to 1.5em (~24px) -- the "thumbnail diagram" defect.  In a
    code listing or a text run the resource has to look like an illustration
    (large, plus caption-like alt inside a paragraph) so that 22px placeholders
    and code glyphs stay inline.  Failing everything else, an explicit
    width=/height= pair keeps the size its publisher authored instead of the
    sentence-height cap.
    """
    if (image.get("role") or "").strip().lower() == "doc-cover":
        return ILLUSTRATION_CLASS
    big = _is_big_image(size)
    caption_like = big and alt_describes_illustration(image.get("alt", "") or "")
    if _ancestor_matches(image, _is_code_container, levels=5):
        return ILLUSTRATION_CLASS if caption_like else GLYPH_CLASS
    if _ancestor_matches(image, _is_figure_container):
        return ILLUSTRATION_CLASS
    if has_inline_style_size(image):
        return STYLED_SIZE_CLASS
    if caption_like:
        return ILLUSTRATION_CLASS
    if big:
        parent = image.parent
        if not isinstance(parent, Tag) or (parent.name or "").lower() not in TEXT_RUN_TAGS:
            # A standalone wrapper (div/section/body) around a large image is an
            # illustration even when the alt text is empty or decorative.
            return ILLUSTRATION_CLASS
    if has_authored_size(image):
        return FIXED_SIZE_CLASS
    return GLYPH_CLASS


def is_illustration_caption_image(image: Tag) -> bool:
    """图注散文即使含数学符号，也不应被当作行内公式。"""
    alt = (image.get("alt") or "").strip()
    return bool(alt and alt_describes_illustration(alt) and (
        (image.get("role") or "").strip().lower() == "doc-cover"
        or _ancestor_matches(image, _is_figure_container)
    ))


def formula_from_content(image: Tag) -> bool:
    """Formula evidence from the markup itself, ignoring any `formula-img` class."""
    if image.find_parent(["math", "svg"]) is not None:
        return True
    if {"math-img", "math-formula"} & set(_classes_of(image)):
        return True
    if is_illustration_caption_image(image):
        return False
    return has_math_formula((image.get("alt", "") or "").strip())


def _is_formula_image(image: Tag) -> bool:
    """Keep formula images out of illustration sizing/deduplication rules."""
    if is_illustration_caption_image(image):
        return False
    if "formula-img" in _classes_of(image):
        return True
    return formula_from_content(image)


def has_math_formula(alt: str) -> bool:
    """Identify both LaTeX expressions and short inline math symbols."""
    value = alt.strip()
    if not value or value == "{%}":
        return False
    # "PIC" / "image" / "logo" are publisher placeholders, not maths symbols.
    if value.strip(".,;:()[]{}").lower() in NON_MATH_ALT_LABELS:
        return False
    # TeX commands and explicit brace/argument markup are unambiguous. Bare
    # "_" and "^" alone are not matched, because prose alt text frequently
    # contains snake_case identifiers such as fact_sales or get_user_name.
    if re.search(r"\\[A-Za-z]+|[\\{}]|[_^][{\s(]", value):
        return True
    if any(char in value for char in "=+−×÷≤≥≠≈∞√∑∫∈⊂⊆∩∪σμθβαλγδπωε∂"):
        return True
    # A short x_i / v_1 style token is still treated as a math symbol.
    if re.fullmatch(r"[A-Za-zα-ωΑ-Ω0-9][_^][A-Za-z0-9]", value):
        return True
    return len(value) <= 4 and bool(
        re.fullmatch(r"[A-Za-zα-ωΑ-Ω0-9\s,.;]+", value)
    )


def normalise_formula_images(soup: BeautifulSoup) -> None:
    """Give image formulae safe inline/block semantics without changing text."""
    for image in list(soup.find_all("img")):
        if is_illustration_caption_image(image):
            continue
        if not has_math_formula((image.get("alt", "") or "").strip()):
            continue
        _add_class(image, "formula-img")
        parent = image.parent
        if isinstance(parent, Tag) and parent.name == "span" and "math-inline" in _classes_of(parent):
            continue
        wrapper = soup.new_tag("span")
        wrapper["class"] = ["math-inline"]
        if image.get("alt"):
            wrapper["data-latex"] = image["alt"]
        image.wrap(wrapper)

    # Only formula-only paragraphs become display equations.
    for paragraph in soup.find_all("p"):
        meaningful = [child for child in paragraph.contents
                      if not (isinstance(child, NavigableString) and not child.strip())]
        if len(meaningful) == 1 and isinstance(meaningful[0], Tag):
            only = meaningful[0]
            if only.name == "img" and "formula-img" in _classes_of(only):
                _add_class(paragraph, "formula-block")
            elif only.name == "span" and "math-inline" in _classes_of(only) and only.find("img", class_="formula-img"):
                _add_class(paragraph, "formula-block")


def normalise_images_for_reading(soup: BeautifulSoup, seen_sources: set[str],
                                 html_dir: Optional[Path] = None) -> int:
    """Show each ordinary image resource once and assign display classes.

    EPUB conversion pipelines commonly duplicate an illustration in consecutive
    containers or in a translated table clone.  A resource seen earlier in the
    book is removed; formula images are deliberately excluded.

    Classification is structural (see ``classify_image``): judging "inline" by
    whether the parent happens to contain text clamped every captioned
    ``<div class="figure">`` illustration to 1.5em (~24px) -- the thumbnail
    diagram defect -- so structure, alt prose and the real pixel size decide.
    """
    normalise_formula_images(soup)
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
        display_class = classify_image(image, image_pixel_size(src, html_dir))
        _add_class(image, display_class)
        if display_class == ILLUSTRATION_CLASS and isinstance(parent, Tag) \
                and parent.name in {"p", "figure", "div"}:
            _add_class(parent, "epub-image-container")
    return removed


def normalise_code_blocks(soup: BeautifulSoup) -> None:
    """清除代码行之间由排版标记产生的空白，保留每行的缩进与文本。"""
    line_classes = {"LineGroup", "linegroup", "FixedLine", "fixedline", "Line", "line"}
    for block in soup.find_all(True):
        if not any(cls in CODE_CONTAINER_CLASSES for cls in _classes_of(block)):
            continue
        direct_tags = [child for child in block.contents if isinstance(child, Tag)]
        if not direct_tags or not all(
            child.name == "p" or bool(line_classes & set(_classes_of(child)))
            for child in direct_tags
        ):
            continue
        # white-space: pre-wrap 会把 <div class="FixedLine"> 间用于美化 XML 的
        # 换行也显示出来；这些换行不是代码内容。
        for child in list(block.contents):
            if isinstance(child, NavigableString) and not child.strip():
                child.extract()
    for pre in soup.find_all("pre"):
        for child in list(pre.descendants):
            if not isinstance(child, NavigableString):
                continue
            original = str(child)
            compact = re.sub(r"\n[ \t]*\n+", "\n", original)
            if compact != original:
                child.replace_with(compact)


def node_text_excluding_block_children(node: Tag) -> str:
    """提取节点文本，但排除内部已经是"独立段落级"的子节点（避免重复翻译）。

    用于 div.Para 这种容器：如果内部嵌套 <p>/<figure>/<figcaption>/<h*>，
    那些将由各自的逻辑单独翻译；外层 div 只翻译散落在外的散文文本。

    行内代码（<code> 与出版社等宽 span）与 node_translation_text() 一致，
    以 <code data-epub-inline-code="N"> marker 的形式保留，避免译文出现空洞。
    """
    block_children_tags = TRANSLATE_TAGS | {"figure", "table", "ul", "ol"}
    code_index = {id(tag): i for i, tag in enumerate(inline_code_nodes(node))}
    parts: List[str] = []
    emitted: set[int] = set()
    for desc in node.descendants:
        if not isinstance(desc, NavigableString):
            continue
        s = str(desc)
        if not s.strip():
            continue
        parent = desc.parent
        code_parent = next(
            (ancestor for ancestor in ([parent] + list(parent.parents))
             if isinstance(ancestor, Tag) and id(ancestor) in code_index),
            None,
        )
        if code_parent is not None:
            # Inline code: the block-child test must start at the code node, not at
            # the text node.  An inner <st>/<span> wrapper would otherwise be seen
            # as skippable (its own ancestor is a <code>, which is in SKIP_TAGS)
            # and the code silently disappeared from the translation input.
            anc = code_parent.parent
            in_block_child = False
            while anc is not None and anc is not node and isinstance(anc, Tag):
                if is_skippable_node(anc) or anc.name in block_children_tags:
                    in_block_child = True
                    break
                anc = anc.parent
            if in_block_child:
                continue
            code_id = id(code_parent)
            if code_id not in emitted:
                index = code_index[code_id]
                code_text = html.escape(code_parent.get_text("", strip=False), quote=False)
                parts.append(f'<code data-epub-inline-code="{index}">{code_text}</code>')
                emitted.add(code_id)
            continue
        # 非行内代码文本：判断是否位于"块级子节点"内部（块级子段落由各自逻辑翻译）
        anc = parent
        in_block_child = False
        while anc is not None and anc is not node and isinstance(anc, Tag):
            if is_skippable_node(anc) or anc.name in block_children_tags:
                in_block_child = True
                break
            anc = anc.parent
        if in_block_child:
            continue
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
        # A contents page's own heading ("Table of Contents") is reader-facing text
        # too; the link loop below only covers the entries themselves.
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            if tag.find_parent(list(SKIP_TAGS)) is not None or _already_translated(tag):
                continue
            text = node_translation_text(tag)
            if should_translate_text(text) and id(tag) not in seen_ids:
                seen_ids.add(id(tag))
                units.append(tag)
        for tag in soup.find_all("a", href=True):
            if tag.find_parent(list(SKIP_TAGS)) is not None or _already_translated(tag):
                continue
            text = " ".join(tag.get_text(" ", strip=True).split())
            if should_translate_text(text) and id(tag) not in seen_ids:
                seen_ids.add(id(tag))
                units.append(tag)
                # A printed TOC label often sits inside a styling span that the
                # skip list covers (<span class="TocItem">), which
                # node_translation_text() would drop.  Record the label here so
                # scan and writeback agree on the same cache key.
                text_overrides[id(tag)] = text
        return units, text_overrides

    # 1. 标准段落级标签
    for tag in soup.find_all(TRANSLATE_TAGS):
        if is_skippable_node(tag):
            continue
        if _already_translated(tag):
            continue
        if tag.name == "li" and tag.find(["p", "blockquote", "ul", "ol", "table", "figure"]):
            # The nested paragraphs/lists are translated by their own logic; translate
            # only the prose the <li> carries itself (its lead-in sentence).  A
            # wholesale skip used to lose that sentence entirely, while translating
            # the whole subtree repeated every nested entry in the parent's Chinese
            # node (Book B's index printed each sub-entry twice).
            residual = node_text_excluding_block_children(tag)
            if should_translate_text(residual) and id(tag) not in seen_ids:
                seen_ids.add(id(tag))
                text_overrides[id(tag)] = residual
                units.append(tag)
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

    # 3. 图注 / 表注 / 清单注释
    # 出版社把这些说明放在带 Caption / CaptionContent 类的容器里（Springer、Apress
    # 的 Figure/Table/Listing 注即如此）。这类容器整体在跳过表里，于是注文从未被翻译：
    # 一本 Apress 书的 36 条图/表/清单注全部保持英文。这里把 CaptionContent 作为
    # 一个翻译单元，`bounded=True` 让它忽略容器自身的跳过类。
    for content in soup.find_all(True):
        if "CaptionContent" not in _classes_of(content):
            continue
        if id(content) in seen_ids or _already_translated(content):
            continue
        text = node_translation_text(content, bounded=True)
        if not should_translate_text(text):
            continue
        seen_ids.add(id(content))
        units.append(content)
        # scan_html_file()/writeback_html_file() re-extract with the unbounded
        # variant, which would return "" for a caption container; hand them the
        # bounded text so both sides agree on the cache key.
        text_overrides[id(content)] = text

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
def plain_text(markup: str) -> str:
    """Rendered text of a cache value that may embed inline-code markers."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", markup)).split())


def is_noop_translation(zh: str, source_text: str) -> bool:
    """True when the "translation" is just the English source echoed back.

    Compared on rendered text, because the model sees inline code as
    ``<code data-epub-inline-code="N">…</code>`` and often echoes the marker
    unchanged: a unit that is nothing but a code span (a URL, a path) would
    otherwise be printed twice.  Any Chinese character disqualifies it.
    """
    if not zh or re.search(r"[\u4e00-\u9fff]", zh):
        return False
    # 比较时忽略空白：出版社把 ", " 放在独立 <st> 里，get_text(" ") 会插出一个空格
    # （"PreToolUse 143 , 144"），与模型的 "PreToolUse 143, 144" 永远不相等。
    squash = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    return squash(plain_text(zh)) == squash(source_text)


def insert_translation_node(soup: BeautifulSoup, node: Tag, zh: str,
                            *, visible_codes_only: bool = False) -> None:
    if not zh:
        return
    source_text = " ".join(node.get_text(" ", strip=True).split())
    # 标题和目录采用“英文 · 中文”同一行，目录仍保持同一个可点击链接。
    if node.name in {"h1", "h2", "h3", "h4", "h5", "h6", "a"}:
        if _has_inline_translation(node):
            return
        # 模型对纯标识符（SessionStart、plugin.json 等）常原样返回；此时
        # “英文 · 中文”会退化成 “X · X”。改为记录一个隐藏的中文节点：
        # 读者看不到重复文本，幂等检查仍能识别该节点已处理。
        if is_noop_translation(zh, source_text):
            hidden = soup.new_tag("span")
            hidden["class"] = ["translated-zh", "translated-inline", "translated-noop"]
            hidden["lang"] = "zh-CN"
            hidden["aria-hidden"] = "true"
            hidden.string = plain_text(zh)
            node.append(hidden)
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
    # 模型对路径、URL、专有名词等原样返回时，正文节点会渲染成英文重复两遍；
    # 与标题处理一致，改放一个隐藏的中文节点（不可见但可被幂等检查识别）。
    if is_noop_translation(zh, source_text):
        hidden = soup.new_tag(node.name)
        hidden["class"] = list(dict.fromkeys(
            _classes_of(node) + ["translated-zh", "translated-noop"]))
        hidden["lang"] = "zh-CN"
        hidden["aria-hidden"] = "true"
        hidden.string = plain_text(zh)
        node.insert_after(hidden)
        return
    new_tag = soup.new_tag(node.name)
    # 去重 class（避免 translated-zh translated-zh）
    classes = list(dict.fromkeys(_classes_of(node) + ["translated-zh"]))
    new_tag["class"] = classes
    new_tag["lang"] = "zh-CN"
    set_translated_contents(soup, new_tag, zh, node, visible_codes_only=visible_codes_only)
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
    # 复制表格不能复制 id，否则中英两表出现重复锚点，破坏 XHTML 定位。
    for element in [cloned] + cloned.find_all(True):
        element.attrs.pop("id", None)
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


def validate_epub(epub_path: Path) -> tuple[bool, Path, int, int, int]:
    """检查最终 EPUB 并保存完整报告；工具不可用时向上报告故障。"""
    from epubcheck import EpubCheck

    report_path = epub_path.with_suffix(".epubcheck.json")
    # 避免检查器启动失败后留下上一次运行的报告，造成误判。
    report_path.unlink(missing_ok=True)
    result = EpubCheck(str(epub_path))
    report_path.write_text(
        json.dumps(result.result_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    checker = result.checker
    return (
        result.valid is True,
        report_path,
        checker.nFatal,
        checker.nError,
        checker.nWarning,
    )


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
def _load_translated_css() -> str:
    """Load the project-owned stylesheet that is embedded in each output EPUB."""
    css_source = Path(__file__).resolve().parent.parent / "assets" / "translated.css"
    try:
        return css_source.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"无法读取内建阅读样式：{css_source}") from exc


def inject_css(work_dir: Path) -> None:
    """把 translated.css 放到与 content.opf 同级的 styles 目录或 OEBPS 根，并写入引用。"""
    # 找到所有 xhtml/html 文件
    html_files = list(work_dir.rglob("*.xhtml")) + list(work_dir.rglob("*.html"))
    if not html_files:
        return

    # 把 css 文件放到第一个 html 同级目录
    base = html_files[0].parent
    css_path = base / "translated.css"
    css_path.write_text(_load_translated_css(), encoding="utf-8")

    # 只插入引用，不重新序列化整份 XHTML。XML 解析器可能丢掉 <pre> 中
    # 仅包含空白的文本节点，进而改变原始代码的换行与缩进。
    for hf in html_files:
        raw = hf.read_text(encoding="utf-8")
        if re.search(r'<(?:\w+:)?link\b[^>]*\bhref\s*=\s*["\'][^"\']*translated\.css["\']',
                     raw, re.IGNORECASE):
            continue
        rel = os.path.relpath(css_path, hf.parent).replace("\\", "/")
        close = re.search(r"</(?:\w+:)?head\s*>", raw, re.IGNORECASE)
        if close is None:
            continue
        link = f'<link href="{html.escape(rel, quote=True)}" rel="stylesheet" type="text/css"/>'
        hf.write_text(raw[:close.start()] + link + raw[close.start():], encoding="utf-8")

    # OPF 也只修改 manifest 的闭合处，保留出版社其余元数据与格式。
    opfs = list(work_dir.rglob("*.opf"))
    for opf in opfs:
        raw = opf.read_text(encoding="utf-8")
        if re.search(r'<(?:\w+:)?item\b[^>]*\bhref\s*=\s*["\'][^"\']*translated\.css["\']',
                     raw, re.IGNORECASE):
            continue
        close = re.search(r"</(?:\w+:)?manifest\s*>", raw, re.IGNORECASE)
        if close is None:
            continue
        rel = os.path.relpath(css_path, opf.parent).replace("\\", "/")
        item = f'<item id="translated-css" href="{html.escape(rel, quote=True)}" media-type="text/css" />'
        opf.write_text(raw[:close.start()] + item + raw[close.start():], encoding="utf-8")


# ============ 主流程 ============
def scan_html_file(hf: Path, *, include_toc_links: bool = False,
                   seen_image_sources: Optional[set[str]] = None) -> Tuple[BeautifulSoup, List[Tag], Dict[int, str], List[Tag], List[str]]:
    """扫描单个 html 文件，返回 (soup, para_units, text_overrides, tables, unique_texts)。
    不调用 API，只解析 DOM 与抽取文本，可以多线程并行执行。
    """
    raw = hf.read_text(encoding="utf-8")
    soup = BeautifulSoup(raw, "html.parser")
    normalise_code_blocks(soup)
    normalise_images_for_reading(
        soup,
        seen_image_sources if seen_image_sources is not None else set(),
        html_dir=hf.parent,
    )
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
    if "<nav" in sample and ("toc" in sample or "table of contents" in sample):
        return True
    # A printed table of contents in the book body is reader-facing navigation as
    # well: Springer/Apress emit <section class="Toc"> / <div class="TocBody"> /
    # <ol class="TocChapter"> with <span class="TocItem"> links.  Without this the
    # contents page stayed English while the EPUB nav was bilingual.
    return 'class="tocbody"' in sample or 'class="tocchapter"' in sample


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
        # `zh not in text` would swallow a legitimate label that happens to be a
        # substring of the English one (README inside READMEs), so compare the
        # already-appended form instead.
        if zh and zh.strip() != text.strip() and not text.endswith(f"· {zh}"):
            # 与 EPUB3 目录一致：英文 · 中文
            node.text = f"{text} · {zh}"
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
            insert_translation_node(soup, n, zh, visible_codes_only=id(n) in text_overrides)
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
    print(f"[CONFIG] model={CONFIG['model_name']} base_url={CONFIG['base_url']} "
          f"reasoning={CONFIG['reasoning_mode']} key=***")

    t0 = time.time()
    in_path = Path(args.input).resolve()
    requested_output = Path(args.output).resolve()
    if not in_path.exists():
        print(f"[ERR] 输入文件不存在: {in_path}", file=sys.stderr)
        return 1
    if importlib.util.find_spec("epubcheck") is None or shutil.which("java") is None:
        print("[ERR] 缺少产物检查依赖：请安装 requirements.txt 并确保 java 命令可用。", file=sys.stderr)
        return 2

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
    try:
        valid, report_path, fatal_count, error_count, warning_count = validate_epub(out_path)
    except (ImportError, OSError, ValueError, KeyError) as exc:
        print(f"[ERR] EPUBCheck 无法完成检查：{exc}", file=sys.stderr)
        print(f"[INFO] 已生成但未经规范检查的 EPUB：{out_path}", file=sys.stderr)
        return 2
    print(f"[EPUBCHECK] fatal={fatal_count} error={error_count} warning={warning_count} 报告={report_path}")
    if not valid:
        print(f"[ERR] EPUBCheck 未通过；请查看报告并修复：{report_path}", file=sys.stderr)
        return 1
    dt = time.time() - t0
    print(f"[DONE] 输出 EPUB: {out_path}  耗时 {dt:.1f}s")
    print_usage_summary()
    print("[NEXT] 成品已包含内建阅读样式；仅在特定阅读器异常时再做定向处理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
