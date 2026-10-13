# -*- coding: utf-8 -*-
"""演示前一键重置：清空所有检测结果与人工校验，恢复未处理状态"""
import database


def reset_all():
    session = database.get_session()
    try:
        # 1. 删除已处理记录
        session.query(database.ProcessedMessage).delete()

        # 2. 清空 raw_messages 里的检测结果，重置状态
        session.query(database.RawMessage).update({
            database.RawMessage.rumor_prob: None,
            database.RawMessage.status: database.STATUS_PENDING,
            database.RawMessage.nature: None,
            database.RawMessage.processed_time: None,
            database.RawMessage.keywords: None,
        })

        # 3. 清空关键词表（词云数据）
        session.query(database.Keyword).delete()

        session.commit()
        print("✓ 重置成功！所有数据已恢复未处理状态。")
    except Exception as e:
        session.rollback()
        print("重置失败：", e)
    finally:
        database.close_session()


if __name__ == "__main__":
    reset_all()