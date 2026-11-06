"""
每日大模型动态同步 —— 数据抓取与处理

流程：
  1. arXiv API 拉取指定分类最近提交（cs.CL / cs.LG / cs.AI / cs.DC）
  2. opencli 补充：hackernews top / 36kr news
  3. 关键词打分 → 路由到综述中的技术点模块
  4. 去重（arXiv id + 标题指纹，保留 30 天）
  5. 挑出每日最多 N 条，翻译成中文，附原文链接
  6. 写入 content/news/<date>.json，并把强相关的合并进对应技术点页

独立运行：python3 sync_news.py [--dry-run] [--limit N]
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOPICS_FILE = os.path.join(BASE_DIR, 'data', 'topics.json')

# arXiv 分类
ARXIV_CATEGORIES = ['cs.CL', 'cs.LG', 'cs.AI', 'cs.DC']
ARXIV_MAX = 120          # 每个分类抓多少条候选
ARXIV_API = 'https://export.arxiv.org/api/query'

# 只保留最近 N 天的提交（arXiv 的 submittedDate 可能滞后）
MAX_AGE_DAYS = 3

UA = 'Mozilla/5.0 (compatible; llm-review-sync/1.0)'

# RSS 源（中文 AI 媒体 / 官方博客）
# 每项：(名称, feed_url, 是否中文)
# 机器之心中文站官方 RSS 已失效（/rss 等路径均返回 404/HTML），
# 故用其英文站 syncedreview.com 作为代替（内容同源）。
RSS_SOURCES = [
    ('量子位', 'https://www.qbitai.com/feed', True),
    ('机器之心', 'https://syncedreview.com/feed/', False),
    ('OpenAI', 'https://openai.com/news/rss.xml', False),
    ('Google AI', 'https://blog.google/technology/ai/rss/', False),
]
RSS_MAX_ITEMS = 20         # 每个 RSS 源最多取多少条
RSS_SOURCE_NAMES = {s[0] for s in RSS_SOURCES} | {'Hacker News', '36氪'}

# 中文源：在「业界动态」栏保底 1 个名额
# 背景：中文媒体内容偏产品发布/财报，命中技术点关键词少（实测 11-06
# 量子位 9 条最高仅 1 分），按分数排序会被 arXiv 挤成 0 条入选。
CN_SOURCES = ['量子位', '机器之心', '36氪']


def _log(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def load_topics():
    """读取 topics.json，返回 {'topics': {...}, 各配置项}"""
    with open(TOPICS_FILE, encoding='utf-8') as f:
        return json.load(f)


def load_topic_map():
    """只取 topics 子字典（slug → 配置），供路由/打分使用"""
    return load_topics().get('topics', {})


# ── arXiv 抓取 ────────────────────────────────────────────────────────────

def fetch_arxiv(categories=None, max_results=100):
    """拉取 arXiv 最近提交，返回 [{id,title,summary,authors,published,url,source}]"""
    cats = categories or ARXIV_CATEGORIES
    out = []
    for cat in cats:
        q = urllib.parse.urlencode({
            'search_query': f'cat:{cat}',
            'sortBy': 'submittedDate',
            'sortOrder': 'descending',
            'start': 0,
            'max_results': max_results,
        })
        url = f'{ARXIV_API}?{q}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                xml = r.read()
        except Exception as e:  # noqa: BLE001
            _log(f'  ⚠️ arXiv {cat} 抓取失败: {e}')
            continue

        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            _log(f'  ⚠️ arXiv {cat} XML 解析失败: {e}')
            continue

        ns = {'a': 'http://www.w3.org/2005/Atom'}
        n = 0
        for entry in root.findall('a:entry', ns):
            eid = (entry.find('a:id', ns).text or '').strip()
            title = ' '.join((entry.find('a:title', ns).text or '').split())
            summary = ' '.join((entry.find('a:summary', ns).text or '').split())
            published = (entry.find('a:published', ns).text or '')[:10]
            url_ = (entry.find('a:id', ns).text or '').strip()
            if 'v' in url_.rsplit('/', 1)[-1]:
                url_ = url_.rsplit('v', 1)[0]

            # 年龄过滤
            if published:
                try:
                    if (date.today() - datetime.strptime(published, '%Y-%m-%d').date()).days > MAX_AGE_DAYS:
                        continue
                except ValueError:
                    pass

            authors = [a.find('a:name', ns).text for a in entry.findall('a:author', ns)
                       if a.find('a:name', ns) is not None]
            out.append({
                'id': eid,
                'title': title,
                'summary': summary,
                'authors': authors[:5],
                'published': published,
                'url': url_,
                'source': 'arXiv',
                'cat': cat,
            })
            n += 1
        _log(f'  ✓ arXiv {cat}: 抓到 {n} 条')
        time.sleep(3)  # arXiv API 要求请求间隔 ≥3s
    return out


# ── opencli 补充源 ───────────────────────────────────────────────────────

def _opencli(args, timeout=60):
    try:
        r = subprocess.run(
            ['opencli'] + args + ['-f', 'json'],
            capture_output=True, text=True, timeout=timeout, cwd='/tmp')
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return None


def _opencli_items(args, timeout=60):
    """调用 opencli 并统一取出条目列表。

    实测返回结构有两种：
      {"ok":true,"data":[...]}              ← hackernews / 36kr
      {"ok":true,"data":{"items":[...]}}     ← 部分适配器
    """
    res = _opencli(args, timeout)
    if not res or not res.get('ok'):
        return []
    data = res.get('data')
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ('items', 'stories', 'news', 'posts', 'results', 'list'):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def fetch_hackernews(limit=40):
    """HN 热帖（抓 LLM 相关）"""
    items = _opencli_items(['hackernews', 'top', '--limit', str(limit)])
    out = []
    for it in items:
        title = it.get('title') or ''
        if not title:
            continue
        out.append({
            'id': f"hn-{it.get('id', '')}",
            'title': title,
            'summary': it.get('text') or '',
            'authors': [it.get('author')] if it.get('author') else [],
            'published': '',
            'url': it.get('url') or f"https://news.ycombinator.com/item?id={it.get('id','')}",
            'source': 'Hacker News',
            'cat': 'hn',
        })
    _log(f'  ✓ Hacker News: 抓到 {len(out)} 条')
    return out


def fetch_36kr(limit=40):
    """36氪科技新闻"""
    items = _opencli_items(['36kr', 'news', '--limit', str(limit)])
    out = []
    for it in items:
        title = it.get('title') or it.get('name') or ''
        if not title:
            continue
        out.append({
            'id': f"36kr-{it.get('id') or title_fingerprint(title)}",
            'title': title,
            'summary': it.get('summary') or it.get('desc') or '',
            'authors': [],
            'published': (it.get('date') or it.get('published_at') or '')[:10],
            'url': it.get('url') or it.get('link') or '',
            'source': '36氪',
            'cat': '36kr',
        })
    _log(f'  ✓ 36氪: 抓到 {len(out)} 条')
    return out


# ── RSS 源 ────────────────────────────────────────────────────────────────

def _strip_html(s):
    """去 HTML 标签 + 解实体，压成纯文本"""
    if not s:
        return ''
    s = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', s, flags=re.S)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = (s.replace('&nbsp;', ' ').replace('&amp;', '&')
          .replace('&lt;', '<').replace('&gt;', '>')
          .replace('&quot;', '"').replace('&#39;', "'")
          .replace('&#8220;', '“').replace('&#8221;', '”'))
    return re.sub(r'\s+', ' ', s).strip()


def _rss_date(raw):
    """把 RFC822 / ISO 日期转成 YYYY-MM-DD"""
    if not raw:
        return ''
    raw = raw.strip()
    for fmt in ('%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S %Z',
                '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d'):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r'(\d{4}-\d{2}-\d{2})', raw)
    return m.group(1) if m else ''


def fetch_rss_source(name, url, is_zh=False, limit=RSS_MAX_ITEMS, retries=2):
    """抓一个 RSS/Atom 源，返回标准条目列表。

    博客类站点（如 blog.google）SSL 握手偶发超时，重试可提高成功率。
    """
    raw = b''
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(3)
    if not raw:
        _log(f'  ⚠️ {name} 抓取失败: {last_err}')
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        _log(f'  ⚠️ {name} XML 解析失败: {e}')
        return []

    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    nodes = root.findall('.//item')
    if not nodes:
        nodes = root.findall('.//atom:entry', ns)

    out = []
    for node in nodes[:limit]:
        def _txt(tag, attr=None):
            el = node.find(tag, ns)
            if el is None:
                el = node.find(f'atom:{tag}', ns)
            if el is None:
                return ''
            if attr and el.get(attr):
                return el.get(attr)
            return (el.text or '')

        title = _strip_html(_txt('title'))
        if not title:
            continue

        link = _txt('link')
        if not link:
            for lk in node.findall('atom:link', ns):
                if lk.get('rel') in (None, 'alternate'):
                    link = lk.get('href') or ''
                    break
        if not link:
            guid = _txt('guid')
            link = guid if guid.startswith('http') else ''

        summary = _strip_html(_txt('description') or _txt('summary'))
        published = _rss_date(_txt('pubDate') or _txt('published') or _txt('updated'))

        out.append({
            'id': f"rss-{name}-{title_fingerprint(title)}",
            'title': title,
            'summary': summary[:600],
            'authors': [],
            'published': published,
            'url': link.strip(),
            'source': name,
            'cat': 'rss',
            'is_zh': is_zh,
        })

    _log(f'  ✓ {name}: 抓到 {len(out)} 条')
    return out


def fetch_all_rss():
    """抓取所有 RSS 源"""
    out = []
    for name, url, is_zh in RSS_SOURCES:
        out.extend(fetch_rss_source(name, url, is_zh))
    return out


# ── 主题路由 ──────────────────────────────────────────────────────────────

# 短裸词（无空格/无连字符且很短）匹配时加词边界，
# 否则会误命中无关单词：
#   'agent' → "60k former agents" / "Multi-Agent System"
#   'ppo'   → "Motorola Signature 27 with GrapheneOS support"（support 含 ppo）
#   'bit'   → 任意含 "bit" 的词
_BARE_WORD_RE = re.compile(r'^[a-z0-9]{2,6}$')
_word_boundary_cache = {}


def _kw_match(kw_l, text):
    """关键词匹配：短裸词走词边界，其余走子串。"""
    if _BARE_WORD_RE.match(kw_l):
        pat = _word_boundary_cache.get(kw_l)
        if pat is None:
            pat = re.compile(r'(?<![a-z0-9])' + re.escape(kw_l) + r'(?![a-z0-9])')
            _word_boundary_cache[kw_l] = pat
        return bool(pat.search(text))
    return kw_l in text


def route_topics(item, topics):
    """给一条内容打分，返回 [(slug, score)] 倒序。

    两类关键词分层处理：
      - 专业关键词（topics.json 原有）：对所有来源生效
      - 通用关键词（topics.json 的 _generic_keywords）：仅对新闻类来源生效
        （Hacker News / 36氪 / RSS 源）。arXiv 论文标题规范、术语准确，
        套通用词会把「评测类论文」误路由到 compare-models。
    """
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    source = item.get('source', '')
    is_news = source in RSS_SOURCE_NAMES
    generic = topics.get('_generic_keywords', {}) if is_news else {}

    hits = []
    for slug, cfg in topics.items():
        if slug.startswith('_'):
            continue
        score = 0
        for kw in cfg.get('keywords', []):
            kw_l = kw.lower()
            if _kw_match(kw_l, text):
                # 长关键词权重更高
                score += 2 if len(kw_l) > 12 else 1
        # 通用词只给新闻源用，且权重低（避免压过专业匹配）
        for kw in generic.get(slug, []):
            if kw.lower() in text:
                score += 1
        if score:
            hits.append((slug, score))
    hits.sort(key=lambda x: -x[1])
    return hits


# ── 去重 ──────────────────────────────────────────────────────────────────

def title_fingerprint(title):
    t = re.sub(r'[^a-z0-9一-鿿]+', '', (title or '').lower())
    return hashlib.md5(t.encode('utf-8')).hexdigest()[:16]


def item_key(item):
    """去重主键：优先用 arXiv id / URL，其次标题指纹"""
    if item.get('id', '').startswith('http://arxiv.org/abs/'):
        return item['id']
    if item.get('url'):
        return item['url'].rstrip('/')
    return 'fp-' + title_fingerprint(item.get('title', ''))


# ── 翻译 ──────────────────────────────────────────────────────────────────

# 翻译时需要保护的技术术语（不被翻译引擎拆译）
PROTECT_TERMS = [
    'LLM', 'LLMs', 'KV Cache', 'MoE', 'RLHF', 'DPO', 'GRPO', 'PPO', 'SFT', 'RLVR',
    'RoPE', 'GQA', 'MLA', 'MHA', 'FFN', 'FlashAttention', 'Flash Attention',
    'Transformer', 'Transformers', 'Embedding', 'Mamba', 'SimPO', 'FP8', 'INT4',
    'INT8', 'AWQ', 'GPTQ', 'NF4', 'Speculative Decoding', 'Attention', 'Context',
    'Agent', 'Agents', 'RL', 'Token', 'Tokens', 'Reasoning', 'Diffusion',
    'LoRA', 'PEFT', 'RAG', 'MCP', 'Transformer', 'Tokenization',
]

_MEM_CACHE = {}
_MYMEM_URL = 'https://api.mymemory.translated.net/get'


def _protect(text):
    """把技术术语换成 ZZA0ZZA 形式的占位符，翻译后再还原。

    实测：普通占位符（如 ⟦0⟧）会被翻译引擎拆译成「⟦1个⟧」，
    ZZA 形式能完整保留。
    """
    store = {}

    def repl(m):
        k = m.group(0)
        if k not in store:
            store[k] = f'ZZA{len(store)}ZZA'
        return store[k]

    pat = '|'.join(re.escape(p) for p in sorted(PROTECT_TERMS, key=len, reverse=True))
    return re.sub(pat, repl, text), store


def _restore(text, store):
    for k, v in store.items():
        text = re.sub(re.escape(v), k, text, flags=re.I)
    return re.sub(r'ZZA\d+ZZA', '', text).strip()


def _translate_mymemory(text):
    """调用 MyMemory 免费翻译 API"""
    prot, store = _protect(text)
    url = (_MYMEM_URL + '?q=' + urllib.parse.quote(prot) + '&langpair=en|zh-CN')
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read())
    zh = j.get('responseData', {}).get('translatedText', '')
    if not zh:
        return None
    # MyMemory 的配额超限会返回一段提示文字
    if 'MYMEMORY WARNING' in zh or 'QUERY LENGTH LIMIT' in zh:
        return None
    return _restore(zh, store)


def _translate_argos(text):
    """本地 argostranslate（若已安装）"""
    try:
        from argostranslate import package
        from argostranslate.translate import translate
    except ImportError:
        return None
    installed = {p.from_code for p in package.get_installed_packages()}
    if 'en' not in installed or 'zh' not in installed:
        return None
    return translate(text, 'en', 'zh')


def _translate_llm_api(text):
    """用 LLM API 翻译（若配置了，质量最好）"""
    key = os.environ.get('LLM_API_KEY', '').strip()
    model = os.environ.get('LLM_MODEL', '').strip()
    base = os.environ.get('LLM_BASE_URL', '').strip()
    if not (key and base):
        return None
    try:
        payload = json.dumps({
            'model': model,
            'messages': [
                {'role': 'system', 'content':
                 '你是学术翻译助手。把英文论文/新闻标题译成简体中文，'
                 '专业术语用通用中文译法，输出纯译文，不要解释。'},
                {'role': 'user', 'content': text},
            ],
            'temperature': 0.2,
        }).encode('utf-8')
        req = urllib.request.Request(
            base.rstrip('/') + '/chat/completions', data=payload,
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {key}'})
        with urllib.request.urlopen(req, timeout=45) as r:
            j = json.loads(r.read())
        return j['choices'][0]['message']['content'].strip()
    except Exception:  # noqa: BLE001
        return None


def translate_title(title):
    """翻译标题为中文。优先 LLM API → argos → MyMemory → 回退原文。"""
    t = (title or '').strip()
    if not t:
        return ''
    if not re.search(r'[a-zA-Z]', t):
        return t  # 已是中文

    for fn in (_translate_llm_api, _translate_argos, _translate_mymemory):
        try:
            res = fn(t)
            if res and res.strip():
                return res.strip()
        except Exception:  # noqa: BLE001
            continue
    return t


def summarize_to_zh(summary, limit=200):
    """把摘要转成中文要点"""
    s = ' '.join((summary or '').split())
    if not s:
        return ''
    if len(s) > 700:
        s = s[:700]
    for fn in (_translate_llm_api, _translate_mymemory):
        try:
            res = fn(s)
            if res and res.strip():
                return ' '.join(res.split())[:limit]
        except Exception:  # noqa: BLE001
            continue
    return s[:limit]



# ── 主流程 ────────────────────────────────────────────────────────────────

def collect():
    """抓取所有数据源"""
    items = []
    _log('抓取 arXiv...')
    items.extend(fetch_arxiv())
    _log('抓取 Hacker News...')
    items.extend(fetch_hackernews())
    _log('抓取 36氪...')
    items.extend(fetch_36kr())
    _log('抓取 RSS 源（量子位 / 机器之心 / OpenAI / Google AI）...')
    items.extend(fetch_all_rss())
    return items


def score_and_pick(items, topics, seen, daily_max, news_max=0):
    """打分 + 去重 + 选出每日 N 条（分两栏）

    打分规则（越高越优先）：
      + 命中技术点关键词：每个关键词 1 分（长词 2 分）
      + 最强命中 >= 2 分（强相关）：额外 +3 —— 这类会合并进模块页
      + 来自 arXiv 且有摘要：+1
      + 标题含数字（通常含具体收益数字）：+1

    硬性门槛：必须至少命中 1 个技术点关键词。
      否则宁可不收 —— 与综述无关的论文（如 ASR、CV、数据集论文）
      哪怕标题里有数字，也不该占用每日 10 条的名额。

    分栏（2026-11-06）：
      论文栏 = arXiv 来源，最多 daily_max 条
      业界栏 = 其余新闻源（RSS/HN/36氪），最多 news_max 条
      两栏独立打分、独立去重，互不挤占名额。
      原因：arXiv 天然占优（有摘要+标题带数字+长专业词），
      混排时新闻源最高仅 6 分而入选线 8 分，实测量子位/OpenAI/
      Google AI 全部零入选。
    """
    scored = []
    in_run_keys = set()   # 本次运行内已选中的条目（arXiv 多分类会重复收录同一篇）
    for it in items:
        key = item_key(it)
        fp = title_fingerprint(it.get('title', ''))
        if key in seen or fp in seen:
            continue  # 已收录
        if key in in_run_keys or fp in in_run_keys:
            continue  # 本次运行内的重复（arXiv 同一篇被多个分类收录）

        hits = route_topics(it, topics)
        if not hits:
            # 一个技术点都没命中 → 与本综述无关，直接丢弃
            continue

        score = sum(s for _, s in hits)
        if hits[0][1] >= 2:
            score += 3   # 强相关，值得合并进模块页
        if it.get('source') == 'arXiv' and it.get('summary'):
            score += 1
        if re.search(r'\d', it.get('title', '')):
            score += 1

        it['_score'] = score
        it['_hits'] = hits
        it['_key'] = key
        it['_fp'] = fp
        scored.append(it)
        in_run_keys.add(key)
        in_run_keys.add(fp)

    scored.sort(key=lambda x: -x['_score'])
    _log(f'  候选 {len(scored)} 条（已过滤未命中任何技术点的内容），按分数排序')

    papers = [s for s in scored if s.get('source') == 'arXiv']
    industry = [s for s in scored if s.get('source') != 'arXiv']

    # 两栏互不重复：业界栏排除已被论文栏占用的 key/fp
    paper_keys = {s['_key'] for s in papers[:daily_max]}
    paper_fps = {s['_fp'] for s in papers[:daily_max]}
    industry = [s for s in industry
                if s['_key'] not in paper_keys and s['_fp'] not in paper_fps]

    # 中文源保底：业界栏按分数排完，量子位/机器之心这类中文源可能
    # 因内容偏产品发布、命中关键词少而被挤出（实测 11-06 量子位 9 条全灭）。
    # 故给每个中文源保底 1 个名额：从该源候选里挑分数最高的补进业界栏。
    industry = _apply_cn_quota(industry, news_max, CN_SOURCES)

    _log(f'  论文栏 {len(papers[:daily_max])} 条 / 业界栏 {len(industry[:news_max])} 条')
    return papers[:daily_max], industry[:news_max]


def _apply_cn_quota(industry, news_max, cn_sources):
    """给中文源在业界栏保底名额。

    规则：每个中文源最多保底 1 条；保底条目优先于分数最低的普通条目，
    但不打乱其余条目的相对顺序。
    """
    if not cn_sources or news_max <= 0:
        return industry

    chosen = industry[:news_max]
    picked_sources = {s.get('source') for s in chosen}

    for src in cn_sources:
        if src in picked_sources:
            continue                      # 该源已有条目入选
        cands = [s for s in industry if s.get('source') == src]
        if not cands:
            continue                      # 该源本轮没抓到/没命中
        # 该源最高分的候选
        best = max(cands, key=lambda x: x['_score'])
        # 挤出当前分数最低的一条（保留其位置语义：补到末尾再整体按分数排）
        if len(chosen) >= news_max:
            chosen = sorted(chosen, key=lambda x: -x['_score'])[:-1]
        chosen.append(best)
        picked_sources.add(src)

    return sorted(chosen, key=lambda x: -x['_score'])[:news_max]


def build_entry(item, topics):
    """构造最终条目（含中文标题、原文链接、模块归属）"""
    title_en = item.get('title', '').strip()
    title_zh = translate_title(title_en)

    note = summarize_to_zh(item.get('summary', ''), limit=180)

    primary = None
    merged = []
    for slug, s in item.get('_hits', []):
        cfg = topics.get(slug, {})
        merged.append({'slug': slug, 'title': cfg.get('title_zh') or cfg.get('title', slug),
                       'score': s})
        if primary is None and s >= 2:
            primary = slug

    return {
        'title_zh': title_zh,
        'title_en': title_en,
        'url': item.get('url', ''),
        'source': item.get('source', ''),
        'date': item.get('published') or date.today().isoformat(),
        'authors': item.get('authors', [])[:4],
        'note': note,
        'topics': merged[:3],
        'merged_into': primary,
        # 收录日（论文发表日 date 可能是几天前）
        'collected_at': date.today().isoformat(),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只抓取和打分，不写文件')
    ap.add_argument('--limit', type=int, default=None, help='每日条数上限')
    ap.add_argument('--date', default=None, help='归档日期 YYYY-MM-DD（默认今天）')
    ap.add_argument('--news-limit', type=int, default=None, help='业界动态条数上限')
    args = ap.parse_args()

    topics = load_topic_map()
    cfg = load_topics()
    daily_max = args.limit or cfg.get('_daily_max', 10)
    news_max = args.news_limit if args.news_limit is not None else cfg.get('_daily_news_max', 6)
    merge_threshold = topics.get('_merge_threshold', 2)
    retention = topics.get('_retention_days', 30)

    target_date = args.date or date.today().isoformat()
    _log(f'=== 大模型动态同步 · {target_date} · 论文 {daily_max} 条 / 业界 {news_max} 条 ===')

    _log('① 抓取数据源')
    items = collect()
    if not items:
        _log('❌ 没有抓到任何内容，退出')
        return 1

    _log('② 打分与去重')
    seen = store.load_seen()
    papers, industry = score_and_pick(items, topics, seen, daily_max, news_max)
    picked = papers + industry
    if not picked:
        _log('ℹ️ 没有新的内容（可能全部已收录）')
        return 0
    _log(f'  选中 {len(picked)} 条（论文 {len(papers)} / 业界 {len(industry)}）：')
    for p in picked:
        top = p['_hits'][0][0] if p['_hits'] else '-'
        _log(f'    [{p["_score"]:>2}] {p["title"][:60]}  → {top}')

    if args.dry_run:
        _log('--dry-run：不写入文件')
        for p in picked:
            hits = p['_hits'][0] if p['_hits'] else None
            print(f'\n--- {p["title"][:70]}')
            print(f'    分数 {p["_score"]} | 命中 {hits} | {p["source"]}')
        return 0

    _log('③ 翻译')
    entries = []
    for p in picked:
        e = build_entry(p, topics)
        entries.append(e)
        _log(f'  ✓ {e["title_zh"][:50]}')
        time.sleep(0.6)  # 避免打爆翻译 API

    _log('④ 写入每日动态')
    existing = store.load_news(target_date) or {'date': target_date, 'items': []}
    # 分栏存储：items = 论文栏，news_items = 业界栏
    # entries 是按 papers + industry 顺序构建的，直接按位置切分。
    # （不能用 URL 匹配 —— 同一条内容可能被两栏共用，或 URL 重复）
    n_paper = len(papers)
    paper_entries = entries[:n_paper]
    news_entries = entries[n_paper:]

    # 按 URL 去重后再合并：同一天多次运行（调试重跑、补抓）时，
    # 新条目排在前面，已存在的旧条目不能重复追加。
    # 否则会出现「论文 20 条 / 业界 12 条」这种超出上限的累积。
    def _merge(new_items, old_items):
        seen_urls = set()
        out = []
        for e in new_items + list(old_items):
            u = e.get('url', '')
            if u and u in seen_urls:
                continue
            if u:
                seen_urls.add(u)
            out.append(e)
        return out

    existing['items'] = _merge(paper_entries, existing.get('items', []))
    existing['news_items'] = _merge(news_entries, existing.get('news_items', []))
    existing['updated_at'] = datetime.now().isoformat(timespec='seconds')
    store.save_news(target_date, existing)
    _log(f'  ✓ content/news/{target_date}.json '
         f'（论文 {len(existing["items"])} 条 / 业界 {len(existing["news_items"])} 条）')

    _log('⑤ 合并进对应技术点页')
    merged_count = 0
    for e in entries:
        slug = e.get('merged_into')
        if not slug:
            continue
        ok = store.merge_into_page(slug, {
            'title_zh': e['title_zh'],
            'title_en': e['title_en'],
            'url': e['url'],
            'source': e['source'],
            'date': e['date'],
            'collected_at': e.get('collected_at') or target_date,
            'note': e['note'][:160],
        })
        if ok:
            merged_count += 1
            _log(f'  ✓ 合并进 {slug}')
    _log(f'  共合并 {merged_count} 条')

    _log('⑥ 更新去重索引')
    for p in picked:
        seen[p['_key']] = target_date
        seen[p['_fp']] = target_date
    store.save_seen(seen)

    _log('⑦ 清理过期动态')
    removed = store.prune_news(retention)
    if removed:
        _log(f'  ✓ 清理 {len(removed)} 天前的动态: {", ".join(removed)}')
    else:
        _log('  无需清理')

    _log(f'=== 完成：{len(entries)} 条动态，{merged_count} 条合并进模块页 ===')
    return 0


if __name__ == '__main__':
    sys.exit(main())
