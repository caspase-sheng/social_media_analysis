# -*- coding: utf-8 -*-
"""
爬虫模块。

数据来源分两块：
1. 真实爬虫 fetch_from_web()：抓六个公开的新闻热榜 / 滚动页，
   都是不用登录就能打开的地址，抓回来的是标题 + 摘要；
2. 兜底数据 load_from_csv()：读 data/sample_data.csv 里 50 条人工整理的模拟数据。

main() 会先试真实爬虫，抓到就用抓到的，抓不到自动切到 CSV。
这样答辩现场就算断网、或者某个站点改了页面结构，流程也能完整跑下来。

接进来的站点（都在 fetch_from_web 的 FETCHERS 里，想加就照着写一个函数）：
    今日头条热榜     JSON 接口
    百度热搜榜       HTML
    腾讯新闻热榜     JSON 接口
    新浪滚动新闻     JSON 接口，支持翻页
    澎湃新闻首页     HTML
    中新网滚动新闻   HTML，支持翻页（页面声明的编码和实际不一致，代码里自动识别）

关于 --keyword：这几个源都是通用热榜，不是按关键词搜索，
所以 keyword 目前只用于打印提示，不影响抓取结果。

用法：
    python spider.py                    自动模式，先爬后兜底
    python spider.py --source web       只用真实爬虫
    python spider.py --source csv       只用本地 CSV
    python spider.py --pages 2          新浪、中新网这类支持翻页的多抓一页
"""

import argparse
import csv
import datetime
import json
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
    # "//@某某:" 这种转发链在社交平台里很常见，留着会干扰分词
    text = re.sub(r"//@[^:：]{1,20}[:：]", " ", text)
    # 百度热搜的卡片里带着"查看更多>"这种引导文字，是页面控件不是正文内容，
    # 不删掉的话它会变成词云里最高频的词
    text = re.sub(r"查看更多>?", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def pick_image(value):
    """
    从图片字段里取出真正的图片地址。

    不同接口给的格式不一样：百度、新浪给的是字符串或者带 u 的字典，
    今日头条给的是 {'uri': ..., 'url': ...} 这种字典。
    统一在这里取字符串，不然字典塞进数据库字段会直接报错。
    """
    if isinstance(value, dict):
        for key in ("url", "u", "src", "uri"):
            if value.get(key):
                return value[key]
        return None
    return value or None


def join_text(title, summary):
    """
    标题和摘要拼成一条正文。

    本项目抓的是新闻列表，只有标题和一小段摘要，分开存意义不大，
    拼成一句话更接近“一条消息”，分词和关键词搜索都好处理。
    摘要开头如果和标题重复，就只留摘要，避免同一句话出现两遍。
    """
    title = (title or "").strip()
    summary = (summary or "").strip()
    if title and summary:
        if summary.startswith(title):
            return summary
        return title + "。" + summary
    return title or summary


def ts_from_unix(value):
    """把 unix 时间戳转成 'YYYY-MM-DD HH:MM:SS'，转不了返回 None。"""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    # 有的接口给的是毫秒，大于这个量级就按毫秒处理
    if seconds > 100000000000:
        seconds = seconds // 1000
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))
    except (ValueError, OSError):
        return None


def ts_from_mmdd(text):
    """'10-3 22:30' 这种没写年份的时间，补上当前年份。"""
    if not text:
        return None
    matched = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", str(text).strip())
    if not matched:
        return None
    month, day, hour, minute = (int(x) for x in matched.groups())
    year = datetime.datetime.now().year
    return "{0:04d}-{1:02d}-{2:02d} {3:02d}:{4:02d}:00".format(year, month, day, hour, minute)


# ============================================================
# 一、请求封装
# ============================================================

def decode_html(resp, encoding=None):
    """
    把响应内容解码成文本。

    有的站点页面里声明的编码和实际内容对不上：中新网声明的是 GBK，
    实际发的是 UTF-8，直接按声明解会得到一屏乱码。
    这里的做法是先按 UTF-8 试（现在绝大多数中文站点都是 UTF-8，
    而 GBK 的字节用 UTF-8 解一般会直接报错），失败了再按声明的编码、
    最后按 GB18030（GBK 的超集，能覆盖大部分老站点）兜底。
    """
    raw = resp.content
    if encoding:
        candidates = [encoding]
    else:
        candidates = ["utf-8", resp.encoding, "gb18030"]

    tried = []
    for enc in candidates:
        if not enc or enc.lower() in tried:
            continue
        tried.append(enc.lower())
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 解出来带替换符，说明这个编码不对，换下一个
        if "\ufffd" not in text:
            return text
    # 全都不行就宽松解一把，宁可个别字乱码也别让程序崩掉
    return raw.decode("utf-8", errors="ignore")


