# -*- coding: utf-8 -*-
"""
爬虫模块。

数据来源分三块：
1. 新闻热榜 fetch_from_web() 里的 FETCHERS：抓六个公开的热榜 / 滚动页，
   不用登录就能打开，抓回来的是标题 + 摘要；
2. 辟谣平台 fetch_from_web() 里的 RUMOR_FETCHERS：抓中国互联网联合辟谣平台。
   这个源存在的意义是补“谣言”样本——热榜和滚动页出的基本都是正常新闻，
   训练集里正类一直是那几十条模拟数据，模型学不到“网传/据说/赶紧别吃”这类措辞；
3. 兜底数据 load_from_csv()：读 data/sample_data.csv 里人工整理的数据。

main() 会先试真实爬虫，抓到就用抓到的，抓不到自动切到 CSV。
这样答辩现场就算断网、或者某个站点改了页面结构，流程也能完整跑下来。

接进来的站点（想加就照着写一个函数，再挂到对应的清单里）：
    新闻热榜组
        今日头条热榜     JSON 接口
        百度热搜榜       HTML
        腾讯新闻热榜     JSON 接口
        新浪滚动新闻     JSON 接口，支持翻页
        澎湃新闻首页     HTML
        中新网滚动新闻   HTML，支持翻页（页面声明的编码和实际不一致，代码里自动识别）
    辟谣组
        中国互联网联合辟谣平台   HTML，列表标题 + “今日辟谣”日报正文

谣言样本怎么进训练集：库里 raw_messages.nature 只由人工校验写入，
爬来的谣言不会带标签进库；想让它们直接变成训练样本，跑的时候加 --append-rumors，
会把抓到的谣言追加到 data/sample_data.csv 末尾，train_model.py 读 CSV 时就是正类。

关于 --keyword：这几个源都是通用热榜 / 栏目列表，不是按关键词搜索，
所以 keyword 目前只用于打印提示，不影响抓取结果。

用法：
    python spider.py                    自动模式，先爬后兜底
    python spider.py --source web       只用真实爬虫（新闻 + 辟谣）
    python spider.py --source csv       只用本地 CSV
    python spider.py --pages 2          新浪、中新网、今日辟谣这类多抓一页
    python spider.py --append-rumors    把抓到的谣言追加进 sample_data.csv
    python spider.py --no-rumor         不抓辟谣平台，只要新闻
"""

import argparse
import csv
import datetime
import json
import os
import random
import re
import time
from html import unescape

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

# 辟谣平台的来源名，写进 raw_messages.source 用的就是这个值。
# 注意它不在 config.NEWS_SOURCES 里：NEWS_SOURCES 是“一律按非谣言训练”的新闻源，
# 这里是谣言样本的来源，两者不能混在一起，否则训练时标签会打架。
RUMOR_SOURCE_PIYAO = "中国互联网联合辟谣平台"

# 和 database.NATURE_RUMOR / NATURE_NORMAL 保持一致。
# 这里没有 import database：那样没装数据库驱动时连 --help 都跑不起来，
# 而这两个常量只是字符串，自己写一份更省事。
NATURE_RUMOR = "谣言"
NATURE_NORMAL = "非谣言"


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


# ============================================================
# 二点五、辟谣平台（谣言样本）
#
# 和上面的新闻源不一样，这里抓的目的是拿“正类样本”。理由：
# 新闻热榜出的基本都是正常新闻，训练集里谣言一直是那几十条模拟数据，
# 模型因此学不到谣言特有的措辞（“网传”“据说”“赶紧别吃了”），
# 上线后遇到真正的谣言反而给不出高分。辟谣平台上的每一条，
# 本身就是一个被官方否认的说法，拿来当谣言样本最合适。
# ============================================================

PIYAO_HOME = "https://www.piyao.org.cn"

# 辟谣平台列表页里的文章链接长这样：20261013/f06e8d2a…/c.html
PIYAO_ARTICLE_RE = re.compile(r'<a[^>]+href="(\d{8}/[0-9a-f]{32}/c\.html)"[^>]*>(.*?)</a>', re.S)

