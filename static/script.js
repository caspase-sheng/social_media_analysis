/* 社交媒体谣言检测系统 - 前端脚本
 *
 * 页面分两种，靠 body 上的 data-page 区分：
 *   index  -> 首页，负责统计数字、消息列表、搜索筛选、词云
 *   detail -> 详情页，负责显示单条消息、调检测接口、提交人工校验
 *
 * 所有数据都通过 fetch 调后端 /api/ 接口拿，页面本身是服务端渲染的空壳。
 */

// ============================================================
// 公共工具
// ============================================================

/**
 * GET 请求封装。
 * 后端统一返回 {code, msg, data}，这里把 code != 0 的情况当成错误抛出去，
 * 调用处用一个 try/catch 就能处理。
 */
async function apiGet(url) {
    const resp = await fetch(url);
    if (!resp.ok) {
        throw new Error('请求失败，HTTP ' + resp.status);
    }
    const json = await resp.json();
    if (json.code !== 0) {
        throw new Error(json.msg || '接口返回异常');
    }
    return json.data;
}

/** POST 请求封装，参数用 JSON 发过去。 */
async function apiPost(url, body) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
    });
    const json = await resp.json();
    if (json.code !== 0) {
        throw new Error(json.msg || '接口返回异常');
    }
    return json.data;
}

