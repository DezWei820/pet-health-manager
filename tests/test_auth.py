# -*- coding: utf-8 -*-
"""JWT 认证单测：不依赖数据库，直接测签名与校验"""
import pytest
from fastapi import HTTPException
import backend


def test_create_and_verify_token():
    """合法 token 应通过认证并返回用户名"""
    token = backend.create_access_token({"sub": "pytest_user"})
    assert backend.get_current_user(authorization=f"Bearer {token}") == "pytest_user"


def test_invalid_token_rejected():
    """伪造/损坏 token 应被拒绝（401）"""
    with pytest.raises(HTTPException) as exc:
        backend.get_current_user(authorization="Bearer bad.token.value")
    assert exc.value.status_code == 401
