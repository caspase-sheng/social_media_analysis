# -*- coding: utf-8 -*-
"""
Flask 主程序。

这个文件是系统的后端入口，负责三件事：
1. 对外提供网页（首页、详情页）；
2. 提供一组 /api/ 开头的 JSON 接口，前端用 fetch 调用；
3. 启动时检查数据库和模型文件，缺东西就在控制台给出明确提示。

接口一览（详细参数写在每个函数上面）：
    GET  /                     首页
    GET  /detail/<id>          消息详情页
    GET  /api/overview         顶部统计数字
    GET  /api/messages         原始消息列表（默认只看没人工校验过的）
    GET  /api/message/<id>     单条消息详情
    POST /api/detect/<id>      服务器检测：跑模型，写回概率和关键词
    POST /api/verify/<id>      人工校验：提交谣言 / 非谣言
    GET  /api/processed        已处理文本列表
    GET  /api/keywords         关键词（词云数据源）
    GET  /api/search           关键字搜索（关键词 + 概率区间过滤）

所有接口统一返回这种结构，前端好处理：
    {"code": 0, "msg": "提示文字", "data": {...}}
code=0 表示正常，非 0 表示出错，msg 里写清楚原因。
"""

from flask import Flask, jsonify, render_template, request

import config
import database
import predict

# 创建 Flask 应用，模板和静态文件走默认的 templates/、static/ 目录
app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY
# 让返回的 JSON 不把中文转成 \uXXXX，调试时肉眼看得清楚
app.config["JSON_AS_ASCII"] = False


# ============================================================
# 公共小工具
# ============================================================

def ok(data=None, msg="ok"):
    """成功返回，把数据塞进 data 字段。"""
    return jsonify({"code": 0, "msg": msg, "data": data})


def fail(msg, code=1):
    """失败返回，code 允许自定义，前端能区分不同错误。"""
    return jsonify({"code": code, "msg": msg, "data": None})


def arg_int(name, default=None):
    """
    从 URL 查询串里取一个整数参数。

    取不到或者填了非数字就返回 default。前端传过来的东西不能全信，
    这里统一转一遍，省得后面各种报类型错误。
    """
    value = request.args.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def arg_float(name, default=None):
    """同上，取浮点数，概率区间过滤用。"""
    value = request.args.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def arg_str(name, default=None):
    """取字符串参数，顺手去掉首尾空格。空字符串当没传处理。"""
    value = request.args.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def body_params():
    """
    把 POST 请求的参数统一取出来。

    前端可能用 JSON 也可能用表单提交，两种都兼容一下，
    省得以后改前端的时候后端还要跟着改。
    """
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def parse_page_params():
    """列表类接口公用的分页参数：page、page_size。"""
    page = arg_int("page", 1) or 1
    page_size = arg_int("page_size", config.PAGE_SIZE) or config.PAGE_SIZE
    # 每页最多 100 条，防止有人手动传个很大的数值把库拖垮
    page_size = max(1, min(page_size, 100))
    return page, page_size


# 每个请求结束以后把数据库 Session 释放掉。
# database 里用的是 scoped_session，同一个线程会复用同一个 Session，
# 不 remove 的话连接会一直被占着，跑久了 MySQL 就会报连接数超限。
@app.teardown_appcontext
def cleanup_session(exception=None):
    database.close_session()


# ============================================================
# 一、页面路由
# ============================================================

@app.route("/")
def index():
    """首页：消息列表、统计数字、词云都在这一页。页面数据由 JS 调接口拿。"""
    return render_template("index.html")


@app.route("/detail/<int:raw_id>")
def detail(raw_id):
    """
    详情页：显示一条消息的完整正文、检测结果，并提供人工校验按钮。

    页面里先不查数据，由 script.js 拿到 id 后再调 /api/message/<id>，
    这样从列表点什么链接都能复用这一张模板。
    """
    return render_template("detail.html", raw_id=raw_id)


# ============================================================
# 二、数据查询接口
# ============================================================

@app.route("/api/overview")
def api_overview():
    """
    顶部统计数字。

    参数：无。
    返回：total/pending/detected/verified/rumor 五个计数。
    """
    try:
        return ok(database.get_overview())
    except Exception as e:
        return fail("统计查询失败：{0}".format(e))


