# -*- coding: utf-8 -*-
"""
数据库模块。

这里做两件事：
1. 用 SQLAlchemy 的 ORM 定义三张表：raw_messages、processed_messages、keywords；
2. 把常用的增删改查包成函数，业务代码（爬虫、模型、Flask 接口）直接调这些函数，
   不用在别的地方再写一遍 SQL。

关于 flask-sqlalchemy：它主要是个 Flask 集成层，本质上还是 SQLAlchemy。
本项目直接用原生 SQLAlchemy ORM + scoped_session，少一层封装，出问题也好排查。
"""

import datetime

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, String, Text,
    create_engine, func, or_, text as sql_text
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool

import config

# ORM 的基类，所有表都要继承它
Base = declarative_base()

# 消息状态只有这几种取值，写成常量，避免各处手写中文写错
STATUS_PENDING = "未处理"     # 刚入库，还没跑模型
STATUS_DETECTED = "已检测"    # 模型给出概率了，等人工校验
STATUS_VERIFIED = "已处理"    # 人工点过谣言/非谣言，已经挪到 processed_messages

NATURE_RUMOR = "谣言"
NATURE_NORMAL = "非谣言"


# ============================================================
# 一、表结构
# ============================================================

class RawMessage(Base):
    """原始消息表：爬虫抓下来或者从这个 CSV 读进来的数据都存这。"""

    __tablename__ = "raw_messages"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键")
    text = Column(Text, nullable=False, comment="消息正文")
    image_url = Column(String(512), nullable=True, comment="配图地址，没有就为空")
    source = Column(String(64), nullable=True, comment="来源平台，如微博、贴吧")
    timestamp = Column(DateTime, nullable=True, index=True, comment="消息发布时间，列表按它倒序")
    # 概率用 FLOAT，还没检测的时候是 NULL，检测完才有值
    rumor_prob = Column(Float, nullable=True, comment="模型算出的谣言概率，0~1")
    status = Column(String(16), nullable=False, default=STATUS_PENDING, index=True,
                    comment="未处理 / 已检测 / 已处理")
    nature = Column(String(16), nullable=True, comment="人工判定的性质：谣言 / 非谣言")
    processed_time = Column(DateTime, nullable=True, comment="人工校验时间")
    keywords = Column(String(255), nullable=True, comment="该消息命中的关键词，逗号分隔")

    def to_dict(self):
        """转成字典，给 Flask 接口做 JSON 序列化用。"""
        return {
            "id": self.id,
            "text": self.text,
            # 列表里只显示前面一小段，太长的正文截断
            "short_text": (self.text[:60] + "...") if self.text and len(self.text) > 60 else self.text,
            "image_url": self.image_url,
            "source": self.source,
            "timestamp": self.timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.timestamp else None,
            "rumor_prob": self.rumor_prob,
            "status": self.status,
            "nature": self.nature,
            "processed_time": self.processed_time.strftime("%Y-%m-%d %H:%M:%S") if self.processed_time else None,
            "keywords": self.keywords,
        }


class ProcessedMessage(Base):
    """已处理文本表：人工校验完成后，从 raw_messages 复制一条记录过来。"""

    __tablename__ = "processed_messages"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键")
    # 一条原始消息只对应一条处理结果，所以 raw_id 加唯一约束，反复点校验也不会重复插入
    raw_id = Column(Integer, ForeignKey("raw_messages.id"), nullable=False, unique=True,
                    comment="对应 raw_messages.id")
    text = Column(Text, nullable=False, comment="消息正文（冗余存一份，方便单独查）")
    nature = Column(String(16), nullable=False, comment="谣言 / 非谣言")
    source = Column(String(64), nullable=True, comment="来源平台")
    rumor_prob = Column(Float, nullable=True, comment="模型给出的谣言概率")
    processed_time = Column(DateTime, nullable=True, index=True, comment="处理时间")

    def to_dict(self):
        return {
            "id": self.id,
            "raw_id": self.raw_id,
            "text": self.text,
            "short_text": (self.text[:60] + "...") if self.text and len(self.text) > 60 else self.text,
            "nature": self.nature,
            "source": self.source,
            "rumor_prob": self.rumor_prob,
            "processed_time": self.processed_time.strftime("%Y-%m-%d %H:%M:%S") if self.processed_time else None,
        }


