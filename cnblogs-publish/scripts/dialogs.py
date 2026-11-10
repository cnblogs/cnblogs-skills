"""Cross-platform secure prompt helpers.

Secrets are collected through a native dialog (or a TTY prompt) and returned to
the caller; they are never written to stdout/stderr and never placed in a
subprocess argument list.  Prompts themselves may contain no sensitive values.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys

__all__ = ["DialogError", "ask_secret", "ask_text"]


class DialogError(Exception):
    """Raised when no interactive prompt can be shown or the user cancels."""


def _quote_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run(cmd, *, env=None, timeout=300, encoding=None):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding=encoding,
            timeout=timeout, env=env
        )
    except FileNotFoundError as exc:
        raise DialogError("找不到可用的系统对话框程序（%s）。" % cmd[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise DialogError("等待输入超时，已取消。") from exc


def _macos_prompt(prompt: str, title: str, secret: bool, default: str) -> str:
    script = (
        'return text returned of (display dialog "%s" default answer "%s"%s '
        'buttons {"取消", "确定"} default button "确定" with title "%s")'
        % (
            _quote_applescript(prompt),
            _quote_applescript(default),
            " with hidden answer" if secret else "",
            _quote_applescript(title),
        )
    )
    proc = _run(["/usr/bin/osascript", "-e", script])
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "-128" in err or "User canceled" in err or "用户已取消" in err:
            raise DialogError("已取消输入。")
        raise DialogError("无法显示 macOS 系统对话框：%s" % err[:200])
    return proc.stdout.rstrip("\n")


def _windows_prompt(prompt: str, title: str, secret: bool, default: str) -> str:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise DialogError("找不到 PowerShell，无法显示 Windows 输入对话框。")

    script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = $env:CNBLOGS_PROMPT_TITLE
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(480, 210)
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$label = New-Object System.Windows.Forms.Label
$label.Text = $env:CNBLOGS_PROMPT_TEXT
$label.SetBounds(18, 18, 430, 50)
$form.Controls.Add($label)

$box = New-Object System.Windows.Forms.TextBox
$box.SetBounds(18, 72, 425, 26)
$box.Text = $env:CNBLOGS_PROMPT_DEFAULT
if ($env:CNBLOGS_PROMPT_SECRET -eq '1') { $box.UseSystemPasswordChar = $true }
$form.Controls.Add($box)

$ok = New-Object System.Windows.Forms.Button
$ok.Text = '确定'
$ok.SetBounds(255, 120, 85, 30)
$ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.Controls.Add($ok)

$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = '取消'
$cancel.SetBounds(355, 120, 85, 30)
$cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.Controls.Add($cancel)

$form.AcceptButton = $ok
$form.CancelButton = $cancel
$form.Add_Shown({ $box.Focus() })
$result = $form.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($box.Text)
}
"""
    env = os.environ.copy()
    env.update({
        "CNBLOGS_PROMPT_TITLE": title,
        "CNBLOGS_PROMPT_TEXT": prompt,
        "CNBLOGS_PROMPT_DEFAULT": default,
        "CNBLOGS_PROMPT_SECRET": "1" if secret else "0",
    })
    proc = _run(
        [powershell, "-NoProfile", "-STA", "-Command", script],
        env=env, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise DialogError("无法显示 Windows 输入对话框：%s" % (proc.stderr or "").strip()[:200])
    return proc.stdout.lstrip("\ufeff").rstrip("\r\n")


def _linux_prompt(prompt: str, title: str, secret: bool, default: str) -> str:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or sys.platform == "darwin"):
        raise DialogError("当前没有图形会话（DISPLAY/WAYLAND_DISPLAY 为空）。")

    zenity = shutil.which("zenity")
    if zenity:
        if secret:
            cmd = [zenity, "--password", "--title", title, "--text", prompt]
        else:
            cmd = [zenity, "--entry", "--title", title, "--text", prompt, "--entry-text", default]
        proc = _run(cmd)
        if proc.returncode == 0:
            return proc.stdout.rstrip("\n")
        if proc.returncode == 1:
            raise DialogError("已取消输入。")

    kdialog = shutil.which("kdialog")
    if kdialog:
        if secret:
            cmd = [kdialog, "--password", prompt, "--title", title]
        else:
            cmd = [kdialog, "--inputbox", prompt, default, "--title", title]
        proc = _run(cmd)
        if proc.returncode == 0:
            return proc.stdout.rstrip("\n")
        if proc.returncode == 1:
            raise DialogError("已取消输入。")

    # Last resort: Tk (present in many Python distributions, optional on Linux).
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        value = simpledialog.askstring(
            title, prompt, initialvalue=default, show="*" if secret else None, parent=root
        )
        root.destroy()
        if value is None:
            raise DialogError("已取消输入。")
        return value
    except DialogError:
        raise
    except Exception as exc:
        raise DialogError(
            "无法显示 Linux 输入对话框（已尝试 zenity / kdialog / Tk）。"
            "请安装 zenity（例如 `sudo apt install zenity`），或在一个可交互的终端里运行 setup。"
            "（%s）" % exc
        )


def _tty_prompt(prompt: str, secret: bool, default: str) -> str:
    if secret:
        return getpass.getpass(prompt + "：")
    suffix = " [%s]" % default if default else ""
    answer = input(prompt + suffix + "：").strip()
    return answer or default


def _ask(prompt: str, title: str, secret: bool, default: str) -> str:
    if sys.stdin.isatty():
        return _tty_prompt(prompt, secret, default)
    if sys.platform == "darwin":
        return _macos_prompt(prompt, title, secret, default)
    if os.name == "nt" or sys.platform.startswith("win"):
        return _windows_prompt(prompt, title, secret, default)
    return _linux_prompt(prompt, title, secret, default)


def ask_text(prompt: str, title: str, default: str = "") -> str:
    return _ask(prompt, title, False, default)


def ask_secret(prompt: str, title: str) -> str:
    value = _ask(prompt, title, True, "").strip()
    if not value:
        raise DialogError("输入为空，已取消。")
    if any(ch.isspace() for ch in value):
        raise DialogError("输入包含空白字符，请确认复制完整后重试。")
    return value