/** 把用户能看到的内容转义一下，防止消息正文里的尖括号把页面结构搞乱。 */
function escapeHtml(value) {
    if (value === null || value === undefined) {
        return '';
    }
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
    return String(value).replace(/[&<>"']/g, function (c) { return map[c]; });
}

/** 状态中文 -> 标签样式类名。 */
function statusClass(status) {
    if (status === '未处理') { return 'pending'; }
    if (status === '已检测') { return 'detected'; }
    if (status === '已处理') { return 'verified'; }
    return 'pending';
}

/** 生成状态标签、性质标签的 HTML。 */
function statusTag(status) {
    if (!status) { return '-'; }
    return '<span class="tag ' + statusClass(status) + '">' + escapeHtml(status) + '</span>';
}

function natureTag(nature) {
    if (!nature) { return '-'; }
    const cls = nature === '谣言' ? 'rumor' : 'normal';
    return '<span class="tag ' + cls + '">' + escapeHtml(nature) + '</span>';
}

/**
 * 概率单元格的 HTML：百分比文字 + 一条进度条。
 * 概率 >= 0.5 用红色，低于 0.5 用绿色，扫一眼就能看出哪些偏高。
 */
function probCell(prob) {
    if (prob === null || prob === undefined) {
        return '<span style="color:#9aa1ad;">未检测</span>';
    }
    const pct = Math.round(prob * 1000) / 10;
    const high = prob >= 0.5;
    return '<span class="prob' + (high ? ' high' : '') + '">' +
        '<span class="bar"><span style="width:' + pct + '%;"></span></span>' +
        '<span class="val">' + pct.toFixed(1) + '%</span>' +
        '</span>';
}

/** 显示一条提示，ok=false 时是红色错误样式。 */
function showNotice(el, message, isOk) {
    el.textContent = message;
    el.className = 'notice show ' + (isOk ? 'ok' : 'err');
}


// ============================================================
// 首页
// ============================================================

const PAGE_SIZE = 10;

// 列表的当前状态，搜索、翻页、切标签都是改这个对象再重新拉数据
const listState = {
    tab: 'pending',   // pending=消息列表，processed=已处理文本
    page: 1,
    keyword: '',
    minProb: '',
    nature: '',
    sort: 'time_desc' // 排序方式，取值和后端 database.SORT_CHOICES 一致
};

// 两种标签页的表头不一样，切换的时候整行替换
const TABLE_HEADS = {
    pending: '<th style="width:52px;">ID</th><th>正文</th><th style="width:96px;">来源</th>' +
        '<th style="width:150px;">发布时间</th><th style="width:130px;">谣言概率</th>' +
        '<th style="width:82px;">状态</th><th style="width:120px;">操作</th>',
    processed: '<th style="width:52px;">ID</th><th>正文</th><th style="width:96px;">来源</th>' +
        '<th style="width:130px;">谣言概率</th><th style="width:82px;">处理结果</th>' +
        '<th style="width:150px;">处理时间</th><th style="width:80px;">操作</th>'
};

/** 拉顶部统计数字。 */
async function loadOverview() {
    try {
        const data = await apiGet('/api/overview');
        document.getElementById('stat-total').textContent = data.total;
        // 还没人工校验的 = 未处理 + 已检测，这两个都要人去点，合并显示一个数
        document.getElementById('stat-undone').textContent = data.pending + data.detected;
        document.getElementById('stat-verified').textContent = data.verified;
        document.getElementById('stat-rumor').textContent = data.rumor;
    } catch (e) {
        console.error('统计数字加载失败', e);
    }
}

/** 根据当前筛选条件拼查询串。 */
function buildQuery(extra) {
    const params = new URLSearchParams();
    params.set('page', listState.page);
    params.set('page_size', PAGE_SIZE);
    if (listState.keyword) { params.set('keyword', listState.keyword); }
    if (listState.minProb !== '') { params.set('min_prob', listState.minProb); }
    if (listState.nature) { params.set('nature', listState.nature); }
    if (listState.sort) { params.set('sort', listState.sort); }
    if (extra) {
        Object.keys(extra).forEach(function (k) { params.set(k, extra[k]); });
    }
    return params.toString();
}

/** 拉当前标签页的列表并渲染。 */
async function loadList() {
    const tbody = document.getElementById('table-body');
    document.getElementById('table-head').innerHTML = TABLE_HEADS[listState.tab];
    tbody.innerHTML = '<tr><td colspan="7" class="empty">正在加载...</td></tr>';

    const url = listState.tab === 'pending'
        ? '/api/messages?' + buildQuery({ only_pending: 1 })
        : '/api/processed?' + buildQuery();

    let data;
    try {
        data = await apiGet(url);
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty">加载失败：' + escapeHtml(e.message) + '</td></tr>';
        return;
    }

    renderRows(tbody, data);
    renderPager(data);
}

/** 渲染表格内容。 */
function renderRows(tbody, data) {
    if (!data.list || data.list.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty">没有符合条件的消息</td></tr>';
        return;
    }

    let html = '';
    data.list.forEach(function (row) {
        if (listState.tab === 'pending') {
            html += '<tr>' +
                '<td><a href="/detail/' + row.id + '">' + row.id + '</a></td>' +
                '<td class="text-cell">' + escapeHtml(row.short_text) + '</td>' +
                '<td class="nowrap">' + escapeHtml(row.source || '-') + '</td>' +
                '<td class="nowrap">' + escapeHtml(row.timestamp || '-') + '</td>' +
                '<td>' + probCell(row.rumor_prob) + '</td>' +
                '<td>' + statusTag(row.status) + '</td>' +
                '<td class="nowrap">' +
                '<button class="btn mini" data-detect="' + row.id + '">检测</button> ' +
                '<a href="/detail/' + row.id + '">详情</a>' +
                '</td>' +
                '</tr>';
        } else {
            html += '<tr>' +
                '<td><a href="/detail/' + row.raw_id + '">' + row.raw_id + '</a></td>' +
                '<td class="text-cell">' + escapeHtml(row.short_text) + '</td>' +
                '<td class="nowrap">' + escapeHtml(row.source || '-') + '</td>' +
                '<td>' + probCell(row.rumor_prob) + '</td>' +
                '<td>' + natureTag(row.nature) + '</td>' +
                '<td class="nowrap">' + escapeHtml(row.processed_time || '-') + '</td>' +
                '<td><a href="/detail/' + row.raw_id + '">详情</a></td>' +
                '</tr>';
        }
    });
    tbody.innerHTML = html;
}

/** 分页条。 */
function renderPager(data) {
    const totalPage = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
    document.getElementById('page-info').textContent =
        '第 ' + data.page + ' / ' + totalPage + ' 页';
    document.getElementById('total-info').textContent = '共 ' + data.total + ' 条';
    document.getElementById('btn-prev').disabled = data.page <= 1;
    document.getElementById('btn-next').disabled = data.page >= totalPage;
}

/** 把搜索框里的条件读出来存进 listState。 */
function readFilters() {
    listState.keyword = document.getElementById('kw').value.trim();
    listState.minProb = document.getElementById('min-prob').value.trim();
    listState.nature = document.getElementById('nature').value;
    listState.sort = document.getElementById('sort').value;
}

/** 切换标签页（消息列表 / 已处理文本）。 */
function switchTab(name) {
    document.querySelectorAll('.tab').forEach(function (t) {
        t.classList.toggle('active', t.dataset.tab === name);
    });
    listState.tab = name;
    listState.page = 1;
    loadList();
}

/**
 * 点词云上的词之后：把词填进搜索框、切回消息列表、重新查询。
 * 词云统计的是全部关键词，所以点击时会先把概率下限和性质清掉，
 * 免得旧的筛选条件把这个词的结果过滤没了让人以为没查到。
 */
function filterByKeyword(word) {
    if (!word) { return; }
    document.getElementById('kw').value = word;
    document.getElementById('min-prob').value = '';
    document.getElementById('nature').value = '';
    listState.keyword = word;
    listState.minProb = '';
    listState.nature = '';
    switchTab('pending');
    // 列表在上方，滚动过去让用户直接看到筛选结果
    document.querySelector('#table-body').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/** 拉词云数据并画出来。 */
async function loadKeywords() {
    const box = document.getElementById('wordcloud');
    let data;
    try {
        data = await apiGet('/api/keywords?top_n=80');
    } catch (e) {
        box.innerHTML = '<div class="empty">关键词加载失败：' + escapeHtml(e.message) + '</div>';
        return;
    }

    const list = data.list || [];
    if (list.length === 0) {
        box.innerHTML = '<div class="empty">还没有关键词数据，先执行 python preprocess.py 统计词频</div>';
        return;
    }

    // ECharts 是从 CDN 加载的，没网就用文字大小表示词频兜底
    if (typeof echarts === 'undefined') {
        renderTagCloud(box, list);
        return;
    }

    const palette = ['#2f6fed', '#1f9d64', '#e2574c', '#b97708', '#7a4fd6', '#0f9bb0'];
    const chart = echarts.init(box);
    chart.setOption({
        tooltip: { show: true },
        series: [{
            type: 'wordCloud',
            shape: 'circle',
            width: '100%',
            height: '100%',
            sizeRange: [14, 52],
            rotationRange: [0, 0],   // 词都横着放，斜着的字看起来累
            gridSize: 8,
            drawOutOfBound: false,
            cursor: 'pointer',       // 鼠标移上去变成手型，提示这个词能点
            textStyle: {
                fontFamily: 'Microsoft YaHei, sans-serif',
                color: function () {
                    return palette[Math.floor(Math.random() * palette.length)];
                }
            },
            data: list.map(function (item) {
                return { name: item.word, value: item.frequency };
            })
        }]
    });
    // 点词去筛选消息
    chart.on('click', function (params) {
        filterByKeyword(params.name);
    });
    window.addEventListener('resize', function () { chart.resize(); });
}

/** 没加载到 ECharts 时的兜底方案：按词频调节字号。 */
function renderTagCloud(box, list) {
    const max = list[0].frequency || 1;
    const min = list[list.length - 1].frequency || 1;
    const html = list.map(function (item) {
        const ratio = max === min ? 1 : (item.frequency - min) / (max - min);
        const size = 14 + Math.round(ratio * 24);
        return '<span class="cloud-word" style="font-size:' + size + 'px;" ' +
            'title="出现 ' + item.frequency + ' 次，点击筛选" data-word="' + escapeHtml(item.word) + '">' +
            escapeHtml(item.word) + '</span>';
    }).join('');
    box.innerHTML = '<div class="cloud-fallback">' + html + '</div>';
}

/** 首页的事件绑定，只在首页调用。 */
function initIndexPage() {
    loadOverview();
    loadList();
    loadKeywords();

    // 搜索 / 重置
    document.getElementById('btn-search').addEventListener('click', function () {
        readFilters();
        listState.page = 1;
        loadList();
    });

    document.getElementById('btn-reset').addEventListener('click', function () {
        document.getElementById('kw').value = '';
        document.getElementById('min-prob').value = '';
        document.getElementById('nature').value = '';
        document.getElementById('sort').value = 'time_desc';
        readFilters();
        listState.page = 1;
        loadList();
    });

    // 一键检测：把所有没人工校验的消息重新跑一遍模型。
    // 消息多的时候要跑几秒，所以先确认一下，跑的过程中按钮禁用防止重复点。
    document.getElementById('btn-detect-all').addEventListener('click', async function () {
        if (!confirm('将对所有还没人工校验的消息重新跑一遍检测，确定继续吗？')) {
            return;
        }
        const btn = this;
        btn.disabled = true;
        btn.textContent = '检测中...';
        try {
            const data = await apiPost('/api/detect-all', {});
            alert('检测完成：成功 ' + data.ok + ' 条，失败 ' + data.fail +
                ' 条，用时 ' + data.elapsed + ' 秒');
            listState.page = 1;
            // 概率、状态、命中的关键词都变了，列表和词云一起刷新
            await loadList();
            loadOverview();
            loadKeywords();
        } catch (err) {
            alert('一键检测失败：' + err.message);
        } finally {
            btn.disabled = false;
            btn.textContent = '一键检测';
        }
    });

    // 排序方式改了立即重新查，不用再点搜索
    document.getElementById('sort').addEventListener('change', function () {
        listState.sort = this.value;
        listState.page = 1;
        loadList();
    });

    // 搜索框里按回车也能搜
    document.getElementById('kw').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
            document.getElementById('btn-search').click();
        }
    });

    // 切标签页
    document.querySelectorAll('.tab').forEach(function (tab) {
        tab.addEventListener('click', function () {
            readFilters();   // 切页前先收一下当前的筛选条件
            switchTab(tab.dataset.tab);
        });
    });

    // 词云兜底模式（ECharts 没加载出来）下点词筛选，用事件委托
    document.getElementById('wordcloud').addEventListener('click', function (e) {
        const span = e.target.closest('.cloud-word');
        if (span) {
            filterByKeyword(span.dataset.word);
        }
    });

    // 翻页
    document.getElementById('btn-prev').addEventListener('click', function () {
        if (listState.page > 1) {
            listState.page -= 1;
            loadList();
        }
    });
    document.getElementById('btn-next').addEventListener('click', function () {
        listState.page += 1;
        loadList();
    });

    // 表格里的“检测”按钮用事件委托，行是动态生成的，直接绑会丢
    document.getElementById('table-body').addEventListener('click', async function (e) {
        const btn = e.target.closest('button[data-detect]');
        if (!btn) { return; }

        const id = btn.dataset.detect;
        btn.disabled = true;
        btn.textContent = '检测中';
        try {
            await apiPost('/api/detect/' + id, {});
            // 检测完概率和状态都变了，整表重新拉一次，概率列才会实时刷新；
            // 不重拉的话行里显示的还是旧的“未检测”
            await loadList();
            loadOverview();
        } catch (err) {
            btn.textContent = '失败';
            alert('检测失败：' + err.message);
        }
    });
}


