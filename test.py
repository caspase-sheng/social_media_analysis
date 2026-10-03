import requests
url = "https://s.weibo.com/weibo?q=谣言&page=1"
r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 ..."})
print(r.status_code)          # 常见 200 但内容不对，或 403/418
print(r.url)                  # 若被重定向到 passport.weibo.com → 登录墙
print(len(r.text))            # 通常只有几 KB，而真实搜索页几百 KB
print("card-wrap" in r.text)  # 大概率 False
print(r.text[:500])           # 看是不是登录提示