# 标题里带这些词的，是榜单 / 通报 / 活动 / 盘点一类的栏目稿，说的是工作不是传言，一律排掉。
# 这一条判断比“有没有谣言特征词”优先：栏目稿标题里也常带“辟谣”“谣言”，
# 但它们本身不是某个被辟谣的说法，收进来会让正类样本变得很脏。
PIYAO_META_WORDS = (
    "辟谣榜", "榜单", "月榜", "季榜", "综述", "沙龙", "培训班", "宣传活动", "专列",
    "倡议", "公开课", "通报", "典型案例", "专项行动", "访谈", "面对面", "举行",
    "开展", "正式发布", "点亮", "手册", "接力", "今日辟谣", "趣味视频", "说清楚",
    "并举", "讲好", "盘点", "合集", "专栏",
)

# 标题里带这些词的，基本可以认定它讲的就是一个被辟谣的说法
PIYAO_CLAIM_WORDS = (
    "网传", "谣言", "不实", "假的", "假消息", "系误读", "系造谣", "系AI伪造",
    "辟谣", "真相", "误区", "警惕", "骗局", "当心", "小心", "别上当", "别信",
    "真的吗", "？", "?",
)

# 辟谣稿挂在结尾的判断语。去掉以后剩下的才是“传言本身”的说法
DEBUNK_TAILS = (
    "系谣言", "是谣言", "为谣言", "系误读", "系造谣", "系AI伪造", "纯属AI虚假编造",
    "纯属谣言", "官方辟谣", "辟谣！", "谣言！", "不实", "假的！", "假的",
    "别上当", "官方回应", "当地回应", "紧急提醒",
)


def strip_source_prefix(text):
    """
    去掉列表标题前面挂的来源标签。

    辟谣平台的聚合列表里，一条是这样写的：
        <span>首都网警 | </span> 网传某某情况？警方通报
    竖线后面才是标题本身，所以取最后一段。
    """
    if not text:
        return ""
    if "|" in text:
        text = text.split("|")[-1]
    return text.strip()


def strip_debunk_tail(text):
    """
    去掉“系谣言”“不实”这类挂在结尾的结论，只留传言本身。

    这些词是发布方下的判断，不是传言的内容。留着的话模型会走捷径——
    只要看到“系谣言”就给高分，指标看着很漂亮，实际学的是发布方的措辞，
    换成真实用户发的消息立刻失效。
    """
    if not text:
        return ""
    result = text.strip()
    # 可能一层套一层（“……不实”前面还有“系谣言”），循环剥到剥不动为止
    changed = True
    while changed:
        changed = False
        for tail in DEBUNK_TAILS:
            if result.endswith(tail):
                result = result[: -len(tail)].strip(" 　，,。.、；;：:！!？?…～~")
                changed = True
    return result


def is_rumor_claim(title):
    """判断一条列表标题是不是“某个被辟谣的说法”，是的话才拿来做谣言样本。"""
    if not title or len(title) < 8:
        return False
    # 先排栏目稿，这类标题里也有“辟谣”两个字，但说的不是传言
    for word in PIYAO_META_WORDS:
        if word in title:
            return False
    for word in PIYAO_CLAIM_WORDS:
        if word in title:
            return True
    return False


def parse_piyao_roundup(page):
    """
    从“今日辟谣”日报的正文里按条抽谣言。

    日报的结构很规整：每条先一行小标题，形如“辟 谣 XXX”“科 普 XXX”“通 报 XXX”，
    紧接着一行“详情：……”。这里只取“辟 谣”开头的——它后面跟的就是一个
    被官方明确否定的说法，拿来当正类正合适；“科 普”讲知识、“通 报”讲案例，
    都不算传言，收了反而污染样本。
    """
    paragraphs = []
    for raw in re.findall(r"<p[^>]*>(.*?)</p>", page, re.S):
        text = unescape(re.sub(r"<[^>]+>", " ", raw))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            paragraphs.append(text)

    items = []
    for index, text in enumerate(paragraphs):
        matched = re.match(r"^辟\s*谣\s*[：:]?\s*(.+)$", text)
        if not matched:
            continue
        claim = strip_debunk_tail(matched.group(1).strip())
        if len(claim) < 6:
            continue

        # 后面紧跟的“详情”是这条传言的来龙去脉，拼上更像一条真实消息，
        # 也让模型能学到“有人冒充……”“短视频平台流传……”这类叙述方式
        detail = ""
        if index + 1 < len(paragraphs) and paragraphs[index + 1].startswith("详情"):
            detail = re.sub(r"^详情[：:]\s*", "", paragraphs[index + 1]).strip()

        items.append({
            "text": join_text(claim, detail),
            "image_url": None,
            "source": RUMOR_SOURCE_PIYAO,
            "timestamp": None,
            "nature": NATURE_RUMOR,
        })
    return items