// ============================================================
// 详情页
// ============================================================

/** 把接口返回的这条消息填到页面上。 */
function fillDetail(row) {
    document.getElementById('detail-text').textContent = row.text || '';
    document.getElementById('m-id').textContent = row.id;
    document.getElementById('m-source').textContent = row.source || '-';
    document.getElementById('m-time').textContent = row.timestamp || '-';
    document.getElementById('m-status').innerHTML = statusTag(row.status);
    document.getElementById('m-nature').innerHTML = row.nature ? natureTag(row.nature) : '-';
    document.getElementById('m-pvtime').textContent = row.processed_time || '-';
    document.getElementById('m-keywords').textContent = row.keywords || '无';

    // 概率那一条
    const probBox = document.getElementById('m-prob');
    const bar = document.getElementById('m-prob-bar');
    const val = document.getElementById('m-prob-val');
    if (row.rumor_prob === null || row.rumor_prob === undefined) {
        probBox.style.display = 'none';
    } else {
        probBox.style.display = 'inline-flex';
        probBox.className = 'prob' + (row.rumor_prob >= 0.5 ? ' high' : '');
        bar.style.width = (row.rumor_prob * 100).toFixed(1) + '%';
        val.textContent = (row.rumor_prob * 100).toFixed(1) + '%';
    }

    // 配图有就显示，没有就藏起来
    const img = document.getElementById('detail-image');
    if (row.image_url) {
        img.src = row.image_url;
        img.style.display = 'block';
    } else {
        img.style.display = 'none';
    }

    // 人工校验过的消息允许改判：两个校验按钮始终可点，按钮文字跟着当前结论走，
    // 用户一眼能看出再点一下会改成什么。已处理的消息不再允许重跑模型检测，
    // 避免模型结果把人工结论盖掉。
    const verified = row.status === '已处理';
    document.getElementById('btn-detect').disabled = verified;

    const btnRumor = document.getElementById('btn-rumor');
    const btnNormal = document.getElementById('btn-normal');
    btnRumor.disabled = false;
    btnNormal.disabled = false;

    const label = document.getElementById('verify-label');
    if (verified) {
        label.textContent = '当前人工结论：' + (row.nature || '未定') + '，可改判为：';
        btnRumor.textContent = row.nature === '谣言' ? '仍判为谣言' : '改为谣言';
        btnNormal.textContent = row.nature === '非谣言' ? '仍判为非谣言' : '改为非谣言';
    } else {
        label.textContent = '人工校验：';
        btnRumor.textContent = '判为谣言';
        btnNormal.textContent = '判为非谣言';
    }
}