@app.route("/api/messages")
def api_messages():
    """
    原始消息列表，按发布时间倒序分页。

    参数（都是可选的）：
        page          页码，默认 1
        page_size     每页条数，默认 config.PAGE_SIZE（10），上限 100
        keyword       关键词，命中正文即可
        min_prob      谣言概率下限，例如 0.5
        max_prob      谣言概率上限
        nature        性质：谣言 / 非谣言
        only_pending  传 1 表示只看还没人工校验的（未处理 + 已检测）

    返回：{list: [...], total: 总条数, page: 当前页, page_size: 每页条数}
    """
    page, page_size = parse_page_params()
    # 列表页默认就只看没处理完的，已经校验过的挪到“已处理文本”那一栏
    only_pending = arg_int("only_pending", 1) == 1

    try:
        rows, total = database.get_messages(
            page=page,
            page_size=page_size,
            keyword=arg_str("keyword"),
            min_prob=arg_float("min_prob"),
            max_prob=arg_float("max_prob"),
            nature=arg_str("nature"),
            only_pending=only_pending,
        )
    except Exception as e:
        return fail("消息查询失败：{0}".format(e))

    return ok({"list": rows, "total": total, "page": page, "page_size": page_size})


@app.route("/api/message/<int:raw_id>")
def api_message_detail(raw_id):
    """
    单条消息详情。

    参数：URL 里的 raw_id。
    返回：这一条消息的全部字段，详情页用。
    """
    try:
        row = database.get_raw_message(raw_id)
    except Exception as e:
        return fail("查询失败：{0}".format(e))

    if row is None:
        return fail("消息不存在，id={0}".format(raw_id), code=404)
    return ok(row)


@app.route("/api/processed")
def api_processed():
    """
    已处理文本列表（人工校验过的），按处理时间倒序。

    参数：
        page       页码，默认 1
        page_size  每页条数
        nature     只看谣言 / 非谣言
        keyword    正文关键词

    返回：{list, total, page, page_size}
    """
    page, page_size = parse_page_params()
    try:
        rows, total = database.get_processed_messages(
            page=page,
            page_size=page_size,
            nature=arg_str("nature"),
            keyword=arg_str("keyword"),
        )
    except Exception as e:
        return fail("已处理文本查询失败：{0}".format(e))

    return ok({"list": rows, "total": total, "page": page, "page_size": page_size})


@app.route("/api/keywords")
def api_keywords():
    """
    关键词列表，词云的数据来源。

    参数：
        top_n  取前几个词，默认 100
        min_frequency  最小词频，默认 1

    返回：{list: [{word, frequency, name, value}, ...]}
    """
    top_n = arg_int("top_n", 100) or 100
    min_frequency = arg_int("min_frequency", 1) or 1
    try:
        rows = database.get_keywords(top_n=top_n, min_frequency=min_frequency)
    except Exception as e:
        return fail("关键词查询失败：{0}".format(e))

    return ok({"list": rows, "total": len(rows)})


@app.route("/api/search")
def api_search():
    """
    关键字搜索。和消息列表的区别是：这里默认把已处理的也一起搜，
    因为搜索是“在所有消息里找跟这个词相关的”，不区分处理状态。

    参数：
        keyword            关键词，命中正文或命中关键词字段都算
        min_prob/max_prob  概率区间
        nature             谣言 / 非谣言
        include_processed  传 1（默认）连已处理一起搜，传 0 只搜在流转中的
        page / page_size   分页

    返回：{list, total, page, page_size, keyword}，把关键词也回传一份，
    前端展示“搜索 xx 的结果”时用。
    """
    page, page_size = parse_page_params()
    keyword = arg_str("keyword")
    include_processed = arg_int("include_processed", 1) == 1

    try:
        rows, total = database.search_messages(
            keyword=keyword,
            min_prob=arg_float("min_prob"),
            max_prob=arg_float("max_prob"),
            nature=arg_str("nature"),
            page=page,
            page_size=page_size,
            include_processed=include_processed,
        )
    except Exception as e:
        return fail("搜索失败：{0}".format(e))

    return ok({
        "list": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "keyword": keyword or "",
    })


