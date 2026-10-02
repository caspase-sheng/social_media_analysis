# -*- coding: utf-8 -*-
"""
数据库初始化脚本。

做两件事：
1. 如果 rumor_detection 库不存在，先建库（字符集 utf8mb4，支持 emoji）；
2. 按 database.py 里定义的表结构建表。

表已经存在时不会重复建，可以放心多次执行。
用法：
    python init_db.py            正常初始化，有表就跳过
    python init_db.py --drop     先删掉三张表再重建（数据会没，演示前重置用）
    python init_db.py --check    只检查连接，什么都不改
"""

import sys

from sqlalchemy import create_engine, text

import config
import database


def create_database_if_not_exists():
    """连到 MySQL 服务器（不带库名），库不存在就建。"""
    # 注意这里用的是不带库名的连接串，因为库还不存在的时候带上库名连不上
    server_engine = create_engine(config.SQLALCHEMY_SERVER_URI, echo=False)
    sql = (
        "CREATE DATABASE IF NOT EXISTS `{db}` "
        "DEFAULT CHARACTER SET utf8mb4 "
        "DEFAULT COLLATE utf8mb4_general_ci"
    ).format(db=config.DB_NAME)

    with server_engine.connect() as conn:
        conn.execute(text(sql))
        # 顺便确认一下库真的在
        result = conn.execute(text(
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = :name"
        ), {"name": config.DB_NAME}).fetchone()

    server_engine.dispose()
    return result is not None


def drop_tables():
    """删掉三张表，注意外键顺序：先删从表再删主表。"""
    with database.engine.connect() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in ("processed_messages", "keywords", "raw_messages"):
            conn.execute(text("DROP TABLE IF EXISTS `{0}`".format(table)))
            print("  已删除表 {0}".format(table))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        conn.commit()


def create_tables():
    """按 ORM 定义建表，已存在的表不会动。"""
    database.Base.metadata.create_all(database.engine)
    return sorted(database.Base.metadata.tables.keys())


def show_table_structure():
    """把建好的表结构打出来，答辩前想确认字段对不对的时候很方便。"""
    with database.engine.connect() as conn:
        for table in ("raw_messages", "processed_messages", "keywords"):
            rows = conn.execute(text(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_COMMENT "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tb "
                "ORDER BY ORDINAL_POSITION"
            ), {"db": config.DB_NAME, "tb": table}).fetchall()

            print("\n表 {0}（共 {1} 个字段）".format(table, len(rows)))
            print("  {0:<18}{1:<18}{2:<8}{3:<8}{4}".format("字段", "类型", "可空", "键", "注释"))
            for col, ctype, nullable, key, comment in rows:
                print("  {0:<18}{1:<18}{2:<8}{3:<8}{4}".format(
                    col, ctype, "是" if nullable == "YES" else "否", key or "-", comment or "-"))


def print_user_hint():
    """打印建业务用户的 SQL，需要的话自己在 MySQL 里执行。"""
    print("\n" + "-" * 60)
    print("如果不想用 root 连数据库，可以建一个专用账号，在 MySQL 里执行：")
    print("  CREATE USER 'rumor'@'localhost' IDENTIFIED BY '你的密码';")
    print("  GRANT ALL PRIVILEGES ON {0}.* TO 'rumor'@'localhost';".format(config.DB_NAME))
    print("  FLUSH PRIVILEGES;")
    print("然后把 config.py 里的 DB_USER / DB_PASSWORD 改成对应值。")


def main():
    args = sys.argv[1:]
    only_check = "--check" in args
    need_drop = "--drop" in args

    print("=" * 60)
    print("社交媒体谣言检测系统 - 数据库初始化")
    print("=" * 60)
    print("连接信息：{0}:{1}  用户 {2}  库 {3}".format(
        config.DB_HOST, config.DB_PORT, config.DB_USER, config.DB_NAME))

    # 第一步：建库
    print("\n[1/3] 检查并创建数据库...")
    try:
        if create_database_if_not_exists():
            print("  数据库 {0} 已就绪".format(config.DB_NAME))
        else:
            print("  [失败] 建库后没能查到 {0}，请检查账号权限".format(config.DB_NAME))
            return 1
    except Exception as e:
        print("  [失败] 连不上 MySQL：{0}".format(e))
        print("\n  排查方向：")
        print("  1. MySQL 服务有没有启动（services.msc 里看 MySQL84）")
        print("  2. config.py 里的 DB_PASSWORD 是不是还是占位符 your_password_here")
        print("  3. 端口是不是 3306，装 MySQL 的时候有没有改过")
        return 1

    if only_check:
        print("\n--check 模式，只检查连接，不建表。数据库正常。")
        return 0

    # 第二步：建表
    print("\n[2/3] 创建数据表...")
    try:
        if need_drop:
            print("  --drop 参数生效，先删除旧表：")
            drop_tables()
        tables = create_tables()
        for name in tables:
            print("  表 {0} 已就绪".format(name))
    except Exception as e:
        print("  [失败] 建表出错：{0}".format(e))
        return 1

    # 第三步：把结构打出来核对
    print("\n[3/3] 当前表结构：")
    try:
        show_table_structure()
    except Exception as e:
        print("  查询表结构失败（不影响使用）：{0}".format(e))

    print_user_hint()
    print("\n初始化完成。下一步可以跑 python spider.py 往库里面灌数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())