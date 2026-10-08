# 社交媒体谣言检测系统

大三课程项目《谣言检测界面设计》。用 Flask + MySQL + scikit-learn 做了一个可以演示的网页系统，
包含数据展示、服务器检测结果呈现、人工校验、关键字搜索四个模块。

爬虫部分同时写了真实抓取和模拟数据兜底两套逻辑：真实抓取接了今日头条、百度、腾讯、
新浪、澎湃、中新网六个公开的新闻热榜和滚动页，不用登录就能抓；答辩的时候就算校园网
访问不了外网，也能用本地 CSV 把整个流程跑完。

## 一、目录结构

```
rumor_detection_project/
├── app.py                  # Flask 主程序，所有接口都在这里
├── config.py               # 配置：数据库、端口、模型路径
├── database.py             # 数据库连接和常用操作封装
├── init_db.py              # 建库建表脚本
├── spider.py               # 爬虫（真实抓取 + 模拟数据兜底）
├── preprocess.py           # 文本清洗、去重、jieba 分词、TF-IDF
├── train_model.py          # 训练 TF-IDF + 逻辑回归模型
├── train_lstm.py           # 训练 LSTM（扩展部分）
├── predict.py              # 加载模型做预测
├── requirements.txt        # 依赖清单
├── README.md               # 本文件
├── run.bat                 # Windows 一键启动脚本
├── data/
│   └── sample_data.csv     # 50 条模拟数据，爬虫失败时的兜底数据
├── models/                 # 训练好的模型文件（.pkl / .pt）
├── static/
│   ├── style.css
│   └── script.js
├── templates/
│   ├── index.html
│   └── detail.html
└── docs/
    ├── 系统概述文档.md
    ├── 系统调研总结报告.md
    ├── 七人分工表.md
    ├── 接口约定.md
    ├── 数据库约定.md
    ├── 文件树约定.md
    ├── PPT大纲.md
    ├── 演示视频脚本.md
    └── 常见报错与解决.md
```

## 二、开发环境

| 项目 | 版本 |
| --- | --- |
| 操作系统 | Windows |
| conda 安装位置 | D:\conda（没有加进系统 PATH） |
| conda 环境名 | rumor_detection |
| Python | 3.10.22 |
| MySQL | 8.4.7，字符集 utf8mb4 |
| 端口 | 5000 |

## 三、安装依赖

本机的 conda 装在 `D:\conda`，没有加进系统 PATH，所以在普通命令行里直接敲 `conda` 会提示
“不是内部或外部命令”。两种激活方式，任选一种：

方式一，直接双击 `D:\conda\Scripts\activate.bat`，会弹出命令行并进入 base 环境，
再执行 `activate rumor_detection`。

方式二，在命令行里用完整路径激活：

```bat
call D:\conda\Scripts\activate.bat rumor_detection
```

激活成功的标志是命令行提示符前面出现 `(rumor_detection)`。
然后进入项目目录安装依赖：

```bat
cd /d D:\program\python\social_media_analysis\rumor_detection_project
pip install -r requirements.txt
```

requirements.txt 里目前是这几项：

```
flask
flask-sqlalchemy
pymysql
jieba
scikit-learn
pandas
requests
beautifulsoup4
lxml
joblib
numpy
```

如果后面要跑 LSTM 那部分，再多装一个 PyTorch：

```bat
pip install torch
```

国内下载慢的话可以加清华源：