def fetch_html(url, timeout=8, retry=2, encoding=None):
    """
    请求一个页面，返回 HTML 文本。失败返回 None。

    简单做了重试：连续两次拿不到就放弃，不要死循环。
    encoding 一般不用传，decode_html 会自动识别；
    确定某个站点就是某个编码时再手动指定。
    """
    if not HAS_SPIDER_LIB:
        print("  [跳过] 没装 requests / beautifulsoup4，不能走真实爬虫")
        return None

    for i in range(retry):
        try:
            resp = requests.get(url, headers=build_headers(), timeout=timeout)
            if resp.status_code == 200:
                return decode_html(resp, encoding)
            print("  [重试] 第 {0} 次请求返回状态码 {1}".format(i + 1, resp.status_code))
        except Exception as e:
            print("  [重试] 第 {0} 次请求出错：{1}".format(i + 1, e))
        # 随机等一下，抓太快容易被封
        time.sleep(random.uniform(1.0, 2.5))
    return None


def fetch_json(url, timeout=8, retry=2):
    """请求一个返回 JSON 的接口，失败返回 None。"""
    if not HAS_SPIDER_LIB:
        return None

    for i in range(retry):
        try:
            resp = requests.get(url, headers=build_headers(), timeout=timeout)
            if resp.status_code == 200:
                try:
                    return json.loads(decode_html(resp))
                except ValueError:
                    # 返回的不是 JSON（可能是被拦了给了个 HTML 页面）
                    print("     返回内容不是 JSON，跳过")
                    return None
            print("  [重试] 第 {0} 次请求返回状态码 {1}".format(i + 1, resp.status_code))
        except Exception as e:
            print("  [重试] 第 {0} 次请求出错：{1}".format(i + 1, e))
        time.sleep(random.uniform(0.8, 1.8))
    return None


# ============================================================
# 二、各个站点的抓取函数
#
# 每个函数都返回字典列表，每项固定四个字段：
#     text / image_url / source / timestamp
# 抓不到就返回空列表，不要抛异常（异常在 fetch_from_web 里统一兜）。
# ============================================================

def fetch_toutiao(pages=1):
    """今日头条热榜。接口直接给 JSON，字段是大写开头的。"""
    url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
    data = fetch_json(url)
    if not data:
        return []

    items = []
    # 置顶的和普通榜单分开放在两个键里，都要取
    for group in ("fixed_top_data", "data"):
        for row in data.get(group) or []:
            title = clean_text(row.get("Title"))
            if not title:
                continue
            items.append({
                "text": title,
                "image_url": pick_image(row.get("Image")),
                "source": "今日头条",
                # 热榜接口只给热度值，没有发布时间
                "timestamp": None,
            })
    return items


def fetch_baidu(pages=1):
    """百度热搜榜。HTML 页面，卡片结构。"""
    url = "https://top.baidu.com/board?tab=realtime"
    html = fetch_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    items = []
    # 卡片的 class 带了一串随机后缀，用子串匹配更稳
    for card in soup.select('div[class*="category-wrap"]'):
        title_node = card.select_one('div[class*="c-single-text-ellipsis"]')
        if not title_node:
            continue
        title = clean_text(title_node.get_text())
        if not title:
            continue
        desc_node = card.select_one('div[class*="hot-desc"]')
        desc = clean_text(desc_node.get_text()) if desc_node else ""
        img_node = card.select_one("img")
        items.append({
            "text": join_text(title, desc),
            "image_url": pick_image(img_node.get("src")) if img_node else None,
            "source": "百度热搜",
            "timestamp": None,
        })
    return items


def fetch_tencent(pages=1):
    """腾讯新闻热榜。JSON 接口，榜单第一条是横幅，没有 source，要跳过。"""
    url = "https://r.inews.qq.com/gw/event/hot_ranking_list?page_size=30"
    data = fetch_json(url)
    if not data:
        return []

    idlist = data.get("idlist") or []
    if not idlist:
        return []

    items = []
    for index, row in enumerate(idlist[0].get("newslist") or []):
        # 第一条是页面顶部的横幅，字段和正经新闻不一样
        if index == 0 and not row.get("source"):
            continue
        title = clean_text(row.get("title"))
        if not title:
            continue
        items.append({
            "text": join_text(title, row.get("abstract")),
            "image_url": None,
            "source": "腾讯新闻",
            "timestamp": row.get("time") or ts_from_unix(row.get("timestamp")),
        })
    return items


