# -*- coding: utf-8 -*-
"""
爬虫模块。

对外提供两个数据来源：
1. 真实爬虫 fetch_from_web()：requests + BeautifulSoup 抓公开页面，
   微博搜索页这种地方不登录会被反爬，所以只是模板，抓不到就返回空列表；
2. 兜底数据 load_from_csv()：读 data/sample_data.csv 里 50 条人工整理的模拟数据。

main() 会先试真实爬虫，抓到就用抓到的，抓不到自动切到 CSV。
这样答辩现场就算断网、或者对方网站改了结构，流程也能完整跑下来。

用法：
    python spider.py                    自动模式，先爬后兜底
    python spider.py --source csv       只用本地 CSV
    python spider.py --source web       只用真实爬虫（失败就退出）
    python spider.py --keyword 地震     指定搜索关键词（只对真实爬虫有意义）
"""

import argparse
import csv
import os
import random
import re
import time

import config

# requests / bs4 不一定装了，这里做了兼容，
# 缺库的时候只影响真实爬虫，读 CSV 那条路照样能走
try:
    import requests
    from bs4 import BeautifulSoup
    HAS_SPIDER_LIB = True
except ImportError:
    HAS_SPIDER_LIB = False

# 几个常见的浏览器 UA，每次随机挑一个，降低被直接拦掉的概率
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]


