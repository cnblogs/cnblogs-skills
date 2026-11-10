---
name: cnblogs-publish
description: 博客园（cnblogs）文章发布与维护。Use when 用户要把 Markdown 一键发布到博客园、更新/查询/删除博客园文章、上传图片到博客园、查看分类、配置或检查博客园 MetaWeblog 凭据。用户名、博客名、访问令牌只加密保存在操作系统凭据库（macOS Keychain / Windows Credential Manager / Linux Secret Service），不在对话中收集，也不回显。支持 macOS、Windows、Linux。
whenToUse: 用户提到“发到博客园 / 发布到 cnblogs / 更新博客园文章 / 博客园草稿 / 博客园分类 / MetaWeblog 凭据”等，或需要把本地 Markdown 文章发布到博客园。
metadata:
  category: publishing
---

# 博客园一键发布（MetaWeblog）

以下命令均在 **skill 根目录**（包含本文件的目录）执行。把本地 Markdown 一键发布到博客园。脚本 `scripts/cnblogs.py` 直连
`https://rpc.cnblogs.com/metaweblog/<博客名>`（XML-RPC / MetaWeblog），
标题、frontmatter、分类、标签、本地图片上传都自动处理。

**平台支持**：macOS / Windows / Linux。Windows 上把示例里的 `python3` 换成 `python` 或 `py -3`。

## 安全模型（不可妥协）

**用户名、博客名、访问令牌** 都视为私密凭据，合并成一条加密记录保存在
**操作系统凭据库**（service `cnblogs-publish`，account `credentials`）：

| 平台 | 存储 |
|---|---|
| macOS | 登录钥匙串（Security.framework） |
| Windows | 凭据管理器（Credential Manager，DPAPI 保护） |
| Linux | Secret Service（`secret-tool` / GNOME Keyring / KWallet），或 Python `keyring` |

- 配置文件 `config.json` 只放偏好（blogid、格式、超时等），**不含任何身份或令牌**。
- 凭据只在脚本进程内展开；不会写入文件、不会进入命令行参数/stdin/环境变量、不会打印。
- 脚本对所有输出做脱敏（令牌 → `***`），JSON 输出不含用户名、博客名、令牌。
  （发布结果里的文章 URL 会包含公开的博客名，这是交付给用户的链接本身。）

**禁止事项**（对 agent 的硬性要求）：

1. 绝不要让用户在聊天里提供用户名、博客名或令牌；这些都必须在系统对话框中由用户本人输入。
2. 绝不要运行 `security find-generic-password ... -w`、`secret-tool lookup ...`、
   `echo $CNBLOGS_TOKEN`，或任何会打印凭据的命令。
3. 绝不要用 `curl`/`wget` 直接调用 MetaWeblog（凭据会出现在命令行/历史）。
   所有 API 调用只走 `scripts/cnblogs.py`。
4. 不要尝试把凭据写进 Markdown、配置、环境变量或临时文件。
5. 用户若已在对话中泄露令牌，提醒他立即到
   <https://i.cnblogs.com/settings#enableServiceAccess> 重置，并运行
   `setup --new-token`。

## 首次配置（只需一次）

```bash
python3 scripts/cnblogs.py status --json
```

`credentials_present` 为 `false` 时运行：

```bash
python3 scripts/cnblogs.py setup
```

- 依次弹出 3 个原生输入对话框：登录用户名（可见）、博客名（可见）、
  访问令牌（**隐藏输入**）。用户直接键入，脚本写入系统凭据库；
  输入内容不会经过对话，也不会出现在命令输出里。
- 对话框实现：macOS 用 osascript；Windows 用 PowerShell WinForms；
  Linux 优先 zenity/kdialog，其次 Tk。Linux 无图形会话时，请在可交互的终端里
  运行同一条命令（会退化为 `input`/`getpass`）。
- 博客名是 rpc 地址里的那段（例如 `your-blog-name`）；用户名是登录名，不是昵称。
- 令牌来源：博客园后台 → 设置 → 开放 API / 服务访问 →
  <https://i.cnblogs.com/settings#enableServiceAccess> 开启并生成。
- 重新运行 `setup` 会复用已保存凭据；`setup --new-token` 只重输令牌；
  `setup --reconfigure` 重输用户名和博客名。