def fetch_sina(pages=2):
    """新浪滚动新闻。JSON 接口，支持翻页，摘要字段是 intro。"""
    items = []
    for page in range(1, pages + 1):
        url = ("https://feed.mix.sina.com.cn/api/roll/get"
               "?pageid=153&lid=2509&num=20&page={0}".format(page))
        data = fetch_json(url)
        if not data:
            continue

        result = data.get("result") or {}
        for row in result.get("data") or []:
            title = clean_text(row.get("title"))
            if not title:
                continue
            # intro 是真实摘要，summary 多数时候是空的，两个都试一下
            desc = clean_text(row.get("intro") or row.get("summary"))
            items.append({
                "text": join_text(title, desc),
                "image_url": pick_image(row.get("img")),
                # 统一写平台名，不用 row 里的 media_name：
                # 六个源的 source 保持一致，训练时需要按来源判断哪些是爬取的新闻
                "source": "新浪新闻",
                "timestamp": ts_from_unix(row.get("ctime")),
            })
        time.sleep(random.uniform(0.8, 1.6))
    return items


def fetch_thepaper(pages=1):
    """
    澎湃新闻首页。

    详情页的链接都带 newsDetail_forward 这一段，所以遍历所有 a 标签过滤。
    （试过用属性选择器 a[href^="/newsDetail_forward_"]，实际匹配不到，
     估计 href 前面带了别的路径，还是按子串过滤靠谱。）
    """
    html = fetch_html("https://www.thepaper.cn/")
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    items = []
    for link in soup.find_all("a", href=True):
        if "newsDetail_forward" not in link["href"]:
            continue
        title = clean_text(link.get_text())
        # 首页里有些链接是图标或栏目标签，太短的直接扔掉
        if len(title) < 6:
            continue
        items.append({
            "text": title,
            "image_url": None,
            "source": "澎湃新闻",
            "timestamp": None,
        })
    return items


def fetch_chinanews(pages=1):
    """中新网滚动新闻。时间格式 '10-3 22:30' 没有年份，要补上。"""
    items = []
    for page in range(1, pages + 1):
        url = "https://www.chinanews.com.cn/scroll-news/news{0}.html".format(page)
        html = fetch_html(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "lxml")
        for li in soup.select("div.content_list li"):
            link = li.select_one("div.dd_bt a")
            if not link:
                continue
            title = clean_text(link.get_text())
            if not title:
                continue
            time_node = li.select_one("div.dd_time")
            items.append({
                "text": title,
                "image_url": None,
                "source": "中新网",
                "timestamp": ts_from_mmdd(time_node.get_text() if time_node else None),
            })
        time.sleep(random.uniform(0.8, 1.6))
    return items


# 站点清单：名字只是为了打印日志好看，加新站点就在这里加一行
FETCHERS = (
    ("今日头条热榜", fetch_toutiao),
    ("百度热搜榜", fetch_baidu),
    ("腾讯新闻热榜", fetch_tencent),
    ("新浪滚动新闻", fetch_sina),
    ("澎湃新闻", fetch_thepaper),
    ("中新网滚动", fetch_chinanews),
)


def fetch_from_web(keyword="", pages=2):
    """
    真实爬虫的入口，把上面几个站点都跑一遍。

    单个站点出错不影响其他站点：每个都在自己的 try 里，失败就是空列表。
    返回的每项包含 text / image_url / source / timestamp 四个字段。
    """
    if not HAS_SPIDER_LIB:
        print("缺少 requests 或 beautifulsoup4，真实爬虫不可用。")
        print("安装命令：pip install requests beautifulsoup4 lxml")
        return []

    print("开始真实爬虫（关键词参数 {0} 只作提示，热榜类站点不按关键词搜索）".format(keyword or "未指定"))

    collected = []
    for name, func in FETCHERS:
        print("  正在抓取：{0}".format(name))
        try:
            got = func(pages)
        except Exception as e:
            # 爬虫是最容易出意外的一环，单站异常吞掉，继续抓下一个
            print("    抓取出错：{0}".format(e))
            got = []
        print("    拿到 {0} 条".format(len(got)))
        collected.extend(got)
        # 站与站之间歇一下，别把人家服务器打得太频繁
        time.sleep(random.uniform(1.0, 2.0))

    # 抓回来的数据统一清一遍，去掉空文本、过短的和重复的
    cleaned, seen = [], set()
    for item in collected:
        text = clean_text(item.get("text"))
        if len(text) < 6 or text in seen:
            continue
        seen.add(text)
        item["text"] = text
        cleaned.append(item)

    print("真实爬虫结束，共拿到 {0} 条有效数据".format(len(cleaned)))
    return cleaned


# ============================================================
# 三、本地 CSV 兜底
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
# 四、入库
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
    parser.add_argument("--keyword", default="",
                        help="关键词，目前只用于提示（热榜类站点不按关键词搜索）")
    parser.add_argument("--pages", type=int, default=2, help="新浪、中新网这类多页站点抓几页")
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
        print("\n没有采集到任何数据，请检查 --source 参数、网络，或者 data/sample_data.csv 是否存在。")
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