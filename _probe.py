# -*- coding: utf-8 -*-
"""临时探测脚本 3：确认字段细节，用完即删。"""
import re
import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}

# 腾讯：跳过首条横幅后看真实字段
r = requests.get("https://r.inews.qq.com/gw/event/hot_ranking_list?page_size=30", headers=H, timeout=12)
nl = r.json()["idlist"][0]["newslist"]
real = [i for i in nl if i.get("source")]
print("腾讯 有效条数:", len(real), "/", len(nl))
if real:
    print("字段:", list(real[0].keys()))
    print("样例:", {k: real[0].get(k) for k in list(real[0].keys())[:12]})

# 百度：卡片内部结构
r = requests.get("https://top.baidu.com/board?tab=realtime", headers=H, timeout=12)
soup = BeautifulSoup(r.text, "lxml")
card = soup.select("div.category-wrap_iQLoo")[1]
print("\n百度 卡片内 class 列表:")
for el in card.find_all(True)[:18]:
    cls = " ".join(el.get("class") or [])
    txt = el.get_text(strip=True)[:36]
    print("   <{}> class={!r} text={!r}".format(el.name, cls, txt))

# 新浪：img / summary 字段形态
r = requests.get("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&num=5&page=1", headers=H, timeout=12)
r.encoding = "utf-8"
for it in r.json()["result"]["data"][:2]:
    print("\n新浪样例:")
    for k in ("title", "summary", "intro", "img", "ctime", "media_name", "url"):
        v = it.get(k)
        print("   {:<10} = {}".format(k, str(v)[:90]))

# 澎湃：确认链接选择器
r = requests.get("https://www.thepaper.cn/", headers=H, timeout=12)
soup = BeautifulSoup(r.text, "lxml")
a = soup.select_one('a[href^="/newsDetail_forward_"]')
print("\n澎湃 首个正文链接:", a.get_text(strip=True) if a else None)

# 中新网：列表结构（GBK）
r = requests.get("https://www.chinanews.com.cn/scroll-news/news1.html", headers=H, timeout=12)
r.encoding = r.apparent_encoding
soup = BeautifulSoup(r.text, "lxml")
box = soup.select_one("div.content_list")
print("中新网 content_list 存在:", bool(box))
if box:
    li = box.select("li")[1]
    print("   li 文本:", re.sub(r"\s+", " ", li.get_text(" ", strip=True))[:80])
    print("   li html:", re.sub(r"\s+", " ", str(li))[:220])