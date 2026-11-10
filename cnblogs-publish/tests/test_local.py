"""Offline tests for the cnblogs skill script (no token, no network).

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import xmlrpc.server
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cnblogs  # noqa: E402
import dialogs  # noqa: E402
import md2html  # noqa: E402
import secretstore  # noqa: E402

CREDS = {
    "username": "example-blog",
    "blog": "example-blog",
    "token": "SECRET-TOKEN-123",
    "endpoint": "https://rpc.cnblogs.com/metaweblog/example-blog",
}


def ns(**kwargs):
    data = {
        "json": True,
        "blogid": None,
        "timeout": None,
        "title": None,
        "tags": None,
        "categories": None,
        "slug": None,
        "summary": None,
        "format": "auto",
        "keep_h1": False,
        "upload_images": False,
        "draft": None,
        "ai_generated": False,
    }
    data.update(kwargs)
    return argparse.Namespace(**data)


class MarkdownTests(unittest.TestCase):
    def test_basics(self):
        html = md2html.markdown_to_html(
            "# 标题\n\n你好 **世界**，`code`。\n\n- a\n  - b\n- c\n\n"
            "| A | B |\n|---|:---:|\n| 1 | 2 |\n\n> 引用\n\n```python\nprint(1)\n```\n"
        )
        self.assertIn("<h1>标题</h1>", html)
        self.assertIn("<strong>世界</strong>", html)
        self.assertIn("<code>code</code>", html)
        self.assertIn("<ul>", html)
        self.assertIn("<table>", html)
        self.assertIn('class="language-python"', html)
        self.assertNotIn("**", html)

    def test_html_escape(self):
        html = md2html.markdown_to_html("a < b & c\n")
        self.assertIn("a &lt; b &amp; c", html)

    def test_links_and_images(self):
        html = md2html.markdown_to_html(
            "[官网](https://example.com \"标题\") ![](./img/a.png)"
        )
        self.assertIn('<a href="https://example.com" title="标题">官网</a>', html)
        self.assertIn('<img src="./img/a.png" alt="" />', html)


class FrontmatterTests(unittest.TestCase):
    def test_parse_and_title(self):
        meta, body = cnblogs.parse_frontmatter(
            "---\n"
            "title: 我的标题\n"
            "tags: [a, b]\n"
            "categories:\n  - .NET\n  - 随笔\n"
            "draft: true\n"
            "---\n\n# 另一个标题\n\n正文\n"
        )
        self.assertEqual(meta["title"], "我的标题")
        self.assertEqual(meta["tags"], ["a", "b"])
        self.assertEqual(meta["categories"], [".NET", "随笔"])
        self.assertIs(meta["draft"], True)
        title, cleaned = cnblogs.choose_title(body, meta["title"])
        self.assertEqual(title, "我的标题")
        self.assertNotIn("# 另一个标题", cleaned)

    def test_choose_title_from_h1(self):
        title, cleaned = cnblogs.choose_title("# 标题\n\n正文\n", None)
        self.assertEqual(title, "标题")
        self.assertTrue(cleaned.startswith("正文"))


class DraftTests(unittest.TestCase):
    def test_flags(self):
        self.assertTrue(cnblogs._draft_flag(ns(draft=True), {}))
        self.assertFalse(cnblogs._draft_flag(ns(draft=False), {"draft": True}))
        self.assertTrue(cnblogs._draft_flag(ns(draft=None), {"draft": "true"}))
        self.assertTrue(cnblogs._draft_flag(ns(draft=None), {"publish": False}))
        self.assertFalse(cnblogs._draft_flag(ns(draft=None), {"publish": True}))
        self.assertTrue(cnblogs._draft_flag(ns(draft=None), {}))
        self.assertFalse(cnblogs._draft_flag(ns(draft=None), {"publish": True}))

    def test_private_alias_sets_non_public(self):
        parser = cnblogs.build_parser()
        args = parser.parse_args(["publish", "post.md", "--private", "--json"])
        self.assertTrue(args.draft)
        args = parser.parse_args(["publish", "post.md", "--public", "--json"])
        self.assertFalse(args.draft)
        args = parser.parse_args(["publish", "post.md", "--no-ai-generated", "--json"])
        self.assertFalse(args.ai_generated)
        args = parser.parse_args(["publish", "post.md", "--ai-generated", "--json"])
        self.assertTrue(args.ai_generated)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cnblogs.default_config()
        self.original_rpc = cnblogs.rpc
        self.original_upload = cnblogs.upload_file

        def fake_rpc(cfg, creds, method, params, timeout=None):
            if method == "metaWeblog.getCategories":
                return [{"title": "随笔"}, {"title": "[Markdown]"}]
            raise AssertionError(method)

        cnblogs.rpc = fake_rpc
        cnblogs.upload_file = lambda cfg, creds, path: "https://img.example/" + path.name

    def tearDown(self):
        cnblogs.rpc = self.original_rpc
        cnblogs.upload_file = self.original_upload
        cnblogs._SECRETS.clear()

    def test_markdown_adds_category(self):
        prepared = cnblogs._prepare(
            ns(), self.cfg, CREDS, {"title": "标题"}, "正文", Path("/tmp")
        )
        self.assertEqual(prepared["format"], "markdown")
        self.assertIn("[Markdown]", prepared["categories"])
        self.assertEqual(prepared["post"]["description"], "正文")
        self.assertNotIn("mt_convert_breaks", prepared["post"])

    def test_html_removes_category(self):
        prepared = cnblogs._prepare(
            ns(format="html", categories=".NET,[Markdown]"), self.cfg, CREDS,
            {"title": "标题"}, "## 小标题\n", Path("/tmp"),
        )
        self.assertNotIn("[Markdown]", prepared["categories"])
        self.assertIn("<h2>小标题</h2>", prepared["post"]["description"])

    def test_image_upload_replaces_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            image = tmp_path / "a.png"
            image.write_bytes(b"\x89PNG\r\n")
            article = "见图 ![](a.png)"
            prepared = cnblogs._prepare(
                ns(upload_images=True), self.cfg, CREDS,
                {"title": "标题"}, article, tmp_path,
            )
            self.assertIn("(https://img.example/a.png)", prepared["post"]["description"])


    def test_ai_declaration_default(self):
        prepared = cnblogs._prepare(
            ns(ai_generated=None), self.cfg, CREDS,
            {"title": "标题"}, "正文", Path("/tmp"),
        )
        self.assertTrue(prepared["ai_generated"])
        self.assertTrue(prepared["ai_declaration_added"])
        self.assertIn(cnblogs.AI_DECLARATION_MARKER, prepared["post"]["description"])

    def test_ai_declaration_can_be_disabled(self):
        prepared = cnblogs._prepare(
            ns(ai_generated=False), self.cfg, CREDS,
            {"title": "标题"}, "正文", Path("/tmp"),
        )
        self.assertFalse(prepared["ai_generated"])
        self.assertFalse(prepared["ai_declaration_added"])
        self.assertNotIn(cnblogs.AI_DECLARATION_MARKER, prepared["post"]["description"])

    def test_ai_declaration_is_not_duplicated(self):
        marker = cnblogs.AI_DECLARATION_MD
        prepared = cnblogs._prepare(
            ns(ai_generated=None), self.cfg, CREDS,
            {"title": "标题"}, marker + "正文", Path("/tmp"),
        )
        self.assertTrue(prepared["ai_generated"])
        self.assertFalse(prepared["ai_declaration_added"])
        self.assertEqual(prepared["post"]["description"].count(cnblogs.AI_DECLARATION_MARKER), 1)

    def test_category_validation_warns(self):
        prepared = cnblogs._prepare(
            ns(categories="不存在的分类"), self.cfg, CREDS,
            {"title": "标题"}, "正文", Path("/tmp"),
        )
        self.assertTrue(any("不存在" in w for w in prepared["warnings"]))


class CredentialStorageTests(unittest.TestCase):
    def test_credentials_roundtrip_has_no_plain_config(self):
        captured = {}

        def fake_store(service, account, secret):
            captured["service"] = service
            captured["account"] = account
            captured["secret"] = secret

        original_store = cnblogs.secret_store_set
        original_get = cnblogs.secret_store_get
        cnblogs.secret_store_set = fake_store
        try:
            cnblogs.store_credentials("user", "blogname", "tok-123")
            self.assertEqual(captured["service"], "cnblogs-publish")
            self.assertEqual(captured["account"], "credentials")
            blob = json.loads(captured["secret"])
            self.assertEqual(blob["username"], "user")
            self.assertEqual(blob["token"], "tok-123")

            cnblogs.secret_store_get = lambda service, account: captured["secret"]
            creds = cnblogs.load_credentials()
            self.assertEqual(creds["blog"], "blogname")
            self.assertEqual(creds["endpoint"], "https://rpc.cnblogs.com/metaweblog/blogname")
        finally:
            cnblogs.secret_store_set = original_store
            cnblogs.secret_store_get = original_get
            cnblogs._SECRETS.clear()

    def test_loads_legacy_keychain_service(self):
        blob = json.dumps({
            "username": "u", "blog": "b", "token": "tok",
            "endpoint": "https://rpc.cnblogs.com/metaweblog/b",
        })
        original = cnblogs.secret_store_get
        cnblogs.secret_store_get = lambda service, account: blob if service == "cnblogs-metaweblog" else None
        try:
            with mock.patch.object(cnblogs.sys, "platform", "darwin"):
                creds = cnblogs.load_credentials()
            self.assertEqual(creds["blog"], "b")
            self.assertEqual(cnblogs._CREDENTIAL_SOURCE, "cnblogs-metaweblog")
        finally:
            cnblogs.secret_store_get = original
            cnblogs._CREDENTIAL_SOURCE = None
            cnblogs._SECRETS.clear()

    def test_missing_backend_becomes_user_error(self):
        original = cnblogs.secret_store_get
        cnblogs.secret_store_get = mock.Mock(
            side_effect=secretstore.SecretStoreError("no backend")
        )
        try:
            with self.assertRaises(cnblogs.UserError) as ctx:
                cnblogs.load_credentials()
            self.assertEqual(ctx.exception.code, cnblogs.EXIT_SECRET_STORE)
        finally:
            cnblogs.secret_store_get = original
            cnblogs._SECRETS.clear()

    def test_default_config_has_no_credentials(self):
        cfg = cnblogs.default_config()
        for key in ("username", "blog", "endpoint", "token", "password"):
            self.assertNotIn(key, cfg)


class PostUrlTests(unittest.TestCase):
    def test_fallback_uses_canonical_extensionless_url(self):
        url = cnblogs._post_url(dict(CREDS), "123456", None)
        self.assertEqual(url, "https://www.cnblogs.com/example-blog/p/123456")

    def test_server_link_is_kept_as_is(self):
        post = {"link": "https://www.cnblogs.com/example-blog/p/123456"}
        url = cnblogs._post_url(dict(CREDS), "123456", post)
        self.assertEqual(url, "https://www.cnblogs.com/example-blog/p/123456")


class PublishFlowTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.original = (
            cnblogs.rpc, cnblogs.require_credentials, cnblogs.upload_file,
        )

        def fake_rpc(cfg, creds, method, params, timeout=None):
            self.calls.append((method, params, creds))
            if method == "metaWeblog.getCategories":
                return [{"title": "随笔"}, {"title": "[Markdown]"}]
            if method == "metaWeblog.newPost":
                return "123456"
            if method == "metaWeblog.getPost":
                return {"postid": "123456", "link": "https://www.cnblogs.com/example-blog/p/123456"}
            raise AssertionError("unexpected method " + method)

        cnblogs.rpc = fake_rpc
        cnblogs.require_credentials = lambda: dict(CREDS)
        cnblogs.upload_file = lambda cfg, creds, path: "https://img.example/" + path.name

    def tearDown(self):
        cnblogs.rpc, cnblogs.require_credentials, cnblogs.upload_file = self.original
        cnblogs._SECRETS.clear()

    def test_publish_json_never_contains_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            article = Path(tmp) / "post.md"
            article.write_text(
                "---\n"
                "title: 测试文章\n"
                "tags: [tag1, tag2]\n"
                "categories: 随笔\n"
                "---\n\n"
                "正文 ![](a.png)\n",
                encoding="utf-8",
            )
            (Path(tmp) / "a.png").write_bytes(b"PNG")
            args = ns(file=str(article), upload_images=True, draft=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cnblogs.cmd_publish(args, cnblogs.default_config(), Path("/tmp/does-not-exist.json"))
            output = buffer.getvalue()
            self.assertNotIn("SECRET-TOKEN-123", output)
            self.assertNotIn("example-blog", json.dumps(
                {k: v for k, v in json.loads(output).items() if k != "url"}, ensure_ascii=False))
            payload = json.loads(output)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["postid"], "123456")
            self.assertEqual(payload["url"], "https://www.cnblogs.com/example-blog/p/123456")
            self.assertIn("[Markdown]", payload["categories"])
            self.assertTrue(payload["review_possible"])
            self.assertEqual(payload["visibility"], "public")
            self.assertIn("人工审核", payload["visibility_note"])

            new_post = [c for c in self.calls if c[0] == "metaWeblog.newPost"][0][1]
            self.assertEqual(new_post[0], "cnblogs")
            self.assertEqual(new_post[1], "example-blog")
            self.assertEqual(new_post[2], "SECRET-TOKEN-123")
            self.assertEqual(new_post[3]["title"], "测试文章")
            self.assertIn("[Markdown]", new_post[3]["categories"])
            self.assertTrue(new_post[4])  # publish
            self.assertIn("https://img.example/a.png", new_post[3]["description"])



    def test_default_publish_is_non_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            article = Path(tmp) / "post.md"
            article.write_text("---\ntitle: 默认私有\n---\n\n正文\n", encoding="utf-8")
            args = ns(file=str(article), upload_images=False, ai_generated=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cnblogs.cmd_publish(args, cnblogs.default_config(), Path("/tmp/none.json"))
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["draft"])
            self.assertEqual(payload["visibility"], "non_public")
            self.assertFalse(payload["review_possible"])
            self.assertIn("不进入审核", payload["visibility_note"])
            new_post = [c for c in self.calls if c[0] == "metaWeblog.newPost"][0][1]
            self.assertFalse(new_post[4])  # publish=false by default

    def test_draft_does_not_claim_public_visibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            article = Path(tmp) / "draft.md"
            article.write_text("---\ntitle: 草稿标题\ndraft: true\n---\n\n正文\n", encoding="utf-8")
            args = ns(file=str(article), upload_images=False, ai_generated=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cnblogs.cmd_publish(args, cnblogs.default_config(), Path("/tmp/none.json"))
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["review_possible"])
            self.assertEqual(payload["visibility"], "non_public")
            self.assertIn("不进入审核", payload["visibility_note"])
            self.assertIn("安全保存", payload["visibility_note"])

    def test_error_output_redacts_token(self):
        cnblogs.register_secret("SECRET-TOKEN-123")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cnblogs._fail("boom SECRET-TOKEN-123", 3, ns())
        self.assertEqual(code, 3)
        self.assertNotIn("SECRET-TOKEN-123", buffer.getvalue())
        self.assertIn("***", buffer.getvalue())


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.original = (cnblogs.rpc, cnblogs.require_credentials)

        def fake_rpc(cfg, creds, method, params, timeout=None):
            self.calls.append((method, params))
            if method == "metaWeblog.getPost":
                return {
                    "postid": "42",
                    "title": "旧标题",
                    "description": "旧正文",
                    "categories": ["随笔"],
                    "mt_keywords": "旧标签",
                    "wp_slug": "",
                    "mt_excerpt": "",
                    "link": "https://www.cnblogs.com/example-blog/p/42",
                }
            if method == "metaWeblog.getCategories":
                return [{"title": "随笔"}, {"title": "[Markdown]"}]
            if method == "metaWeblog.editPost":
                return True
            raise AssertionError(method)

        cnblogs.rpc = fake_rpc
        cnblogs.require_credentials = lambda: dict(CREDS)

    def tearDown(self):
        cnblogs.rpc, cnblogs.require_credentials = self.original
        cnblogs._SECRETS.clear()


    def test_update_default_is_non_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            article = Path(tmp) / "update.md"
            article.write_text("新的正文内容。\n", encoding="utf-8")
            args = ns(postid="42", file=str(article), upload_images=False,
                      ai_generated=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cnblogs.cmd_update(args, cnblogs.default_config(), Path("/tmp/none.json"))
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["draft"])
            self.assertEqual(payload["visibility"], "non_public")
            self.assertFalse(payload["review_possible"])
            edit = [c for c in self.calls if c[0] == "metaWeblog.editPost"][0][1]
            self.assertFalse(edit[4])

    def test_update_preserves_metadata_and_ensures_markdown_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            article = Path(tmp) / "update.md"
            article.write_text("新的正文内容。\n", encoding="utf-8")
            args = ns(postid="42", file=str(article), upload_images=False,
                      draft=False, ai_generated=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cnblogs.cmd_update(args, cnblogs.default_config(), Path("/tmp/none.json"))
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "update")
            self.assertEqual(payload["title"], "旧标题")
            edit = [c for c in self.calls if c[0] == "metaWeblog.editPost"][0][1]
            self.assertEqual(edit[0], "42")
            self.assertIn("随笔", edit[3]["categories"])
            self.assertIn("[Markdown]", edit[3]["categories"])
            self.assertEqual(edit[3]["mt_keywords"], "旧标签")
            self.assertEqual(edit[3]["description"], "新的正文内容。\n")
            self.assertTrue(edit[4])
            self.assertTrue(payload["review_possible"])
            self.assertEqual(payload["visibility"], "public")
            self.assertIn("人工审核", payload["visibility_note"])


class RpcIntegrationTests(unittest.TestCase):
    def test_xmlrpc_roundtrip(self):
        received = {}

        class Handler:
            def newPost(self, blogid, username, password, post, publish):
                received.update(locals())
                return "999"

        server = xmlrpc.server.SimpleXMLRPCServer(("127.0.0.1", 0), logRequests=False, allow_none=False)
        server.register_function(Handler().newPost, "metaWeblog.newPost")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cfg = cnblogs.default_config()
            creds = dict(CREDS)
            creds["endpoint"] = "http://127.0.0.1:%d/" % server.server_address[1]
            result = cnblogs.rpc(
                cfg, creds, "metaWeblog.newPost",
                ["cnblogs", "user", "tok", {"title": "t", "description": "d"}, True],
            )
            self.assertEqual(result, "999")
            self.assertEqual(received["blogid"], "cnblogs")
            self.assertEqual(received["post"]["title"], "t")
        finally:
            server.shutdown()
            server.server_close()

    def test_http_non_loopback_rejected(self):
        with self.assertRaises(cnblogs.UserError):
            cnblogs.validate_endpoint("http://example.com/metaweblog/x")


class SecurityScrubTests(unittest.TestCase):
    def test_scrub_masks_credentials_email_and_url(self):
        original = cnblogs.load_credentials
        cnblogs.load_credentials = lambda: dict(CREDS)
        try:
            text = (
                "user example-blog token SECRET-TOKEN-123 mail a@b.com "
                "https://rpc.cnblogs.com/metaweblog/example-blog"
            )
            scrubbed = cnblogs._scrub_identity(text)
            for secret in ("example-blog", "SECRET-TOKEN-123", "a@b.com"):
                self.assertNotIn(secret, scrubbed)
            self.assertNotIn("metaweblog/example-blog", scrubbed)
            self.assertIn("***", scrubbed)
        finally:
            cnblogs.load_credentials = original
            cnblogs._SECRETS.clear()

    def test_blog_name_rejects_email_and_url(self):
        for bad in ("a@b.com", "https://www.cnblogs.com/x", "a/b", "a b"):
            with self.assertRaises(cnblogs.UserError):
                cnblogs.validate_blog_name(bad)
        self.assertEqual(cnblogs.validate_blog_name("example-blog"), "example-blog")


class CheckOutputTests(unittest.TestCase):
    def test_check_output_has_no_identity(self):
        original_rpc = cnblogs.rpc
        original_creds = cnblogs.require_credentials
        cnblogs.rpc = lambda cfg, creds, method, params, timeout=None: [
            {"blogid": "cnblogs", "blogName": "example-blog", "url": "https://www.cnblogs.com/example-blog"}
        ]
        cnblogs.require_credentials = lambda: dict(CREDS)
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cnblogs.cmd_check(ns(), cnblogs.default_config(), Path("/tmp/none.json"))
            data = json.loads(buffer.getvalue())
            self.assertEqual(data["blog_count"], 1)
            self.assertNotIn("example-blog", buffer.getvalue())
            self.assertNotIn("blogs", data)
        finally:
            cnblogs.rpc = original_rpc
            cnblogs.require_credentials = original_creds
            cnblogs._SECRETS.clear()


class SecretStoreTests(unittest.TestCase):

    def test_selects_macos_backend(self):
        fake = object()
        with mock.patch.object(secretstore.sys, "platform", "darwin"), \
             mock.patch.object(secretstore, "_MacKeychain", return_value=fake), \
             mock.patch.object(secretstore, "_BACKEND", None):
            self.assertIs(secretstore._get_backend(), fake)

    def test_selects_windows_backend(self):
        fake = object()
        with mock.patch.object(secretstore.sys, "platform", "win32"), \
             mock.patch.object(secretstore, "_WindowsCredentialManager", return_value=fake), \
             mock.patch.object(secretstore, "_BACKEND", None):
            self.assertIs(secretstore._get_backend(), fake)

    def test_selects_linux_secret_tool_backend(self):
        fake = object()
        with mock.patch.object(secretstore.sys, "platform", "linux"), \
             mock.patch.object(secretstore.os, "name", "posix"), \
             mock.patch.object(secretstore.shutil, "which", return_value="/usr/bin/secret-tool"), \
             mock.patch.object(secretstore, "_LinuxSecretTool", return_value=fake), \
             mock.patch.object(secretstore, "_BACKEND", None):
            self.assertIs(secretstore._get_backend(), fake)

    def test_selects_linux_keyring_backend(self):
        fake = object()
        with mock.patch.object(secretstore.sys, "platform", "linux"), \
             mock.patch.object(secretstore.os, "name", "posix"), \
             mock.patch.object(secretstore.shutil, "which", return_value=None), \
             mock.patch.object(secretstore, "_LinuxKeyring", return_value=fake), \
             mock.patch.object(secretstore, "_BACKEND", None):
            self.assertIs(secretstore._get_backend(), fake)

    def test_windows_target_name(self):
        self.assertEqual(
            secretstore.windows_target("cnblogs-publish", "credentials"),
            "cnblogs-publish:credentials",
        )

    def test_linux_secret_tool_roundtrip_uses_stdin(self):
        backend = secretstore._LinuxSecretTool("/usr/bin/secret-tool")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[1] == "lookup":
                return subprocess.CompletedProcess(cmd, 0, "secret-json\n", "")
            if cmd[1] == "store":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[1] == "clear":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(cmd)

        with mock.patch.object(secretstore.subprocess, "run", side_effect=fake_run):
            self.assertEqual(backend.get("svc", "acct"), "secret-json")
            backend.set("svc", "acct", "secret-json")
            self.assertTrue(backend.delete("svc", "acct"))

        store_call = [c for c in calls if c[0][1] == "store"][0]
        self.assertEqual(store_call[1]["input"], "secret-json")
        self.assertNotIn("secret-json", store_call[0])


    def test_linux_secret_tool_real_subprocess(self):
        if os.name == "nt":
            self.skipTest("POSIX shell helper not available on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store.txt"
            script = Path(tmp) / "secret-tool"
            script.write_text(
                "#!/bin/sh\n"
                "D=\"$(dirname \"$0\")/store.txt\"\n"
                "case \"$1\" in\n"
                "  store) cat > \"$D\" ;;\n"
                "  lookup) [ -f \"$D\" ] && cat \"$D\" || exit 1 ;;\n"
                "  clear) rm -f \"$D\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            os.chmod(script, 0o700)
            backend = secretstore._LinuxSecretTool(str(script))
            backend.set("svc", "acct", "secret-json")
            self.assertTrue(store.exists())
            self.assertEqual(backend.get("svc", "acct"), "secret-json")
            self.assertTrue(backend.delete("svc", "acct"))
            self.assertIsNone(backend.get("svc", "acct"))

    def test_backend_name_on_current_platform(self):
        if sys.platform == "darwin":
            self.assertEqual(secretstore.secret_store_backend_name(), "macos-keychain")
        elif os.name == "nt":
            self.assertEqual(
                secretstore.secret_store_backend_name(), "windows-credential-manager"
            )

    def test_linux_secret_tool_missing_and_error(self):
        backend = secretstore._LinuxSecretTool("/usr/bin/secret-tool")
        with mock.patch.object(
            secretstore.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 1, "", ""),
        ):
            self.assertIsNone(backend.get("svc", "acct"))
        with mock.patch.object(
            secretstore.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 2, "", "dbus unavailable"),
        ):
            with self.assertRaises(secretstore.SecretStoreError):
                backend.get("svc", "acct")


class WindowsCredentialManagerTests(unittest.TestCase):
    def test_write_and_delete_use_generic_credential(self):
        captured = {}

        class FakeAdvapi:
            pass

        fake = FakeAdvapi()
        fake.CredReadW = lambda *args: 0
        fake.CredDeleteW = lambda *args: 1
        fake.CredFree = lambda ptr: None

        def write(cred_ptr, flags):
            cred = cred_ptr._obj
            captured["target"] = cred.TargetName
            captured["user"] = cred.UserName
            captured["blob"] = ctypes.string_at(
                cred.CredentialBlob, cred.CredentialBlobSize
            )
            return 1

        fake.CredWriteW = write
        with mock.patch.object(ctypes, "WinDLL", lambda *a, **k: fake, create=True):
            backend = secretstore._WindowsCredentialManager()
            backend.set("cnblogs-publish", "credentials", "secret-json")
            self.assertTrue(backend.delete("cnblogs-publish", "credentials"))
        self.assertEqual(captured["target"], "cnblogs-publish:credentials")
        self.assertEqual(captured["blob"], b"secret-json")



    def test_read_missing_credential_returns_none(self):
        class FakeAdvapi:
            pass

        fake = FakeAdvapi()
        fake.CredReadW = lambda *args: 0
        fake.CredWriteW = lambda *args: 0
        fake.CredDeleteW = lambda *args: 0
        fake.CredFree = lambda ptr: None
        with mock.patch.object(ctypes, "WinDLL", lambda *a, **k: fake, create=True), \
             mock.patch.object(ctypes, "get_last_error", return_value=1168, create=True):
            backend = secretstore._WindowsCredentialManager()
            self.assertIsNone(backend.get("cnblogs-publish", "credentials"))

class DialogTests(unittest.TestCase):
    def test_macos_secret_dialog_uses_hidden_answer(self):
        with mock.patch.object(
            dialogs, "_run",
            return_value=subprocess.CompletedProcess([], 0, "tok\n", ""),
        ) as run:
            value = dialogs._macos_prompt("prompt", "title", True, "")
        self.assertEqual(value, "tok")
        script = run.call_args[0][0][-1]
        self.assertIn("with hidden answer", script)

    def test_windows_dialog_passes_secret_by_env(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(cmd, 0, "tok", "")

        with mock.patch.object(dialogs.shutil, "which", return_value="/usr/bin/powershell"), \
             mock.patch.object(dialogs, "_run", side_effect=fake_run):
            value = dialogs._windows_prompt("prompt", "title", True, "")
        self.assertEqual(value, "tok")
        self.assertEqual(captured["env"]["CNBLOGS_PROMPT_SECRET"], "1")
        self.assertNotIn("tok", captured["cmd"])

    def test_linux_zenity_uses_password_mode(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "tok\n", "")

        with mock.patch.dict(dialogs.os.environ, {"DISPLAY": ":0"}, clear=False), \
             mock.patch.object(dialogs.shutil, "which", return_value="/usr/bin/zenity"), \
             mock.patch.object(dialogs, "_run", side_effect=fake_run):
            value = dialogs._linux_prompt("prompt", "title", True, "")
        self.assertEqual(value, "tok")
        self.assertIn("--password", calls[0])

    def test_ask_secret_rejects_whitespace(self):
        with mock.patch.object(dialogs, "_ask", return_value="a b"):
            with self.assertRaises(dialogs.DialogError):
                dialogs.ask_secret("prompt", "title")

    def test_dispatch_to_macos_helper(self):
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False
        with mock.patch.object(dialogs.sys, "stdin", fake_stdin), \
             mock.patch.object(dialogs.sys, "platform", "darwin"), \
             mock.patch.object(dialogs, "_macos_prompt", return_value="x") as helper:
            self.assertEqual(dialogs._ask("prompt", "title", False, ""), "x")
            helper.assert_called_once()

    def test_dispatch_to_windows_helper(self):
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False
        with mock.patch.object(dialogs.sys, "stdin", fake_stdin), \
             mock.patch.object(dialogs.sys, "platform", "win32"), \
             mock.patch.object(dialogs, "_windows_prompt", return_value="x") as helper:
            self.assertEqual(dialogs._ask("prompt", "title", False, ""), "x")
            helper.assert_called_once()

    def test_dispatch_to_linux_helper(self):
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False
        with mock.patch.object(dialogs.sys, "stdin", fake_stdin), \
             mock.patch.object(dialogs.sys, "platform", "linux"), \
             mock.patch.object(dialogs.os, "name", "posix"), \
             mock.patch.object(dialogs, "_linux_prompt", return_value="x") as helper:
            self.assertEqual(dialogs._ask("prompt", "title", False, ""), "x")
            helper.assert_called_once()


class StatusTests(unittest.TestCase):
    def test_status_never_reads_credentials(self):
        original = cnblogs.load_credentials
        cnblogs.load_credentials = lambda: None
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cnblogs.cmd_status(ns(), cnblogs.default_config(), Path("/tmp/nonexistent-config.json"))
            data = json.loads(buffer.getvalue())
            self.assertFalse(data["credentials_present"])
            self.assertNotIn("username", data)
            self.assertNotIn("blog", data)
        finally:
            cnblogs.load_credentials = original


if __name__ == "__main__":
    unittest.main()