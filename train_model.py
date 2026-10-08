# -*- coding: utf-8 -*-
"""
模型训练脚本：TF-IDF + 逻辑回归。

训练数据有三个来源，合并起来用：
1. data/sample_data.csv 里 50 条人工标注的数据（nature 列当标签），正类样本主要靠它；
2. 数据库 processed_messages 里已经人工校验过的记录——这是"人在用系统"产生的反馈，
   校验得越多，模型越接近我们自己的判断标准；
3. 数据库 raw_messages 里爬虫抓来的新闻，按"非谣言"参与训练。

这里有个明确的假设：我们按"热榜和滚动页的新闻是媒体机构发布的真实信息"把来源属于
config.NEWS_SOURCES 的消息统一当作非谣言。它不是人工标注，所以只用于训练，
不写进 raw_messages.nature（界面上看到的"人工性质"仍然是人工校验出来的结果）。
不需要这个假设时用 --no-news 关掉。

流程：读数据 -> 分词（复用 preprocess，保证和线上预测一致）-> TF-IDF -> 逻辑回归
      -> 交叉验证 + 测试集评估 -> 模型存到 models/ 下。

用法：
    python train_model.py                  正常训练
    python train_model.py --test-size 0.2  调整测试集比例
    python train_model.py --csv-only       只用 CSV 训练（不看库里的数据）
    python train_model.py --no-news        不把爬虫抓的新闻当非谣言样本
"""

import argparse
import os
import sys

import joblib
import numpy as np

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, classification_report, confusion_matrix,
                             f1_score, precision_score, recall_score)
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split

import config
import preprocess
import database

# 标签就用 0/1，逻辑回归直接吃数字。
# 1 表示谣言，0 表示非谣言，这个顺序不要改，predict.py 里是照着这个来的
LABEL_MAP = {
    database.NATURE_RUMOR: 1,
    database.NATURE_NORMAL: 0,
}
LABEL_NAME = {1: "谣言", 0: "非谣言"}