```bat
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 四、初始化数据库

### 1. 改密码

打开 `config.py`，把这一行的占位符改成自己本机 MySQL 的 root 密码：

```python
DB_PASSWORD = "your_password_here"
```

### 2. 建库建表

手工建库也可以，用命令行或者 Navicat 执行：

```sql
CREATE DATABASE IF NOT EXISTS rumor_detection
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_general_ci;
```

也可以直接让脚本自动建，省事：

```bat
python init_db.py
```

脚本里用的是不带库名的连接串，所以库不存在也能连上 MySQL，会先 `CREATE DATABASE` 再建三张表。
表已经存在的时候不会重复创建，可以放心重复执行。

### 3. 三张表

- `raw_messages`：原始消息，检测前的数据都在这
- `processed_messages`：人工校验完的消息
- `keywords`：关键词和词频，词云用的

## 五、跑数据、训练模型

按顺序执行下面几条命令（都在项目目录下、已激活环境的前提下）：

```bat
python spider.py          # 抓六个新闻源的数据，抓不到就自动读 data/sample_data.csv
python preprocess.py      # 清洗、去重、分词、算 TF-IDF
python train_model.py     # 训练模型，结果保存到 models/
```

`train_model.py` 的训练样本有三个来源：`data/sample_data.csv` 里 50 条人工标注数据、
数据库里已经人工校验过的记录，以及爬虫抓来的新闻正文。第三条是按“热榜和滚动页的新闻
是媒体发布的真实信息”这个假设，把来源属于 `config.NEWS_SOURCES` 的消息统一当成非谣言。
不想要这个假设就加 `--no-news` 参数。

加进去的原因是：词表只由训练样本构成，如果只用那几十条模拟数据，真实新闻里的词模型
一个都没见过，特征向量全为 0，逻辑回归只能输出一个常数，界面上就会看到一大批消息的
概率都挤在 49.6% 这种同一个值上。把新闻正文纳入训练以后词表覆盖了新闻用词，概率才有区分度。

想试 LSTM 的话再跑：

```bat
python train_lstm.py
```

## 六、启动项目

### 方式一：一键脚本

双击 `run.bat`，它会自动激活环境、初始化数据库、启动服务。

run.bat 里的提示文字是英文的，别觉得奇怪：cmd 读 .bat 用的是系统 OEM 编码（中文 Windows 下是 936），
脚本里写 UTF-8 中文会把注释读成乱码，字节错位以后连后面的命令都会被吞掉。所以这个文件故意只放 ASCII 字符，
要改的话也请不要往里加中文注释。脚本开头的 `chcp 65001` 是为了让 Python 输出的中文日志正常显示。

### 方式二：手动敲命令

```bat
call D:\conda\Scripts\activate.bat rumor_detection
cd /d D:\program\python\social_media_analysis\rumor_detection_project
python app.py
```

如果懒得激活，也可以直接用环境里的解释器跑，效果一样：

```bat
D:\conda\envs\rumor_detection\python.exe app.py
```

启动后浏览器打开 <http://127.0.0.1:5000> 就能看到首页。
端口被占用的话，去 `config.py` 里把 `PORT` 改成别的，比如 5001。

## 七、接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 首页 |
| GET | `/detail/<id>` | 消息详情页 |
| GET | `/api/overview` | 顶部统计数字 |
| GET | `/api/messages` | 分页获取未处理消息 |
| GET | `/api/message/<id>` | 单条消息详情 |
| POST | `/api/detect/<id>` | 对单条消息做检测，返回谣言概率 |
| POST | `/api/detect-all` | 一键检测：把所有没人工校验的消息重跑一遍 |
| POST | `/api/verify/<id>` | 人工校验，参数 nature（谣言 / 非谣言） |
| GET | `/api/processed` | 已处理消息列表 |
| GET | `/api/keywords` | 词云数据 |
| GET | `/api/search` | 按关键词和概率过滤 |

所有接口返回的结构统一是 `{"code": 0, "msg": "提示文字", "data": {...}}`，
`code` 为 0 表示成功，非 0 表示出错，原因写在 `msg` 里。

`/api/messages`、`/api/processed`、`/api/search` 都支持一个 `sort` 参数，取值
`time_desc`（发布时间从新到旧，默认）、`time_asc`（发布时间从旧到新）、
`prob_desc`（谣言概率从高到低），传了别的值按默认排序处理。

列表页标签行右边有个“一键检测”按钮，走的就是 `/api/detect-all`：
把所有还没人工校验的消息重新跑一遍当前模型。模型重训过以后旧概率会失准，
用这个按钮刷一遍就行，已经人工校验过的消息不会被覆盖。

## 八、遇到问题

数据库连不上、端口被占用、词云不显示这些常见毛病，
都记在 `docs/常见报错与解决.md` 里了，先去那里翻一翻。
