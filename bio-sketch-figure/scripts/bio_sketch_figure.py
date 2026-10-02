#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bio-sketch-figure 引擎

把 BioSketch 的出图能力做成自包含 CLI：
主题(+参考图) -> 内容大纲 -> 逐页配图。
全部由本脚本完成，不依赖 agent 的任何内置视觉/生图能力。

子命令：init / check / styles / outline / render / all
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
import yaml
from PIL import Image

SKILL_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = SKILL_ROOT / "prompts"
STYLES_DIR = SKILL_ROOT / "styles"
DEFAULT_CONFIG_PATH = Path.home() / ".bio-sketch-figure" / "config.yaml"

TEXT_KEY_ENV = "BIO_SKETCH_TEXT_KEY"
IMAGE_KEY_ENV = "BIO_SKETCH_IMAGE_KEY"
BASE_URL_ENV = "BIO_SKETCH_BASE_URL"
GATEWAY_BASE_URL = "http://123.56.95.34"
NO_STYLE_TEXT = "无特定风格要求"
NONE_STYLE_ID = "none"
MAX_DIMENSION = 2048

# 文本模型只在大纲阶段使用；一次大纲最多发一个请求，禁止自动重试，避免失败重试造成重复计费
TEXT_MAX_RETRIES = 1

# 出图提交（流式）在「尚未拿到任务号」时最多重试次数。
# 一旦拿到 task_id 就绝不再重发提交，避免创建第二个任务造成重复计费。
SUBMIT_MAX_RETRIES = 3
DOWNLOAD_MAX_RETRIES = 3

# 对外展示名（不暴露底层服务与模型）
SERVICE_NAME = "BioSketch"
IMAGE_SERVICE_LABEL = "biosketch"
THANKS_MESSAGE = "感谢 BioSketch 提供服务"

TYPE_MAPPING = {"封面": "cover", "内容": "content", "总结": "summary"}
ORDER_CN = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]

ROLE_STYLE_REF = (
    "视觉风格参考 — 唯一视觉风格基准，必须严格复刻其配色、笔触、角色造型、"
    "版式与背景；与文字说明冲突时以此图为准"
)
ROLE_STYLE_REF_SECONDARY = (
    "视觉风格参考（次要）— 与用户参考图融合，但以用户参考图的风格为优先"
)
ROLE_USER_REF = (
    "用户提供的参考图 — 风格与构图的优先基准，优先复刻其配色、笔触与整体风格"
)
ELEMENT_ROLE_TEMPLATE = "元素素材「{name}」— 请直接复用其造型、结构与配色"


def is_style_role(role: str) -> bool:
    """判断某条参考图角色是否属于「风格图」（决定其体积上限用风格级配置）。"""
    return role in (ROLE_STYLE_REF, ROLE_STYLE_REF_SECONDARY)


# 元素素材库（沿用 skill 根目录下的 sucai/）
ELEMENTS_DIR = SKILL_ROOT / "sucai"
ELEMENTS_INDEX_NAME = "index.yaml"
ELEMENT_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
NO_PALETTE_TEXT = "沿用参考图与风格说明中的配色，并确保各页完全一致"


# ==================== 纯函数 ====================


def parse_outline(outline_text: str) -> List[Dict[str, Any]]:
    """把大纲文本切成页。
    - 优先按 <page> 标签切分；无标签时退回按 --- 切分
    - 页码 0-based 连续编号；剥离块尾的 </page>
    - [封面]/[内容]/[总结] 映射为 cover/content/summary，未识别的类型归为 content"""
    if "<page>" in outline_text.lower():
        parts = re.split(r"<page>", outline_text, flags=re.IGNORECASE)
    else:
        parts = outline_text.split("---")

    pages: List[Dict[str, Any]] = []
    index = 0
    for raw in parts:
        text = raw.strip()
        if not text:
            continue
        text = re.sub(r"</page>\s*$", "", text, flags=re.IGNORECASE).strip()
        if not text:
            continue
        page_type = "content"
        match = re.match(r"\[(\S+)\]", text)
        if match:
            page_type = TYPE_MAPPING.get(match.group(1), "content")
        pages.append({"index": index, "type": page_type, "content": text})
        index += 1
    return pages


def resolve_reference_image(
    rel: Optional[str], skill_root: Optional[Path] = None
) -> Optional[Path]:
    """风格参考图路径（yaml 里相对技能根目录）-> 绝对路径。
    skill_root 传 None 时在调用期读取模块级 SKILL_ROOT（便于测试替换）。"""
    if not rel:
        return None
    root = Path(skill_root) if skill_root is not None else SKILL_ROOT
    path = Path(rel)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def list_styles(
    styles_dir: Optional[Path] = None, skill_root: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """扫描 styles/*.yaml，末尾追加 none 选项"""
    styles_dir = Path(styles_dir) if styles_dir is not None else STYLES_DIR
    skill_root = Path(skill_root) if skill_root is not None else SKILL_ROOT
    styles: List[Dict[str, Any]] = []
    for path in sorted(styles_dir.glob("*.yaml")):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict) or not data.get("id"):
            continue
        styles.append(
            {
                "id": data["id"],
                "name": data.get("name", data["id"]),
                "description": (data.get("description") or "").strip(),
                "tier": data.get("tier", "free"),
                "aspect_ratio": data.get("aspect_ratio"),
                "reference_image": resolve_reference_image(
                    data.get("reference_image"), skill_root
                ),
            }
        )
    styles.append(
        {
            "id": NONE_STYLE_ID,
            "name": "无风格参考",
            "description": "不使用任何视觉风格约束",
            "tier": "free",
            "aspect_ratio": None,
            "reference_image": None,
        }
    )
    return styles