- 配置完成后 `check --json` 返回 `ok: true` 和 `blog_count`。
  鉴权失败通常是用户名写成昵称、令牌未启用或复制不完整；**不要**向用户索要
  凭据内容，引导其重新运行相应的 setup 命令。
- `config.json` 默认写在 skill 根目录；如果安装目录只读，用
  `--config <可写路径>` 指到用户目录，凭据仍然只进系统凭据库。

### 平台依赖

- **macOS**：系统自带 `/usr/bin/osascript` 和钥匙串，无需额外安装。
- **Windows**：Python 3；PowerShell（系统自带）。凭据管理器不弹出额外授权。
- **Linux**：Python 3；建议安装 `libsecret-tools`（`secret-tool`）并运行
  GNOME Keyring / KWallet；或安装 Python `keyring` 包。图形对话框建议安装
  `zenity`。无 Secret Service 时脚本会明确报错，不会退化为明文存储。

## 一键发布

1. 在 workspace 里把文章写成 `.md` 文件（可带 frontmatter）。
2. 运行（默认非公开）：

```bash
python3 scripts/cnblogs.py publish <文章.md> --json
```

需要公开发布时显式加参数：

```bash
python3 scripts/cnblogs.py publish <文章.md> --publish --json
```

3. 解析 JSON 的 `postid`、`url`、`draft`、`visibility`、`review_possible`、
   `visibility_note`、`ai_generated`、`ai_declaration_added`、`warnings`，
   把链接和可见性提示返回给用户。

默认**非公开发布**（草稿/私有），不进入审核、可立即安全保存。只有显式
`--publish`（或 `--public`）才公开发布，并可能进入人工审核。`update` 也默认
非公开；更新已公开文章必须显式 `--publish`，否则会把它转为非公开。

### 发布后的可见性（重要）

**只有公开发布的博文需要审核；非公开博文（草稿/私有）不进入审核，可以立即
放心存储，不需要等待，也不会出现在首页或公网。**

公开发布的博文可能进入人工审核，审核通过前文章可能：

- 不出现在博客首页；
- 公开访问文章 URL 时返回 404/不可见（或仅作者可见）。

因此：

- **不要**用浏览器/`curl` 抓首页或文章页来“验证发布是否成功”；
- **不要**因为首页没有或公开页 404 就重复调用 `publish`（会产生重复文章）；
- 发布/更新命令成功返回 `postid` 就表示已写入；公开发布时告诉用户
  “已提交，等待审核”，非公开发布时告诉用户“已保存，不会公开、无需审核”；
  需要确认时只用 `get <postid> --json` 检查 API 侧记录；
- 审核结果由博客园后台决定；MetaWeblog 不提供审核状态查询。官方 OpenAPI
  的 `reviewStatus`（0 待审核 / 1 通过 / 2 不通过）需要另一套 PAT，当前未接入；
- 非公开（`--draft` / `--private`）不进入审核，可立即安全存储；
- 更新已公开文章也可能重新进入审核，同样不要立即检查公网可见性。

### AI 生成声明（默认开启）

脚本默认在正文顶部添加一条可见声明：

```markdown
> **AI 生成声明**：本文内容由 AI 生成或经 AI 辅助完成，请注意甄别。
```

- 关闭方式：命令行 `--no-ai-generated`，或 frontmatter `ai_generated: false`；
- 正文里已存在“AI 生成声明”时不会重复添加；
- MetaWeblog 的 `Post` 结构没有官方 `IsAigc` 字段，所以这是**正文可见声明**，
  不是博客园平台的官方 AI 标识；
- 官方 OpenAPI 的 `IsAigc` 需要另一套 PAT，当前未接入。

常用动作：

| 目的 | 命令 |
|---|---|
| 发布（默认非公开） | `publish post.md --json` |
| 公开发布（可能需审核） | `publish post.md --publish --json` |
| 显式非公开（默认行为） | `publish post.md --draft --json` 或 `--private` |
| 更新（默认非公开） | `update <postid> post.md --json` |
| 更新并公开发布 | `update <postid> post.md --publish --json` |
| 发布并推荐到首页 | `publish post.md --publish --categories "[发布至博客园首页]" --json` |
| 最近文章 | `list --count 10 --json` |
| 读取文章 | `get <postid> --json` |
| 删除 | `delete <postid> --yes --json`（必须先获得用户明确同意） |
| 分类 | `categories --json` |
| 上传图片 | `upload a.png b.jpg --json` |
| 本地 HTML 预览 | `render post.md`（不联网） |

