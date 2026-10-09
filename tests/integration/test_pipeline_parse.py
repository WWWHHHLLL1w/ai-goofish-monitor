import asyncio

from src.parsers import _parse_search_results_json


def test_parse_search_results(load_json_fixture):
    raw = load_json_fixture("search_results.json")
    items = asyncio.run(_parse_search_results_json(raw, source="search"))
    assert len(items) == 1
    item = items[0]
    assert item["商品标题"] == "Sony A7M4 Body"
    assert item["当前售价"].startswith("¥")
    assert "包邮" in item["商品标签"]
    assert "验货宝" in item["商品标签"]
    assert item["商品链接"].startswith("https://www.goofish.com/")
