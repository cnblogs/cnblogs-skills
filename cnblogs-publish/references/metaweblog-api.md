# 博客园 MetaWeblog API 备忘

来源：<https://rpc.cnblogs.com/metaweblog/<博客名>>（博客园自述的 MetaWeblog 文档）。
接口协议：XML-RPC 1.0，服务端 `BlogServer.Service.XmlRpc`。

## 端点与鉴权

- 端点：`https://rpc.cnblogs.com/metaweblog/<博客名>`，例如
  `https://rpc.cnblogs.com/metaweblog/<博客名>`。
- 用户名：博客园**登录用户名**（不是昵称）。
- 密码字段：填**访问令牌（Access Token）**。博客园已取消密码登录，若填登录密码会返回：

  ```
  faultCode 401
  请配置正确的用户名与访问令牌(access token), 密码登录已取消 ...
  在博客后台获取访问令牌 https://i.cnblogs.com/settings#enableServiceAccess
  ```

- 开启/生成令牌：博客园后台 → 设置 → 开放 API / 服务访问 →
  <https://i.cnblogs.com/settings#enableServiceAccess>。

## 支持的方法

| 方法 | 参数（按顺序） | 返回 |
|---|---|---|
| `blogger.getUsersBlogs` | appKey/blogid, username, token | BlogInfo 数组 |
| `blogger.deletePost` | blogid, postid, username, token, publish(bool) | bool |
| `metaWeblog.newPost` | blogid, username, token, Post 结构, publish(bool) | postid（字符串） |
| `metaWeblog.editPost` | postid, username, token, Post 结构, publish(bool) | bool/any |
| `metaWeblog.getPost` | postid, username, token | Post |
| `metaWeblog.getRecentPosts` | blogid, username, token, numberOfPosts(int) | Post 数组 |
| `metaWeblog.getCategories` | blogid, username, token | CategoryInfo 数组 |
| `metaWeblog.newMediaObject` | blogid, username, token, FileData | `{url}` |
| `wp.newCategory` | blog_id, username, token, WpCategory | int |

### Post 结构（newPost/editPost）

必填：`title`、`description`。常用可选：

- `categories`：字符串数组（分类**标题**）。
- `mt_keywords`：逗号分隔的标签字符串。
- `wp_slug`：别名。
- `mt_excerpt`：摘要。
- `dateCreated`：ISO8601（本次实现默认不传，由服务端取当前时间）。
- `mt_convert_breaks`、`mt_text_more`、`link`、`permalink` 等：本实现未使用。

### FileData（newMediaObject）

```text
{ name: "a.png", type: "image/png", bits: <文件内容的 base64 字符串> }
```

返回 `{ url: "https://img..../a.png" }`。上传成功后应把正文中的本地图片路径替换成该 URL。

## 博客园的两个关键约定

1. **`blogid` 固定传 `cnblogs`**（不是博客名）。博客名已经在端点路径里。
   本脚本默认 `blogid=cnblogs`，可用 `--blogid` 覆盖。
2. **Markdown 渲染开关是特殊分类 `[Markdown]`**。
   正文以 Markdown 原文提交时，`categories` 里必须包含 `[Markdown]`；
   博客园据此按 Markdown 渲染。该分类通常不会出现在普通分类列表中。
   本脚本 `--format markdown`（默认）会自动追加，`--format html` 会移除。
   （该约定来自可用的第三方发布器实现：siyuan-plugin-publisher 的
   `src/adaptors/api/cnblogs/cnblogsApiAdaptor.ts`。）
3. **首页推荐分类是 `[发布至博客园首页]`**（分类 id 为 0）。
   需要在 `categories` 中显式加入该分类，文章才会进入首页推荐流程；
   该推荐同样需要审核。

## 文章链接（无 .html 才是规范地址）

`newPost` 只返回 postid。文章 URL 形如：

```text
https://www.cnblogs.com/<博客名>/p/<postid>
```