/** 详情页的事件绑定。 */
function initDetailPage() {
    const rawId = document.body.dataset.id;
    const notice = document.getElementById('notice');
    // 记住这条消息当前的人工结论，用来判断这次点按钮是首次校验还是改判
    let currentNature = null;

    async function reload() {
        try {
            const row = await apiGet('/api/message/' + rawId);
            currentNature = row.nature || null;
            fillDetail(row);
        } catch (e) {
            document.getElementById('detail-text').textContent = '加载失败：' + e.message;
        }
    }

    document.getElementById('btn-detect').addEventListener('click', async function () {
        const btn = this;
        btn.disabled = true;
        try {
            const result = await apiPost('/api/detect/' + rawId, {});
            showNotice(notice, '检测完成：谣言概率 ' + result.prob_text +
                '，判定为「' + result.label + '」，主要依据的词：' + (result.keywords || '无'), true);
            await reload();
        } catch (e) {
            showNotice(notice, '检测失败：' + e.message, false);
        } finally {
            // reload 之后按钮状态会被重新设置，这里先恢复可点
            btn.disabled = false;
        }
    });

    // 两个校验按钮共用一段逻辑，只是性质不同
    function bindVerify(buttonId, nature) {
        document.getElementById(buttonId).addEventListener('click', async function () {
            // 已经校验过的是改判，把旧结论也写进提示里，确认框说得更清楚
            const tip = currentNature
                ? '确定把这条消息的人工结论从「' + currentNature + '」改为「' + nature + '」吗？'
                : '确定把这条消息标记为「' + nature + '」吗？';
            if (!confirm(tip)) {
                return;
            }
            try {
                await apiPost('/api/verify/' + rawId, { nature: nature });
                // 接口只返回这条消息的最新内容，提示文字在前端拼，区分首次校验和改判
                const tip = currentNature
                    ? '已改判：人工结论从「' + currentNature + '」改为「' + nature + '」。'
                    : '已提交，这条消息标记为「' + nature + '」，并移入已处理文本。';
                showNotice(notice, tip, true);
                await reload();
            } catch (e) {
                showNotice(notice, '提交失败：' + e.message, false);
            }
        });
    }

    bindVerify('btn-rumor', '谣言');
    bindVerify('btn-normal', '非谣言');

    reload();
}


// ============================================================
// 入口：按 body 上的 data-page 决定初始化哪个页面
// ============================================================

document.addEventListener('DOMContentLoaded', function () {
    const page = document.body.dataset.page;
    if (page === 'index') {
        initIndexPage();
    } else if (page === 'detail') {
        initDetailPage();
    }
});