class Keyword(Base):
    """关键词表：词云的数据来源，word + frequency。"""

    __tablename__ = "keywords"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键")
    word = Column(String(64), nullable=False, unique=True, comment="关键词")
    frequency = Column(Integer, nullable=False, default=0, comment="出现次数")
    updated_at = Column(DateTime, nullable=True, comment="最近一次统计时间")

    def to_dict(self):
        # ECharts 词云要的是 name / value 两个字段，这里顺手给出来
        return {
            "id": self.id,
            "word": self.word,
            "frequency": self.frequency,
            "name": self.word,
            "value": self.frequency,
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M:%S") if self.updated_at else None,
        }


# ============================================================
# 二、引擎和会话
# ============================================================

def get_engine(with_db=True):
    """
    创建数据库引擎。

    with_db=True  连到 rumor_detection 库，正常读写用这个；
    with_db=False 只连到 MySQL 服务器、不带库名，init_db.py 建库之前用这个
                  （库都还没建，带上库名会直接报 Unknown database）。
    """
    url = config.SQLALCHEMY_DATABASE_URI if with_db else config.SQLALCHEMY_SERVER_URI
    return create_engine(
        url,
        echo=False,               # 想看 SQL 的话改成 True
        poolclass=QueuePool,
        pool_pre_ping=True,       # 每次取连接前 ping 一下，防止 MySQL 把闲置连接掐掉
        pool_recycle=3600,
        pool_size=5,
        max_overflow=10,
    )


# 全局引擎和会话工厂。scoped_session 保证同一个线程拿到的是同一个 Session，
# Flask 每个请求一个线程，用完记得 remove()（在 app.py 的 teardown 里做了）。
engine = get_engine(with_db=True)
Session = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))


def get_session():
    """拿一个 Session，业务函数里用。"""
    return Session()


def close_session():
    """请求结束时释放 Session，避免连接泄漏。"""
    Session.remove()


def test_connection():
    """测试一下数据库通不通，init_db 和启动时都会调一次。"""
    try:
        session = get_session()
        session.execute(sql_text("SELECT 1"))
        return True, "数据库连接正常"
    except Exception as e:
        return False, "数据库连接失败：{0}".format(e)
    finally:
        close_session()


# ============================================================
# 三、raw_messages 相关操作
# ============================================================

def parse_datetime(value):
    """
    把各种格式的时间字符串转成 datetime 对象。

    CSV 里我写的是 "2026-03-12 09:15:00" 这种格式，但爬虫抓回来的时间可能
    带 "T" 或者带时区，所以多试几种格式，实在解析不出来就返回当前时间。
    """
    if value is None:
        return datetime.datetime.now()
    if isinstance(value, datetime.datetime):
        return value

    text = str(value).strip().replace("T", " ").replace("/", "-")
    # 带时区的先截掉，本项目不需要精确到时区
    text = text.split("+")[0].strip()

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d%H%M%S"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.datetime.now()


def insert_raw_messages(records):
    """
    批量插入原始消息，自动按正文去重。

    records 是一个字典列表，字段和 raw_messages 对应，
    缺的字段用默认值补上。返回实际插入的条数。
    """
    session = get_session()
    try:
        # 先把库里已有的正文捞出来，量不大（课程项目万把条），用集合比一条条查快
        existing = set(row[0] for row in session.query(RawMessage.text).all())

        new_rows = []
        for item in records:
            text = (item.get("text") or "").strip()
            # 正文为空或者重复的，直接跳过
            if not text or text in existing:
                continue
            existing.add(text)
            new_rows.append(RawMessage(
                text=text,
                image_url=item.get("image_url"),
                source=item.get("source") or "未知来源",
                timestamp=parse_datetime(item.get("timestamp")),
                rumor_prob=item.get("rumor_prob"),
                status=item.get("status") or STATUS_PENDING,
                nature=item.get("nature"),
                keywords=item.get("keywords"),
            ))

        if new_rows:
            session.add_all(new_rows)
            session.commit()
        return len(new_rows)
    except Exception:
        session.rollback()
        raise
    finally:
        close_session()


