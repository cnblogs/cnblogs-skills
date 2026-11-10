#!/usr/bin/env python3
"""Secure one-command publisher for cnblogs (博客园) via the MetaWeblog XML-RPC API.

Security model
--------------
The **username, blog name and access token** are all treated as private
credentials.  They are stored together as one encrypted item in the platform
secret store (macOS Keychain / Windows Credential Manager / Linux Secret
Service or keyring; service ``cnblogs-publish``, account ``credentials``) and
are only ever unwrapped inside this process.  They are:

  * never written to the config file (the config holds preferences only),
  * never accepted on the command line / stdin / environment,
  * never printed; all output is scrubbed.

``setup`` collects them through a native dialog on macOS, Windows or Linux
(the token in a hidden field), so they never pass through the chat transcript.

Run ``python3 cnblogs.py <command> --help`` for details.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import mimetypes
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import xmlrpc.client
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import md2html  # noqa: E402  (local module next to this file)
from dialogs import DialogError, ask_secret, ask_text  # noqa: E402
from secretstore import (  # noqa: E402
    SecretStoreError,
    secret_store_backend_name,
    secret_store_delete,
    secret_store_get,
    secret_store_set,
)

SERVICE_NAME = "cnblogs-publish"
LEGACY_SERVICE_NAMES = ("cnblogs-metaweblog",)
CREDENTIAL_ACCOUNT = "credentials"
DEFAULT_BLOGID = "cnblogs"
DEFAULT_MD_CATEGORY = "[Markdown]"
DEFAULT_ENDPOINT = "https://rpc.cnblogs.com/metaweblog/{blog}"
REVIEW_NOTE = (
    "博客园可能对公开博文进行人工审核；审核通过前文章可能不会出现在首页或公网，"
    "请勿用首页/404 判断失败，也不要重复发布。可用 `get <postid>` 确认 API 侧已保存。"
)
DRAFT_NOTE = (
    "这是非公开发布（草稿/私有），不进入审核，可立即安全保存；"
    "不会出现在首页或公网，确认后再用 update --publish 发布。"
)
AI_DECLARATION_MARKER = "AI 生成声明"
AI_DECLARATION_MD = (
    "> **AI 生成声明**：本文内容由 AI 生成或经 AI 辅助完成，请注意甄别。\n\n"
)
SKILL_ROOT = SCRIPT_DIR.parent
DEFAULT_CONFIG_PATH = SKILL_ROOT / "config.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_FAULT = 3
EXIT_NETWORK = 4
EXIT_SECRET_STORE = 5
EXIT_SECRET_STORE = EXIT_SECRET_STORE  # backward-compatible alias

# Collected secrets are scrubbed from every rendered message.
_SECRETS: list[str] = []


class UserError(Exception):
    def __init__(self, message: str, code: int = EXIT_USAGE):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# secret hygiene
# ---------------------------------------------------------------------------

def register_secret(value: str | None) -> None:
    if value and value not in _SECRETS:
        _SECRETS.append(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, "***")
    return text


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value


def jsonable(value: Any) -> Any:
    if isinstance(value, xmlrpc.client.DateTime):
        return value.value
    if isinstance(value, xmlrpc.client.Binary):
        return base64.b64encode(value.data).decode("ascii")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# config (preferences only -- never credentials)
# ---------------------------------------------------------------------------

def default_config() -> dict[str, Any]:
    return {
        "blogid": DEFAULT_BLOGID,
        "markdown_category": DEFAULT_MD_CATEGORY,
        "format": "markdown",
        "upload_images": True,
        "validate_categories": True,
        "ai_generated": True,
        "timeout": 30,
    }


def load_config(path: Path) -> dict[str, Any]:
    cfg = default_config()
    if not path.exists():
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UserError("配置文件无法读取：%s（%s）" % (path, exc))
    if not isinstance(raw, dict):
        raise UserError("配置文件格式不正确：%s" % path)
    for key, value in raw.items():
        if key in cfg:
            cfg[key] = value
    return cfg


def save_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except (OSError, NotImplementedError):
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def resolve_config(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(path)
    blogid = getattr(args, "blogid", None)
    timeout = getattr(args, "timeout", None)
    if blogid:
        cfg["blogid"] = blogid.strip()
    if timeout:
        cfg["timeout"] = int(timeout)
    return cfg


# ---------------------------------------------------------------------------
# credentials (username + blog + endpoint + token in one encrypted item)
# ---------------------------------------------------------------------------

_CREDENTIAL_SOURCE: str | None = None


def _read_credentials_raw() -> tuple[str | None, str | None]:
    try:
        raw = secret_store_get(SERVICE_NAME, CREDENTIAL_ACCOUNT)
    except SecretStoreError as exc:
        raise UserError(str(exc), EXIT_SECRET_STORE)
    if raw:
        return raw, SERVICE_NAME
    # The legacy service name only exists on macOS (old skill versions).
    if sys.platform == "darwin":
        for legacy in LEGACY_SERVICE_NAMES:
            try:
                raw = secret_store_get(legacy, CREDENTIAL_ACCOUNT)
            except SecretStoreError:
                continue
            if raw:
                return raw, legacy
    return None, None


def _delete_legacy_credentials() -> None:
    if sys.platform != "darwin":
        return
    for legacy in LEGACY_SERVICE_NAMES:
        try:
            secret_store_delete(legacy, CREDENTIAL_ACCOUNT)
        except SecretStoreError:
            pass


def load_credentials() -> dict[str, str] | None:
    global _CREDENTIAL_SOURCE
    raw, source = _read_credentials_raw()
    _CREDENTIAL_SOURCE = source
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        raise UserError("凭据存储中的凭据已损坏，请运行 setup --new-token 重新保存。", EXIT_SECRET_STORE)
    if not isinstance(data, dict):
        raise UserError("凭据存储中的凭据格式不正确，请重新运行 setup。", EXIT_SECRET_STORE)
    username = str(data.get("username") or "").strip()
    blog = str(data.get("blog") or "").strip()
    token = str(data.get("token") or "").strip()
    endpoint = str(data.get("endpoint") or "").strip() or DEFAULT_ENDPOINT.format(blog=blog)
    if not username or not blog or not token:
        raise UserError("凭据存储中的凭据不完整，请运行 setup --new-token 重新保存。", EXIT_SECRET_STORE)
    if re.search(r"\s", token):
        raise UserError("凭据存储中的令牌格式异常，请运行 setup --new-token 更新。", EXIT_SECRET_STORE)
    register_secret(token)
    return {
        "username": username,
        "blog": blog,
        "token": token,
        "endpoint": endpoint,
    }


def store_credentials(username: str, blog: str, token: str) -> None:
    endpoint = DEFAULT_ENDPOINT.format(blog=blog)
    payload = json.dumps(
        {"username": username, "blog": blog, "token": token, "endpoint": endpoint},
        ensure_ascii=False,
        sort_keys=True,
    )
    try:
        secret_store_set(SERVICE_NAME, CREDENTIAL_ACCOUNT, payload)
    except SecretStoreError as exc:
        raise UserError(str(exc), EXIT_SECRET_STORE)
    register_secret(token)


def require_credentials() -> dict[str, str]:
    creds = load_credentials()
    if creds is None:
        raise UserError(
            "尚未配置博客园凭据。请先运行 setup：\n"
            "  python3 scripts/cnblogs.py setup\n"
            "用户名、博客名和访问令牌都会在系统对话框中由你本人输入，不会进入对话。"
        )
    validate_endpoint(creds["endpoint"])
    return creds


def validate_endpoint(endpoint: str) -> str:
    parts = urllib.parse.urlsplit(endpoint)
    if not parts.hostname:
        raise UserError("API 地址无效。")
    if parts.scheme == "https":
        return endpoint
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        return endpoint
    raise UserError("出于安全考虑只允许 HTTPS 的 API 地址（本地回环地址除外）。")


def validate_blog_name(blog: str) -> str:
    blog = (blog or "").strip()
    if not blog:
        raise UserError("博客名不能为空。")
    if re.search(r"[@/:\s]", blog) or blog.startswith("."):
        raise UserError(
            "博客名格式不正确：它应是博客首页地址里的那段（例如 your-blog-name），"
            "不是邮箱、昵称或完整网址。"
        )
    return blog


def _scrub_identity(text: str) -> str:
    """Remove credentials / URLs from error text before it reaches the model."""
    text = redact(text)
    try:
        creds = load_credentials()
    except Exception:
        creds = None
    if creds:
        for value in (creds.get("username"), creds.get("blog"), creds.get("token")):
            # Avoid mangling error text when a username/blog is a very short string.
            if value and len(value) >= 3:
                text = text.replace(value, "***")
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "***@***", text)
    text = re.sub(r"(https?://[^/\s]+/metaweblog/)[^\s\'\"<>]+", r"\1***", text)
    return text


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------

class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float, context: ssl.SSLContext):
        super().__init__(context=context)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def rpc(
    cfg: dict[str, Any],
    creds: dict[str, str],
    method: str,
    params: list[Any],
    timeout: int | None = None,
) -> Any:
    endpoint = validate_endpoint(creds["endpoint"])
    wait = float(timeout or cfg.get("timeout") or 30)
    if endpoint.startswith("https"):
        transport = _TimeoutSafeTransport(wait, ssl.create_default_context())
    else:
        transport = _TimeoutTransport(wait)
    proxy = xmlrpc.client.ServerProxy(
        endpoint, transport=transport, allow_none=False, use_builtin_types=True
    )
    target: Any = proxy
    for part in method.split("."):
        target = getattr(target, part)
    return target(*params)


def auth_params(cfg: dict[str, Any], creds: dict[str, str]) -> tuple[str, str, str]:
    return cfg.get("blogid") or DEFAULT_BLOGID, creds["username"], creds["token"]


# ---------------------------------------------------------------------------
# article parsing
# ---------------------------------------------------------------------------

def _scalar(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        return [_scalar(part) for part in value[1:-1].split(",") if part.strip()]
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            end = idx
            break
    if end is None:
        return {}, text
    meta: dict[str, Any] = {}
    current: str | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if match:
            key = match.group(1).lower()
            raw = match.group(2).strip()
            if raw == "":
                meta[key] = []
                current = key
            else:
                meta[key] = _scalar(raw)
                current = None
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and current:
            if not isinstance(meta.get(current), list):
                meta[current] = []
            meta[current].append(_scalar(item.group(1)))
    body = "\n".join(lines[end + 1:])
    return meta, body.lstrip("\n")


def choose_title(body: str, explicit: str | None, keep_h1: bool = False) -> tuple[str, str]:
    lines = body.split("\n")
    h1_index = None
    h1_text = ""
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        match = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
        if match:
            h1_index = idx
            h1_text = match.group(1).strip()
        break
    if h1_index is not None and not keep_h1:
        title = (explicit or "").strip() or h1_text
        del lines[h1_index]
        return title, "\n".join(lines).lstrip("\n")
    return (explicit or "").strip() or h1_text, body


def normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，]", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        parts = [str(value)]
    return [part.strip() for part in parts if part and part.strip()]


def ensure_md_category(categories: list[str], md_category: str) -> list[str]:
    if not any(cat.lower() == md_category.lower() for cat in categories):
        categories = categories + [md_category]
    return categories


def remove_md_category(categories: list[str], md_category: str) -> list[str]:
    return [cat for cat in categories if cat.lower() != md_category.lower()]


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "on", "1", "draft"):
            return True
        if low in ("false", "no", "off", "0", "publish"):
            return False
    return None


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------

_MD_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\(\s*(<[^>\n]*>|[^)\s]+)(\s+[\"']([^\"']*)[\"'])?\s*\)"
)
_HTML_IMAGE_RE = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*")([^"]+)(")', re.IGNORECASE)


def _looks_local(target: str) -> bool:
    if not target:
        return False
    if target.startswith(("data:", "#")):
        return False
    return not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", target)


def upload_file(cfg: dict[str, Any], creds: dict[str, str], path: Path) -> str:
    if not path.is_file():
        raise UserError("图片不存在：%s" % path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    blogid, username, token = auth_params(cfg, creds)
    result = rpc(
        cfg, creds,
        "metaWeblog.newMediaObject",
        [blogid, username, token, {"name": path.name, "type": mime, "bits": payload}],
    )
    if isinstance(result, dict):
        url = result.get("url") or result.get("link")
        if url:
            return str(url)
    raise UserError("图片上传接口没有返回 url：%s" % path.name)


def upload_images(
    description: str,
    base_dir: Path,
    format_name: str,
    uploader,
    warnings: list[str],
) -> str:
    cache: dict[Path, str] = {}

    def get_url(path: Path) -> str | None:
        resolved = path.resolve()
        if resolved not in cache:
            try:
                cache[resolved] = uploader(resolved)
            except Exception as exc:  # non-fatal: keep original reference
                warnings.append("图片上传失败，保留原引用：%s（%s）" % (path, exc))
                return None
        return cache.get(resolved)

    if format_name == "markdown":
        def replace_md(match: re.Match) -> str:
            alt = match.group(1)
            target = match.group(2)
            title = match.group(4)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            if not _looks_local(target):
                return match.group(0)
            path = Path(target)
            if not path.is_absolute():
                path = base_dir / path
            if not path.is_file():
                warnings.append("图片路径不存在，保留原引用：%s" % target)
                return match.group(0)
            url = get_url(path)
            if not url:
                return match.group(0)
            title_part = ' "%s"' % title if title else ""
            return "![%s](%s%s)" % (alt, url, title_part)

        return _MD_IMAGE_RE.sub(replace_md, description)

    def replace_html(match: re.Match) -> str:
        prefix, target, suffix = match.group(1), match.group(2), match.group(3)
        if not _looks_local(target):
            return match.group(0)
        path = Path(target)
        if not path.is_absolute():
            path = base_dir / path
        if not path.is_file():
            warnings.append("图片路径不存在，保留原引用：%s" % target)
            return match.group(0)
        url = get_url(path)
        if not url:
            return match.group(0)
        return prefix + url + suffix

    return _HTML_IMAGE_RE.sub(replace_html, description)


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

class Reporter:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def emit(self, payload: dict[str, Any], human_lines: list[str]) -> None:
        payload = redact_obj(payload)
        if self.as_json:
            print(json.dumps(jsonable(payload), ensure_ascii=False, indent=2))
        else:
            print(redact("\n".join(human_lines)))


def _post_url(creds: dict[str, str], postid: str, post: dict[str, Any] | None) -> str:
    if post:
        for key in ("link", "permalink", "url"):
            value = post.get(key)
            if value:
                return str(value)
    blog = creds.get("blog") or ""
    if blog:
        # cnblogs canonical URLs are extensionless; ".html" 301-redirects.
        return "https://www.cnblogs.com/%s/p/%s" % (blog, postid)
    return ""


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_setup(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    title = "博客园发布 Skill · 安全配置"
    existing = load_credentials()
    reconfigure = bool(args.reconfigure)

    if existing and not (args.new_token or reconfigure):
        username = existing["username"]
        blog = existing["blog"]
        token = existing["token"]
        changed = False
    else:
        ask_identity = existing is None or reconfigure
        if ask_identity:
            username = ask_text(
                "请输入博客园登录用户名（登录名，不是昵称）。只写入本机系统凭据库，不会进入对话。",
                title,
            ).strip()
            if not username:
                raise UserError("用户名为空，已取消。")
            blog_default = "" if "@" in username else username
            blog = ask_text(
                "请输入博客名（博客首页地址里的那段，例如 your-blog-name；不是邮箱）。"
                "只写入本机系统凭据库，不会进入对话。",
                title,
                default=blog_default,
            ).strip()
            if not blog:
                raise UserError("博客名为空，已取消。")
            blog = validate_blog_name(blog)
        else:
            username = existing["username"]
            blog = existing["blog"]

        if existing and not args.new_token and not reconfigure:
            token = existing["token"]
        else:
            token = ask_secret(
                "请输入博客园访问令牌（MetaWeblog Access Token）。"
                "令牌只写入本机系统凭据库，不会显示、不会进入对话。",
                title,
            )
        validate_endpoint(DEFAULT_ENDPOINT.format(blog=blog))
        store_credentials(username, blog, token)
        _delete_legacy_credentials()
        changed = True

    cfg_saved = False
    if not config_path.exists():
        save_config(config_path, cfg)
        cfg_saved = True

    creds = require_credentials()
    if _CREDENTIAL_SOURCE in LEGACY_SERVICE_NAMES:
        store_credentials(creds["username"], creds["blog"], creds["token"])
        _delete_legacy_credentials()
        changed = True
    auth_ok = False
    blog_count = 0
    auth_error = None
    if not args.skip_check:
        try:
            blogs = rpc(cfg, creds, "blogger.getUsersBlogs",
                        [cfg.get("blogid") or DEFAULT_BLOGID, creds["username"], creds["token"]])
            auth_ok = True
            blog_count = len(blogs or [])
        except xmlrpc.client.Fault as fault:
            auth_error = fault.faultString
        except Exception as exc:  # network etc: never echo URL/email
            auth_error = "%s（详细信息已隐藏以免泄露身份）" % type(exc).__name__

    payload = {
        "ok": True,
        "action": "setup",
        "credentials_saved": True,
        "credentials_changed": changed,
        "config_path": str(config_path),
        "config_saved": cfg_saved,
        "auth_ok": auth_ok,
        "blog_count": blog_count,
    }
    if auth_error:
        payload["error"] = auth_error
        payload["hint"] = (
            "凭据已加密保存在本机系统凭据库。请确认用户名/博客名/令牌后重新运行 "
            "setup --new-token（输入在系统对话框内完成）。"
        )
        raise UserError(redact("鉴权失败：%s" % auth_error), EXIT_FAULT)

    human = [
        "✅ 博客园凭据已加密保存到本机系统凭据库",
        "用户名 / 博客名 / 访问令牌：只在本地系统凭据库中，不写入配置文件、不进入对话",
        "鉴权：%s" % ("通过（可用博客 %d 个）" % blog_count if auth_ok else "已跳过"),
    ]
    if not config_path.exists():
        human.append("配置文件：%s" % config_path)
    Reporter(args.json).emit(payload, human)
    return payload


def cmd_status(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    credentials_present = False
    credential_error = None
    try:
        credentials_present = load_credentials() is not None
    except UserError as exc:
        credential_error = str(exc)
    payload = {
        "ok": True,
        "action": "status",
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "credential_service": SERVICE_NAME,
        "credential_backend": secret_store_backend_name(),
        "credentials_present": credentials_present,
        "credential_error": credential_error,
        "blogid": cfg.get("blogid") or DEFAULT_BLOGID,
        "format": cfg.get("format") or "markdown",
    }
    human = [
        "配置文件：%s" % ("已存在" if payload["config_exists"] else "未创建"),
        "凭据存储：%s" % payload["credential_backend"],
        "博客园凭据：%s" % ("已加密保存" if credentials_present else "未保存"),
        "说明：用户名、博客名、访问令牌均不在此处展示。",
    ]
    if credential_error:
        human.append("凭据读取：%s" % credential_error)
    Reporter(args.json).emit(payload, human)
    return payload


def _category_titles(cfg: dict[str, Any], creds: dict[str, str]) -> list[str]:
    blogid, username, token = auth_params(cfg, creds)
    try:
        categories = rpc(cfg, creds, "metaWeblog.getCategories", [blogid, username, token])
    except Exception:
        return []
    return [str(item.get("title") or item.get("categoryName") or "") for item in categories or []]


def _validate_categories(cfg, creds, categories, warnings, md_category):
    if not cfg.get("validate_categories", True):
        return
    known = _category_titles(cfg, creds)
    if not known:
        return
    known_lower = {title.lower() for title in known}
    for category in categories:
        if category.lower() == md_category.lower():
            continue
        if category.lower() not in known_lower:
            warnings.append("分类「%s」不存在，可能不会生效；可先在博客后台创建。" % category)


def _load_document(path_arg: str) -> tuple[dict[str, Any], str, Path]:
    path = Path(path_arg).expanduser()
    if not path.is_file():
        raise UserError("找不到文章文件：%s" % path)
    raw = path.read_text(encoding="utf-8-sig")
    meta, body = parse_frontmatter(raw)
    return meta, body, path.parent


def _draft_flag(args: argparse.Namespace, meta: dict[str, Any]) -> bool:
    if getattr(args, "draft", None) is not None:
        return bool(args.draft)
    if "draft" in meta:
        return bool(_as_bool(meta.get("draft")))
    if "publish" in meta:
        return _as_bool(meta.get("publish")) is False
    # Safe default: write non-public first; public requires explicit --publish.
    return True


def _prepare(
    args: argparse.Namespace,
    cfg: dict[str, Any],
    creds: dict[str, str],
    meta: dict[str, Any],
    body: str,
    base_dir: Path,
    fallback_title: str = "",
) -> dict[str, Any]:
    fmt = (getattr(args, "format", None) or meta.get("format") or cfg.get("format") or "markdown").lower()
    if fmt == "auto":
        fmt = str(meta.get("format") or cfg.get("format") or "markdown").lower()
    if fmt not in ("markdown", "html"):
        raise UserError("发布格式只能是 markdown 或 html。")

    explicit_title = getattr(args, "title", None) or meta.get("title")
    if not explicit_title and fallback_title:
        explicit_title = fallback_title
    title, body = choose_title(body, explicit_title, keep_h1=bool(getattr(args, "keep_h1", False)))
    if not title:
        raise UserError("无法确定文章标题：请在 frontmatter 写 title，或用 --title 指定。")

    tags = normalize_list(
        getattr(args, "tags", None) if getattr(args, "tags", None) is not None else meta.get("tags") or meta.get("keywords")
    )
    categories = normalize_list(
        getattr(args, "categories", None) if getattr(args, "categories", None) is not None else meta.get("categories")
    )
    slug = (getattr(args, "slug", None) or meta.get("slug") or "").strip()
    summary = (getattr(args, "summary", None) or meta.get("summary") or meta.get("excerpt") or "").strip()
    md_category = str(cfg.get("markdown_category") or DEFAULT_MD_CATEGORY)

    ai_flag = getattr(args, "ai_generated", None)
    if ai_flag is None:
        meta_ai = meta.get("ai_generated", meta.get("ai_declaration"))
        if meta_ai is None:
            ai_flag = bool(cfg.get("ai_generated", True))
        else:
            parsed_ai = _as_bool(meta_ai)
            ai_flag = True if parsed_ai is None else parsed_ai
    ai_declaration_added = False
    if ai_flag and AI_DECLARATION_MARKER not in body:
        body = AI_DECLARATION_MD + body
        ai_declaration_added = True

    warnings: list[str] = []
    if fmt == "markdown":
        categories = ensure_md_category(categories, md_category)
        description = body
    else:
        categories = remove_md_category(categories, md_category)
        description = md2html.markdown_to_html(body)

    if fmt == "html" and re.search(r"(\$\$|\$\w|\|.*\|)", body):
        warnings.append("HTML 模式使用内置简易转换器，数学公式/复杂表格可能需要人工检查。")

    _validate_categories(cfg, creds, categories, warnings, md_category)

    upload_images_flag = getattr(args, "upload_images", None)
    if upload_images_flag is None:
        upload_images_flag = _as_bool(meta.get("upload_images"))
        if upload_images_flag is None:
            upload_images_flag = bool(cfg.get("upload_images", True))
    if upload_images_flag:
        def uploader(path: Path) -> str:
            return upload_file(cfg, creds, path)

        description = upload_images(description, base_dir, fmt, uploader, warnings)

    post: dict[str, Any] = {
        "title": title,
        "description": description,
        "categories": categories,
    }
    if tags:
        post["mt_keywords"] = ",".join(tags)
    if slug:
        post["wp_slug"] = slug
    if summary:
        post["mt_excerpt"] = summary

    return {
        "post": post,
        "title": title,
        "format": fmt,
        "categories": categories,
        "tags": tags,
        "description": description,
        "warnings": warnings,
        "draft": _draft_flag(args, meta),
        "ai_generated": ai_flag,
        "ai_declaration_added": ai_declaration_added,
        "md_category": md_category,
    }


def cmd_check(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    creds = require_credentials()
    blogid, username, token = auth_params(cfg, creds)
    blogs = rpc(cfg, creds, "blogger.getUsersBlogs", [blogid, username, token])
    payload = {
        "ok": True,
        "action": "check",
        "blog_count": len(blogs or []),
    }
    human = ["✅ 鉴权通过（可用博客 %d 个）" % payload["blog_count"]]
    Reporter(args.json).emit(payload, human)
    return payload


def cmd_publish(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    creds = require_credentials()
    meta, body, base_dir = _load_document(args.file)
    prepared = _prepare(args, cfg, creds, meta, body, base_dir)
    blogid, username, token = auth_params(cfg, creds)
    draft = prepared["draft"]

    postid = rpc(
        cfg, creds,
        "metaWeblog.newPost",
        [blogid, username, token, prepared["post"], not draft],
    )
    postid = str(postid).replace('"', "")
    warnings = list(prepared["warnings"])

    fetched = None
    try:
        fetched = rpc(cfg, creds, "metaWeblog.getPost", [postid, username, token])
    except Exception:
        warnings.append("未能读取文章 permalink，链接为按博客名推测。")

    url = _post_url(creds, postid, fetched)
    payload = {
        "ok": True,
        "action": "publish",
        "postid": postid,
        "title": prepared["title"],
        "url": url,
        "draft": draft,
        "format": prepared["format"],
        "categories": prepared["categories"],
        "tags": prepared["tags"],
        "warnings": warnings,
        "visibility": "non_public" if draft else "public",
        "review_possible": not draft,
        "visibility_note": DRAFT_NOTE if draft else REVIEW_NOTE,
        "ai_generated": prepared["ai_generated"],
        "ai_declaration_added": prepared["ai_declaration_added"],
    }
    human = [
        "✅ %s《%s》" % (
            "已保存非公开博文（草稿/私有）" if draft else "已提交发布（博客园可能需人工审核）",
            prepared["title"],
        ),
        "文章 ID：%s" % postid,
    ]
    if url:
        human.append("链接：%s" % url)
    human.append("格式：%s" % prepared["format"])
    if prepared["categories"]:
        human.append("分类：%s" % "、".join(prepared["categories"]))
    if prepared["tags"]:
        human.append("标签：%s" % "、".join(prepared["tags"]))
    for warning in warnings:
        human.append("⚠ %s" % warning)
    human.append("%s %s" % ("📝" if draft else "⏳", payload["visibility_note"]))
    human.append(
        "AI 生成声明：%s" % (
            "已添加" if prepared["ai_declaration_added"]
            else ("内容中已存在" if prepared["ai_generated"] else "未启用")
        )
    )
    Reporter(args.json).emit(payload, human)
    return payload


def cmd_update(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    creds = require_credentials()
    blogid, username, token = auth_params(cfg, creds)
    postid = str(args.postid)
    current = rpc(cfg, creds, "metaWeblog.getPost", [postid, username, token])
    if not isinstance(current, dict):
        raise UserError("读取文章失败：%s" % postid, EXIT_FAULT)

    meta, body, base_dir = _load_document(args.file)
    meta = dict(meta)
    if not (args.title or meta.get("title")):
        meta["title"] = current.get("title") or ""
    if args.categories is None and not meta.get("categories"):
        meta["categories"] = current.get("categories") or []
    if args.tags is None and not (meta.get("tags") or meta.get("keywords")):
        meta["tags"] = current.get("mt_keywords") or []
    if not (args.slug or meta.get("slug")):
        meta["slug"] = current.get("wp_slug") or ""
    if not (args.summary or meta.get("summary") or meta.get("excerpt")):
        meta["summary"] = current.get("mt_excerpt") or ""

    prepared = _prepare(args, cfg, creds, meta, body, base_dir,
                        fallback_title=str(current.get("title") or ""))
    draft = prepared["draft"]
    ok = rpc(
        cfg, creds,
        "metaWeblog.editPost",
        [postid, username, token, prepared["post"], not draft],
    )
    if ok is False:
        raise UserError("服务端返回编辑失败：%s" % postid, EXIT_FAULT)

    fetched = None
    try:
        fetched = rpc(cfg, creds, "metaWeblog.getPost", [postid, username, token])
    except Exception:
        pass
    url = _post_url(creds, postid, fetched or current)
    payload = {
        "ok": True,
        "action": "update",
        "postid": postid,
        "title": prepared["title"],
        "url": url,
        "draft": draft,
        "format": prepared["format"],
        "categories": prepared["categories"],
        "tags": prepared["tags"],
        "warnings": prepared["warnings"],
        "visibility": "non_public" if draft else "public",
        "review_possible": not draft,
        "visibility_note": DRAFT_NOTE if draft else REVIEW_NOTE,
        "ai_generated": prepared["ai_generated"],
        "ai_declaration_added": prepared["ai_declaration_added"],
    }
    human = [
        "✅ %s《%s》" % (
            "已更新非公开博文（草稿/私有）" if draft else "已更新并提交（博客园可能需人工审核）",
            prepared["title"],
        ),
        "文章 ID：%s" % postid,
    ]
    if url:
        human.append("链接：%s" % url)
    for warning in prepared["warnings"]:
        human.append("⚠ %s" % warning)
    human.append("%s %s" % ("📝" if draft else "⏳", payload["visibility_note"]))
    human.append(
        "AI 生成声明：%s" % (
            "已添加" if prepared["ai_declaration_added"]
            else ("内容中已存在" if prepared["ai_generated"] else "未启用")
        )
    )
    Reporter(args.json).emit(payload, human)
    return payload


def cmd_get(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    creds = require_credentials()
    blogid, username, token = auth_params(cfg, creds)
    post = rpc(cfg, creds, "metaWeblog.getPost", [str(args.postid), username, token])
    if not isinstance(post, dict):
        raise UserError("读取文章失败：%s" % args.postid, EXIT_FAULT)
    payload = {
        "ok": True,
        "action": "get",
        "postid": str(post.get("postid") or args.postid),
        "title": post.get("title") or "",
        "url": _post_url(creds, str(args.postid), post),
        "dateCreated": jsonable(post.get("dateCreated")),
        "categories": post.get("categories") or [],
        "tags": post.get("mt_keywords") or "",
        "description": post.get("description") or "",
    }
    human = [
        "《%s》" % (payload["title"] or ""),
        "文章 ID：%s" % payload["postid"],
    ]
    if payload["url"]:
        human.append("链接：%s" % payload["url"])
    if payload["dateCreated"]:
        human.append("时间：%s" % payload["dateCreated"])
    if payload["categories"]:
        human.append("分类：%s" % "、".join(str(c) for c in payload["categories"]))
    if payload["tags"]:
        human.append("标签：%s" % payload["tags"])
    human.extend(["", "---", "", payload["description"]])
    Reporter(args.json).emit(payload, human)
    return payload


def cmd_list(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    creds = require_credentials()
    blogid, username, token = auth_params(cfg, creds)
    count = max(1, min(int(args.count), 100))
    posts = rpc(cfg, creds, "metaWeblog.getRecentPosts", [blogid, username, token, count])
    items = []
    for post in posts or []:
        postid = str(post.get("postid") or "")
        items.append({
            "postid": postid,
            "title": post.get("title") or "",
            "url": _post_url(creds, postid, post),
            "dateCreated": jsonable(post.get("dateCreated")),
            "categories": post.get("categories") or [],
        })
    payload = {"ok": True, "action": "list", "count": len(items), "items": items}
    human = ["最近 %d 篇文章：" % len(items)]
    for item in items:
        human.append("%s  %s  %s" % (item["postid"], item["dateCreated"] or "", item["title"]))
    Reporter(args.json).emit(payload, human)
    return payload


def cmd_delete(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    if not args.yes:
        raise UserError("删除前需要显式加 --yes。")
    creds = require_credentials()
    blogid, username, token = auth_params(cfg, creds)
    postid = str(args.postid)
    title = ""
    try:
        current = rpc(cfg, creds, "metaWeblog.getPost", [postid, username, token])
        if isinstance(current, dict):
            title = str(current.get("title") or "")
    except Exception:
        pass
    result = rpc(cfg, creds, "blogger.deletePost", [blogid, postid, username, token, True])
    payload = {"ok": bool(result), "action": "delete", "postid": postid, "title": title}
    human = ["✅ 已删除文章 %s《%s》" % (postid, title)]
    Reporter(args.json).emit(payload, human)
    return payload


def cmd_categories(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    creds = require_credentials()
    blogid, username, token = auth_params(cfg, creds)
    categories = rpc(cfg, creds, "metaWeblog.getCategories", [blogid, username, token])
    items = []
    for item in categories or []:
        items.append({
            "title": item.get("title") or item.get("categoryName") or "",
            "id": str(item.get("categoryid") or item.get("categoryId") or ""),
            "description": item.get("description") or "",
        })
    payload = {"ok": True, "action": "categories", "items": items}
    human = ["分类（%d）：" % len(items)]
    for item in items:
        human.append("- %s" % item["title"])
    Reporter(args.json).emit(payload, human)
    return payload


def cmd_upload(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    creds = require_credentials()
    urls = []
    warnings = []
    for name in args.files:
        path = Path(name).expanduser()
        try:
            urls.append({"file": str(path), "url": upload_file(cfg, creds, path)})
        except UserError as exc:
            warnings.append(str(exc))
    if not urls and warnings:
        raise UserError("；".join(warnings))
    payload = {"ok": True, "action": "upload", "items": urls, "warnings": warnings}
    human = ["✅ 已上传 %d 个文件" % len(urls)]
    for item in urls:
        human.append("- %s -> %s" % (item["file"], item["url"]))
    for warning in warnings:
        human.append("⚠ %s" % warning)
    Reporter(args.json).emit(payload, human)
    return payload


def cmd_render(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise UserError("找不到文件：%s" % path)
    rendered = md2html.markdown_to_html(path.read_text(encoding="utf-8-sig"))
    if args.out:
        Path(args.out).expanduser().write_text(rendered, encoding="utf-8")
        Reporter(args.json).emit(
            {"ok": True, "action": "render", "out": args.out},
            ["✅ 已写入 %s" % args.out],
        )
    elif args.json:
        Reporter(True).emit({"ok": True, "action": "render", "html": rendered}, [])
    else:
        print(rendered, end="")
    return {"ok": True, "action": "render"}


def cmd_forget(args: argparse.Namespace, cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    try:
        removed = secret_store_delete(SERVICE_NAME, CREDENTIAL_ACCOUNT)
    except SecretStoreError as exc:
        raise UserError(str(exc), EXIT_SECRET_STORE)
    if sys.platform == "darwin":
        for legacy in LEGACY_SERVICE_NAMES:
            try:
                removed = secret_store_delete(legacy, CREDENTIAL_ACCOUNT) or removed
            except SecretStoreError:
                pass
    config_removed = False
    if args.all and config_path.exists():
        config_path.unlink()
        config_removed = True
    payload = {
        "ok": True,
        "action": "forget",
        "credentials_removed": removed,
        "config_removed": config_removed,
    }
    human = [
        "系统凭据：%s" % ("已删除" if removed else "未找到"),
        "配置文件：%s" % ("已删除" if config_removed else "保留"),
    ]
    Reporter(args.json).emit(payload, human)
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cnblogs.py",
        description="安全地发布/更新/查询博客园文章（MetaWeblog XML-RPC）。凭据只存本机系统凭据库（macOS Keychain / Windows Credential Manager / Linux Secret Service）。",
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认：skill 根目录 config.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="输出 JSON（供 agent 解析）")
    common.add_argument("--blogid", help="XML-RPC blogid（默认 cnblogs）")
    common.add_argument("--timeout", type=int, help="网络超时秒数")

    setup = sub.add_parser("setup", parents=[common], help="在系统对话框中录入凭据并加密保存")
    setup.add_argument("--new-token", action="store_true", help="重新输入令牌（覆盖系统凭据）")
    setup.add_argument("--reconfigure", action="store_true", help="重新输入用户名和博客名")
    setup.add_argument("--skip-check", action="store_true", help="只保存凭据，不做鉴权检查")

    sub.add_parser("status", parents=[common], help="查看配置与系统凭据状态（不打印身份/令牌）")
    sub.add_parser("check", parents=[common], help="验证凭据并读取可用博客")

    def add_publish_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("file", help="Markdown 文章路径")
        p.add_argument("--title", help="标题（默认 frontmatter 或首个 H1）")
        p.add_argument("--tags", help="标签，逗号分隔")
        p.add_argument("--categories", help="分类，逗号分隔")
        p.add_argument("--slug", help="wp_slug")
        p.add_argument("--summary", help="摘要 mt_excerpt")
        p.add_argument("--format", choices=["auto", "markdown", "html"], default="auto",
                       help="正文格式，默认 auto（优先 markdown）")
        p.add_argument("--keep-h1", action="store_true", help="保留正文首个 H1（默认用它当标题并移除）")
        p.add_argument("--upload-images", dest="upload_images", action="store_true", default=None,
                       help="上传本地图片并替换链接（默认开启）")
        p.add_argument("--no-upload-images", dest="upload_images", action="store_false",
                       help="不上传本地图片")
        p.add_argument("--ai-generated", dest="ai_generated", action="store_true", default=None,
                       help="在正文顶部添加 AI 生成声明（默认开启）")
        p.add_argument("--no-ai-generated", dest="ai_generated", action="store_false",
                       help="不添加 AI 生成声明")

    publish = sub.add_parser("publish", parents=[common], help="新建并发布（或存草稿）一篇 Markdown")
    add_publish_options(publish)
    draft_group = publish.add_mutually_exclusive_group()
    draft_group.add_argument(
        "--draft", "--private", dest="draft", action="store_true",
        help="非公开发布（默认；草稿/私有，不进入审核）",
    )
    draft_group.add_argument(
        "--publish", "--public", dest="draft", action="store_false",
        help="公开发布（可能需人工审核）",
    )
    publish.set_defaults(draft=None)

    update = sub.add_parser("update", parents=[common], help="更新已有文章")
    update.add_argument("postid", help="文章 ID")
    add_publish_options(update)
    update_draft = update.add_mutually_exclusive_group()
    update_draft.add_argument("--draft", "--private", dest="draft", action="store_true")
    update_draft.add_argument("--publish", "--public", dest="draft", action="store_false")
    update.set_defaults(draft=None)

    get = sub.add_parser("get", parents=[common], help="读取一篇文章")
    get.add_argument("postid")

    list_p = sub.add_parser("list", parents=[common], help="最近文章")
    list_p.add_argument("--count", type=int, default=10)

    delete = sub.add_parser("delete", parents=[common], help="删除文章（需 --yes）")
    delete.add_argument("postid")
    delete.add_argument("--yes", action="store_true")

    sub.add_parser("categories", parents=[common], help="列出分类")
    upload = sub.add_parser("upload", parents=[common], help="上传图片/文件到博客园")
    upload.add_argument("files", nargs="+")
    render = sub.add_parser("render", parents=[common], help="本地 Markdown→HTML 预览（不联网）")
    render.add_argument("file")
    render.add_argument("--out")
    forget = sub.add_parser("forget", parents=[common], help="删除系统凭据")
    forget.add_argument("--all", action="store_true", help="同时删除配置文件")

    return parser


HANDLERS = {
    "setup": cmd_setup,
    "status": cmd_status,
    "check": cmd_check,
    "publish": cmd_publish,
    "update": cmd_update,
    "get": cmd_get,
    "list": cmd_list,
    "delete": cmd_delete,
    "categories": cmd_categories,
    "upload": cmd_upload,
    "render": cmd_render,
    "forget": cmd_forget,
}


def _fail(message: str, code: int, args: argparse.Namespace) -> int:
    message = _scrub_identity(message)
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": message, "code": code}, ensure_ascii=False))
    else:
        print("错误：%s" % message, file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser() if args.config else DEFAULT_CONFIG_PATH
    try:
        cfg = resolve_config(config_path, args)
        handler = HANDLERS[args.command]
        handler(args, cfg, config_path)
        return EXIT_OK
    except UserError as exc:
        return _fail(str(exc), exc.code, args)
    except DialogError as exc:
        return _fail(str(exc), EXIT_USAGE, args)
    except SecretStoreError as exc:
        return _fail(str(exc), EXIT_SECRET_STORE, args)
    except xmlrpc.client.Fault as fault:
        return _fail("服务端拒绝：%s" % fault.faultString, EXIT_FAULT, args)
    except xmlrpc.client.ProtocolError as exc:
        return _fail("服务端返回异常：HTTP %s %s" % (exc.errcode, exc.errmsg), EXIT_FAULT, args)
    except (socket.timeout, TimeoutError) as exc:
        return _fail("网络超时：%s" % exc, EXIT_NETWORK, args)
    except urllib.error.URLError as exc:
        return _fail("网络错误：%s" % exc, EXIT_NETWORK, args)
    except OSError as exc:
        return _fail("系统错误：%s" % exc, EXIT_ERROR, args)
    except Exception as exc:  # last resort; never dump locals or requests
        return _fail("未预期错误：%s: %s" % (type(exc).__name__, exc), EXIT_ERROR, args)


if __name__ == "__main__":
    sys.exit(main())