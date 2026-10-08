# -*- coding: utf-8 -*-
"""
项目公共配置文件。

数据库地址、端口、模型存放路径这些东西都集中放在这里，
后面其他脚本 import config 就行，不用每个文件都写一遍。
数据库密码跑项目之前一定要改成自己本机 MySQL 的密码。
"""

import os

# 项目根目录（也就是 config.py 所在的目录）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- 数据库配置 ----------------
DB_HOST = "127.0.0.1"
DB_PORT = 3306
DB_USER = "root"
DB_PASSWORD = "root"   # TODO 改成自己 MySQL 的密码
DB_NAME = "rumor_detection"
DB_CHARSET = "utf8mb4"

# 带库名的连接串，正常读写数据用这个
SQLALCHEMY_DATABASE_URI = (
    "mysql+pymysql://{user}:{pwd}@{host}:{port}/{db}?charset={charset}".format(
        user=DB_USER, pwd=DB_PASSWORD, host=DB_HOST,
        port=DB_PORT, db=DB_NAME, charset=DB_CHARSET
    )
)

# 不带库名的连接串，init_db.py 建库的时候用（那时候库还不存在）
SQLALCHEMY_SERVER_URI = (
    "mysql+pymysql://{user}:{pwd}@{host}:{port}/?charset={charset}".format(
        user=DB_USER, pwd=DB_PASSWORD, host=DB_HOST,
        port=DB_PORT, charset=DB_CHARSET
    )
)

# 关闭对象修改追踪，省点内存
SQLALCHEMY_TRACK_MODIFICATIONS = False
# 连接长时间不用会被 MySQL 断开，pool_pre_ping 会自动检测并重连
SQLALCHEMY_ENGINE_OPTIONS = {
    "pool_pre_ping": True,
    "pool_recycle": 3600,
    "pool_size": 5,
    "max_overflow": 10,
}

# ---------------- Flask 配置 ----------------
SECRET_KEY = "rumor-detection-course-project-2026"
DEBUG = True
HOST = "0.0.0.0"
PORT = 5000

# 列表页一页显示多少条
PAGE_SIZE = 10

# ---------------- 文件路径 ----------------
MODEL_DIR = os.path.join(BASE_DIR, "models")
DATA_DIR = os.path.join(BASE_DIR, "data")

TFIDF_MODEL_PATH = os.path.join(MODEL_DIR, "tfidf_vectorizer.pkl")  # TF-IDF 向量器
LR_MODEL_PATH = os.path.join(MODEL_DIR, "lr_model.pkl")             # 逻辑回归模型
LSTM_MODEL_PATH = os.path.join(MODEL_DIR, "lstm_model.pt")          # LSTM 权重（扩展用）
LSTM_VOCAB_PATH = os.path.join(MODEL_DIR, "lstm_vocab.pkl")         # LSTM 词表（扩展用）
SAMPLE_CSV_PATH = os.path.join(DATA_DIR, "sample_data.csv")         # 爬虫失败时的兜底数据

# ---------------- 数据来源标记 ----------------
# 爬虫抓取的六个新闻平台，spider.py 里写 source 用的就是这几个名字。
# 训练模型时这些来源的消息默认按“非谣言”参与训练：热榜和滚动页的新闻是媒体机构发布的，我们把它当作真实信息；理由和影响写在 train_model.py 的注释里。
NEWS_SOURCES = ("今日头条", "百度热搜", "腾讯新闻", "新浪新闻", "澎湃新闻", "中新网")

# ---------------- 业务参数 ----------------
# 谣言概率大于这个值就判成谣言，界面上可以自己调
RUMOR_THRESHOLD = 0.5

# jieba 分词时过滤掉的停用词。
# 前一部分是常规虚词；后面那批是看词云的时候发现的噪声词：
# “查看、表示、报道”这类是新闻稿的固定说法，几乎每条都有，放进词云反而把真正有信息量的词挤掉了；“报价、生意、产地”是新浪滚动里商品行情稿的套话。
# 加进来以后词云会干净很多，模型也少学一些没用的特征。
STOP_WORDS = set([
    "的", "了", "是", "在", "和", "就", "都", "而", "及", "与", "着", "或",
    "一个", "没有", "我们", "你们", "他们", "自己", "这个", "那个", "什么",
    "怎么", "还是", "因为", "所以", "但是", "如果", "可以", "已经", "还有",
    "一样", "不是", "就是", "然后", "现在", "知道", "觉得", "可能", "一下",
    "转发", "微博", "分享", "网页", "链接",
    # 新闻稿常见套话
    "查看", "表示", "报道", "记者", "讯", "日讯", "此前", "最新", "目前", "相关",
    "介绍", "认为", "指出", "发布", "进行", "其中", "以及", "同时", "此外",
    "根据", "显示", "据悉", "来源", "编辑", "责编", "综合", "消息",
    # 商品行情稿套话
    "报价", "生意", "产地", "品牌", "交易商", "交货地",
])