# ============================================================
# 三、检测与校验接口
# ============================================================

@app.route("/api/detect/<int:raw_id>", methods=["POST"])
def api_detect(raw_id):
    """
    对一条消息跑模型检测。

    参数：
        URL 里的 raw_id；
        请求体可选传 threshold，不传就用 config.RUMOR_THRESHOLD。

    做的事：读出正文 -> predict.predict_text 算概率 -> 写回 raw_messages。
    返回：{prob, prob_text, label, keywords, is_rumor, threshold}
    """
    params = body_params()
    threshold = params.get("threshold")
    if threshold in (None, ""):
        threshold = config.RUMOR_THRESHOLD
    else:
        # 阈值必须落在 0~1，填 5 这种明显是手滑，直接按默认值来
        try:
            threshold = float(threshold)
            if not 0 <= threshold <= 1:
                threshold = config.RUMOR_THRESHOLD
        except (TypeError, ValueError):
            threshold = config.RUMOR_THRESHOLD

    row = database.get_raw_message(raw_id)
    if row is None:
        return fail("消息不存在，id={0}".format(raw_id), code=404)

    if row["status"] == database.STATUS_VERIFIED:
        return fail("这条消息已经人工校验过了，不用再检测", code=403)

    try:
        result = predict.predict_text(row["text"], threshold)
    except predict.ModelNotReady as e:
        # 模型文件没生成的情况，提示用户去跑 train_model.py
        return fail("模型还没准备好：{0}".format(e), code=500)
    except Exception as e:
        return fail("检测出错：{0}".format(e), code=500)

    success, msg = database.update_detect_result(raw_id, result["prob"], result["keywords"])
    if not success:
        return fail(msg, code=500)

    result["threshold"] = threshold
    result["id"] = raw_id
    return ok(result, msg="检测完成")


@app.route("/api/verify/<int:raw_id>", methods=["POST"])
def api_verify(raw_id):
    """
    人工校验一条消息。

    参数：
        URL 里的 raw_id；
        请求体必须传 nature，取值只能是“谣言”或“非谣言”。

    做的事：改 raw_messages 的状态和性质，往 processed_messages 插一条记录。
    返回：成功的话把这条最新的消息数据带回去，前端好刷新界面。
    """
    params = body_params()
    nature = (params.get("nature") or "").strip()

    if nature not in (database.NATURE_RUMOR, database.NATURE_NORMAL):
        return fail("nature 参数只能是“谣言”或“非谣言”", code=400)

    success, msg = database.verify_message(raw_id, nature)
    if not success:
        # 消息不存在返回 404，其他情况（比如写库失败）返回 500
        code = 404 if "不存在" in msg else 500
        return fail(msg, code=code)

    # 校验完再查一遍，返回最新的状态给前端
    try:
        row = database.get_raw_message(raw_id)
    except Exception:
        row = None

    return ok(row, msg=msg)


# ============================================================
# 四、启动
# ============================================================

def check_environment():
    """
    启动前的自检，有问题就在控制台说清楚，不然前端报错很难查。

    检查两项：数据库通不通、模型文件在不在。
    这两个都不是致命错误，检查不通过也让服务起来，
    至少能看到界面，提示文字会显示在控制台。
    """
    print("=" * 60)
    print("社交媒体谣言检测系统 - 启动自检")
    print("=" * 60)

    db_ok, db_msg = database.test_connection()
    print("[数据库] {0}".format(db_msg))
    if not db_ok:
        print("         检查 MySQL 服务是否启动、config.py 里的用户名密码是否正确，")
        print("         库还没建的话先执行 python init_db.py。")

    if predict.is_model_ready():
        print("[模型]   模型文件已就绪")
    else:
        print("[模型]   没找到模型文件，检测功能用不了。")
        print("         先执行 python train_model.py 训练模型。")

    print("-" * 60)


def main():
    check_environment()
    print("服务地址：http://127.0.0.1:{0}".format(config.PORT))
    print("按 Ctrl+C 停止服务")
    print("=" * 60)
    # use_reloader=False：调试模式自动重载会开两个进程，换个端口容易冲突，
    # 课程演示阶段关掉更稳；需要热重载的话把 debug 打开后自己改这里。
    app.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()