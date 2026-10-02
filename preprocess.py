# -*- coding: utf-8 -*-
"""
文本预处理模块。

流程是：读库里的消息 -> 清洗 -> jieba 分词 -> 统计词频写 keywords 表 ->
拿出一份 TF-IDF 特征矩阵看看维度对不对。

这里定义的几个函数不只本文件在用，train_model.py 和 predict.py 也会 import，
所以分词规则统一放在这里，保证训练和线上预测的处理方式完全一致
（这一点挺关键，之前我训练用了一套清洗、预测用另一套，结果线上效果比训练差很多）。
"""

import re
from collections import Counter

import config

# jieba 第一次导入会加载词典，比较慢（两三秒），所以放在模块顶层只加载一次
try:
    import jieba
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


# ---------------- 清洗用到的正则，提前编译好，比每次现编译快 ----------------
RE_URL = re.compile(r"(https?://\S+|www\.\S+)")          # 网址
RE_AT = re.compile(r"@[\w\u4e00-\u9fa5\-_]{1,20}")       # @某人
RE_TOPIC = re.compile(r"#([^#]{1,30})#")                 # #话题#，只保留里面的字
RE_EMOJI_TAG = re.compile(r"\[[^\[\]]{1,10}\]")          # [微笑] 这类表情文字
RE_SPECIAL = re.compile(r"[^\u4e00-\u9fa5a-zA-Z0-9]+")   # 汉字、字母、数字以外的都算符号
RE_MULTI_SPACE = re.compile(r"\s+")


def clean_text(text):
    """
    清洗一条文本。

    做的事：去网址、去 @、话题符号去掉但保留话题词、去表情标签、
    去所有标点符号、全角空格换半角、合并连续空格。

    注意我们不改库里的原文，清洗只用于分词和特征提取，
    界面上展示的还是要看原文，不然用户看不懂。
    """
    if not text:
        return ""

    text = str(text)
    text = RE_URL.sub(" ", text)
    text = RE_AT.sub(" ", text)
    # #某某话题# -> 某某话题，中间的词是有信息量的，不能跟着符号一起删
    text = RE_TOPIC.sub(r"\1", text)
    text = RE_EMOJI_TAG.sub(" ", text)
    text = text.replace("\u3000", " ").replace("\xa0", " ").replace("\u200b", "")
    # 这行会把标点全干掉，中文标点也在内
    text = RE_SPECIAL.sub(" ", text)
    text = RE_MULTI_SPACE.sub(" ", text)
    return text.strip()


def cut_words(text, remove_stopwords=True, min_length=2):
    """
    分词。

    jieba.lcut 是精确模式，课程项目够用。
    过滤规则：去掉停用词、去掉长度小于 2 的词（单个字大多是“的、了”这类，
    也有一些有用但噪声大于收益）、去掉纯数字。

    返回词列表。
    """
    if not text:
        return []
    if not HAS_JIEBA:
        # 没装 jieba 的话退化成按单字切，过滤规则和下面保持一致，
        # 这样就算环境缺库，后续流程也不会因为维度对不上直接报错
        chars = [ch for ch in clean_text(text) if ch.strip()]
        return [ch for ch in chars
                if len(ch) >= 1 and not ch.isdigit() and not (remove_stopwords and ch in config.STOP_WORDS)]

    words = jieba.lcut(clean_text(text))

    result = []
    for word in words:
        word = word.strip()
        if len(word) < min_length:
            continue
        if word.isdigit():
            continue
        if remove_stopwords and word in config.STOP_WORDS:
            continue
        result.append(word)
    return result


def cut_to_string(text):
    """
    分词后拼成空格分隔的字符串，TF-IDF 直接吃这个格式。
    TfidfVectorizer 默认按空格切词，所以中文必须先自己分好。
    """
    return " ".join(cut_words(text))


def top_keywords_of_text(text, top_n=5):
    """
    取一条消息里最有代表性的几个词，写进 raw_messages.keywords 字段。
    按词长排一下，长词一般比短词更有指向性，比如“地震”比“地”强。
    """
    words = cut_words(text)
    if not words:
        return ""
    counter = Counter(words)
    ranked = sorted(counter.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)
    return ",".join([w for w, _ in ranked[:top_n]])


# ---------------- 语料和词频 ----------------

def build_corpus(rows):
    """
    rows 是 [(id, text), ...]，转成 [(id, 分词字符串), ...]。
    """
    corpus = []
    for raw_id, text in rows:
        corpus.append((raw_id, cut_to_string(text)))
    return corpus


def count_word_frequency(rows, top_n=100):
    """
    统计全库词频，返回 Counter。

    rows 传 [(id, text)] 就行，内部会自己分词。
    """
    counter = Counter()
    for _, text in rows:
        counter.update(cut_words(text))

    # 只保留前 top_n 个，词云画太多词会糊成一团
    return Counter(dict(counter.most_common(top_n)))


# ---------------- TF-IDF ----------------