def build_headers():
    """拼一个像浏览器发出来的请求头。"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }


def clean_text(raw):
    """把抓下来的文本清一遍：去标签、去多余空白、去掉转发前缀。"""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", str(raw))
    text = text.replace("\u200b", "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    # "//@某某:" 这种转发链在微博里很常见，留着会干扰分词
    text = re.sub(r"//@[^:：]{1,20}[:：]", " ", text)
    return text.strip()


# ============================================================
# 一、真实爬虫（模板）
# ============================================================

def fetch_html(url, timeout=8, retry=2):
    """
    请求一个页面，返回 HTML 文本。失败返回 None。

    简单做了重试：连续两次拿不到就放弃，不要死循环。
    反爬常见表现是返回 403/418，或者给一个要求登录的页面，这两种都算失败。
    """
    if not HAS_SPIDER_LIB:
        print("  [跳过] 没装 requests / beautifulsoup4，不能走真实爬虫")
        return None

    for i in range(retry):
        try:
            resp = requests.get(url, headers=build_headers(), timeout=timeout)
            if resp.status_code == 200:
                # 有些站点不管编码，中文会乱码，这里强制按页面声明的编码来解
                if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
                    resp.encoding = "utf-8"
                return resp.text
            print("  [重试] 第 {0} 次请求返回状态码 {1}".format(i + 1, resp.status_code))
        except Exception as e:
            print("  [重试] 第 {0} 次请求出错：{1}".format(i + 1, e))
        # 随机等一下，抓太快容易被封
        time.sleep(random.uniform(1.0, 2.5))
    return None


def fetch_weibo(keyword, pages=2):
    """
    微博搜索页爬虫模板。

    说明一下为什么它经常抓不到东西：微博的搜索页内容主要靠 JS 渲染，
    直接 requests 拿到的是空壳 HTML；就算拿到，unknown 反爬也会要求登录。
    所以这里老老实实按模板写，解析规则按常见的 .card-wrap 结构来，
    真要用得配 cookie 或者换成 Selenium。
    """
    results = []
    base_url = "https://s.weibo.com/weibo?q={0}&page={1}"

    for page in range(1, pages + 1):
        url = base_url.format(keyword, page)
        print("  正在抓取：{0}".format(url))
        html = fetch_html(url)
        if not html:
            return results

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("div.card-wrap")
        print("    本页解析到 {0} 个节点".format(len(cards)))

        for card in cards:
            # 正文在 p.txt 里，作者在 a.name 里，时间在 a[href*=date] 的 title 属性上
            text_node = card.select_one("p.txt")
            if not text_node:
                continue
            text = clean_text(text_node.get_text())
            if len(text) < 8:
                continue

            time_node = card.select_one("a[href*='date']")
            img_node = card.select_one("img")

            results.append({
                "text": text,
                "image_url": img_node.get("src") if img_node else None,
                "source": "微博",
                "timestamp": time_node.get("title") if time_node else None,
            })

        # 页与页之间歇久一点
        time.sleep(random.uniform(2.0, 4.0))

    return results


def fetch_from_web(keyword="谣言", pages=2):
    """
    真实爬虫的入口。

    目前只接了微博一个模板，返回列表（可能为空）。
    想加别的站点就在这加分支，函数签名保持一致就行：
    返回的每项包含 text / image_url / source / timestamp 四个字段。
    """
    if not HAS_SPIDER_LIB:
        print("缺少 requests 或 beautifulsoup4，真实爬虫不可用。")
        print("安装命令：pip install requests beautifulsoup4 lxml")
        return []

    print("开始真实爬虫，关键词：{0}".format(keyword))
    try:
        data = fetch_weibo(keyword, pages)
    except Exception as e:
        # 爬虫是最容易出意外的一环，任何异常都吞掉走兜底
        print("爬虫过程出错：{0}".format(e))
        return []

    # 抓回来的数据统一清一遍，去掉空文本和重复
    cleaned, seen = [], set()
    for item in data:
        text = clean_text(item.get("text"))
        if not text or text in seen:
            continue
        seen.add(text)
        item["text"] = text
        cleaned.append(item)

    print("真实爬虫结束，共拿到 {0} 条有效数据".format(len(cleaned)))
    return cleaned


# ============================================================
# 二、本地 CSV 兜底
# ============================================================

def load_from_csv(path=None, limit=None):
    """
    读 data/sample_data.csv。

    文件保存时带了 BOM（为了让 Excel 打开不乱码），所以用 utf-8-sig 读；
    如果读出来还是乱码，说明文件被别的工具存成了 GBK，这里再兜一次 GBK。
    """
    path = path or config.SAMPLE_CSV_PATH
    if not os.path.exists(path):
        print("找不到数据文件：{0}".format(path))
        return []

    rows = []
    for encoding in ("utf-8-sig", "gbk"):
        try:
            with open(path, "r", encoding=encoding, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    text = (row.get("text") or "").strip()
                    if not text:
                        continue
                    rows.append({
                        "text": text,
                        # 空字符串统一成 None，免得往数据库里塞空串
                        "image_url": (row.get("image_url") or "").strip() or None,
                        "source": (row.get("source") or "").strip() or "未知来源",
                        "timestamp": (row.get("timestamp") or "").strip() or None,
                        # 下面这两列是给训练用的标注，入库时怎么处理由调用方决定
                        "nature": (row.get("nature") or "").strip() or None,
                        "status": (row.get("status") or "").strip() or None,
                    })
            break
        except UnicodeDecodeError:
            print("用 {0} 解码失败，换一种编码再试".format(encoding))
            rows = []

    print("从 CSV 读到 {0} 条数据".format(len(rows)))
    if limit:
        rows = rows[:limit]
    return rows


# ============================================================
# 三、入库
# ============================================================

def save_to_db(records, keep_label=False):
    """
    把数据写进 raw_messages。

    keep_label 控制 CSV 里的 nature 要不要入库：
    False（默认）时置空，因为我们希望库里 nature 只由“人工校验”写入，
    未处理的消息不应该提前有性质；CSV 里的标注留给 train_model.py 当训练标签用。
    返回实际插入条数。
    """
    if not records:
        return 0

    import database   # 放在函数里导入，这样 --help 之类不需要连数据库

    payload = []
    for item in records:
        payload.append({
            "text": item.get("text"),
            "image_url": item.get("image_url"),
            "source": item.get("source"),
            "timestamp": item.get("timestamp"),
            "nature": item.get("nature") if keep_label else None,
            "status": database.STATUS_PENDING,
        })

    return database.insert_raw_messages(payload)


def main():
    parser = argparse.ArgumentParser(description="社交媒体谣言数据采集")
    parser.add_argument("--source", choices=["auto", "web", "csv"], default="auto",
                        help="数据来源：auto 先爬后兜底 / web 只爬 / csv 只读本地")
    parser.add_argument("--keyword", default="谣言", help="真实爬虫的搜索关键词")
    parser.add_argument("--pages", type=int, default=2, help="真实爬虫抓几页")
    parser.add_argument("--limit", type=int, default=None, help="最多入库多少条")
    parser.add_argument("--keep-label", action="store_true",
                        help="把 CSV 里的 nature 标注也写进库（默认不写）")
    args = parser.parse_args()

    print("=" * 60)
    print("社交媒体谣言检测系统 - 数据采集")
    print("=" * 60)

    records = []
    if args.source in ("auto", "web"):
        records = fetch_from_web(args.keyword, args.pages)

    if not records and args.source in ("auto", "csv"):
        if args.source == "auto":
            print("\n真实爬虫没拿到数据，切换到本地 CSV 兜底。")
        print("读取本地数据文件...")
        records = load_from_csv(limit=args.limit)

    if not records:
        print("\n没有采集到任何数据，请检查 --source 参数或者 data/sample_data.csv 是否存在。")
        return 1

    print("\n开始写入数据库...")
    try:
        count = save_to_db(records, keep_label=args.keep_label)
    except Exception as e:
        print("写库失败：{0}".format(e))
        print("先确认 MySQL 起没起、config.py 里的密码改了没，然后跑一次 python init_db.py。")
        return 1

    print("本次新增 {0} 条（重复的正文会被自动跳过）".format(count))

    # 入库后报一下当前总量，方便确认
    try:
        import database
        overview = database.get_overview()
        print("库中现有消息 {0} 条，其中未处理 {1} 条".format(
            overview["total"], overview["pending"]))
    except Exception:
        pass

    print("\n数据采集完成。下一步跑 python preprocess.py 做清洗和分词。")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())