def load_training_data(use_db=True, use_news=True):
    """
    收集训练样本，返回 (文本列表, 标签列表)。

    CSV 那边用 nature 列当标签；数据库那边取人工校验过的记录；
    爬虫抓来的新闻按非谣言算（理由见文件开头的说明）。
    几条来源之间重复的正文会去重。
    """
    texts, labels = [], []
    seen = set()

    # 来源一：本地 CSV 的标注数据
    from spider import load_from_csv
    csv_rows = load_from_csv()
    for row in csv_rows:
        nature = row.get("nature")
        text = (row.get("text") or "").strip()
        if not text or nature not in LABEL_MAP or text in seen:
            continue
        seen.add(text)
        texts.append(text)
        labels.append(LABEL_MAP[nature])
    csv_count = len(texts)

    # 来源二：库里人工校验过的数据
    db_count = 0
    if use_db:
        try:
            for text, nature in database.get_labeled_samples_from_db():
                text = (text or "").strip()
                if not text or nature not in LABEL_MAP or text in seen:
                    continue
                seen.add(text)
                texts.append(text)
                labels.append(LABEL_MAP[nature])
                db_count += 1
        except Exception as e:
            # 数据库连不上也不该让训练整个跑不起来，CSV 那部分还能用
            print("[警告] 读数据库里的标注数据失败：{0}".format(e))
            print("        本次只跳过这部分数据。")

    # 来源三：爬虫抓来的新闻，统一按非谣言
    news_count = 0
    if use_news:
        try:
            for text in database.get_news_samples_from_db(config.NEWS_SOURCES):
                text = (text or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                texts.append(text)
                labels.append(LABEL_MAP[database.NATURE_NORMAL])
                news_count += 1
        except Exception as e:
            print("[警告] 读爬虫新闻样本失败：{0}".format(e))

    print("训练样本来源：CSV {0} 条，人工校验 {1} 条，爬虫新闻（按非谣言）{2} 条，合计 {3} 条".format(
        csv_count, db_count, news_count, len(texts)))
    return texts, labels


def check_data(texts, labels):
    """检查一下样本够不够训练，不够就说清楚差什么，别让它稀里糊涂报错。"""
    if len(texts) < 10:
        print("[错误] 样本只有 {0} 条，太少了，训练没意义。".format(len(texts)))
        print("       先确认 data/sample_data.csv 在不在、里面 nature 列有没有值。")
        return False

    n_rumor = sum(labels)
    n_normal = len(labels) - n_rumor
    print("标签分布：谣言 {0} 条，非谣言 {1} 条".format(n_rumor, n_normal))

    if n_rumor == 0 or n_normal == 0:
        print("[错误] 只有一种标签，模型没法学。")
        return False
    return True


def evaluate(model, x_test, y_test):
    """在测试集上算一遍指标，顺便把结果打出来。"""
    y_pred = model.predict(x_test)

    acc = accuracy_score(y_test, y_pred)
    prec = precision_score(y_test, y_pred, zero_division=0)   # 精确率：判成谣言的里面有多少真的是谣言
    rec = recall_score(y_test, y_pred, zero_division=0)       # 召回率：真谣言里有多少被找出来了
    f1 = f1_score(y_test, y_pred, zero_division=0)

    print("\n测试集评估（测试集 {0} 条）：".format(len(y_test)))
    print("  准确率 Accuracy : {0:.4f}".format(acc))
    print("  精确率 Precision: {0:.4f}".format(prec))
    print("  召回率 Recall   : {0:.4f}".format(rec))
    print("  F1 值           : {0:.4f}".format(f1))

    # 混淆矩阵手写成中文，答辩的时候好讲：多少条判错了、错在哪一边
    cm = confusion_matrix(y_test, y_pred, labels=[1, 0])
    print("\n混淆矩阵（行是真实标签，列是预测标签）：")
    print("              预测谣言  预测非谣言")
    print("  真实谣言      {0:<9}{1}".format(cm[0][0], cm[0][1]))
    print("  真实非谣言    {0:<9}{1}".format(cm[1][0], cm[1][1]))

    print("\n分类报告：")
    # labels 必须显式写成 [1, 0]，否则 sklearn 默认按 0/1 排序，
    # 会把 target_names 里的“谣言”对应到标签 0 上，指标全错行
    print(classification_report(y_test, y_pred, labels=[1, 0],
                                target_names=["谣言", "非谣言"], zero_division=0))
    return acc, f1


def show_top_features(vectorizer, model, top_n=15):
    """
    把逻辑回归系数最大的词打出来。

    系数是正数的偏"谣言"，负数偏"非谣言"。这是线性模型的好处，
    能直接看出来它到底靠哪些词在做判断——老师问"模型学到了什么"的时候有东西可说。
    """
    names = vectorizer.get_feature_names_out()
    coefs = model.coef_[0]

    order = np.argsort(coefs)
    print("\n最偏“谣言”的 {0} 个特征（系数为正）：".format(top_n))
    for idx in order[::-1][:top_n]:
        print("    {0:<12}{1:+.4f}".format(names[idx], coefs[idx]))

    print("\n最偏“非谣言”的 {0} 个特征（系数为负）：".format(top_n))
    for idx in order[:top_n]:
        print("    {0:<12}{1:+.4f}".format(names[idx], coefs[idx]))


def save_models(vectorizer, model):
    """模型存磁盘，predict.py 和 Flask 接口启动时直接加载，不用每次重训。"""
    os.makedirs(config.MODEL_DIR, exist_ok=True)
    joblib.dump(vectorizer, config.TFIDF_MODEL_PATH)
    joblib.dump(model, config.LR_MODEL_PATH)
    print("\n模型已保存：")
    print("  {0}".format(config.TFIDF_MODEL_PATH))
    print("  {0}".format(config.LR_MODEL_PATH))


def main():
    parser = argparse.ArgumentParser(description="训练谣言检测模型")
    parser.add_argument("--test-size", type=float, default=0.3, help="测试集比例，默认 0.3")
    parser.add_argument("--csv-only", action="store_true", help="只用 CSV 数据训练")
    parser.add_argument("--no-news", action="store_true",
                        help="不把爬虫抓的新闻当非谣言样本")
    parser.add_argument("--top-n", type=int, default=15, help="打印多少个特征词")
    args = parser.parse_args()

    print("=" * 60)
    print("社交媒体谣言检测系统 - 模型训练（TF-IDF + 逻辑回归）")
    print("=" * 60)

    # 第一步：准备数据
    texts, labels = load_training_data(use_db=not args.csv_only,
                                       use_news=not args.no_news)
    if not check_data(texts, labels):
        return 1

    # 第二步：分词 + TF-IDF
    print("\n分词和特征提取...")
    corpus = [preprocess.cut_to_string(t) for t in texts]
    vectorizer, matrix = preprocess.build_tfidf(corpus, max_features=2000)
    print("  特征矩阵：{0} 条样本 x {1} 个特征".format(matrix.shape[0], matrix.shape[1]))

    y = np.array(labels)

    # 第三步：交叉验证。
    # 样本只有几十条，单次划分的结果偶然性太大，交叉验证看均值更靠谱
    n_splits = min(5, min(sum(y == 1), sum(y == 0)))
    if n_splits >= 2:
        print("\n{0} 折交叉验证...".format(n_splits))
        cv_model = LogisticRegression(C=1.0, max_iter=1000, class_weight="balanced")
        cv_scores = cross_val_score(cv_model, matrix, y,
                                    cv=StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42),
                                    scoring="f1")
        print("  各折 F1：{0}".format("  ".join("{0:.4f}".format(s) for s in cv_scores)))
        print("  平均 F1：{0:.4f}（标准差 {1:.4f}）".format(cv_scores.mean(), cv_scores.std()))
    else:
        print("\n[提示] 某类样本太少，跳过交叉验证。")

    # 第四步：划分训练集/测试集，正式训练
    try:
        x_train, x_test, y_train, y_test = train_test_split(
            matrix, y, test_size=args.test_size, random_state=42, stratify=y)
    except ValueError:
        # 样本太少时 stratify 会失败，退化成不按比例分
        print("[提示] 样本偏少，本次不按标签比例划分。")
        x_train, x_test, y_train, y_test = train_test_split(
            matrix, y, test_size=args.test_size, random_state=42)

    print("\n训练集 {0} 条，测试集 {1} 条".format(len(y_train), len(y_test)))

    model = LogisticRegression(
        C=1.0,                    # 正则强度，1.0 是常用默认值，越小正则越强
        max_iter=1000,            # 迭代上限，默认 100 有时候不收敛
        class_weight="balanced",  # 两类样本数量不均衡时自动调权重
        solver="liblinear",       # 小数据集上用这个比 lbfgs 稳
    )
    model.fit(x_train, y_train)
    print("训练完成，迭代次数 {0}".format(model.n_iter_[0]))

    # 第五步：评估
    evaluate(model, x_test, y_test)
    show_top_features(vectorizer, model, args.top_n)

    # 第六步：用全部数据重训一遍再保存。
    # 测试集那部分样本也是标注好的，扔掉可惜；评估已经做完了，
    # 重训不会影响刚才的评估结论（测试集结果不能拿来当最终指标看，这点在文档里会说清楚）
    final_model = LogisticRegression(C=1.0, max_iter=1000,
                                     class_weight="balanced", solver="liblinear")
    final_model.fit(matrix, y)
    save_models(vectorizer, final_model)

    print("\n训练结束。下一步跑 python predict.py 对库里未处理的消息做检测。")
    return 0


if __name__ == "__main__":
    sys.exit(main())