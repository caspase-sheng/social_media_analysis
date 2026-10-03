# -*- coding: utf-8 -*-
"""
预测模块。

职责很单一：把训练好的模型加载起来，输入文本，输出谣言概率。
被两处调用：
1. 命令行 python predict.py --text "某条消息"；
2. Flask 里的 POST /api/detect/<id> 接口。

模型的加载做了缓存，进程里只读一次磁盘，接口被连续调用也不会反复读文件。
"""

import os

import joblib

import config
import preprocess
import database

# 模块级缓存，第一次用到才真正读磁盘
_vectorizer = None
_model = None

# 判成谣言的阈值，config 里配的是 0.5，界面上也能改成别的值传进来
DEFAULT_THRESHOLD = config.RUMOR_THRESHOLD


class ModelNotReady(Exception):
    """模型文件不存在或者加载失败时抛这个，调用方好区分是哪种错。"""
    pass


def load_model(force_reload=False):
    """
    加载 TF-IDF 向量器和逻辑回归模型。

    返回 (vectorizer, model)。文件不存在会抛 ModelNotReady，
    提示先去跑 train_model.py。
    """
    global _vectorizer, _model

    if _vectorizer is not None and _model is not None and not force_reload:
        return _vectorizer, _model

    for path in (config.TFIDF_MODEL_PATH, config.LR_MODEL_PATH):
        if not os.path.exists(path):
            raise ModelNotReady(
                "找不到模型文件 {0}，请先执行 python train_model.py".format(path))

    try:
        _vectorizer = joblib.load(config.TFIDF_MODEL_PATH)
        _model = joblib.load(config.LR_MODEL_PATH)
    except Exception as e:
        raise ModelNotReady("模型加载失败：{0}".format(e))

    return _vectorizer, _model


def is_model_ready():
    """模型文件在不在，app.py 启动时检查一下，好给个明确的提示。"""
    return (os.path.exists(config.TFIDF_MODEL_PATH) and os.path.exists(config.LR_MODEL_PATH))


def predict_text(text, threshold=None):
    """
    预测单条文本。

    返回一个字典：
      prob       谣言概率，0~1 的小数
      label      判定结果：“谣言” / “非谣言”
      prob_text  概率的百分数写法，前端直接显示这个，省得再算
      keywords   对这次判断影响最大的几个词（线性模型能解释，下面说）
      is_rumor   布尔值，方便接口里做判断
    """
    threshold = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    vectorizer, model = load_model()

    clean = preprocess.clean_text(text)
    if not clean:
        # 空文本或者全是标点，没什么可判断的，直接给中间值
        return {
            "prob": 0.5, "label": database.NATURE_NORMAL, "prob_text": "50.0%",
            "keywords": "", "is_rumor": False,
        }

    vec = vectorizer.transform([preprocess.cut_to_string(text)])
    # predict_proba 返回 [非谣言概率, 谣言概率]，第 1 列才是我们要的
    prob = float(model.predict_proba(vec)[0][1])
    is_rumor = prob >= threshold

    return {
        "prob": round(prob, 4),
        "label": database.NATURE_RUMOR if is_rumor else database.NATURE_NORMAL,
        "prob_text": "{0:.1f}%".format(prob * 100),
        "keywords": explain_keywords(vectorizer, model, vec, top_n=5),
        "is_rumor": is_rumor,
    }


def explain_keywords(vectorizer, model, vec, top_n=5):
    """
    找出对这次判断贡献最大的几个词。

    思路：逻辑回归的打分等于“每个特征的取值 x 对应系数”求和。
    对于某条消息，把它的 TF-IDF 向量和系数按位相乘，得到每个词对总分贡献了多少，
    取正贡献最大的前几个，就是模型认为"像谣言"的主要依据。

    这是线性模型比深度模型好用的地方——深度模型只能给个概率，
    这个能告诉人为什么。当时做界面的时候就靠它填的"命中关键词"那一列。
    """
    coefs = model.coef_[0]
    row = vec.toarray()[0]
    names = vectorizer.get_feature_names_out()

    nonzero = row.nonzero()[0]
    if len(nonzero) == 0:
        return ""

    contributions = [(names[i], row[i] * coefs[i]) for i in nonzero]
    # 按贡献从大到小，只要正的（负贡献的是往"非谣言"那边拉，不作为判断依据展示）
    contributions = [item for item in contributions if item[1] > 0]
    contributions.sort(key=lambda kv: kv[1], reverse=True)

    return ",".join([w for w, _ in contributions[:top_n]])