## 文章格式与约定

frontmatter 支持（均为可选）：

```yaml
---
title: 文章标题
tags: [.NET, AI]
categories: [随笔, 后端]
slug: my-post
summary: 摘要
draft: true          # 默认 true（非公开）；publish: true 或 --publish 才公开
format: markdown     # markdown | html
upload_images: true
ai_generated: true   # 默认 true；false 关闭 AI 生成声明
---
```

- 没有 frontmatter 时，正文第一个 `# 标题` 会作为标题并从正文移除；
  `--keep-h1` 保留正文 H1。也可以用 `--title` 显式指定。
- **Markdown 模式是默认值**，脚本会自动附加特殊分类 `[Markdown]`。
  这是博客园把正文按 Markdown 渲染的开关，不要删除；它通常不会显示在分类列表。
- 本地图片（相对/绝对路径）默认通过 `metaWeblog.newMediaObject` 上传到博客园，
  并把正文引用替换成返回的 URL；外链和 `data:` 不动。单张失败只产生
  `warnings`，不阻塞发布。`--no-upload-images` 可关闭上传。
- 标题/标签/分类优先级：命令行参数 > frontmatter > 默认值。
- **首页推荐**：在 `categories` 中加 `[发布至博客园首页]`（可用
  `--categories "[发布至博客园首页]"`），文章才进入首页推荐流程；首页推荐同样需要审核。
- `--format html` 使用内置的简易 Markdown→HTML 转换（复杂公式/表格需人工检查）；
  绝大多数情况保持默认 `markdown`。
- 发布前会读取博客分类做校验，不存在的分类只给 warning，不阻塞。

## 输出契约与退出码

成功时 stdout 是单个 JSON，不含任何凭据：

默认（非公开）发布：

```json
{"ok": true, "action": "publish", "postid": "123456",
 "url": "https://www.cnblogs.com/your-blog-name/p/123456",
 "draft": true, "format": "markdown",
 "visibility": "non_public",
 "review_possible": false,
 "visibility_note": "这是非公开发布（草稿/私有），不进入审核，可立即安全保存……",
 "ai_generated": true, "ai_declaration_added": true,
 "categories": ["随笔", "[Markdown]"], "tags": ["AI"],
 "warnings": []}
```

显式 `--publish` 后则是 `draft: false`、`visibility: "public"`、
`review_possible: true`，`visibility_note` 会提示可能人工审核。

失败时 stdout 是 `{"ok": false, "error": "...", "code": N}`，且已脱敏。

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 参数/配置/输入取消 |
| 3 | 博客园服务端拒绝（鉴权、分类等） |
| 4 | 网络错误/超时 |
| 5 | 系统凭据库错误（沙箱、锁定、Linux 无 Secret Service） |

## 排障

- `code: 3` + “请配置正确的用户名与访问令牌”：令牌未启用/错误，或用户名不是
  登录名。让用户检查开放 API 设置并运行 `setup --new-token` / `--reconfigure`。
- 发布成功但显示的是原始 Markdown：确认分类里有 `[Markdown]`，且没有用
  `--format html` 却把 Markdown 当 HTML 发。
- 发布成功但首页没有/公开页 404：如果这次是**公开发布**，博客园可能在人工审核，
  **不是失败**；不要重发，用 `get <postid>` 确认 API 侧记录，等审核通过即可。
- 非公开（默认行为；`--draft`/`--private`）本来就不会公开，无需等待审核，
  也不要去查首页。
- `warnings` 里“分类不存在”：先在博客后台创建分类，或改用已有分类。
- `code: 5` + macOS `100001`：命令在受限沙箱中运行，钥匙串写入被拒绝；用
  danger-full-access 重试一次，或让用户在终端运行 setup。
- `code: 5` + Linux “未找到可用的安全凭据存储”：安装 `libsecret-tools` 并启动
  GNOME Keyring/KWallet，或 `pip install keyring`；不要在 Linux 上退化为明文文件。
- `code: 5` + Windows 凭据管理器错误：确认当前用户会话可以访问 Credential
  Manager（远程/服务会话可能不可用），并在用户自己的桌面会话中运行 setup。
- `check` 成功但发布失败：把完整 `error`（已脱敏）返回给用户，不要自行改用 curl。

更多细节见 `references/metaweblog-api.md` 与 `references/security.md`。