def load_style(
    style_id: Optional[str], styles_dir: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """读取风格 yaml；none / 空 -> None；文件缺失 -> FileNotFoundError"""
    if not style_id or style_id == NONE_STYLE_ID:
        return None
    styles_dir = Path(styles_dir) if styles_dir is not None else STYLES_DIR
    path = styles_dir / f"{style_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"风格文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data["_path"] = str(path)
    return data


def compress_image(
    image_data: bytes, max_size_kb: int = 200, max_dimension: int = MAX_DIMENSION
) -> bytes:
    """压缩图片到指定大小以内（先缩放，再逐步降质）。"""
    max_size_bytes = max_size_kb * 1024
    if len(image_data) <= max_size_bytes:
        return image_data
    try:
        img = Image.open(io.BytesIO(image_data))
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(
                img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None
            )
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        width, height = img.size
        if width > max_dimension or height > max_dimension:
            ratio = min(max_dimension / width, max_dimension / height)
            img = img.resize(
                (int(width * ratio), int(height * ratio)), Image.Resampling.LANCZOS
            )

        quality = 85
        compressed: Optional[bytes] = None
        while quality >= 20:
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            compressed = out.getvalue()
            if len(compressed) <= max_size_bytes:
                break
            quality -= 5

        width, height = img.size
        while (
            compressed is not None
            and len(compressed) > max_size_bytes
            and max(width, height) > 512
        ):
            width, height = int(width * 0.9), int(height * 0.9)
            resized = img.resize((width, height), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            resized.save(out, format="JPEG", quality=20, optimize=True)
            compressed = out.getvalue()

        if compressed is None:
            return image_data
        print(
            f"[压缩] {len(image_data) / 1024:.1f}KB -> {len(compressed) / 1024:.1f}KB"
        )
        return compressed
    except Exception as exc:  # 压缩失败不阻断主流程，与原实现一致
        print(f"[压缩] 失败，改用原图: {exc}")
        return image_data


def match_elements(
    page_text: str,
    elements: Sequence[Dict[str, Any]],
    limit: int = 2,
) -> List[Dict[str, Any]]:
    """在素材库里按关键词命中数挑出与当前页相关的素材。
    只有在页面文本里真正命中关键词（命中数 > 0）的素材才会返回；
    按命中数降序、同分保持索引顺序，最多取 limit 个。"""
    if not page_text or not elements or limit <= 0:
        return []
    scored: List[Tuple[int, int, Dict[str, Any]]] = []
    for order, entry in enumerate(elements):
        if not isinstance(entry, dict):
            continue
        keywords = entry.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        score = sum(1 for kw in keywords if kw and str(kw) in page_text)
        if score > 0:
            scored.append((score, order, entry))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [entry for _score, _order, entry in scored[:limit]]


def load_elements(
    index_path: Optional[Path] = None, elements_dir: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """读取 sucai/index.yaml；不存在或为空时返回空列表。
    每一项补上 _path（素材文件绝对路径），供后续读取字节。"""
    base = Path(elements_dir) if elements_dir is not None else ELEMENTS_DIR
    path = Path(index_path) if index_path is not None else base / ELEMENTS_INDEX_NAME
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    if isinstance(data, dict):
        data = data.get("elements") or []
    if not isinstance(data, list):
        return []
    elements: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("file"):
            continue
        entry = dict(item)
        entry["_path"] = str((base / entry["file"]).resolve())
        elements.append(entry)
    return elements


def element_status(
    elements: Sequence[Dict[str, Any]], elements_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """素材库体检：已索引数量、索引了但文件缺失、在磁盘上但未入索引。"""
    base = Path(elements_dir) if elements_dir is not None else ELEMENTS_DIR
    indexed: set = set()
    missing: List[str] = []
    for entry in elements:
        filename = entry.get("file")
        if not filename:
            continue
        indexed.add(filename)
        if not (base / filename).exists():
            missing.append(filename)
    on_disk = {
        path.name
        for path in base.glob("*")
        if path.is_file() and path.suffix.lower() in ELEMENT_IMAGE_SUFFIXES
    }
    return {
        "count": len(elements),
        "dir": str(base),
        "missing": sorted(missing),
        "unindexed": sorted(on_disk - indexed),
    }


def build_reference_hint(count: int) -> str:
    if count <= 0:
        return ""
    return (
        f"随本消息提供 {count} 张参考图片，请参考它们的整体风格、配色与构图，"
        "使生成的配图在视觉上保持一致；标注为「元素素材」的图片请直接复用其造型、结构与配色。"
    )


def plan_page_references(
    page_text: str,
    user_refs: Sequence[bytes],
    style_ref: Optional[bytes],
    elements: Sequence[Dict[str, Any]],
    max_ref_images: int = 5,
    max_elements_per_page: int = 2,
) -> Tuple[List[bytes], List[str], str]:
    """为单页装配参考图，顺序固定为：用户图 → 风格图 → 命中素材，依次填到上限。

    带参考图的风格会**预留 1 个名额**：用户图最多占 max_ref_images-1 个，
    保证所选风格的风格图不会被用户图挤掉。

    返回 (参考图字节列表, 角色说明列表, 可读的裁决原因)。
    """
    refs: List[bytes] = []
    roles: List[str] = []
    parts: List[str] = []

    # 有风格图时为用户图预留一个名额，风格图必定入选
    user_limit = max(0, max_ref_images - 1) if style_ref is not None else max_ref_images
    included_users = list(user_refs)[:user_limit]
    for data in included_users:
        refs.append(data)
        roles.append(ROLE_USER_REF)
    if included_users:
        label = f"用户图 {len(included_users)} 张"
        if len(user_refs) > user_limit:
            label += "（已为风格图预留名额）"
        parts.append(label)

    if style_ref is not None and len(refs) < max_ref_images:
        # 有用户图时风格图降为次要（用户图优先）；无用户图时风格图是唯一基准
        refs.append(style_ref)
        roles.append(ROLE_STYLE_REF_SECONDARY if included_users else ROLE_STYLE_REF)
        parts.append("风格图 1 张（已预留名额）")

    matched = (
        match_elements(page_text, elements, max_elements_per_page) if elements else []
    )
    picked: List[str] = []
    for entry in matched:
        if len(refs) >= max_ref_images:
            break
        path = entry.get("_path")
        if not path or not Path(path).exists():
            continue
        refs.append(Path(path).read_bytes())
        name = entry.get("name") or entry.get("id") or "未命名素材"
        roles.append(ELEMENT_ROLE_TEMPLATE.format(name=name))
        picked.append(str(name))
    if picked:
        parts.append(f"素材 {len(picked)} 张（{'、'.join(picked)}）")
    elif matched:
        parts.append("素材 0 张（名额已满，已跳过）")

    reason = "、".join(parts) if parts else "无参考图"
    return refs, roles, reason


def build_reference_preamble(*roles: str) -> str:
    """带序号的参考图说明，拼在正式提示词之前。"""
    if not roles:
        return ""
    lines = [f"随本消息提供 {len(roles)} 张参考图片，请按顺序理解："]
    for index, role in enumerate(roles):
        lines.append(f"【图{ORDER_CN[index]}】{role}")
    return "\n".join(lines)


def build_style_lock(template: str, style: Optional[Dict[str, Any]]) -> str:
    """把全局风格锁模板里的 {palette} 替换为该风格的调色板。
    没有调色板时退化为「沿用参考图配色」，保证各页仍然统一。"""
    palette = (style or {}).get("palette") or []
    if isinstance(palette, str):
        palette = [palette]
    palette_text = (
        "、".join(str(color) for color in palette) if palette else NO_PALETTE_TEXT
    )
    return template.replace("{palette}", palette_text)


def build_prompt(
    template: str,
    page: Dict[str, Any],
    style_instructions: str = "",
    user_images_hint: str = "",
    full_outline: str = "",
    user_topic: str = "",
    preamble: str = "",
    style_lock: str = "",
) -> str:
    """填充 image_prompt.txt 的占位符。"""
    try:
        prompt = template.format(
            page_content=page["content"],
            page_type=page["type"],
            style_instructions=style_instructions or NO_STYLE_TEXT,
            style_lock=style_lock,
            user_images_hint=user_images_hint,
            full_outline=full_outline,
            user_topic=user_topic or "未提供",
        )
    except (KeyError, IndexError) as exc:
        raise ValueError(
            f"提示词模板占位符不匹配（缺少 {exc}）。请检查 prompts/image_prompt.txt"
        ) from exc
    if preamble:
        return f"{preamble}\n\n{prompt}"
    return prompt


# ==================== HTTP 通用 ====================


def normalize_endpoint(endpoint: Optional[str], default: str) -> str:
    value = endpoint or default
    if value == "images":
        value = "/v1/images/generations"
    elif value == "chat":
        value = "/v1/chat/completions"
    if not value.startswith("/"):
        value = "/" + value
    return value


def normalize_base_url(base_url: str) -> str:
    value = (base_url or "").rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3]
    return value


def mask_key(key: Optional[str]) -> str:
    if not key:
        return "(未配置)"
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}...{key[-4:]} (len={len(key)})"


# proxy 配置取这些值（大小写不敏感）时表示「直连」：不使用任何代理
PROXY_DIRECT_VALUES = {"", "none", "off", "false", "direct", "no"}


def build_proxies(value: Optional[str]) -> Dict[str, str]:
    """把配置里的代理地址转成 requests 需要的 proxies 字典。

    留空或取 none / off / false / direct / no 时返回空字典，表示**直连**。
    """
    proxy = (value or "").strip()
    if proxy.lower() in PROXY_DIRECT_VALUES:
        return {}
    return {"http": proxy, "https": proxy}


def make_session(config: Optional[Dict[str, Any]] = None) -> requests.Session:
    """构造 HTTP 会话。

    本技能默认**直连**：`trust_env = False`，不读取 HTTP_PROXY / HTTPS_PROXY
    环境变量（服务为国内中继，走环境代理往往反而连不上）。只有显式配置
    `proxy: <url>` 时才通过该代理访问。
    """
    session = requests.Session()
    session.trust_env = False
    session.proxies = build_proxies((config or {}).get("proxy"))
    return session


def post_json(
    url: str,
    payload: Dict[str, Any],
    api_key: str,
    timeout: int = 300,
    max_retries: int = 3,
    base_delay: int = 2,
    proxies: Optional[Dict[str, str]] = None,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """POST JSON 并解析，限流/服务端临时故障自动退避重试。
    对外错误信息不包含服务地址，避免暴露技术链路。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    http = session if session is not None else requests
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            response = http.post(
                url, json=payload, headers=headers, timeout=timeout, proxies=proxies
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt < max_retries - 1:
                wait = min(base_delay**attempt, 10) + 1
                print(
                    f"[重试] 网络异常，{wait:.1f}s 后重试 ({attempt + 2}/{max_retries})"
                )
                time.sleep(wait)
                continue
            raise RuntimeError(f"网络连接失败，请检查网络后重试。\n{exc}") from exc

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"服务返回了无法解析的内容，请稍后重试。\n{response.text[:200]}"
                ) from exc

        detail = response.text[:200]
        if (
            response.status_code in (429, 500, 502, 503, 504)
            and attempt < max_retries - 1
        ):
            wait = (base_delay**attempt) + 1
            print(f"[重试] 服务繁忙，{wait:.1f}s 后重试 ({attempt + 2}/{max_retries})")
            time.sleep(wait)
            continue
        raise RuntimeError(_describe_http_error(response.status_code, detail))
    raise RuntimeError(f"重试 {max_retries} 次仍未成功，请稍后再试。\n{last_error}")


def _describe_http_error(status: int, detail: str) -> str:
    """把服务端状态翻译成人话，不暴露地址与端点"""
    if status in (401, 403):
        reason = "访问凭证无效或没有权限，请检查凭证（注意首尾空格）"
    elif status == 404:
        reason = "服务地址不可用，请更新到最新版本"
    elif status == 429:
        reason = "请求过于频繁，已被限流，请稍后再试"
    elif 500 <= status < 600:
        reason = "服务端临时故障，请稍后重试"
    else:
        reason = "请求未被接受"
    return f"{reason}。\n服务返回：{detail}"


def to_data_uri(image_data: bytes, max_size_kb: int) -> str:
    compressed = compress_image(image_data, max_size_kb)
    return "data:image/jpeg;base64," + base64.b64encode(compressed).decode("utf-8")


# ==================== 文本 / 识图 ====================


class TextClient:
    """内容规划 + 参考图识别。"""

    def __init__(self, config: Dict[str, Any]):
        self.base_url = normalize_base_url(config.get("base_url", ""))
        self.endpoint = self.base_url + normalize_endpoint(
            config.get("endpoint_type"), "/v1/chat/completions"
        )
        self.api_key = config.get("api_key") or ""
        self.model = config.get("model", "gemini-3-pro")
        self.temperature = config.get("temperature", 1.0)
        self.max_output_tokens = config.get("max_output_tokens", 8000)
        self.vision_support = config.get("vision_support", True)
        self.session = make_session(config)

    def generate_text(
        self, prompt: str, images: Optional[Sequence[bytes]] = None
    ) -> str:
        content: Any = prompt
        if images:
            content = [{"type": "text", "text": prompt}]
            for image_data in images:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": to_data_uri(image_data, 200)},
                    }
                )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "stream": False,
        }
        # 收紧重试：文本模型只发一次请求，失败即报错，保证一次大纲最多一次计费
        data = post_json(
            self.endpoint,
            payload,
            self.api_key,
            timeout=300,
            max_retries=TEXT_MAX_RETRIES,
            session=self.session,
        )
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("服务返回了无法识别的内容，请稍后重试。") from exc


# ==================== 图片 ====================


class ImageClient:
    """出图客户端：提交任务 -> 等待完成 -> 取回图片。细节由本类封装。"""

    def __init__(self, config: Dict[str, Any]):
        self.base_url = normalize_base_url(config.get("base_url", ""))
        self.endpoint = normalize_endpoint(
            config.get("endpoint_type"), "/v1/images/generations"
        )
        self.result_endpoint = config.get("result_endpoint") or ""
        self.model = config.get("model", "nano-banana-2")
        self.api_key = config.get("api_key") or ""
        self.aspect_ratio = config.get("aspect_ratio", "3:4")
        self.reference_max_kb = int(config.get("reference_max_kb", 200))
        self.poll_interval = float(config.get("poll_interval", 3))
        self.poll_timeout = float(config.get("poll_timeout", 300))
        self.session = make_session(config)
        self._proxy_fallback = False

    def _use_proxy(self) -> bool:
        return bool(getattr(self.session, "proxies", None))

    def _direct_session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        session.proxies = {}
        return session

    def _fallback_to_direct(self) -> bool:
        """显式代理连接失败时，切到直连会话；只降级一次。返回是否已切。"""
        if self._proxy_fallback or not self._use_proxy():
            return False
        self._proxy_fallback = True
        self.session = self._direct_session()
        print("[网络] 代理连接失败，改为直连重试…")
        return True

    def generate(
        self,
        prompt: str,
        references: Sequence[bytes],
        aspect_ratio: Optional[str] = None,
        reference_max_kbs: Optional[Sequence[int]] = None,
    ) -> bytes:
        size = aspect_ratio or self.aspect_ratio
        if self.result_endpoint:
            return self._generate_poll(prompt, size, references, reference_max_kbs)
        return self._generate_images_api(prompt, size, references, reference_max_kbs)

    def _reference_data_uris(
        self,
        references: Sequence[bytes],
        reference_max_kbs: Optional[Sequence[int]],
    ) -> List[str]:
        """逐张参考图转 data URI；缺省或长度不符时回落到全局 reference_max_kb。"""
        kbs = list(reference_max_kbs) if reference_max_kbs is not None else []
        return [
            to_data_uri(data, kbs[index] if index < len(kbs) else self.reference_max_kb)
            for index, data in enumerate(references)
        ]

    def _submit_stream(
        self,
        submit_url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """流式提交出图任务。

        返回 (task_id, final_result)。若流在「已拿到 task_id」之后中断，
        不抛错，返回 (task_id, None)，交由调用方转轮询恢复；若在拿到 task_id
        之前就中断，则原样抛出网络异常，交由调用方重试提交。
        """
        task_id: Optional[str] = None
        final_result: Optional[Dict[str, Any]] = None
        with self.session.post(
            submit_url,
            headers=headers,
            json=payload,
            timeout=120,
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(
                    _describe_http_error(response.status_code, response.text[:200])
                )
            try:
                for line in response.iter_lines():
                    if not line:
                        continue
                    line_str = line.decode("utf-8").strip()
                    if not line_str.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line_str[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    inner = (
                        data.get("data") if isinstance(data.get("data"), dict) else {}
                    )
                    if not task_id:
                        task_id = data.get("id") or inner.get("id")
                    status = data.get("status") or inner.get("status")
                    if status in ("succeeded", "success", "completed"):
                        final_result = data
                        break
                    if status in ("failed", "error"):
                        raise RuntimeError(
                            "图片生成任务失败: "
                            f"{data.get('error') or data.get('failure_reason') or '未知错误'}"
                        )
            except requests.RequestException:
                if task_id:
                    print("[出图] 提交流中断，任务已创建，转为轮询恢复…")
                    return task_id, None
                raise
        return task_id, final_result

    def _generate_poll(
        self,
        prompt: str,
        size: str,
        references: Sequence[bytes],
        reference_max_kbs: Optional[Sequence[int]] = None,
    ) -> bytes:
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
            "variants": 1,
            "shutProgress": False,
        }
        if references:
            payload["urls"] = self._reference_data_uris(references, reference_max_kbs)

        submit_url = f"{self.base_url}{self.endpoint}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # 提交阶段容错：仅在「尚未拿到 task_id」时重试提交；
        # 一旦拿到 task_id 就绝不重发，避免创建第二个任务造成重复计费。
        task_id: Optional[str] = None
        final_result: Optional[Dict[str, Any]] = None
        last_error: Optional[Exception] = None
        for attempt in range(SUBMIT_MAX_RETRIES):
            try:
                task_id, final_result = self._submit_stream(
                    submit_url, headers, payload
                )
            except requests.RequestException as exc:
                last_error = exc
                task_id, final_result = None, None
                # 显式代理连不上时，降级为直连再试
                self._fallback_to_direct()
            if task_id:
                break
            if attempt < SUBMIT_MAX_RETRIES - 1:
                wait = min(2**attempt, 10) + 1
                print(
                    f"[重试] 出图提交流中断，{wait:.1f}s 后重试 "
                    f"({attempt + 2}/{SUBMIT_MAX_RETRIES})"
                )
                time.sleep(wait)
                continue
            raise RuntimeError(
                "出图提交失败（网络中断），请检查网络后重试。"
            ) from last_error

        if not task_id:
            raise RuntimeError("出图任务未成功创建，请稍后重试。")
        if final_result is not None:
            return self._download(self._extract_url(final_result))

        print(f"[出图] 任务 {task_id} 处理中，继续等待…")
        result_url = f"{self.base_url}{normalize_endpoint(self.result_endpoint, '/v1/draw/result')}"
        deadline = time.time() + self.poll_timeout
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            try:
                query_result = post_json(
                    result_url,
                    {"id": task_id},
                    self.api_key,
                    timeout=60,
                    max_retries=2,
                    session=self.session,
                )
            except RuntimeError as exc:
                self._fallback_to_direct()
                print(f"[出图] 查询失败，继续等待: {str(exc)[:120]}")
                continue
            try:
                return self._download(self._extract_url(query_result))
            except RuntimeError:
                inner = (
                    query_result.get("data")
                    if isinstance(query_result.get("data"), dict)
                    else query_result
                )
                status = (inner or {}).get("status")
                if status in ("failed", "error"):
                    raise RuntimeError(
                        f"图片生成任务失败: {(inner or {}).get('error') or '未知错误'}"
                    )
                if status in ("running", "pending", "processing"):
                    continue
                raise RuntimeError("出图未返回可用结果，请重试。")
        raise RuntimeError(
            f"生成超时（{self.poll_timeout:.0f}s），任务 {task_id} 可稍后重跑。"
        )

    def _generate_images_api(
        self,
        prompt: str,
        size: str,
        references: Sequence[bytes],
        reference_max_kbs: Optional[Sequence[int]] = None,
    ) -> bytes:
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "response_format": "b64_json",
        }
        if references:
            payload["image"] = self._reference_data_uris(references, reference_max_kbs)
        data = post_json(
            f"{self.base_url}{self.endpoint}",
            payload,
            self.api_key,
            timeout=300,
            session=self.session,
        )
        items = data.get("data") or []
        if not items:
            raise RuntimeError(
                "出图未返回结果（内容可能未通过审核），请调整描述后重试。"
            )
        item = items[0]
        if item.get("b64_json"):
            raw = item["b64_json"]
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[1]
            return base64.b64decode(raw)
        if item.get("url"):
            return self._download(item["url"])
        raise RuntimeError("未能取得生成结果，请重试。")

    @staticmethod
    def _extract_url(result: Dict[str, Any]) -> str:
        inner = result.get("data") if isinstance(result.get("data"), dict) else result
        url = None
        if inner:
            results = inner.get("results")
            if isinstance(results, list) and results:
                url = results[0].get("url")
            if not url:
                url = inner.get("url")
        if not url:
            raise RuntimeError("未能取得生成结果，请重试。")
        return url

    def _download(
        self, url: str, max_retries: int = DOWNLOAD_MAX_RETRIES, base_delay: int = 2
    ) -> bytes:
        """取回最终图片；网络抖动或服务端临时故障时退避重试。"""
        print("[出图] 正在取回图片…")
        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=60)
            except requests.RequestException as exc:
                last_error = exc
                self._fallback_to_direct()
                if attempt < max_retries - 1:
                    wait = min(base_delay**attempt, 10) + 1
                    print(
                        f"[重试] 取图网络异常，{wait:.1f}s 后重试 "
                        f"({attempt + 2}/{max_retries})"
                    )
                    time.sleep(wait)
                    continue
                raise RuntimeError("取回图片失败（网络异常），请重试。") from exc
            if response.status_code == 200:
                return response.content
            if (
                response.status_code in (429, 500, 502, 503, 504)
                and attempt < max_retries - 1
            ):
                wait = (base_delay**attempt) + 1
                print(
                    f"[重试] 取图失败，{wait:.1f}s 后重试 ({attempt + 2}/{max_retries})"
                )
                time.sleep(wait)
                continue
            raise RuntimeError("取回图片失败，请重试。")
        raise RuntimeError("取回图片失败，请重试。") from last_error


# ==================== 配置与 IO ====================

DEFAULT_TEXT = {
    "base_url": GATEWAY_BASE_URL,
    "endpoint_type": "/v1/chat/completions",
    "model": "gemini-3-pro",
    "temperature": 1,
    "max_output_tokens": 8000,
    "vision_support": True,
    "api_key": "",
    "proxy": "",
}
DEFAULT_IMAGE = {
    "base_url": GATEWAY_BASE_URL,
    "endpoint_type": "/v1/draw/nano-banana",
    "result_endpoint": "/v1/draw/result",
    "model": "nano-banana-2",
    "aspect_ratio": "3:4",
    "reference_max_kb": 200,
    "poll_interval": 3,
    "poll_timeout": 300,
    "api_key": "",
    "proxy": "",
}
DEFAULT_DEFAULTS = {
    "out_dir": "./out",
    "max_ref_images": 5,
    "max_elements_per_page": 2,
}


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH
    raw: Dict[str, Any] = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    text = {**DEFAULT_TEXT, **(raw.get("text") or {})}
    image = {**DEFAULT_IMAGE, **(raw.get("image") or {})}
    defaults = {**DEFAULT_DEFAULTS, **(raw.get("defaults") or {})}
    text["api_key"] = os.environ.get(TEXT_KEY_ENV) or text.get("api_key") or ""
    image["api_key"] = os.environ.get(IMAGE_KEY_ENV) or image.get("api_key") or ""
    env_base_url = os.environ.get(BASE_URL_ENV)
    if env_base_url:
        text["base_url"] = env_base_url
        image["base_url"] = env_base_url
    return {"text": text, "image": image, "defaults": defaults, "_path": str(path)}


def require_key(config: Dict[str, Any], section: str) -> str:
    key = (config.get(section) or {}).get("api_key")
    if not key:
        env_name = TEXT_KEY_ENV if section == "text" else IMAGE_KEY_ENV
        label = "规划" if section == "text" else "出图"
        raise SystemExit(
            f"❌ 未配置{label}凭证。\n"
            f"方案一：设置环境变量 {env_name}\n"
            "方案二：运行 init 写入用户级配置\n"
            f"  python scripts/bio_sketch_figure.py init --{section}-key <KEY>\n"
            f"配置文件位置: {config['_path']}"
        )
    return key


def read_files(paths: Sequence[str], limit: Optional[int] = None) -> List[bytes]:
    blobs: List[bytes] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise SystemExit(f"❌ 参考图不存在: {path}")
        blobs.append(path.read_bytes())
    if limit is not None and len(blobs) > limit:
        print(f"[参考图] 传入 {len(blobs)} 张，按上限截取前 {limit} 张")
        blobs = blobs[:limit]
    return blobs


def read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def resolve_out_dir(value: Optional[str], config: Dict[str, Any]) -> Path:
    raw = value or config["defaults"]["out_dir"]
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = SKILL_ROOT / path
    return path


def format_style_list(styles: List[Dict[str, Any]]) -> str:
    lines = []
    for style in styles:
        mark = " [含参考图]" if style.get("reference_image") else ""
        desc = f" — {style['description']}" if style.get("description") else ""
        lines.append(f"  - {style['id']:<14} {style['name']}{mark}{desc}")
    return "\n".join(lines)


def resolve_style(style_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """风格闸口：--style 缺失即报错退出并打印清单"""
    styles = list_styles()
    ids = [s["id"] for s in styles]
    if style_id is None:
        raise SystemExit(
            "❌ 缺少 --style。\n"
            "请先把下面的风格清单交给用户确认，再用 --style 指定（禁止擅自默认）：\n"
            + format_style_list(styles)
        )
    if style_id not in ids:
        raise SystemExit(
            f"❌ 未知风格: {style_id}\n可选风格:\n" + format_style_list(styles)
        )
    return load_style(style_id)


def load_style_reference(style: Optional[Dict[str, Any]]) -> Optional[bytes]:
    if not style:
        return None
    path = resolve_reference_image(style.get("reference_image"))
    if not path:
        return None
    if not path.exists():
        raise SystemExit(
            f"❌ 风格参考图缺失: {path}\n"
            f"请检查 styles/{style.get('id')}.yaml 的 reference_image"
        )
    return path.read_bytes()


# ==================== 命令：outline ====================


def run_outline(
    topic: str,
    ref_paths: Sequence[str],
    config: Dict[str, Any],
    out_dir: Path,
    task_id: str,
) -> Dict[str, Any]:
    require_key(config, "text")
    text_config = config["text"]
    client = TextClient(text_config)

    max_refs = int(config["defaults"].get("max_ref_images", 5))
    ref_images = read_files(ref_paths, max_refs) if ref_paths else []
    if ref_images and text_config.get("vision_support") is False:
        raise SystemExit("❌ 当前服务不支持识别参考图片。\n请移除参考图后重试。")

    prompt = read_text(PROMPTS_DIR / "outline_prompt.txt").format(topic=topic)
    if ref_images:
        prompt += (
            f"\n\n注意：用户提供了 {len(ref_images)} 张参考图片，"
            "请在生成大纲时考虑这些图片的内容和风格。"
            "这些图片可能是产品图、个人照片或场景图，请根据图片内容来优化大纲，"
            "使生成的内容与图片相关联。"
        )
        print(f"[识图] 已发送 {len(ref_images)} 张参考图，正在识别…")
    else:
        print(f"[规划] 正在由 {SERVICE_NAME} 规划内容大纲…")

    # 识图与大纲合并为同一次文本模型调用：整个大纲阶段只产生一个计费请求
    outline_text = client.generate_text(prompt, images=ref_images or None)
    pages = parse_outline(outline_text)
    if not pages:
        raise SystemExit(f"❌ 未能解析出内容大纲，服务返回原文:\n{outline_text[:1000]}")

    task_dir = out_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "outline.txt").write_text(outline_text, encoding="utf-8")
    (task_dir / "pages.json").write_text(
        json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[大纲] 共 {len(pages)} 页 -> {task_dir / 'pages.json'}")
    for page in pages:
        first_line = page["content"].splitlines()[0][:60]
        print(f"  {page['index']}: [{page['type']}] {first_line}")
    return {
        "task_id": task_id,
        "task_dir": str(task_dir),
        "outline": outline_text,
        "pages": pages,
    }


# ==================== 命令：render ====================


def generate_one(
    page: Dict[str, Any],
    template: str,
    style_instructions: str,
    style_lock: str,
    hint: str,
    full_outline: str,
    user_topic: str,
    preamble: str,
    client: ImageClient,
    references: Sequence[bytes],
    aspect_ratio: Optional[str],
    reference_max_kbs: Optional[Sequence[int]] = None,
) -> Tuple[int, bool, Optional[str], Optional[bytes], Optional[str]]:
    try:
        prompt = build_prompt(
            template,
            page,
            style_instructions=style_instructions,
            user_images_hint=hint,
            full_outline=full_outline,
            user_topic=user_topic,
            preamble=preamble,
            style_lock=style_lock,
        )
        image_data = client.generate(
            prompt,
            references,
            aspect_ratio=aspect_ratio,
            reference_max_kbs=reference_max_kbs,
        )
        print(f"[出图] 第 {page['index']} 页完成 ({len(image_data) / 1024:.0f}KB)")
        return page["index"], True, None, image_data, None
    except Exception as exc:  # 单页失败不影响其他页
        print(f"[出图] 第 {page['index']} 页失败: {str(exc)[:300]}")
        return page["index"], False, None, None, str(exc)


def count_existing(task_dir: Path, pages: Sequence[Dict[str, Any]]) -> int:
    """统计 task_dir 下已存在的页数（页码.png），用于出图计划的「预计出图」提示。"""
    return sum(1 for page in pages if (task_dir / f"{page['index']}.png").exists())


def run_render(
    pages_path: str,
    style_id: Optional[str],
    ref_paths: Sequence[str],
    concurrency: int,
    aspect_ratio: Optional[str],
    config: Dict[str, Any],
    out_dir: Path,
    task_id: str,
    full_outline: str = "",
    user_topic: str = "",
    elements: Optional[Sequence[Dict[str, Any]]] = None,
    max_elements_per_page: Optional[int] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    # 风格闸口必须在 key 校验之前：无论是否配置 key，缺 --style 都要先被拦下
    style = resolve_style(style_id)
    require_key(config, "image")
    style_instructions = (style or {}).get("instructions") or ""
    style_lock = build_style_lock(read_text(PROMPTS_DIR / "style_lock.txt"), style)

    pages_file = Path(pages_path)
    if not full_outline and (pages_file.parent / "outline.txt").exists():
        full_outline = read_text(pages_file.parent / "outline.txt")
    pages = json.loads(read_text(pages_file))

    max_refs = int(config["defaults"].get("max_ref_images", 5))
    if max_elements_per_page is None:
        max_elements_per_page = int(config["defaults"].get("max_elements_per_page", 2))
    user_refs = read_files(ref_paths, max_refs) if ref_paths else []
    style_ref = load_style_reference(style)
    if elements is None:
        elements = load_elements()

    # 参考图体积上限：风格图可用风格级配置（更大以保留风格细节），其余用全局默认
    default_ref_kb = int(config["image"].get("reference_max_kb", 200))
    style_ref_kb = int((style or {}).get("reference_max_kb") or default_ref_kb)

    # 断点续跑：默认跳过已生成页，避免重跑时重复出图、重复计费（--overwrite 可强制重画）
    task_dir = out_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    prior_existing = {
        page["index"] for page in pages if (task_dir / f"{page['index']}.png").exists()
    }
    existing_indexes = set() if overwrite else prior_existing
    pending_pages = [page for page in pages if page["index"] not in existing_indexes]
    if overwrite and prior_existing:
        print(
            f"[出图] 已获准重绘（覆盖已有）{len(prior_existing)} 页: "
            f"{sorted(prior_existing)}"
        )
    if existing_indexes:
        print(
            f"[出图] 跳过已生成 {len(existing_indexes)} 页: "
            f"{sorted(existing_indexes)}（--overwrite 可重绘，需用户同意）"
        )
    if not pending_pages:
        print(f"[完成] 全部页面已存在，无需重跑 -> {task_dir}")
        return {
            "task_id": task_id,
            "task_dir": str(task_dir),
            "saved": [],
            "skipped": [f"{page['index']}.png" for page in pages],
            "failed": [],
        }

    # 每页独立装配参考图：用户图 → 风格图 → 命中素材
    page_plans: List[Dict[str, Any]] = []
    for page in pending_pages:
        refs, roles, reason = plan_page_references(
            page.get("content", ""),
            user_refs,
            style_ref,
            elements,
            max_refs,
            max_elements_per_page,
        )
        ref_kbs = [
            style_ref_kb if is_style_role(role) else default_ref_kb for role in roles
        ]
        page_plans.append(
            {
                "index": page["index"],
                "refs": refs,
                "roles": roles,
                "ref_kbs": ref_kbs,
                "reason": reason,
            }
        )
        print(f"[出图] 第 {page['index']} 页 参考图: {reason}")

    template = read_text(PROMPTS_DIR / "image_prompt.txt")
    client = ImageClient(config["image"])
    effective_ratio = aspect_ratio or (style or {}).get("aspect_ratio")

    print(
        f"[出图] 风格={style_id} | 页数={len(pages)} | 本次出图={len(pending_pages)} | "
        f"并发={concurrency} | 比例={effective_ratio or client.aspect_ratio} | "
        f"素材库={len(elements)} 项"
    )

    def _call(page: Dict[str, Any], plan: Dict[str, Any]):
        return generate_one(
            page,
            template,
            style_instructions,
            style_lock,
            build_reference_hint(len(plan["refs"])),
            full_outline,
            user_topic,
            build_reference_preamble(*plan["roles"]),
            client,
            plan["refs"],
            effective_ratio,
            reference_max_kbs=plan["ref_kbs"],
        )

    results: List[Tuple[int, bool, Optional[str], Optional[bytes], Optional[str]]] = []
    if concurrency > 1 and len(pending_pages) > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [
                pool.submit(_call, page, page_plans[order])
                for order, page in enumerate(pending_pages)
            ]
            for future in futures:
                results.append(future.result())
    else:
        for order, page in enumerate(pending_pages):
            results.append(_call(page, page_plans[order]))

    saved: List[str] = []
    failed: List[Dict[str, Any]] = []
    skipped: List[str] = [f"{index}.png" for index in sorted(existing_indexes)]
    for index, ok, _filename, image_data, error in sorted(results):
        if ok and image_data:
            filename = f"{index}.png"
            (task_dir / filename).write_bytes(image_data)
            (task_dir / f"thumb_{index}.jpg").write_bytes(
                compress_image(image_data, 50)
            )
            saved.append(filename)
        else:
            failed.append({"index": index, "error": error})

    (task_dir / "run.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "style_id": style_id,
                "style_instructions_source": (style or {}).get("_path"),
                "style_lock_source": str(PROMPTS_DIR / "style_lock.txt"),
                "element_library_count": len(elements),
                "max_elements_per_page": max_elements_per_page,
                "reference_plan": [
                    {
                        "index": plan["index"],
                        "roles": plan["roles"],
                        "reason": plan["reason"],
                        "count": len(plan["refs"]),
                        "ref_kbs": plan["ref_kbs"],
                    }
                    for plan in page_plans
                ],
                "aspect_ratio": effective_ratio or client.aspect_ratio,
                "full_outline_chars": len(full_outline),
                "saved": saved,
                "skipped": skipped,
                "failed": failed,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if failed:
        (task_dir / "failed.json").write_text(
            json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        f"[完成] 成功 {len(saved)} 页，跳过 {len(skipped)} 页，"
        f"失败 {len(failed)} 页 -> {task_dir}"
    )
    return {
        "task_id": task_id,
        "task_dir": str(task_dir),
        "saved": saved,
        "skipped": skipped,
        "failed": failed,
    }


# ==================== 命令实现 ====================


def cmd_init(args: argparse.Namespace) -> None:
    """只写入必要的字段（访问凭证），不把服务地址等内部信息落到用户配置里。"""
    path = Path(args.config).expanduser() if args.config else DEFAULT_CONFIG_PATH
    existing: Dict[str, Any] = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    text = dict(existing.get("text") or {})
    image = dict(existing.get("image") or {})
    defaults = dict(existing.get("defaults") or {})
    if args.text_key:
        text["api_key"] = args.text_key
    if args.image_key:
        image["api_key"] = args.image_key
    if getattr(args, "base_url", None):
        text["base_url"] = args.base_url
        image["base_url"] = args.base_url
    payload: Dict[str, Any] = {}
    if text:
        payload["text"] = text
    if image:
        payload["image"] = image
    if defaults:
        payload["defaults"] = defaults
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
    print(f"[init] 配置已写入: {path}")
    print(f"[init] 规划凭证 = {mask_key(text.get('api_key'))}")
    print(f"[init] 出图凭证 = {mask_key(image.get('api_key'))}")
    print(f"[init] 服务地址 = {text.get('base_url') or GATEWAY_BASE_URL}")
    print("[init] 提示：该文件属敏感信息，请勿提交到任何仓库。")


def cmd_check(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    exists = Path(config["_path"]).exists()
    print(
        f"[check] 配置文件: {config['_path']}（{'存在' if exists else '不存在，使用默认值'}）"
    )
    text = config["text"]
    image = config["image"]
    print(f"[check] 规划凭证: {'已配置' if text.get('api_key') else '未配置'}")
    print(f"[check] 出图凭证: {'已配置' if image.get('api_key') else '未配置'}")
    print(f"[check] 服务地址: {text.get('base_url') or GATEWAY_BASE_URL}")
    print(f"[check] 服务: {SERVICE_NAME} / {IMAGE_SERVICE_LABEL} — 已就绪")
    for name in ("outline_prompt.txt", "image_prompt.txt", "style_lock.txt"):
        path = PROMPTS_DIR / name
        print(f"[check] 提示词 {name}: {'OK' if path.exists() else '缺失'} ({path})")
    for style in list_styles():
        ref = style.get("reference_image")
        if ref:
            state = "OK" if Path(ref).exists() else "缺失"
            print(f"[check] 风格 {style['id']}: {state} 参考图 ({ref})")
        else:
            print(f"[check] 风格 {style['id']}: 无参考图")
    elements = load_elements()
    status = element_status(elements)
    if not elements and not status["unindexed"]:
        print(f"[check] 素材库: 空（{status['dir']} 下无索引与素材）")
    else:
        print(f"[check] 素材库: 已索引 {status['count']} 张（{status['dir']}）")
        if status["missing"]:
            print(f"[check] 素材索引了但文件缺失: {'、'.join(status['missing'])}")
        if status["unindexed"]:
            print(
                f"[check] 素材未入索引（不会被使用）: {'、'.join(status['unindexed'])}"
            )
    print("[check] 说明：check 不会调用文本模型，不产生任何计费")
    print("[check] 文本模型仅在大纲阶段（outline）调用，且一次大纲只发一个请求")


def cmd_credit(args: argparse.Namespace) -> None:
    """查询剩余额度（网关 /v1/credit），不调用文本/出图模型，不产生计费。"""
    config = load_config(args.config)
    text = config["text"]
    image = config["image"]
    key = text.get("api_key") or image.get("api_key")
    if not key:
        raise SystemExit(
            "❌ 未配置访问凭证，无法查询额度。\n"
            f"方案一：设置环境变量 {TEXT_KEY_ENV}\n"
            "方案二：运行 init 写入用户级配置\n"
            "  python scripts/bio_sketch_figure.py init --text-key <KEY>"
        )
    base = normalize_base_url(text.get("base_url") or GATEWAY_BASE_URL)
    url = f"{base}/v1/credit"
    session = make_session(text)
    try:
        response = session.get(
            url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {key}"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise SystemExit(f"❌ 网络连接失败，未能查询额度，请检查网络后重试。\n{exc}")
    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            raise SystemExit(f"❌ 服务返回了无法解析的内容。\n{response.text[:200]}")
        remaining = None
        if isinstance(data, dict):
            for field in ("remaining", "credit", "credits", "quota"):
                if data.get(field) is not None:
                    remaining = data.get(field)
                    break
        if remaining is None:
            print(f"[额度] 原始返回：{json.dumps(data, ensure_ascii=False)[:200]}")
            print("[额度] 未能从返回中解析出额度字段。")
            return
        print(f"[额度] 剩余额度：{remaining} 次（5 次 ≈ 1 张图）")
        return
    raise SystemExit(
        "❌ " + _describe_http_error(response.status_code, response.text[:200])
    )


def cmd_styles(args: argparse.Namespace) -> None:
    print("可用风格（生成前必须让用户从中明确选择）：")
    print(format_style_list(list_styles()))


def cmd_outline(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    out_dir = resolve_out_dir(args.out_dir, config)
    task_id = args.task_id or f"task_{uuid.uuid4().hex[:8]}"
    run_outline(args.topic, args.ref or [], config, out_dir, task_id)


def _print_render_plan(
    action: str,
    pages: Sequence[Dict[str, Any]],
    style: Optional[Dict[str, Any]],
    style_id: Optional[str],
    aspect_ratio: Optional[str],
    pages_path: Optional[str] = None,
    existing: int = 0,
    overwrite: bool = False,
) -> None:
    """出图前的计费确认提示；打印计划后以退出码 1 结束，绝不发起出图。"""
    style_name = (style or {}).get("name") or style_id or "无风格"
    ratio = aspect_ratio or (style or {}).get("aspect_ratio") or "（默认）"
    prior = existing
    skipped = 0 if overwrite else prior
    pending = max(0, len(pages) - skipped)
    print("[!] 出图是计费操作，尚未获得 --confirm 确认，未发起任何出图请求。")
    print(f"[计划] 命令: {action}")
    print(f"[计划] 风格: {style_name}（{style_id}）")
    print(f"[计划] 页数: {len(pages)} 张 / 本次预计出图: {pending} 张")
    if skipped:
        print(f"[计划] 已生成将跳过: {skipped} 页（如需重画加 --overwrite）")
    if overwrite and prior:
        print(
            f"[计划] ⚠ 重绘（覆盖已有）: {prior} 页 —— 重绘属于计费操作，"
            "必须先向用户说明并获得明确同意，才可加 --confirm 重跑"
        )
    print(f"[计划] 比例: {ratio}")
    print("[计划] 参考图: 用户图 → 风格图 → 命中素材（按需从素材库挑选）")
    if pages_path:
        print(f"[计划] 大纲已产出: {pages_path}")
        print(
            "[计划] 获得用户明确同意后，重跑：python scripts/bio_sketch_figure.py "
            f'render --pages "{pages_path}" --style {style_id} --confirm'
        )
    else:
        print("[计划] 获得用户明确同意后，重跑并在命令末尾加上 --confirm")
    raise SystemExit(1)


def _print_thanks(result: Dict[str, Any]) -> None:
    """本次出图全部成功（无失败页）时，打印对服务方的致谢。"""
    if not (result or {}).get("failed"):
        print(f"\n{THANKS_MESSAGE}")


def cmd_render(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    pages_file = Path(args.pages).resolve()
    style = resolve_style(args.style)  # 风格闸口优先于一切
    if args.out_dir:
        out_dir = resolve_out_dir(args.out_dir, config)
    else:
        # 默认把产物写回 pages.json 所在的任务目录
        out_dir = pages_file.parent.parent
    task_id = args.task_id or pages_file.parent.name
    if not args.confirm:
        if not pages_file.exists():
            raise SystemExit(f"❌ pages.json 不存在: {pages_file}")
        pages = json.loads(read_text(pages_file))
        existing = count_existing(out_dir / task_id, pages)
        _print_render_plan(
            "render",
            pages,
            style,
            args.style,
            args.aspect_ratio,
            str(pages_file),
            existing,
            args.overwrite,
        )
    result = run_render(
        str(pages_file),
        args.style,
        args.ref or [],
        args.concurrency,
        args.aspect_ratio,
        config,
        out_dir,
        task_id,
        user_topic=args.topic or "",
        overwrite=args.overwrite,
    )
    _print_thanks(result)


def cmd_all(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    out_dir = resolve_out_dir(args.out_dir, config)
    task_id = args.task_id or f"task_{uuid.uuid4().hex[:8]}"
    style = resolve_style(args.style)  # 风格闸口：先拦截，避免白跑大纲
    result = run_outline(args.topic, args.ref or [], config, out_dir, task_id)
    task_dir = Path(result["task_dir"])
    if not args.confirm:
        existing = count_existing(task_dir, result["pages"])
        _print_render_plan(
            "all",
            result["pages"],
            style,
            args.style,
            args.aspect_ratio,
            str(task_dir / "pages.json"),
            existing,
            args.overwrite,
        )
    render_result = run_render(
        str(task_dir / "pages.json"),
        args.style,
        args.ref or [],
        args.concurrency,
        args.aspect_ratio,
        config,
        out_dir,
        task_id,
        full_outline=result["outline"],
        user_topic=args.topic,
        overwrite=args.overwrite,
    )
    print(f"[all] 产物目录: {result['task_dir']}")
    _print_thanks(render_result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bio-sketch-figure",
        description="BioSketch 创作中心链路：主题(+参考图) -> 大纲 -> 科研配图",
    )
    parser.add_argument(
        "--config", help="自定义配置文件路径（默认 ~/.bio-sketch-figure/config.yaml）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="写入用户级配置")
    p.add_argument("--text-key")
    p.add_argument("--image-key")
    p.add_argument("--base-url", help="服务地址（默认内置网关，可覆盖）")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("check", help="校验配置与文件完整性（不调用文本模型）")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("credit", help="查询剩余额度（不调用模型，不产生计费）")
    p.set_defaults(func=cmd_credit)

    p = sub.add_parser("styles", help="列出可用风格")
    p.set_defaults(func=cmd_styles)

    p = sub.add_parser("outline", help="主题 -> 大纲")
    p.add_argument("topic")
    p.add_argument("--ref", nargs="*", help="参考图片路径（最多 5 张）")
    p.add_argument("--out-dir")
    p.add_argument("--task-id")
    p.set_defaults(func=cmd_outline)

    p = sub.add_parser("render", help="大纲 -> 配图（必须指定 --style）")
    p.add_argument("--pages", required=True, help="pages.json 路径")
    p.add_argument("--style", help="风格 id；缺失即报错")
    p.add_argument("--ref", nargs="*")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--aspect-ratio")
    p.add_argument("--topic", help="用户原始主题（写入提示词）")
    p.add_argument("--out-dir")
    p.add_argument("--task-id")
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="重绘已存在的页（默认跳过，只补失败/缺失页）；重绘需用户明确同意，须与 --confirm 同用",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="确认出图（计费操作，必须先获得用户授权）",
    )
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("all", help="主题 -> 大纲 -> 配图（必须指定 --style）")
    p.add_argument("topic")
    p.add_argument("--style", help="风格 id；缺失即报错")
    p.add_argument("--ref", nargs="*")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--aspect-ratio")
    p.add_argument("--out-dir")
    p.add_argument("--task-id")
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="重绘已存在的页（默认跳过，只补失败/缺失页）；重绘需用户明确同意，须与 --confirm 同用",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="确认出图（计费操作，必须先获得用户授权）",
    )
    p.set_defaults(func=cmd_all)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n❌ 执行失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