def fetch_piyao(pages=1):
    """
    中国互联网联合辟谣平台。

    取两部分：
    1. 首页列表里本身就写成传言的标题（“网传……？”“……系谣言”），一条就是一句传言；
    2. “今日辟谣”日报的正文，一篇日报里通常有好几条，信息量比列表标题大。
       pages 控制最多再跟进几篇日报，默认 1 篇，演示时够用又不至于太慢。
    """
    home = fetch_html(PIYAO_HOME)
    if not home:
        return []

    items = []
    seen = set()

    def add(item):
        """统一的收尾：再清一遍文本、砍掉太短的、去重。"""
        text = clean_text(item.get("text"))
        if len(text) < 8 or text in seen:
            return
        seen.add(text)
        item["text"] = text
        items.append(item)

    links = PIYAO_ARTICLE_RE.findall(home)

    # 第 1 部分：首页里的传言式标题
    for _, raw_title in links:
        title = strip_source_prefix(clean_text(raw_title))
        if not is_rumor_claim(title):
            continue
        add({
            "text": strip_debunk_tail(title),
            "image_url": None,
            "source": RUMOR_SOURCE_PIYAO,
            "timestamp": None,
            "nature": NATURE_RUMOR,
        })

    # 第 2 部分：跟进“今日辟谣”日报。
    # 同一个链接在页面上可能重复出现（不同栏目各引一次），先去重再取前 pages 篇
    roundups = []
    for href, raw_title in links:
        title = clean_text(raw_title)
        if ("今日辟谣" in title or "辟谣（" in title) and href not in roundups:
            roundups.append(href)

    for href in roundups[:max(0, pages)]:
        url = href if href.startswith("http") else "{0}/{1}".format(PIYAO_HOME, href)
        page = fetch_html(url)
        if not page:
            continue
        for item in parse_piyao_roundup(page):
            add(item)
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

# 辟谣源清单，结构和上面的 FETCHERS 一样，只是抓回来的东西带 nature="谣言"
RUMOR_FETCHERS = (
    ("中国互联网联合辟谣平台", fetch_piyao),
)


def fetch_from_web(keyword="", pages=2, with_rumor=True):
    """
    真实爬虫的入口：先跑新闻热榜，再跑辟谣平台。

    单个站点出错不影响其他站点：每个都在自己的 try 里，失败就是空列表。
    with_rumor=False 时只抓新闻热榜（对应命令行的 --no-rumor）。
    返回的每项包含 text / image_url / source / timestamp，
    来自辟谣平台的还会多一个 nature="谣言"。
    """
    if not HAS_SPIDER_LIB:
        print("缺少 requests 或 beautifulsoup4，真实爬虫不可用。")
        print("安装命令：pip install requests beautifulsoup4 lxml")
        return []

    print("开始真实爬虫（关键词参数 {0} 只作提示，热榜类站点不按关键词搜索）".format(keyword or "未指定"))

    groups = [("新闻热榜", FETCHERS)]
    if with_rumor:
        groups.append(("辟谣平台（谣言样本）", RUMOR_FETCHERS))

    collected = []
    for group_name, fetchers in groups:
        print("  —— {0} ——".format(group_name))
        for name, func in fetchers:
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

    # 抓回来的数据统一清一遍：去掉空文本、过短的和重复的。
    # 重复时优先留带 nature 标注的那条——同一个说法可能新闻源和辟谣源都出现，
    # 带标注的才是能进训练集的谣言样本，别让先到的那条把它挤掉
    cleaned, index = [], {}
    for item in collected:
        text = clean_text(item.get("text"))
        if len(text) < 6:
            continue
        item["text"] = text
        old = index.get(text)
        if old is None:
            index[text] = item
            cleaned.append(item)
        elif not old.get("nature") and item.get("nature"):
            old.update(item)

    rumor_count = sum(1 for item in cleaned if item.get("nature") == NATURE_RUMOR)
    print("真实爬虫结束，共拿到 {0} 条有效数据，其中谣言样本 {1} 条".format(
        len(cleaned), rumor_count))
    return cleaned


# ============================================================
# 三、本地 CSV（兜底数据 + 谣言样本回流）
# ============================================================

# CSV 的列顺序，写回去的时候按这个来，和现有文件保持一致
CSV_FIELDS = ("id", "text", "image_url", "source", "timestamp",
              "rumor_prob", "status", "nature", "processed_time", "keywords")


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


