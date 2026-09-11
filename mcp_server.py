# mcp_server.py —— 把项目工具包装成 MCP Server（独立运行，不改动 backend.py）
# 启动：mcp run mcp_server.py   （stdio 传输，可被任意 MCP 宿主连接）
import asyncio, json, os
import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pet-tools")

@mcp.tool()
async def search_nearby_hospitals(city: str) -> str:
    """当宠物出现健康问题、生病、受伤或需要医疗帮助时，搜索指定城市内的宠物医院。参数 city: 城市名称，例如"深圳"。返回：医院名称、地址、距离、电话等信息的 JSON 字符串"""
    AMAP_KEY = os.getenv("AMAP_KEY")
    if not AMAP_KEY:
        return json.dumps({"error": "未配置 AMAP_KEY"}, ensure_ascii=False)
    try:
        url = f"https://restapi.amap.com/v3/place/text?keywords=宠物医院&city={city}&offset=10&key={AMAP_KEY}"
        response = await asyncio.to_thread(requests.get, url)
        if response.status_code != 200:
            return json.dumps({"error": "无法获取医院数据"}, ensure_ascii=False)
        data = response.json()
        hospitals = [{
            "name": poi.get("name"),
            "address": poi.get("address"),
            "distance": poi.get("distance", "未知"),
            "phone": poi.get("tel", "未知")
        } for poi in data.get("pois", [])]
        return json.dumps(hospitals, ensure_ascii=False)
    except Exception:
        return json.dumps({"error": "无法获取医院数据"}, ensure_ascii=False)

@mcp.tool()
async def web_search(query: str) -> str:
    """当用户需要最新资讯、实时信息、天气、新闻、或知识库中无法覆盖的内容时，联网搜索。参数 query: 搜索关键词。返回：相关网页结果的 JSON 字符串"""
    BOCHA_API_KEY = os.getenv("WEB_SEARCH_KEY", "")
    if not BOCHA_API_KEY:
        return json.dumps({"error": "未配置 WEB_SEARCH_KEY"}, ensure_ascii=False)
    try:
        response = await asyncio.to_thread(requests.post, "https://api.bochaai.com/v1/web-search",
            json={"query": query, "summary": True, "count": 5},
            headers={"Authorization": f"Bearer {BOCHA_API_KEY}"})
        if response.status_code != 200:
            return json.dumps({"error": "搜索服务异常"}, ensure_ascii=False)
        data = response.json()
        results = [{"title": r.get("name"), "url": r.get("url"),
                    "content": (r.get("summary") or r.get("snippet") or "")[:200]}
                   for r in data.get("data", {}).get("webPages", {}).get("value", [])]
        return json.dumps(results, ensure_ascii=False)
    except Exception:
        return json.dumps({"error": "搜索失败"}, ensure_ascii=False)

if __name__ == "__main__":
    mcp.run()
