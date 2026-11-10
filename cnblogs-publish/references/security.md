# 凭据安全设计（跨平台）

目标：**用户名、博客名、访问令牌** 都视为私密信息，做到

- 不落盘明文；
- 不进入对话 / 大模型上下文；
- 不出现在命令行参数、stdin、环境变量或日志中。

## 存储：操作系统凭据库（单一加密条目）

| 项 | 值 |
|---|---|
| Service | `cnblogs-publish` |
| Account | `credentials` |
| 内容 | 一条 JSON：`{"username": "...", "blog": "...", "token": "...", "endpoint": "..."}` |
| 写入/读取 | `scripts/cnblogs.py` → `scripts/secretstore.py` |

| 平台 | 后端 | 实现要点 |
|---|---|---|
| macOS | 登录钥匙串 | `Security.framework`：`SecKeychainAddGenericPassword` / `SecKeychainFindGenericPassword` / `SecKeychainItemDelete`，ctypes 直调 |
| Windows | Credential Manager | `advapi32`：`CredWriteW` / `CredReadW` / `CredDeleteW`，target 为 `cnblogs-publish:credentials`，由 Windows DPAPI 加密 |
| Linux | Secret Service | 优先 `secret-tool`（libsecret；secret 通过 stdin 传入，不进 argv），否则 Python `keyring`（GNOME Keyring / KWallet） |

Linux 没有 Secret Service/keyring 时，脚本返回退出码 `5` 并给出安装提示，
**绝不退化为明文文件**。

凭据只在脚本进程内存里短暂展开，不进入进程参数（argv）、不经过 shell 的
stdout/stderr。

`config.json` 只保存偏好，**没有** `username`、`blog`、`token`、`password`、`endpoint`：

```json
{
  "blogid": "cnblogs",
  "format": "markdown",
  "markdown_category": "[Markdown]",
  "timeout": 30,
  "upload_images": true,
  "validate_categories": true,
  "ai_generated": true
}
```

文件权限为 `0600`（Windows 上由用户目录 ACL 保护）。即使被读取，也拿不到身份或令牌。

## 输入：原生系统对话框

`setup` 在非交互环境下依次弹出 3 个输入框：

1. 登录用户名（可见输入）；
2. 博客名（可见输入）；
3. 访问令牌（**隐藏输入**）。

| 平台 | 对话框 |
|---|---|
| macOS | `osascript` + `display dialog ... with hidden answer` |
| Windows | PowerShell WinForms 窗口；令牌框设置 `UseSystemPasswordChar`；提示文本通过环境变量传入，不进命令行 |
| Linux | `zenity --password` / `kdialog --password`；无图形会话时退化为 Tk，再退化为终端 `getpass` |

内容由用户本人键入，直接交给 Python 写入系统凭据库；**不经过聊天、不显示、
不写文件**。在用户自己的终端里运行 setup 时改用 `input` / `getpass`，同样不回显、
不进入对话。

脚本 **拒绝** 从命令行、stdin 或环境变量接收任何凭据。
(`--new-token` / `--reconfigure` 只是让脚本重新弹框，不接受值本身。)

## 输出：统一脱敏

- 令牌读入后注册到进程内 `_SECRETS`；所有人类输出、JSON、错误信息都替换为 `***`。
- 错误文本还会屏蔽用户名、博客名、邮箱和 API URL（`_scrub_identity`）。
- `status` 只报告“凭据已保存/未保存”和后端名称，不显示用户名、博客名、endpoint。
- `check` 只报告 `ok` 和 `blog_count`，不报告博客名。
- `setup` 不显示任何输入值。
- XML-RPC 请求体从不打印；也没有 `--debug`/`--verbose` 会 dump 请求。
- 唯一会包含博客名的输出是发布结果里的 **文章 URL**（`https://www.cnblogs.com/<博客名>/p/<id>`），
  它是交付给用户的公开链接本身，不包含用户名或令牌。

## 为什么不用其他方式

| 方式 | 问题 |
|---|---|
| 明文 `config.json` / `.env` | 身份和令牌可被任意读取 |
| 环境变量 | 可能被 `env`、崩溃日志、子进程继承泄露 |
| 命令行参数 / curl | 进入 shell 历史和 `ps` |
| 让用户把凭据贴进对话 | 直接进入大模型上下文，不可撤回 |

## 沙箱与系统凭据库

- **macOS**：DSH 的 `workspace-write` 沙箱通常允许读钥匙串，但拒绝写（OSStatus
  `100001`）。`setup` / `forget` / `--new-token` 可能需要用 danger-full-access
  重试一次；日常 `publish` / `update` / `list` 只读，一般无需提权。
- **Windows**：Credential Manager 在普通用户会话可用；远程/服务会话（无交互桌面）
  可能不可用，此时请在用户桌面会话中运行 setup。
- **Linux**：Secret Service 依赖运行中的 D-Bus session 和已解锁的 keyring；headless
  环境请先启动/解锁 GNOME Keyring 或 KWallet，或改用 `keyring` 后端。

如果凭据库不可用，脚本返回退出码 `5` 和明确提示；此时让用户在自己的
“终端”里运行 setup，或调整会话权限，**不要**改为明文保存。

> 如果换用另一个 Python 解释器或另一套平台凭据服务，系统可能弹出一次
> “允许访问凭据”的确认；这仍是系统级保护，不会把内容打印出来。

## 凭据泄露后的处置

1. 立即到 <https://i.cnblogs.com/settings#enableServiceAccess> 重置/删除旧令牌。
2. 运行 `python3 scripts/cnblogs.py setup --new-token` 在系统对话框中录入新令牌。
3. 如果用户名/博客名曾出现在聊天、日志或截图中，检查博客是否有异常文章或设置变更；
   必要时运行 `setup --reconfigure` 更改绑定。

## 手工检查（不打印凭据）

优先使用脚本自身：

```bash
python3 scripts/cnblogs.py status --json
```

- macOS：`security find-generic-password -s cnblogs-publish -a credentials >/dev/null`
  只判断存在性；**不要**加 `-w`。
- Windows：`cmdkey /list` 只列出凭据名称，不显示 secret。
- Linux：不要运行 `secret-tool lookup`（会打印 secret）；用 `status` 判断即可。

> 任何 `... -w`、`secret-tool lookup`、`keyring get_password` 的直接调用都可能把
> 凭据打印到终端，可能被 agent 的工具输出捕获。