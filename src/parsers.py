import json
from datetime import datetime

from src.utils import safe_get


async def _parse_search_results_json(json_data: dict, source: str) -> list[dict]:
    """解析闲鱼搜索列表的基础商品信息。"""
    page_data = []
    try:
        items = await safe_get(json_data, "data", "resultList", default=[])
        if not items:
            print(f"LOG: ({source}) API响应中未找到商品列表 (resultList)。")
            return []

        for item in items:
            main_data = await safe_get(item, "data", "item", "main", "exContent", default={})
            click_params = await safe_get(item, "data", "item", "main", "clickParam", "args", default={})
            title = await safe_get(main_data, "title", default="未知标题")
            price_parts = await safe_get(main_data, "price", default=[])
            price = (
                "".join(
                    str(part.get("text", ""))
                    for part in price_parts
                    if isinstance(part, dict)
                ).replace("当前价", "").strip()
                if isinstance(price_parts, list)
                else "价格异常"
            )
            price = str(price)
            if "万" in price:
                price = f"¥{float(price.replace('¥', '').replace('万', '')) * 10000:.0f}"

            area = await safe_get(main_data, "area", default="地区未知")
            seller = await safe_get(main_data, "userNickName", default="匿名卖家")
            raw_link = await safe_get(item, "data", "item", "main", "targetUrl", default="")
            image_url = await safe_get(main_data, "picUrl", default="")
            publish_time = click_params.get("publishTime", "")
            item_id = await safe_get(main_data, "itemId", default="未知ID")
            original_price = await safe_get(main_data, "oriPrice", default="暂无")
            wants_count = await safe_get(click_params, "wantNum", default="NaN")

            tags = []
            if await safe_get(click_params, "tag") == "freeship":
                tags.append("包邮")
            r1_tags = await safe_get(main_data, "fishTags", "r1", "tagList", default=[])
            for tag_item in r1_tags:
                content = await safe_get(tag_item, "data", "content", default="")
                if "验货宝" in content:
                    tags.append("验货宝")

            if isinstance(publish_time, str) and publish_time.isdigit():
                publish_time = datetime.fromtimestamp(int(publish_time) / 1000).strftime(
                    "%Y-%m-%d %H:%M"
                )
            else:
                publish_time = "未知时间"

            page_data.append(
                {
                    "商品标题": title,
                    "商品主图链接": image_url,
                    "当前售价": price,
                    "商品原价": original_price,
                    "“想要”人数": wants_count,
                    "商品标签": tags,
                    "发货地区": area,
                    "卖家昵称": seller,
                    "商品链接": raw_link.replace("fleamarket://", "https://www.goofish.com/"),
                    "发布时间": publish_time,
                    "商品ID": item_id,
                }
            )
        print(f"LOG: ({source}) 成功解析到 {len(page_data)} 条商品基础信息。")
        return page_data
    except Exception as exc:
        print(f"LOG: ({source}) JSON数据处理异常: {exc}")
        return []