def count_messages(status=None):
    """统计消息条数。status 传 None 就是全部，传具体状态就只统计那一种。"""
    session = get_session()
    try:
        query = session.query(func.count(RawMessage.id))
        if status:
            query = query.filter(RawMessage.status == status)
        return query.scalar() or 0
    finally:
        close_session()


def get_messages(page=1, page_size=None, keyword=None, min_prob=None,
                 max_prob=None, nature=None, only_pending=False):
    """
    按条件查询原始消息，按时间倒序分页返回。

    参数说明（Flask 接口基本是一一对应的）：
      page         第几页，从 1 开始
      page_size    每页多少条，不传就用 config.PAGE_SIZE
      keyword      关键词，命中正文就算
      min_prob     谣言概率下限，比如 0.5
      max_prob     谣言概率上限
      nature       性质筛选：谣言 / 非谣言
      only_pending True 表示只看还没人工校验的（未处理 + 已检测）

    返回 (数据列表, 总条数)。
    """
    page_size = page_size or config.PAGE_SIZE
    page = max(1, int(page or 1))

    session = get_session()
    try:
        query = session.query(RawMessage)

        if only_pending:
            # 已经人工处理过的就不在检测列表里出现了
            query = query.filter(RawMessage.status.in_([STATUS_PENDING, STATUS_DETECTED]))
        if keyword:
            query = query.filter(RawMessage.text.like("%{0}%".format(keyword)))
        if min_prob is not None:
            query = query.filter(RawMessage.rumor_prob.isnot(None), RawMessage.rumor_prob >= min_prob)
        if max_prob is not None:
            query = query.filter(RawMessage.rumor_prob.isnot(None), RawMessage.rumor_prob <= max_prob)
        if nature:
            query = query.filter(RawMessage.nature == nature)

        total = query.count()
        rows = (query.order_by(RawMessage.timestamp.desc(), RawMessage.id.desc())
                     .offset((page - 1) * page_size)
                     .limit(page_size)
                     .all())
        return [row.to_dict() for row in rows], total
    finally:
        close_session()


def get_raw_message(raw_id):
    """按 id 取一条原始消息，取不到返回 None。"""
    session = get_session()
    try:
        row = session.get(RawMessage, raw_id)
        return row.to_dict() if row else None
    finally:
        close_session()


def update_detect_result(raw_id, prob, keywords=None):
    """
    模型检测完写回结果：更新概率，状态从“未处理”改成“已检测”。

    已经人工处理过的记录不允许再改，返回 (是否成功, 提示信息)。
    """
    session = get_session()
    try:
        row = session.get(RawMessage, raw_id)
        if row is None:
            return False, "消息不存在"
        if row.status == STATUS_VERIFIED:
            return False, "该消息已人工处理，不能再改检测结果"

        # 概率限制在 0~1 之间，防止模型输出异常值
        row.rumor_prob = float(max(0.0, min(1.0, prob)))
        if keywords is not None:
            row.keywords = keywords[:255] if keywords else None
        row.status = STATUS_DETECTED
        session.commit()
        return True, "检测结果已保存"
    except Exception as e:
        session.rollback()
        return False, "保存失败：{0}".format(e)
    finally:
        close_session()


# ============================================================
# 四、人工校验相关操作
# ============================================================