def append_to_csv(records, path=None):
    """
    把抓到的谣言追加进 data/sample_data.csv，让它们直接成为训练样本。

    为什么需要这一步：库里的 nature 只由人工校验写入，爬来的“谣言”标注不能直接进库，
    而 train_model.py 的正类样本主要读的就是这个 CSV。
    跑 spider.py 时加上 --append-rumors，抓到的谣言就会追加到文件末尾，
    下一次训练自动把它们吃进去，不用手工复制粘贴。

    返回实际追加的条数。文件不存在会直接创建。
    """
    path = path or config.SAMPLE_CSV_PATH

    # 先把现有内容读进来，一是去重，二是取最大 id 接着往下排
    rows, existing_texts, max_id = [], set(), 0
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                text = (row.get("text") or "").strip()
                if text:
                    existing_texts.add(text)
                try:
                    max_id = max(max_id, int(row.get("id") or 0))
                except (TypeError, ValueError):
                    pass
                # 原样保留已有行，重写的时候不至于弄丢字段
                rows.append({key: (row.get(key) or "") for key in CSV_FIELDS})

    added = 0
    for item in records:
        # 只回流谣言：新闻源那些在训练时另有一套“按非谣言”的处理，不用往 CSV 塞
        if item.get("nature") != NATURE_RUMOR:
            continue
        text = (item.get("text") or "").strip()
        if not text or text in existing_texts:
            continue
        existing_texts.add(text)
        max_id += 1
        added += 1
        rows.append({
            "id": str(max_id),
            "text": text,
            "image_url": item.get("image_url") or "",
            "source": item.get("source") or "未知来源",
            "timestamp": item.get("timestamp") or "",
            "rumor_prob": "",
            "status": "未处理",
            "nature": NATURE_RUMOR,
            "processed_time": "",
            "keywords": "",
        })

    if not added:
        print("  抓到的谣言都已经在 CSV 里了，没有需要追加的")
        return 0

    # 整体重写：以追加方式打开会再写一个 BOM，BOM 落在文件中间会把新记录弄脏。
    # 文件才一两百行，重写一遍的开销可以忽略
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return added


# ============================================================
# 四、入库
# ============================================================

def save_to_db(records, keep_label=False):
    """
    把数据写进 raw_messages。

    keep_label 控制记录里的 nature 要不要入库：
    False（默认）时置空，因为我们希望库里 nature 只由“人工校验”写入，
    未处理的消息不应该提前有性质。CSV 里的人工标注、爬来的谣言标注，
    都留给训练用：前者 train_model.py 直接读 CSV，后者用 --append-rumors 回流到 CSV。
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
    parser.add_argument("--no-rumor", action="store_true",
                        help="不抓辟谣平台，只抓新闻热榜")
    parser.add_argument("--append-rumors", action="store_true",
                        help="把抓到的谣言追加到 data/sample_data.csv，直接补充训练集正样本")
    args = parser.parse_args()

    print("=" * 60)
    print("社交媒体谣言检测系统 - 数据采集")
    print("=" * 60)

    records = []
    if args.source in ("auto", "web"):
        records = fetch_from_web(args.keyword, args.pages,
                                 with_rumor=not args.no_rumor)

    # 抓到谣言就顺手回流到样本文件。放在写库之前：写库要连 MySQL，
    # 万一库没起来，这一步至少已经把样本留在本地了
    if args.append_rumors and records:
        print("\n把抓到的谣言追加到样本文件...")
        try:
            added = append_to_csv(records)
            print("  已追加 {0} 条到 {1}".format(added, config.SAMPLE_CSV_PATH))
        except Exception as e:
            print("  追加失败：{0}".format(e))

    if not records and args.source in ("auto", "csv"):
        if args.source == "auto":
            print("\n真实爬虫没拿到数据，切换到本地 CSV 兜底。")
        print("读取本地数据文件...")
        records = load_from_csv(limit=args.limit)

    if not records:
        print("\n没有采集到任何数据，请检查 --source 参数、网络，或者 data/sample_data.csv 是否存在。")
        return 1

    rumor_count = sum(1 for item in records if item.get("nature") == NATURE_RUMOR)
    if rumor_count:
        print("\n本次采集到的谣言样本 {0} 条（加 --append-rumors 可以追加进样本文件）".format(rumor_count))

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