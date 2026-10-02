# -*- coding: utf-8 -*-
"""
LSTM 扩展模型（PyTorch）。

这部分是"扩展"，不算系统主流程：主流程用的是 TF-IDF + 逻辑回归（train_model.py），
LSTM 放在这里是为了对比——同样是分类任务，深度模型和传统特征工程的做法差在哪，
写进报告里也好看。答辩的时候如果被问"有没有试过深度学习"，这个文件就是答案。

结构很简单：词嵌入 -> LSTM -> 全连接 -> 一个概率值。
参数量也很小（词表 5000 以内、隐藏层 64），因为训练样本只有几十条，
模型一大就纯粹是过拟合，反而不如逻辑回归稳。

用法：
    python train_lstm.py                 训练，默认 30 轮
    python train_lstm.py --epochs 50     多训几轮
    python train_lstm.py --predict "文本"  用训好的模型预测一条

需要安装 PyTorch：
    pip install torch
"""

import argparse
import os
import random
from collections import Counter

import numpy as np

import config
import preprocess
import database
import train_model

# PyTorch 不是必装依赖，这里放到 try 里，没装的话给出安装命令而不是一堆报错
try:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, Dataset
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

# 超参数，都写得比较小，样本量决定的上限
PAD_ID = 0          # 补齐用的占位符
UNK_ID = 1          # 词表里没有的词
MAX_VOCAB = 5000    # 词表最大容量
MAX_LEN = 40        # 每条消息截断/补齐到这个长度
EMBED_DIM = 64
HIDDEN_DIM = 64
BATCH_SIZE = 8
EPOCHS = 30
LEARNING_RATE = 1e-3


# ---------------- 词表 ----------------

def build_vocab(texts):
    """
    根据训练语料建词表。

    PAD_ID 和 UNK_ID 固定占 0 和 1，所以真实词的编号从 2 开始。
    只保留出现过的词，低频词不额外过滤——样本本来就少，再滤就没词了。
    """
    counter = Counter()
    for text in texts:
        counter.update(preprocess.cut_words(text))

    word2id = {"<pad>": PAD_ID, "<unk>": UNK_ID}
    for word, _ in counter.most_common(MAX_VOCAB):
        if word not in word2id:
            word2id[word] = len(word2id)

    return word2id, counter


def text_to_ids(text, word2id, max_len=MAX_LEN):
    """
    文本 -> 定长 id 序列。

    短的补 0，长的截断。截断这里取的是前 max_len 个词，
    因为谣言消息的重点往往在开头（网传、据说、转发这类词都在前面）。
    """
    words = preprocess.cut_words(text)
    ids = [word2id.get(w, UNK_ID) for w in words][:max_len]
    if len(ids) < max_len:
        ids += [PAD_ID] * (max_len - len(ids))
    return ids


# ---------------- 数据集 ----------------

if HAS_TORCH:
    class RumorDataset(Dataset):
        """PyTorch 数据集，把文本和标签包成张量。"""

        def __init__(self, texts, labels, word2id):
            self.texts = texts
            self.labels = labels
            self.word2id = word2id

        def __len__(self):
            return len(self.texts)

        def __getitem__(self, index):
            ids = text_to_ids(self.texts[index], self.word2id)
            return (torch.tensor(ids, dtype=torch.long),
                    torch.tensor(float(self.labels[index]), dtype=torch.float32))

    class LSTMClassifier(nn.Module):
        """
        分类模型本体。

        Embedding 把词编号变成向量，LSTM 按顺序读一遍，最后一个时间步的输出
        就当成整句话的表示，再接一层全连接压成 1 维（logit）。
        padding_idx=PAD_ID 告诉 Embedding 补齐位不用学习，免得补齐影响结果。
        """

        def __init__(self, vocab_size, embed_dim=EMBED_DIM, hidden_dim=HIDDEN_DIM):
            super(LSTMClassifier, self).__init__()
            self.embedding = nn.Embedding(vocab_size, embed_dim, padding_idx=PAD_ID)
            self.lstm = nn.LSTM(embed_dim, hidden_dim, batch_first=True)
            self.dropout = nn.Dropout(0.3)   # 样本少，靠 dropout 压一压过拟合
            self.fc = nn.Linear(hidden_dim, 1)

        def forward(self, x):
            emb = self.embedding(x)
            out, _ = self.lstm(emb)
            # out 的形状是 (batch, 序列长度, 隐藏维度)，取最后一步
            last = out[:, -1, :]
            return self.fc(self.dropout(last)).squeeze(-1)