def verify_message(raw_id, nature):
    """
    人工校验一条消息。

    干三件事：raw_messages 里写上性质和状态、processed_messages 里插一条新记录。
    同一条消息重复点校验不会插两条（靠 raw_id 的唯一约束 + 先查后写处理）。
    返回 (是否成功, 提示信息)。
    """
    if nature not in (NATURE_RUMOR, NATURE_NORMAL):
        return False, "性质只能是“谣言”或“非谣言”"

    session = get_session()
    try:
        row = session.get(RawMessage, raw_id)
        if row is None:
            return False, "消息不存在"

        now = datetime.datetime.now()
        row.nature = nature
        row.status = STATUS_VERIFIED
        row.processed_time = now

        # 已经处理过的就更新原记录，不重复插
        exist = session.query(ProcessedMessage).filter(ProcessedMessage.raw_id == raw_id).first()
        if exist:
            exist.nature = nature
            exist.rumor_prob = row.rumor_prob
            exist.processed_time = now
        else:
            session.add(ProcessedMessage(
                raw_id=row.id,
                text=row.text,
                nature=nature,
                source=row.source,
                rumor_prob=row.rumor_prob,
                processed_time=now,
            ))

        session.commit()
        return True, "校验完成，已移入已处理文本"
    except Exception as e:
        session.rollback()
        return False, "校验失败：{0}".format(e)
    finally:
        close_session()


def get_processed_messages(page=1, page_size=None, nature=None, keyword=None):
    """查已处理文本，按处理时间倒序分页。返回 (数据列表, 总条数)。"""
    page_size = page_size or config.PAGE_SIZE
    page = max(1, int(page or 1))

    session = get_session()
    try:
        query = session.query(ProcessedMessage)
        if nature:
            query = query.filter(ProcessedMessage.nature == nature)
        if keyword:
            query = query.filter(ProcessedMessage.text.like("%{0}%".format(keyword)))

        total = query.count()
        rows = (query.order_by(ProcessedMessage.processed_time.desc(), ProcessedMessage.id.desc())
                     .offset((page - 1) * page_size)
                     .limit(page_size)
                     .all())
        return [row.to_dict() for row in rows], total
    finally:
        close_session()


def count_processed_by_nature():
    """统计已处理文本里谣言和非谣言各有多少条，首页上显示用。"""
    session = get_session()
    try:
        rows = (session.query(ProcessedMessage.nature, func.count(ProcessedMessage.id))
                       .group_by(ProcessedMessage.nature).all())
        result = {NATURE_RUMOR: 0, NATURE_NORMAL: 0}
        for nature, cnt in rows:
            if nature in result:
                result[nature] = cnt
        return result
    finally:
        close_session()


def update_message_keywords(raw_id, keyword_str):
    """
    把一条消息命中的关键词写回 raw_messages.keywords 字段（逗号分隔）。
    这个不改变消息状态，只补一个展示用的字段。
    """
    session = get_session()
    try:
        row = session.get(RawMessage, raw_id)
        if row is None:
            return False
        row.keywords = (keyword_str or "")[:255]
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        close_session()


def get_all_raw_texts(include_processed=True):
    """
    取出所有消息的 (id, text)，预处理和词频统计用。

    include_processed=False 时只取没人工校验过的，
    搜索模块默认就是这样，只看还在流转中的消息。
    """
    session = get_session()
    try:
        query = session.query(RawMessage.id, RawMessage.text)
        if not include_processed:
            query = query.filter(RawMessage.status != STATUS_VERIFIED)
        return query.all()
    finally:
        close_session()


def get_labeled_samples_from_db():
    """
    从已处理文本里取带标注的样本，格式 [(text, nature), ...]。

    这是人工校验的反馈数据：校验过的条目可以拿来继续训练模型，
    数据越多模型越贴近我们自己的判断。
    """
    session = get_session()
    try:
        rows = (session.query(ProcessedMessage.text, ProcessedMessage.nature)
                .filter(ProcessedMessage.nature.in_([NATURE_RUMOR, NATURE_NORMAL])).all())
        return [(text, nature) for text, nature in rows if text]
    finally:
        close_session()