def predict_batch(texts, threshold=None):
    """批量预测，输入文本列表，返回结果列表。少一次模型加载，比一条条调快。"""
    threshold = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    vectorizer, model = load_model()

    corpus = [preprocess.cut_to_string(t) for t in texts]
    probs = model.predict_proba(vectorizer.transform(corpus))[:, 1]

    results = []
    for text, prob in zip(texts, probs):
        prob = float(prob)
        results.append({
            "prob": round(prob, 4),
            "label": database.NATURE_RUMOR if prob >= threshold else database.NATURE_NORMAL,
            "prob_text": "{0:.1f}%".format(prob * 100),
            "is_rumor": prob >= threshold,
        })
    return results


def detect_pending_messages(limit=None, threshold=None, recheck=False):
    """
    把库里还没人工校验过的消息跑一遍，结果写回 raw_messages。

    recheck=False（默认）：只跑“未处理”的，已经检测过的不重复跑，适合第一次全量检测。
    recheck=True：连“已检测”的一起重跑。模型重训以后概率会变，旧结果得用新模型刷一遍，
    界面上那个“一键检测”按钮走的就是这条路。

    两种模式都不碰已经人工校验过的消息（status=已处理），人工结论优先级最高。

    返回 (处理条数, 失败条数)。这个是给命令行用的；
    Flask 的接口是单条检测，逻辑一样，但走的是 predict_text + update_detect_result。
    """
    threshold = DEFAULT_THRESHOLD if threshold is None else float(threshold)

    # only_pending=True 拿到的是“未处理 + 已检测”，也就是全部还没人工校验的
    rows, total = database.get_messages(page=1, page_size=limit or 10000,
                                        only_pending=True)
    if recheck:
        # 重跑模式：未处理和已检测的都要过一遍
        todo = rows
    else:
        todo = [r for r in rows if r["status"] == database.STATUS_PENDING]
    if not todo:
        print("没有待检测的消息（库里未处理 0 条）。")
        return 0, 0

    print("待检测 {0} 条，开始跑模型...".format(len(todo)))
    ok, fail = 0, 0
    for i, item in enumerate(todo, 1):
        try:
            result = predict_text(item["text"], threshold)
            success, msg = database.update_detect_result(
                item["id"], result["prob"], result["keywords"])
            if success:
                ok += 1
                print("  [{0}/{1}] id={2} 概率 {3} -> {4}  关键词：{5}".format(
                    i, len(todo), item["id"], result["prob_text"],
                    result["label"], result["keywords"] or "无"))
            else:
                fail += 1
                print("  [{0}/{1}] id={2} 写库失败：{3}".format(i, len(todo), item["id"], msg))
        except Exception as e:
            fail += 1
            print("  [{0}/{1}] id={2} 预测出错：{3}".format(i, len(todo), item["id"], e))

    return ok, fail


def main():
    import argparse
    parser = argparse.ArgumentParser(description="谣言检测预测")
    parser.add_argument("--text", help="直接预测一段文本，不传就是对库里未处理的消息批量检测")
    parser.add_argument("--limit", type=int, default=None, help="批量检测最多处理多少条")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="判定阈值，默认 0.5")
    parser.add_argument("--recheck", action="store_true",
                        help="连已检测的一起重跑，模型重训后用这个刷新旧概率")
    args = parser.parse_args()

    print("=" * 60)
    print("社交媒体谣言检测系统 - 模型预测")
    print("=" * 60)

    if not is_model_ready():
        print("模型文件不存在，先跑 python train_model.py 训练。")
        print("缺少的文件：{0}".format(config.LR_MODEL_PATH))
        return 1

    # 单条预测模式
    if args.text:
        try:
            result = predict_text(args.text, args.threshold)
        except ModelNotReady as e:
            print("[错误] {0}".format(e))
            return 1

        print("\n输入文本：{0}".format(args.text))
        print("分词结果：{0}".format(" / ".join(preprocess.cut_words(args.text))))
        print("谣言概率：{0}".format(result["prob_text"]))
        print("判定结果：{0}（阈值 {1}）".format(result["label"], args.threshold))
        print("主要依据的词：{0}".format(result["keywords"] or "无"))
        return 0

    # 批量检测模式
    try:
        ok, fail = detect_pending_messages(args.limit, args.threshold,
                                           recheck=args.recheck)
    except ModelNotReady as e:
        print("[错误] {0}".format(e))
        return 1
    except Exception as e:
        print("[错误] 批量检测失败：{0}".format(e))
        print("       先确认 MySQL 起到没起、config.py 里的密码对不对。")
        return 1

    print("\n批量检测完成：成功 {0} 条，失败 {1} 条".format(ok, fail))

    try:
        overview = database.get_overview()
        print("库里当前状态：{0}".format(overview))
    except Exception:
        pass

    print("\n下一步可以跑 python app.py 启动界面看效果。")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())