# ---------------- 训练 ----------------

def train(texts, labels, epochs=EPOCHS, seed=42):
    """训练 LSTM，返回 (模型, 词表)。"""
    if not HAS_TORCH:
        raise RuntimeError("没有安装 PyTorch，请先执行 pip install torch")

    # 固定随机种子，保证每次跑出来的结果差不多，写报告时好复现
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    word2id, counter = build_vocab(texts)
    print("词表大小：{0}（含 pad 和 unk）".format(len(word2id)))
    print("语料里出现过的不同词：{0} 个".format(len(counter)))

    # 划分训练集和验证集。样本少，验证集给 20% 就够看了
    total = len(texts)
    indices = list(range(total))
    random.shuffle(indices)
    split = max(1, int(total * 0.8))
    train_idx, val_idx = indices[:split], indices[split:]

    train_set = RumorDataset([texts[i] for i in train_idx], [labels[i] for i in train_idx], word2id)
    val_set = RumorDataset([texts[i] for i in val_idx], [labels[i] for i in val_idx], word2id)
    print("训练集 {0} 条，验证集 {1} 条".format(len(train_set), len(val_set)))

    # 类别不均衡时给正类（谣言）加一点权重
    n_rumor = sum(labels)
    n_normal = len(labels) - n_rumor
    pos_weight = torch.tensor([n_normal / n_rumor if n_rumor else 1.0], dtype=torch.float32)

    model = LSTMClassifier(len(word2id))
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)

    train_loader = DataLoader(train_set, batch_size=min(BATCH_SIZE, len(train_set)), shuffle=True)

    print("\n开始训练，共 {0} 轮".format(epochs))
    for epoch in range(1, epochs + 1):
        model.train()
        total_loss, correct, seen = 0.0, 0, 0

        for batch_x, batch_y in train_loader:
            optimizer.zero_grad()
            logits = model(batch_x)
            loss = criterion(logits, batch_y)
            loss.backward()
            optimizer.step()

            total_loss += loss.item() * len(batch_y)
            preds = (torch.sigmoid(logits) >= 0.5).float()
            correct += int((preds == batch_y).sum().item())
            seen += len(batch_y)

        # 每 5 轮报一次，不用每轮都刷屏
        if epoch % 5 == 0 or epoch == 1:
            val_acc, val_f1 = evaluate_lstm(model, val_set)
            print("  第 {0:>2} 轮  训练损失 {1:.4f}  训练准确率 {2:.4f}  验证准确率 {3:.4f}  验证 F1 {4:.4f}".format(
                epoch, total_loss / seen, correct / seen, val_acc, val_f1))

    return model, word2id


def evaluate_lstm(model, dataset):
    """在验证集上算准确率和 F1。"""
    model.eval()
    preds, truths = [], []
    with torch.no_grad():
        loader = DataLoader(dataset, batch_size=BATCH_SIZE)
        for batch_x, batch_y in loader:
            prob = torch.sigmoid(model(batch_x))
            preds.extend((prob >= 0.5).long().tolist())
            truths.extend(batch_y.long().tolist())

    accuracy = sum(p == t for p, t in zip(preds, truths)) / max(1, len(truths))
    # F1 手算一遍，省得为这点东西再引 sklearn 的接口
    tp = sum(1 for p, t in zip(preds, truths) if p == 1 and t == 1)
    fp = sum(1 for p, t in zip(preds, truths) if p == 1 and t == 0)
    fn = sum(1 for p, t in zip(preds, truths) if p == 0 and t == 1)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return accuracy, f1


