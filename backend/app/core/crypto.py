# -*- coding: utf-8 -*-
"""Cookie 加密存储（需求文档 F-01-R2）。

方案：cryptography Fernet 对称加密；
密钥派生：本机特征（机器名 + 用户名）经 SHA-256 派生 Fernet 密钥，密钥文件落程序目录 config/.key。
密钥文件首次生成；若丢失则历史 Cookie 无法解密（提示重新登录）。
"""

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# 密钥文件路径（由 setting_service 初始化 data 目录后设置）
_key_file: Path | None = None
_fernet: Fernet | None = None


def _machine_secret() -> str:
    """本机特征串：机器名 + 用户名 + 处理器数（不联网、不上传）。"""
    return f"{os.environ.get('COMPUTERNAME', '')}|{os.environ.get('USERNAME', '')}|{os.cpu_count()}"


def init_crypto(key_dir: Path) -> None:
    """初始化 Fernet 实例：读取或生成密钥文件。

    参数:
        key_dir: 密钥目录（程序目录 config/）
    """
    global _key_file, _fernet
    key_dir.mkdir(parents=True, exist_ok=True)
    _key_file = key_dir / ".key"
    if _key_file.exists():
        key = _key_file.read_bytes().strip()
    else:
        # 首次生成：本机特征派生 32 字节密钥，base64 编码落文件
        digest = hashlib.sha256(_machine_secret().encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
        _key_file.write_bytes(key)
    _fernet = Fernet(key)


def _get_fernet() -> Fernet:
    """获取 Fernet 实例，未初始化则抛错。"""
    if _fernet is None:
        raise RuntimeError("加密模块未初始化，请先在应用启动时调用 init_crypto()")
    return _fernet


def encrypt(plaintext: str) -> str:
    """加密明文（Cookie），返回密文字符串。"""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """解密密文，失败抛 ValueError（密钥变更或数据损坏）。"""
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError(f"Cookies 解密失败（密钥变更或数据损坏）：{e}") from e
