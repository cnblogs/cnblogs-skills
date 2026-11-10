# 多平台测试说明

运行：

```bash
python3 -m unittest discover -s tests -v
```

当前测试覆盖（50 项）：

| 平台 | 自动化覆盖 | 真实主机 |
|---|---|---|
| macOS | Keychain 后端选择、状态读取、发布/URL 逻辑；本机 live `status`/`check` | ✅ 已在 macOS 上运行 |
| Windows | Credential Manager `CredWriteW`/`CredDeleteW` 结构模拟、读取不存在项、PowerShell 对话框命令构造、平台路由 | ⚠️ 本次环境无 Windows 主机，需在 Windows 上跑一遍冒烟 |
| Linux | `secret-tool` 真实子进程读写（用临时假 `secret-tool`）、缺失/错误处理、keyring 后端选择、zenity 对话框命令构造 | ⚠️ 本次环境无 Linux 主机，需在 Linux 上跑一遍冒烟 |

说明：

- Windows 测试通过 mock `ctypes.WinDLL` 验证 `CREDENTIALW` 的 target、blob、删除路径；它不能替代真实 Windows 的 DPAPI/Credential Manager 行为。
- Linux 测试用 POSIX 临时脚本模拟 `secret-tool store/lookup/clear`，验证 secret 通过 **stdin** 而不是 argv 传递；真实 Secret Service / keyring 仍需在 Linux 桌面或已解锁的 headless keyring 上验证。
- 平台路由测试会临时替换 `sys.platform`，验证 macOS/Windows/Linux 各自选择正确的凭据后端和对话框实现。
- 不在测试中写真实 macOS 钥匙串或真实 Secret Service，避免污染用户凭据。

## 在真实 Windows / Linux 上冒烟

```bash
# Windows: python 或 py -3
python scripts/cnblogs.py setup
python scripts/cnblogs.py status --json
python scripts/cnblogs.py check --json
python scripts/cnblogs.py publish sample.md --draft --json
```

```bash
# Linux: 需要 secret-tool/keyring + 可用的 keyring 会话
python3 scripts/cnblogs.py setup
python3 scripts/cnblogs.py status --json
python3 scripts/cnblogs.py check --json
python3 scripts/cnblogs.py publish sample.md --draft --json
```

若要在 CI 中真正覆盖三平台，需要 GitLab Runner 提供 `linux` 和 `windows` 执行器；
仅靠 mock 测试不能证明目标机器的 DPAPI / Secret Service 可用。