这是博客园当前的规范地址；`.../p/<postid>.html` 会 **301 跳转**到无后缀地址，
页面的 `<link rel="canonical">` 也是无后缀。因此：

- 不要把 `metaWeblog.getPost` 返回的 `link`/`permalink` 改写成 `.html`；
- 只有在 `getPost` 也拿不到链接时，脚本才按无后缀规则拼接。

官方 OpenAPI 文档（<https://www.cnblogs.com/cmt/articles/19246558>）返回的
`postUrl` 同样没有 `.html` 后缀，可作为旁证。

## 发布审核与可见性

**审核只针对公开发布的博文**。非公开发布（`publish=false`，即草稿/私有）不会进入
审核，可立即安全存储，也不会出现在首页或公网。本 skill 的**默认值是
`publish=false`**，只有显式 `--publish` 才提交公开审核。

`metaWeblog.newPost` / `editPost` 成功只代表博文已写入博客园；对**公开发布**而言，
不代表已经公开可见。博客园可能对公开博文进行人工审核，审核期间：

- 博客首页可能没有这篇文章；
- 公开 URL 可能返回 404 或仅作者可见；
- 不应轮询公开页面，也不应因为看不到就重复发布。

MetaWeblog 的 `Post` 结构没有审核状态字段；`publish=false` 用于非公开存储，
不需要等待审核。官方 OpenAPI 提供
`POST /openapi/v1/posts/reviewStatus:check`（0 待审核 / 1 通过 / 2 不通过），
但需要 PAT 认证，和本 skill 的 MetaWeblog 令牌不是同一套；当前未接入。

如需判断发布成功，只以 `newPost` 返回 postid、`getPost` 能读回记录为准，并向用户
说明“已提交，等待审核”。

## AI 生成声明

MetaWeblog 的 `Post` 结构与 `newPost`/`editPost` 参数均**没有** `IsAigc` 之类的
官方 AI 标识字段；`getPost` 返回的字段也验证了这一点。因此本 skill 默认在正文
顶部插入一条可见声明：

```markdown
> **AI 生成声明**：本文内容由 AI 生成或经 AI 辅助完成，请注意甄别。
```

可用 `--no-ai-generated` 或 frontmatter `ai_generated: false` 关闭。它只是正文内容，
不会设置博客园平台的官方 AI 标识。官方 OpenAPI 的 `IsAigc` 字段需要 PAT 认证，
当前未接入；若必须使用官方标识，应单独实现 OpenAPI 通道。

## 官方 OpenAPI（另一条通道，本 skill 未使用）

博客园还有一套 PAT 认证的 OpenAPI：

```text
POST https://i.cnblogs.com/openapi/v1/posts
Authorization: Bearer <PAT>
Authorization-Type: pat
```

支持 `PostFormat=Markdown`、分类、标签、`IsAigc` 等字段，响应包含
`postId` 和 `postUrl`。它与本 skill 的 MetaWeblog 通道是两套独立凭据：

- MetaWeblog：用户名 + 开放 API 访问令牌，按本文档使用；
- OpenAPI：账号中心创建的 PAT（Bearer），走 `i.cnblogs.com/openapi`。

本 skill 不自动混用两种令牌；如需要 OpenAPI 的 `IsAigc` 等能力，应单独实现
适配器，而不是把 PAT 塞进 MetaWeblog 流程。

## 图片

`metaWeblog.newMediaObject` 将 base64 内容上传到博客园图床，返回 URL。
本脚本默认自动上传正文中的本地图片；上传失败不会中断发布，只在 `warnings`
里提示并保留原引用。

## 调试建议

- 连通性/鉴权：`python3 scripts/cnblogs.py check --json`（内部调用
  `blogger.getUsersBlogs`）。
- 分类：`categories --json`。
- **不要**用 curl 手工构造请求：令牌会进入命令行参数和 shell 历史。