def save_lstm(model, word2id):
    """保存模型权重和词表。词表必须一起存，不然预测时对不上编号。"""
    os.makedirs(config.MODEL_DIR, exist_ok=True)
    torch.save(model.state_dict(), config.LSTM_MODEL_PATH)
    torch.save({
        "word2id": word2id,
        "max_len": MAX_LEN,
        "embed_dim": EMBED_DIM,
        "hidden_dim": HIDDEN_DIM,
        "vocab_size": len(word2id),
    }, config.LSTM_VOCAB_PATH)

    print("\nLSTM 已保存：")
    print("  {0}".format(config.LSTM_MODEL_PATH))
    print("  {0}".format(config.LSTM_VOCAB_PATH))


# ---------------- 预测 ----------------

def load_lstm():
    """加载 LSTM 和词表，返回 (模型, 词表信息)。"""
    if not HAS_TORCH:
        raise RuntimeError("没有安装 PyTorch，请先执行 pip install torch")
    for path in (config.LSTM_MODEL_PATH, config.LSTM_VOCAB_PATH):
        if not os.path.exists(path):
            raise RuntimeError("找不到 {0}，请先执行 python train_lstm.py".format(path))

    info = torch.load(config.LSTM_VOCAB_PATH, weights_only=False)
    model = LSTMClassifier(info["vocab_size"], info["embed_dim"], info["hidden_dim"])
    model.load_state_dict(torch.load(config.LSTM_MODEL_PATH, weights_only=True))
    model.eval()
    return model, info


def predict_lstm(text):
    """对一条文本做 LSTM 预测，返回概率字典。字段和 predict.predict_text 保持一致。"""
    model, info = load_lstm()
    ids = text_to_ids(text, info["word2id"], info["max_len"])

    with torch.no_grad():
        prob = float(torch.sigmoid(model(torch.tensor([ids], dtype=torch.long)))[0])

    return {
        "prob": round(prob, 4),
        "prob_text": "{0:.1f}%".format(prob * 100),
        "label": database.NATURE_RUMOR if prob >= config.RUMOR_THRESHOLD else database.NATURE_NORMAL,
        "model": "LSTM",
    }


def main():
    parser = argparse.ArgumentParser(description="训练 LSTM 扩展模型")
    parser.add_argument("--epochs", type=int, default=EPOCHS, help="训练轮数，默认 30")
    parser.add_argument("--predict", help="训练完之后顺便预测一条文本")
    args = parser.parse_args()

    print("=" * 60)
    print("社交媒体谣言检测系统 - LSTM 扩展模型（PyTorch）")
    print("=" * 60)

    if not HAS_TORCH:
        print("[错误] 没有安装 PyTorch。")
        print("       安装命令：pip install torch")
        print("       这个包比较大（几百 MB），装的时候耐心等一下。")
        print("       如果只是想演示系统，不装也行，主流程用的是逻辑回归。")
        return 1

    print("PyTorch 版本：{0}".format(torch.__version__))
    print("CUDA 可用：{0}（没有显卡也用 CPU 跑，这个模型很小）".format(torch.cuda.is_available()))

    # 训练数据和逻辑回归用的是同一份，保证两者对比是公平的
    texts, labels = train_model.load_training_data()
    if len(texts) < 10:
        print("[错误] 样本只有 {0} 条，太少，训不动。".format(len(texts)))
        return 1

    try:
        model, word2id = train(texts, labels, epochs=args.epochs)
    except Exception as e:
        print("[错误] 训练失败：{0}".format(e))
        return 1

    save_lstm(model, word2id)

    if args.predict:
        try:
            result = predict_lstm(args.predict)
            print("\n预测：{0}".format(args.predict))
            print("谣言概率：{0}  判定：{1}".format(result["prob_text"], result["label"]))
        except Exception as e:
            print("预测出错：{0}".format(e))

    print("\n需要说明的一点：样本只有 {0} 条，LSTM 很容易过拟合，".format(len(texts)))
    print("验证集上的成绩看着不错，其实换一批数据就会掉。")
    print("所以系统实际用的是逻辑回归，LSTM 只是作为对照实验放在这里。")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())