# ============================================================
# 五、关键词相关操作
# ============================================================

def save_keywords(word_freq, top_n=100):
    """
    把词频统计结果写进 keywords 表。

    word_freq 是个字典 {词: 次数}，只保留前 top_n 个（词云画太多词反而看不清）。
    库里已有这个词就更新次数，没有就插入；这次没统计到的老词会被删掉，
    免得词云里混进过期数据。
    """
    now = datetime.datetime.now()
    items = sorted(word_freq.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    session = get_session()
    try:
        exist_map = {row.word: row for row in session.query(Keyword).all()}
        keep_words = set()

        for word, freq in items:
            keep_words.add(word)
            if word in exist_map:
                exist_map[word].frequency = int(freq)
                exist_map[word].updated_at = now
            else:
                session.add(Keyword(word=word, frequency=int(freq), updated_at=now))

        # 删掉这次没出现的词
        for word, row in exist_map.items():
            if word not in keep_words:
                session.delete(row)

        session.commit()
        return len(items)
    except Exception:
        session.rollback()
        raise
    finally:
        close_session()


def get_keywords(top_n=100, min_frequency=1):
    """取词云数据，按词频倒序。"""
    session = get_session()
    try:
        rows = (session.query(Keyword)
                .filter(Keyword.frequency >= min_frequency)
                .order_by(Keyword.frequency.desc())
                .limit(top_n).all())
        return [row.to_dict() for row in rows]
    finally:
        close_session()


def search_messages(keyword=None, min_prob=None, max_prob=None, nature=None,
                    page=1, page_size=None, include_processed=False):
    """
    关键字搜索模块用的查询。

    和 get_messages 的区别：这个会把已处理的也一起搜（可选），
    因为搜索模块是“查全部消息里跟这个词相关的”，不区分处理状态。
    """
    page_size = page_size or config.PAGE_SIZE
    page = max(1, int(page or 1))

    session = get_session()
    try:
        query = session.query(RawMessage)
        if not include_processed:
            query = query.filter(RawMessage.status.in_([STATUS_PENDING, STATUS_DETECTED]))
        if keyword:
            # 关键词命中正文或者命中的关键词字段都算
            query = query.filter(or_(
                RawMessage.text.like("%{0}%".format(keyword)),
                RawMessage.keywords.like("%{0}%".format(keyword)),
            ))
        if min_prob is not None:
            query = query.filter(RawMessage.rumor_prob.isnot(None), RawMessage.rumor_prob >= min_prob)
        if max_prob is not None:
            query = query.filter(RawMessage.rumor_prob.isnot(None), RawMessage.rumor_prob <= max_prob)
        if nature:
            query = query.filter(RawMessage.nature == nature)

        total = query.count()
        rows = (query.order_by(RawMessage.rumor_prob.desc(), RawMessage.timestamp.desc())
                     .offset((page - 1) * page_size)
                     .limit(page_size)
                     .all())
        return [row.to_dict() for row in rows], total
    finally:
        close_session()


def get_overview():
    """首页顶部的统计数字：总条数、待处理、已检测、已处理、谣言数。"""
    session = get_session()
    try:
        total = session.query(func.count(RawMessage.id)).scalar() or 0
        pending = (session.query(func.count(RawMessage.id))
                   .filter(RawMessage.status == STATUS_PENDING).scalar() or 0)
        detected = (session.query(func.count(RawMessage.id))
                    .filter(RawMessage.status == STATUS_DETECTED).scalar() or 0)
        verified = (session.query(func.count(RawMessage.id))
                    .filter(RawMessage.status == STATUS_VERIFIED).scalar() or 0)
        rumor_cnt = (session.query(func.count(RawMessage.id))
                     .filter(RawMessage.nature == NATURE_RUMOR).scalar() or 0)
        return {
            "total": total,
            "pending": pending,
            "detected": detected,
            "verified": verified,
            "rumor": rumor_cnt,
        }
    finally:
        close_session()