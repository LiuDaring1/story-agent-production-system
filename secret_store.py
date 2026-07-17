from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys


def keychain_service(secret_name: str) -> str:
    return f"story-agent.{secret_name}"


def read_secret(secret_name: str) -> str:
    """Read a provider secret from the environment, then the macOS Keychain."""
    value = os.getenv(secret_name, "").strip()
    if value:
        return value
    if sys.platform != "darwin":
        return ""
    process = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a",
            os.getenv("USER", "story-agent"),
            "-s",
            keychain_service(secret_name),
            "-w",
        ],
        text=True,
        capture_output=True,
    )
    return process.stdout.strip() if process.returncode == 0 else ""


def write_secret(secret_name: str, value: str) -> None:
    """Store a secret in the current user's macOS Keychain without logging it."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", secret_name):
        raise ValueError("密钥名称只能使用大写字母、数字和下划线")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("密钥内容不能为空")
    if sys.platform != "darwin":
        raise RuntimeError("当前只支持写入 macOS Keychain")
    process = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-a",
            os.getenv("USER", "story-agent"),
            "-s",
            keychain_service(secret_name),
            "-U",
            "-w",
            cleaned,
        ],
        text=True,
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError((process.stderr or "写入 macOS Keychain 失败").strip())


def import_secret_from_clipboard(secret_name: str, *, clear_clipboard: bool = True) -> None:
    """Import a copied provider key without exposing it in shell history or output."""
    if sys.platform != "darwin":
        raise RuntimeError("当前只支持从 macOS 剪贴板导入")
    pasted = subprocess.run(["pbpaste"], text=True, capture_output=True)
    if pasted.returncode != 0 or not pasted.stdout.strip():
        raise RuntimeError("剪贴板为空或无法读取")
    write_secret(secret_name, pasted.stdout)
    if clear_clipboard:
        subprocess.run(["pbcopy"], input="", text=True, capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="故事 Agent 系统安全存储；不会把密钥写入仓库或日志")
    subparsers = parser.add_subparsers(dest="command", required=True)
    set_parser = subparsers.add_parser("set", help="通过隐藏输入写入 macOS Keychain")
    set_parser.add_argument("secret_name")
    clipboard_parser = subparsers.add_parser("import-clipboard", help="从剪贴板导入后立即清空，不显示内容")
    clipboard_parser.add_argument("secret_name")
    status_parser = subparsers.add_parser("status", help="只检查密钥是否可读取，不显示内容")
    status_parser.add_argument("secret_name")
    args = parser.parse_args()
    if args.command == "set":
        value = getpass.getpass(f"请输入 {args.secret_name}（输入不可见）：")
        write_secret(args.secret_name, value)
        print(f"{args.secret_name}: configured")
    elif args.command == "import-clipboard":
        import_secret_from_clipboard(args.secret_name)
        print(f"{args.secret_name}: configured")
    else:
        print(f"{args.secret_name}: {'configured' if read_secret(args.secret_name) else 'missing'}")


if __name__ == "__main__":
    main()
