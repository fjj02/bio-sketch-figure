# -*- coding: utf-8 -*-
"""bio_sketch_figure 纯函数单元测试（unittest，零额外依赖）"""

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bio_sketch_figure as bsf  # noqa: E402


class TestParseOutline(unittest.TestCase):
    def test_page_tags_are_split_and_indexed_from_zero(self):
        text = "<page>\n[封面]\n封面内容\n</page>\n<page>\n[内容]\n正文内容\n</page>"
        pages = bsf.parse_outline(text)
        self.assertEqual([p["index"] for p in pages], [0, 1])
        self.assertEqual([p["type"] for p in pages], ["cover", "content"])
        self.assertEqual(pages[1]["content"], "[内容]\n正文内容")

    def test_unknown_bracket_type_falls_back_to_content(self):
        pages = bsf.parse_outline("<page>\n[图表]\n某内容\n</page>")
        self.assertEqual(pages[0]["type"], "content")

    def test_dash_fallback_when_no_page_tag(self):
        pages = bsf.parse_outline("[总结]\n甲\n---\n[内容]\n乙")
        self.assertEqual([p["type"] for p in pages], ["summary", "content"])
        self.assertEqual([p["index"] for p in pages], [0, 1])

    def test_blank_chunks_are_skipped_without_gaps(self):
        pages = bsf.parse_outline("<page>\n\n</page>\n<page>\n[内容]\n乙\n</page>")
        self.assertEqual([p["index"] for p in pages], [0])


class TestMatchElements(unittest.TestCase):
    ELEMENTS = [
        {"id": "mito", "name": "线粒体", "keywords": ["线粒体", "能量代谢"]},
        {"id": "nucleus", "name": "细胞核", "keywords": ["细胞核", "DNA"]},
        {"id": "mouse", "name": "小鼠", "keywords": ["小鼠", "动物模型"]},
    ]

    def test_keyword_hit_is_returned(self):
        matched = bsf.match_elements("讨论线粒体的能量代谢机制", self.ELEMENTS, 5)
        self.assertEqual([e["id"] for e in matched], ["mito"])

    def test_no_hit_returns_empty(self):
        self.assertEqual(bsf.match_elements("讨论水稻抗病", self.ELEMENTS, 5), [])

    def test_higher_hit_count_ranks_first(self):
        matched = bsf.match_elements("线粒体与细胞核中的 DNA", self.ELEMENTS, 5)
        self.assertEqual([e["id"] for e in matched], ["nucleus", "mito"])

    def test_limit_is_respected(self):
        matched = bsf.match_elements("线粒体、细胞核、小鼠", self.ELEMENTS, 2)
        self.assertEqual(len(matched), 2)

    def test_empty_inputs(self):
        self.assertEqual(bsf.match_elements("", self.ELEMENTS, 5), [])
        self.assertEqual(bsf.match_elements("线粒体", [], 5), [])
        self.assertEqual(bsf.match_elements("线粒体", self.ELEMENTS, 0), [])