def build_tfidf(corpus_texts, max_features=2000, min_df=1, max_df=0.9):
    """
    在一批已经分好词、空格分隔的文本上训练 TF-IDF。

    几个参数的含义：
      max_features  最多保留多少个特征词，2000 对课程项目够了，太大费内存
      min_df        至少出现在几个文档里才算特征词，1 表示出现就算
      max_df        出现在超过 90% 文档里的词直接丢掉，这种词区分度太低

    ngram_range 用 (1, 2)，也就是除了单字词，还把相邻两个词组合起来看，
    像“地震 预警”这种搭配本身就有信息。
    返回 (vectorizer, 特征矩阵)。
    """
    if not HAS_SKLEARN:
        raise RuntimeError("缺少 scikit-learn，请先执行 pip install scikit-learn")

    vectorizer = TfidfVectorizer(
        max_features=max_features,
        min_df=min_df,
        max_df=max_df,
        ngram_range=(1, 2),
        # token_pattern 用默认的就行，因为我们已经在 cut_to_string 里分好词、用空格隔开了
    )
    matrix = vectorizer.fit_transform(corpus_texts)
    return vectorizer, matrix


def transform_tfidf(vectorizer, texts):
    """用训练好的向量器处理新文本，predict.py 里用。"""
    return vectorizer.transform([cut_to_string(t) for t in texts])


# ---------------- 去重 ----------------

def find_duplicates(rows):
    """
    找出正文重复的消息，返回 {正文: [id1, id2, ...]}。

    入库时 database.insert_raw_messages 已经做过一轮去重，
    但爬虫分多次跑、或者人工往库里补数据的时候还是可能撞上，所以这里再做一次检查。
    """
    groups = {}
    for raw_id, text in rows:
        key = clean_text(text)
        if not key:
            continue
        groups.setdefault(key, []).append(raw_id)
    return {k: v for k, v in groups.items() if len(v) > 1}


def main():
    print("=" * 60)
    print("社交媒体谣言检测系统 - 文本预处理")
    print("=" * 60)

    if not HAS_JIEBA:
        print("[错误] 没有安装 jieba，无法分词。安装命令：pip install jieba")
        return 1

    import database   # 放函数里导入，避免没装数据库驱动时连 --help 都跑不了

    # 第一步：把库里的文本拿出来
    rows = database.get_all_raw_texts()
    if not rows:
        print("库里没有消息，先跑 python spider.py 灌数据。")
        return 1
    print("读入消息 {0} 条".format(len(rows)))

    # 第二步：重复检查
    dups = find_duplicates(rows)
    if dups:
        print("\n发现 {0} 组重复正文（入库时已自动跳过重复，这里只是提醒）：".format(len(dups)))
        for key, ids in list(dups.items())[:3]:
            print("  正文“{0}” 对应 id {1}".format(key[:20], ids))
    else:
        print("正文重复检查：没有发现重复")

    # 第三步：分词，顺便看几个例子，确认清洗没把有用的字删掉
    print("\n分词示例：")
    for raw_id, text in rows[:3]:
        words = cut_words(text)
        print("  id={0} 原文：{1}".format(raw_id, text[:28]))
        print("       分词：{0}".format(" / ".join(words[:12])))

    # 第四步：词频统计，写 keywords 表 + 回填每条消息的 keywords 字段
    print("\n统计词频...")
    word_freq = count_word_frequency(rows, top_n=100)
    print("  高频词前 20 个：")
    for word, freq in word_freq.most_common(20):
        print("    {0:<8}{1}".format(word, freq))

    try:
        saved = database.save_keywords(word_freq, top_n=100)
        print("  已写入 keywords 表 {0} 个词".format(saved))
    except Exception as e:
        print("  [失败] 写 keywords 表出错：{0}".format(e))
        return 1

    filled = 0
    for raw_id, text in rows:
        if database.update_message_keywords(raw_id, top_keywords_of_text(text)):
            filled += 1
    print("  已为 {0} 条消息回填关键词字段".format(filled))

    # 第五步：跑一遍 TF-IDF，主要是确认特征维度、看看 top 特征词合不合理
    if HAS_SKLEARN:
        print("\nTF-IDF 特征提取...")
        corpus = [cut_to_string(text) for _, text in rows]
        vectorizer, matrix = build_tfidf(corpus, max_features=2000)
        print("  特征矩阵形状：{0} 行（消息数） x {1} 列（特征词数）".format(
            matrix.shape[0], matrix.shape[1]))
        print("  矩阵非零元素个数：{0}（稀疏矩阵，大部分是 0，正常）".format(matrix.nnz))
        names = vectorizer.get_feature_names_out()
        print("  部分特征词：{0}".format(" / ".join(list(names[:15]))))
    else:
        print("\n没装 scikit-learn，跳过 TF-IDF 这一步。安装命令：pip install scikit-learn")

    print("\n预处理完成。下一步跑 python train_model.py 训练模型。")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())