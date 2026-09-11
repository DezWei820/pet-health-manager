# -*- coding: utf-8 -*-
"""宠物 CRUD 集成测试：走真实接口（MySQL/Redis），用随机用户隔离数据"""
import uuid
import pytest
from fastapi.testclient import TestClient
import backend


@pytest.fixture(scope="module")
def client():
    # with 块会触发 FastAPI lifespan（MySQL 连接池、Agent 构建）
    with TestClient(backend.app) as c:
        yield c


def _register_login(client, name):
    r = client.post("/api/register", json={"username": name, "password": "test123456"})
    assert r.status_code == 200
    r = client.post("/api/login", json={"username": name, "password": "test123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_pet_create_and_list(client):
    """创建宠物后，宠物列表应能查到"""
    auth = _register_login(client, f"pytest_{uuid.uuid4().hex[:8]}")
    r = client.post("/api/pets", json={"name": "测试猫", "species": "猫咪", "age": 2, "weight": 3.5}, headers=auth)
    assert r.status_code == 201
    pets = client.get("/api/pets", headers=auth).json()
    assert any(p["name"] == "测试猫" for p in pets)


def test_pet_delete(client):
    """删除宠物后，列表不再包含该宠物"""
    auth = _register_login(client, f"pytest_{uuid.uuid4().hex[:8]}")
    client.post("/api/pets", json={"name": "待删狗", "species": "狗狗"}, headers=auth)
    pet_id = next(p["id"] for p in client.get("/api/pets", headers=auth).json() if p["name"] == "待删狗")
    assert client.delete(f"/api/pets/{pet_id}", headers=auth).status_code == 200
    pets = client.get("/api/pets", headers=auth).json()
    assert all(p["id"] != pet_id for p in pets)