class TestPlanPageReferences(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.asset = self.dir / "mito.png"
        self.asset.write_bytes(b"MITO")
        self.elements = [
            {
                "id": "mito",
                "name": "线粒体",
                "keywords": ["线粒体"],
                "_path": str(self.asset),
            }
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_reference_at_all(self):
        refs, roles, reason = bsf.plan_page_references("无关文本", [], None, [])
        self.assertEqual((refs, roles, reason), ([], [], "无参考图"))

    def test_order_is_user_then_style_then_element(self):
        refs, roles, reason = bsf.plan_page_references(
            "线粒体", [b"u1"], b"style", self.elements
        )
        self.assertEqual(refs, [b"u1", b"style", b"MITO"])
        self.assertEqual(roles[0], bsf.ROLE_USER_REF)
        # 有用户图时风格图降为次要（用户图优先）
        self.assertEqual(roles[1], bsf.ROLE_STYLE_REF_SECONDARY)
        self.assertIn("元素素材「线粒体」", roles[2])
        self.assertIn("用户图 1 张", reason)
        self.assertIn("风格图 1 张", reason)
        self.assertIn("素材 1 张", reason)

    def test_user_and_style_coexist_even_with_user_refs(self):
        refs, roles, _reason = bsf.plan_page_references(
            "无关", [b"u1", b"u2"], b"style", []
        )
        self.assertEqual(refs, [b"u1", b"u2", b"style"])

    def test_cap_stops_elements_when_slots_full(self):
        refs, _roles, reason = bsf.plan_page_references(
            "线粒体", [b"u1", b"u2", b"u3", b"u4"], b"style", self.elements, 5, 2
        )
        self.assertEqual(refs, [b"u1", b"u2", b"u3", b"u4", b"style"])
        self.assertIn("名额已满", reason)

    def test_style_image_survives_full_user_refs(self):
        # 用户图 5 张占满上限，风格图仍必须入选（用户图被截到 4 张）
        refs, roles, reason = bsf.plan_page_references(
            "无关", [b"u1", b"u2", b"u3", b"u4", b"u5"], b"style", [], 5, 2
        )
        self.assertEqual(len(refs), 5)
        self.assertIn(b"style", refs)
        self.assertEqual(roles[-1], bsf.ROLE_STYLE_REF_SECONDARY)
        self.assertIn("已为风格图预留名额", reason)

    def test_reserved_style_slot_with_single_slot(self):
        # 只有 1 个名额时，风格图优先，用户图让位
        refs, roles, reason = bsf.plan_page_references(
            "无关", [b"u1"], b"style", [], 1, 2
        )
        self.assertEqual(refs, [b"style"])
        self.assertEqual(roles, [bsf.ROLE_STYLE_REF])
        self.assertIn("已预留名额", reason)

    def test_without_style_all_user_refs_are_kept(self):
        # 无风格图时不预留名额，用户图按上限正常截取
        refs, _roles, _reason = bsf.plan_page_references(
            "无关", [b"u1", b"u2", b"u3", b"u4", b"u5", b"u6"], None, [], 5, 2
        )
        self.assertEqual(refs, [b"u1", b"u2", b"u3", b"u4", b"u5"])

    def test_missing_element_file_is_skipped(self):
        elements = [
            {
                "id": "x",
                "name": "缺失",
                "keywords": ["线粒体"],
                "_path": str(self.dir / "nope.png"),
            }
        ]
        refs, roles, _reason = bsf.plan_page_references("线粒体", [], None, elements)
        self.assertEqual(refs, [])
        self.assertEqual(roles, [])

    def test_no_element_match_keeps_nothing(self):
        refs, _roles, reason = bsf.plan_page_references("无关", [], None, self.elements)
        self.assertEqual(refs, [])
        self.assertEqual(reason, "无参考图")

    def test_style_role_is_sole_basis_without_user_refs(self):
        _refs, roles, _reason = bsf.plan_page_references("无关", [], b"style", [])
        self.assertEqual(roles, [bsf.ROLE_STYLE_REF])
        self.assertTrue(bsf.is_style_role(roles[0]))

    def test_style_role_is_secondary_with_user_refs(self):
        _refs, roles, _reason = bsf.plan_page_references("无关", [b"u1"], b"style", [])
        self.assertEqual(roles, [bsf.ROLE_USER_REF, bsf.ROLE_STYLE_REF_SECONDARY])
        self.assertFalse(bsf.is_style_role(roles[0]))
        self.assertTrue(bsf.is_style_role(roles[1]))


class TestReferencePreamble(unittest.TestCase):
    def test_ordinal_numbering_and_roles(self):
        text = bsf.build_reference_preamble("一张风格图")
        self.assertIn("随本消息提供 1 张参考图片", text)
        self.assertIn("【图一】一张风格图", text)

    def test_empty_roles_gives_empty_string(self):
        self.assertEqual(bsf.build_reference_preamble(), "")


class TestElementLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "a.png").write_bytes(b"A")
        (self.dir / "b.png").write_bytes(b"B")
        (self.dir / "loose.png").write_bytes(b"L")
        (self.dir / "index.yaml").write_text(
            yaml.safe_dump(
                [
                    {"id": "a", "name": "甲", "keywords": ["甲"], "file": "a.png"},
                    {"id": "b", "name": "乙", "keywords": ["乙"], "file": "b.png"},
                    {
                        "id": "gone",
                        "name": "缺失",
                        "keywords": ["丙"],
                        "file": "nope.png",
                    },
                ],
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_elements_reads_and_resolves_paths(self):
        elements = bsf.load_elements(self.dir / "index.yaml", self.dir)
        self.assertEqual([e["id"] for e in elements], ["a", "b", "gone"])
        self.assertEqual(Path(elements[0]["_path"]), (self.dir / "a.png").resolve())

    def test_missing_index_returns_empty(self):
        self.assertEqual(bsf.load_elements(self.dir / "nope.yaml", self.dir), [])

    def test_element_status_reports_missing_and_unindexed(self):
        elements = bsf.load_elements(self.dir / "index.yaml", self.dir)
        status = bsf.element_status(elements, self.dir)
        self.assertEqual(status["count"], 3)
        self.assertEqual(status["missing"], ["nope.png"])
        self.assertEqual(status["unindexed"], ["loose.png"])


class TestBuildStyleLock(unittest.TestCase):
    TEMPLATE = "系列一致性锁\n调色板:{palette}"

    def test_palette_is_joined(self):
        lock = bsf.build_style_lock(self.TEMPLATE, {"palette": ["#111111", "#222222"]})
        self.assertIn("#111111、#222222", lock)

    def test_missing_palette_falls_back(self):
        lock = bsf.build_style_lock(self.TEMPLATE, {})
        self.assertIn(bsf.NO_PALETTE_TEXT, lock)
        self.assertNotIn("{palette}", lock)

    def test_string_palette_is_tolerated(self):
        lock = bsf.build_style_lock(self.TEMPLATE, {"palette": "#abc"})
        self.assertIn("#abc", lock)


class TestBuildPrompt(unittest.TestCase):
    TEMPLATE = "内容:{page_content}|类型:{page_type}|风格:{style_instructions}|图:{user_images_hint}|大纲:{full_outline}|主题:{user_topic}|锁:{style_lock}"

    def _page(self):
        return {"index": 0, "type": "content", "content": "线粒体能量代谢"}

    def test_placeholders_are_filled(self):
        p = bsf.build_prompt(
            self.TEMPLATE, self._page(), "风格X", "提示Y", "大纲Z", "主题W"
        )
        self.assertEqual(
            p,
            "内容:线粒体能量代谢|类型:content|风格:风格X|图:提示Y|大纲:大纲Z|主题:主题W|锁:",
        )

    def test_style_lock_is_filled(self):
        p = bsf.build_prompt(self.TEMPLATE, self._page(), style_lock="一致性规则")
        self.assertIn("锁:一致性规则", p)

    def test_empty_style_falls_back_to_no_style_text(self):
        p = bsf.build_prompt(self.TEMPLATE, self._page())
        self.assertIn("风格:无特定风格要求", p)
        self.assertIn("主题:未提供", p)

    def test_preamble_is_prepended(self):
        p = bsf.build_prompt(self.TEMPLATE, self._page(), preamble="前言")
        self.assertTrue(p.startswith("前言\n\n"))

    def test_missing_placeholder_raises_clear_error(self):
        with self.assertRaises(ValueError):
            bsf.build_prompt("{unknown_key}", self._page())


class TestCompressImage(unittest.TestCase):
    @staticmethod
    def _png(size=(1200, 900), noise=False) -> bytes:
        if noise:
            # 纯色 PNG 会被 zlib 压到 16KB 以下，无法触发压缩分支；
            # 必须用不可压缩的随机噪声才能让原始体积真正超过阈值。
            import random

            rnd = random.Random(20260916)
            img = Image.frombytes("RGB", size, rnd.randbytes(size[0] * size[1] * 3))
        else:
            img = Image.new("RGB", size, (200, 120, 90))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_small_image_is_returned_unchanged(self):
        raw = self._png((10, 10))
        self.assertEqual(bsf.compress_image(raw, 200), raw)

    def test_large_image_is_compressed_under_limit(self):
        raw = self._png((2400, 1800), noise=True)
        self.assertGreater(len(raw), 200 * 1024)  # 前置条件：确实超过阈值
        out = bsf.compress_image(raw, 200)
        self.assertLessEqual(len(out), 200 * 1024)
        self.assertTrue(out.startswith(b"\xff\xd8"))  # JPEG magic

    def test_rgba_is_flattened_without_error(self):
        img = Image.new("RGBA", (600, 600), (10, 20, 30, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        self.assertLessEqual(len(bsf.compress_image(buf.getvalue(), 10)), 10 * 1024)


class TestStyleLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "demo.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "示例",
                    "id": "demo",
                    "tier": "free",
                    "description": "d",
                    "instructions": "风格说明",
                    "reference_image": None,
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        (self.dir / "withref.yaml").write_text(
            yaml.safe_dump(
                {"name": "带图", "id": "withref", "reference_image": "assets/x.png"},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_styles_includes_none_option(self):
        styles = bsf.list_styles(self.dir, skill_root=self.dir)
        self.assertEqual([s["id"] for s in styles], ["demo", "withref", "none"])

    def test_load_style_none_returns_none(self):
        self.assertIsNone(bsf.load_style("none", self.dir))
        self.assertIsNone(bsf.load_style(None, self.dir))

    def test_load_style_reads_instructions(self):
        self.assertEqual(bsf.load_style("demo", self.dir)["instructions"], "风格说明")

    def test_load_style_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            bsf.load_style("nope", self.dir)

    def test_resolve_reference_image_is_absolute_and_nullable(self):
        self.assertIsNone(bsf.resolve_reference_image(None, self.dir))
        self.assertEqual(
            bsf.resolve_reference_image("assets/x.png", self.dir),
            (self.dir / "assets/x.png").resolve(),
        )


class TestRenderReferenceWiring(unittest.TestCase):
    """用假 ImageClient 验证 run_render 的参考图裁决与产物落盘（不联网）。

    覆盖 2.5D高级 这类「风格自带参考图」的核心行为。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "prompts").mkdir()
        (self.root / "styles").mkdir()
        (self.root / "assets").mkdir()
        (self.root / "assets" / "style.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"x" * 10
        )
        (self.root / "prompts" / "image_prompt.txt").write_text(
            "内容:{page_content}|风格:{style_instructions}|图:{user_images_hint}"
            "|大纲:{full_outline}|主题:{user_topic}|锁:{style_lock}",
            encoding="utf-8",
        )
        (self.root / "prompts" / "style_lock.txt").write_text(
            "系列一致性锁\n调色板:{palette}", encoding="utf-8"
        )
        (self.root / "sucai").mkdir()
        (self.root / "sucai" / "mito.png").write_bytes(b"MITO")
        self.elements = [
            {
                "id": "mito",
                "name": "线粒体",
                "keywords": ["线粒体"],
                "_path": str(self.root / "sucai" / "mito.png"),
            }
        ]
        self._write_style("assets/style.png")
        (self.root / "pages.json").write_text(
            json.dumps(
                [{"index": 0, "type": "content", "content": "线粒体"}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.captured = {}
        outer = self

        class FakeImageClient:
            def __init__(self, config):
                self.aspect_ratio = "3:4"

            def generate(
                self, prompt, references, aspect_ratio=None, reference_max_kbs=None
            ):
                outer.captured["prompt"] = prompt
                outer.captured["refs"] = list(references)
                outer.captured["ratio"] = aspect_ratio
                outer.captured["ref_kbs"] = (
                    list(reference_max_kbs) if reference_max_kbs is not None else None
                )
                return b"\xff\xd8fakejpeg" + b"0" * 100

        self._original = (
            bsf.PROMPTS_DIR,
            bsf.STYLES_DIR,
            bsf.SKILL_ROOT,
            bsf.ImageClient,
        )
        bsf.PROMPTS_DIR = self.root / "prompts"
        bsf.STYLES_DIR = self.root / "styles"
        bsf.SKILL_ROOT = self.root
        bsf.ImageClient = FakeImageClient

    def tearDown(self):
        (bsf.PROMPTS_DIR, bsf.STYLES_DIR, bsf.SKILL_ROOT, bsf.ImageClient) = (
            self._original
        )
        self.tmp.cleanup()

    def _write_style(self, reference):
        (self.root / "styles" / "refstyle.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "带图风格",
                    "id": "refstyle",
                    "instructions": "风格说明",
                    "reference_image": reference,
                    "aspect_ratio": "4:5",
                    "reference_max_kb": 400,
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _config():
        return {
            "text": {},
            "image": {"api_key": "dummy"},
            "defaults": {
                "out_dir": "out",
                "max_ref_images": 5,
                "max_elements_per_page": 2,
            },
            "_path": "<test>",
        }

    def _render(self, task_id, refs=(), elements=None, overwrite=False):
        return bsf.run_render(
            str(self.root / "pages.json"),
            "refstyle",
            list(refs),
            1,
            None,
            self._config(),
            self.root / "out",
            task_id,
            elements=[] if elements is None else elements,
            overwrite=overwrite,
        )

    def test_style_reference_image_is_sent_and_labelled(self):
        result = self._render("t1")
        self.assertEqual(len(self.captured["refs"]), 1)
        self.assertIn("【图一】视觉风格参考", self.captured["prompt"])
        self.assertIn("内容:线粒体", self.captured["prompt"])
        self.assertIn("风格:风格说明", self.captured["prompt"])
        self.assertIn("锁:系列一致性锁", self.captured["prompt"])
        self.assertIn(bsf.NO_PALETTE_TEXT, self.captured["prompt"])
        self.assertEqual(self.captured["ratio"], "4:5")
        # 风格图使用风格级体积上限（yaml 的 reference_max_kb=400）
        self.assertEqual(self.captured["ref_kbs"], [400])
        self.assertTrue((self.root / "out" / "t1" / "0.png").exists())
        self.assertTrue((self.root / "out" / "t1" / "thumb_0.jpg").exists())
        self.assertTrue((self.root / "out" / "t1" / "run.json").exists())
        self.assertEqual(result["failed"], [])

    def test_user_and_style_references_coexist(self):
        user = self.root / "u.png"
        user.write_bytes(b"u" * 50)
        self._render("t2", [str(user)])
        self.assertEqual(len(self.captured["refs"]), 2)
        self.assertIn("【图一】用户提供的参考图", self.captured["prompt"])
        self.assertIn("【图二】视觉风格参考", self.captured["prompt"])
        # 用户图用全局默认 200KB，风格图用风格级 400KB
        self.assertEqual(self.captured["ref_kbs"], [200, 400])

    def test_element_asset_is_appended_after_style(self):
        self._render("t4", elements=self.elements)
        self.assertEqual(len(self.captured["refs"]), 2)
        self.assertEqual(self.captured["refs"][1], b"MITO")
        self.assertIn("【图二】元素素材「线粒体」", self.captured["prompt"])

    def test_no_element_match_adds_nothing(self):
        other = [
            {
                "id": "x",
                "name": "小鼠",
                "keywords": ["小鼠"],
                "_path": str(self.root / "sucai" / "mito.png"),
            }
        ]
        self._render("t5", elements=other)
        self.assertEqual(len(self.captured["refs"]), 1)

    def test_missing_style_reference_image_exits(self):
        self._write_style("assets/nope.png")
        with self.assertRaises(SystemExit):
            self._render("t3")

    def test_resume_skips_existing_pages(self):
        task_dir = self.root / "out" / "t7"
        task_dir.mkdir(parents=True)
        (task_dir / "0.png").write_bytes(b"OLD")
        result = self._render("t7")
        self.assertEqual(result["saved"], [])
        self.assertEqual(result["skipped"], ["0.png"])
        self.assertEqual((task_dir / "0.png").read_bytes(), b"OLD")
        self.assertNotIn("refs", self.captured)  # 已存在的页不触发出图调用

    def test_overwrite_regenerates_existing_page(self):
        task_dir = self.root / "out" / "t8"
        task_dir.mkdir(parents=True)
        (task_dir / "0.png").write_bytes(b"OLD")
        result = self._render("t8", overwrite=True)
        self.assertEqual(result["saved"], ["0.png"])
        self.assertEqual(result["skipped"], [])
        self.assertNotEqual((task_dir / "0.png").read_bytes(), b"OLD")


class TestImageClientResilience(unittest.TestCase):
    """出图客户端网络健壮性：提交流中断恢复、重试与代理透传（全部离线）。"""

    @staticmethod
    def _config(**overrides):
        config = {
            "base_url": "https://example.invalid",
            "endpoint_type": "/v1/draw/nano-banana",
            "result_endpoint": "/v1/draw/result",
            "model": "nano-banana-2",
            "api_key": "k",
            "poll_interval": 0,
            "poll_timeout": 5,
        }
        config.update(overrides)
        return config

    @staticmethod
    def _stream(lines, status_code=200):
        class _Resp:
            def __init__(self):
                self.status_code = status_code
                self.text = "err"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_lines(self):
                for item in lines:
                    if isinstance(item, Exception):
                        raise item
                    yield item.encode("utf-8")

        return _Resp()

    def test_network_defaults_to_direct_and_ignores_env_proxy(self):
        with patch.dict(
            "os.environ",
            {"HTTP_PROXY": "http://broken:1", "HTTPS_PROXY": "http://broken:1"},
        ):
            client = bsf.ImageClient(self._config())
        self.assertFalse(client.session.trust_env)  # 不读环境代理
        self.assertEqual(client.session.proxies, {})  # 无代理 → 直连
        self.assertEqual(bsf.build_proxies(""), {})
        self.assertEqual(bsf.build_proxies("   "), {})
        self.assertEqual(bsf.build_proxies("none"), {})
        self.assertEqual(bsf.build_proxies(" DIRECT "), {})

    def test_explicit_proxy_is_applied_to_session(self):
        client = bsf.ImageClient(self._config(proxy="http://127.0.0.1:9910"))
        self.assertFalse(client.session.trust_env)
        self.assertEqual(
            client.session.proxies,
            {"http": "http://127.0.0.1:9910", "https": "http://127.0.0.1:9910"},
        )

    def test_stream_break_after_task_id_falls_back_to_poll(self):
        lines = [
            'data: {"id": "T1", "status": "running"}',
            bsf.requests.exceptions.ChunkedEncodingError("boom"),
        ]
        result = {"data": {"status": "succeeded", "results": [{"url": "http://img"}]}}
        client = bsf.ImageClient(self._config())
        with (
            patch.object(
                client.session, "post", return_value=self._stream(lines)
            ) as post,
            patch.object(bsf, "post_json", return_value=result),
            patch.object(bsf.ImageClient, "_download", return_value=b"IMG"),
            patch.object(bsf.time, "sleep", return_value=None),
        ):
            data = client.generate("提示词", [])
        self.assertEqual(data, b"IMG")
        self.assertEqual(post.call_count, 1)  # 拿到 task_id 后绝不再重发提交

    def test_stream_break_before_task_id_retries_then_raises(self):
        lines = [bsf.requests.exceptions.ChunkedEncodingError("boom")]
        client = bsf.ImageClient(self._config())
        with (
            patch.object(bsf, "SUBMIT_MAX_RETRIES", 2),
            patch.object(
                client.session, "post", return_value=self._stream(lines)
            ) as post,
            patch.object(bsf.time, "sleep", return_value=None),
        ):
            with self.assertRaises(RuntimeError):
                client.generate("提示词", [])
        self.assertEqual(post.call_count, 2)

    def test_submit_recovers_on_retry(self):
        first = self._stream([bsf.requests.exceptions.ChunkedEncodingError("boom")])
        second = self._stream(
            [
                'data: {"id": "T2", "status": "succeeded", '
                '"results": [{"url": "http://img"}]}'
            ]
        )
        client = bsf.ImageClient(self._config())
        with (
            patch.object(client.session, "post", side_effect=[first, second]) as post,
            patch.object(bsf.ImageClient, "_download", return_value=b"IMG2"),
            patch.object(bsf.time, "sleep", return_value=None),
        ):
            data = client.generate("提示词", [])
        self.assertEqual(data, b"IMG2")
        self.assertEqual(post.call_count, 2)

    def test_download_retries_on_network_error(self):
        ok = Mock()
        ok.status_code = 200
        ok.content = b"X"
        client = bsf.ImageClient(self._config())
        with (
            patch.object(
                client.session,
                "get",
                side_effect=[bsf.requests.RequestException("boom"), ok],
            ) as get,
            patch.object(bsf.time, "sleep", return_value=None),
        ):
            self.assertEqual(client._download("http://x"), b"X")
        self.assertEqual(get.call_count, 2)

    def test_explicit_proxy_failure_falls_back_to_direct(self):
        proxy_session = Mock()
        proxy_session.proxies = {"http": "http://p", "https": "http://p"}
        proxy_session.post.side_effect = bsf.requests.RequestException("proxy down")
        direct_session = Mock()
        direct_session.proxies = {}
        direct_session.post.return_value = self._stream(
            [
                'data: {"id": "T9", "status": "succeeded", '
                '"results": [{"url": "http://img"}]}'
            ]
        )
        client = bsf.ImageClient(self._config(proxy="http://p"))
        with (
            patch.object(
                bsf.ImageClient, "_direct_session", return_value=direct_session
            ),
            patch.object(bsf.ImageClient, "_download", return_value=b"IMG"),
            patch.object(bsf.time, "sleep", return_value=None),
        ):
            client.session = proxy_session
            data = client.generate("提示词", [])
        self.assertEqual(data, b"IMG")
        self.assertEqual(proxy_session.post.call_count, 1)  # 代理只试一次
        self.assertEqual(direct_session.post.call_count, 1)  # 随后直连成功


class TestConfirmGate(unittest.TestCase):
    """出图是计费操作：没有 --confirm 时任何命令都必须零出图调用、退出码 1。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text("text: {}\nimage: {}\n", encoding="utf-8")
        self.pages_dir = self.root / "task_x"
        self.pages_dir.mkdir()
        self.pages_file = self.pages_dir / "pages.json"
        self.pages_file.write_text(
            json.dumps(
                [
                    {"index": 0, "type": "content", "content": "甲"},
                    {"index": 1, "type": "content", "content": "乙"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.calls = {"render": 0}
        self._orig = (bsf.run_render, bsf.run_outline, bsf.ImageClient)

        def _forbidden_render(*_args, **_kwargs):
            self.calls["render"] += 1
            raise AssertionError("未确认却调用了 run_render")

        class ForbiddenClient:
            def __init__(self, _config):
                raise AssertionError("未确认却构造了出图客户端")

        bsf.run_render = _forbidden_render
        bsf.ImageClient = ForbiddenClient

    def tearDown(self):
        (bsf.run_render, bsf.run_outline, bsf.ImageClient) = self._orig
        self.tmp.cleanup()

    def _render_args(self, confirm, overwrite=False):
        return argparse.Namespace(
            config=str(self.config_path),
            pages=str(self.pages_file),
            style="2.5d",
            ref=None,
            concurrency=1,
            aspect_ratio=None,
            topic=None,
            out_dir=None,
            task_id=None,
            overwrite=overwrite,
            confirm=confirm,
        )

    def test_render_without_confirm_exits_without_calling(self):
        with self.assertRaises(SystemExit) as ctx:
            bsf.cmd_render(self._render_args(False))
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.calls["render"], 0)

    def test_render_with_overwrite_but_no_confirm_exits(self):
        # 重绘必须取得用户同意：没有 --confirm 时直接拦截，绝不重绘
        with self.assertRaises(SystemExit) as ctx:
            bsf.cmd_render(self._render_args(False, overwrite=True))
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.calls["render"], 0)

    def test_overwrite_plan_shows_redraw_warning(self):
        (self.pages_dir / "0.png").write_bytes(b"OLD")  # 模拟已有页
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                bsf.cmd_render(self._render_args(False, overwrite=True))
        out = buf.getvalue()
        self.assertIn("重绘", out)
        self.assertIn("明确同意", out)
        self.assertEqual(self.calls["render"], 0)

    def test_overwrite_with_confirm_passes_overwrite(self):
        seen = {}

        def fake_render(*_args, **kwargs):
            seen["overwrite"] = kwargs.get("overwrite")
            return {}

        bsf.run_render = fake_render
        bsf.cmd_render(self._render_args(True, overwrite=True))
        self.assertTrue(seen["overwrite"])

    def test_all_without_confirm_runs_outline_only(self):
        captured = {}

        def fake_outline(topic, ref_paths, config, out_dir, task_id):
            captured["outline"] = True
            return {
                "task_id": task_id,
                "task_dir": str(self.pages_dir),
                "outline": "大纲",
                "pages": [{"index": 0, "type": "content", "content": "甲"}],
            }

        bsf.run_outline = fake_outline
        args = argparse.Namespace(
            config=str(self.config_path),
            topic="主题",
            style="2.5d",
            ref=None,
            concurrency=1,
            aspect_ratio=None,
            out_dir=None,
            task_id="task_x",
            overwrite=False,
            confirm=False,
        )
        with self.assertRaises(SystemExit) as ctx:
            bsf.cmd_all(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(captured.get("outline"))
        self.assertEqual(self.calls["render"], 0)

    def test_render_with_confirm_calls_run_render(self):
        seen = {}

        def fake_render(*_args, **_kwargs):
            seen["yes"] = True
            return {}

        bsf.run_render = fake_render
        bsf.cmd_render(self._render_args(True))
        self.assertTrue(seen.get("yes"))


class TestSkillFrontmatter(unittest.TestCase):
    """SKILL.md 的 YAML frontmatter 必须能被严格解析器直接读取。

    背景：description 里若出现未加引号的半角「冒号+空格」（如 `Triggers on: `），
    YAML 会判为嵌套映射，抛 ScannerError("mapping values are not allowed here")，
    导致平台（如豆包）导入技能时报「文件格式错误」。opencode 的宽松解析器察觉不到，
    所以必须由严格解析来兜底。
    """

    MAX_DESCRIPTION = 1024
    MAX_NAME = 64
    ALLOWED_KEYS = {"name", "description", "license", "allowed-tools", "metadata"}

    @classmethod
    def setUpClass(cls):
        cls.skill_root = Path(__file__).resolve().parent.parent
        cls.skill_md = cls.skill_root / "SKILL.md"
        cls.raw = cls.skill_md.read_text(encoding="utf-8")

    def _frontmatter_block(self) -> str:
        match = re.match(r"^---\r?\n(.*?)\r?\n---", self.raw, re.S)
        self.assertIsNotNone(match, "SKILL.md 必须以 --- 分隔的 YAML frontmatter 开头")
        return match.group(1)

    def test_frontmatter_is_strictly_parseable_yaml(self):
        # 严格解析；非法 YAML（例如未加引号的 "Triggers on: "）会在这里抛错
        data = yaml.safe_load(self._frontmatter_block())
        self.assertIsInstance(
            data, dict, "frontmatter 必须解析为映射（不是字符串/列表）"
        )

    def test_description_is_a_plain_string(self):
        data = yaml.safe_load(self._frontmatter_block())
        self.assertIn("description", data)
        # 出现未加引号的 "冒号+空格" 时，这里会变成 dict 或 None，被此断言拦下
        self.assertIsInstance(data["description"], str)
        self.assertTrue(data["description"].strip())

    def test_name_matches_directory_and_spec(self):
        data = yaml.safe_load(self._frontmatter_block())
        name = data.get("name")
        self.assertEqual(name, self.skill_root.name, "name 必须与技能目录名一致")
        self.assertLessEqual(len(name), self.MAX_NAME)
        self.assertRegex(
            name, r"^[a-z0-9]+(-[a-z0-9]+)*$", "name 只允许小写字母、数字与连字符"
        )

    def test_description_within_length_limit(self):
        data = yaml.safe_load(self._frontmatter_block())
        self.assertLessEqual(len(data["description"]), self.MAX_DESCRIPTION)

    def test_only_known_frontmatter_keys(self):
        data = yaml.safe_load(self._frontmatter_block())
        unknown = set(data) - self.ALLOWED_KEYS
        self.assertFalse(unknown, f"出现非预期的 frontmatter 字段: {sorted(unknown)}")

    def test_skill_md_has_no_bom_and_uses_lf(self):
        raw_bytes = self.skill_md.read_bytes()
        self.assertFalse(
            raw_bytes.startswith(b"\xef\xbb\xbf"), "SKILL.md 不得带 UTF-8 BOM"
        )
        self.assertNotIn(b"\r\n", raw_bytes, "SKILL.md 应使用 LF 换行")


class TestTextModelSingleBilling(unittest.TestCase):
    """文本模型只在大纲阶段调用，且一次大纲最多一次 HTTP 请求（禁止自动重试）。"""

    def _client(self):
        return bsf.TextClient(
            {
                "base_url": "https://example.invalid",
                "endpoint_type": "/v1/chat/completions",
                "model": "m",
                "api_key": "k",
            }
        )

    def test_success_makes_exactly_one_request(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": "大纲"}}]}
        client = self._client()
        client.session = Mock()
        client.session.post.return_value = response
        reply = client.generate_text("你好")
        self.assertEqual(reply, "大纲")
        self.assertEqual(client.session.post.call_count, 1)

    def test_server_error_is_not_retried(self):
        response = Mock()
        response.status_code = 500
        response.text = "boom"
        client = self._client()
        client.session = Mock()
        client.session.post.return_value = response
        with self.assertRaises(RuntimeError):
            client.generate_text("你好")
        self.assertEqual(client.session.post.call_count, 1)

    def test_text_client_defaults_to_direct(self):
        client = self._client()
        self.assertFalse(client.session.trust_env)
        self.assertEqual(client.session.proxies, {})


class TestOutlineSingleTextCall(unittest.TestCase):
    """识图与大纲合并：带参考图时 run_outline 只调用文本模型一次。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "outline_prompt.txt").write_text("主题:{topic}", encoding="utf-8")
        self.ref = self.root / "ref.png"
        self.ref.write_bytes(b"REF")
        self.calls = []
        outer = self

        class FakeTextClient:
            def __init__(self, _config):
                pass

            def generate_text(self, prompt, images=None):
                outer.calls.append({"prompt": prompt, "images": images})
                return "<page>\n[内容]\n甲\n</page>"

        self._orig = (bsf.PROMPTS_DIR, bsf.TextClient)
        bsf.PROMPTS_DIR = prompts
        bsf.TextClient = FakeTextClient

    def tearDown(self):
        (bsf.PROMPTS_DIR, bsf.TextClient) = self._orig
        self.tmp.cleanup()

    @staticmethod
    def _config():
        return {
            "text": {"api_key": "k", "vision_support": True},
            "image": {},
            "defaults": {"max_ref_images": 5, "max_elements_per_page": 2},
            "_path": "<test>",
        }

    def test_reference_images_go_into_the_same_call(self):
        result = bsf.run_outline(
            "主题", [str(self.ref)], self._config(), self.root / "out", "t1"
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["images"], [b"REF"])
        self.assertEqual([p["index"] for p in result["pages"]], [0])


class TestPingRemovedAndNoBilling(unittest.TestCase):
    """check 不再调用文本模型：--ping 已移除，且 check 不会构造文本客户端。"""

    def test_ping_flag_is_gone(self):
        with self.assertRaises(SystemExit):
            bsf.build_parser().parse_args(["check", "--ping"])

    def test_check_never_constructs_text_client(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = Path(tmp.name) / "config.yaml"
        config_path.write_text("text: {}\nimage: {}\n", encoding="utf-8")

        class ForbiddenTextClient:
            def __init__(self, _config):
                raise AssertionError("check 不应调用文本模型")

        original = bsf.TextClient
        bsf.TextClient = ForbiddenTextClient
        try:
            bsf.cmd_check(argparse.Namespace(config=str(config_path)))
        finally:
            bsf.TextClient = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
