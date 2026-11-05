"""
内容存储层
—— 综述内容以 JSON 文件形式存放在 content/ 目录
—— 每个「页面」对应一个 JSON 文件，文件名即页面 slug
—— 支持用户通过页面上的「编辑」按钮修改并落盘
"""
import json
import os
import re
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONTENT_DIR = os.path.join(BASE_DIR, 'content')
NAV_FILE = os.path.join(BASE_DIR, 'data', 'nav.json')

# 写操作串行化，避免并发编辑互相覆盖
_write_lock = threading.Lock()

# slug 合法字符：字母、数字、连字符
SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9\-]{0,80}$')


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def _read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path, data):
    """原子写：先写临时文件再 rename，避免写一半被读到"""
    _ensure_dir(os.path.dirname(path))
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ── 导航树 ────────────────────────────────────────────────────────────────

def load_nav():
    """读取导航树（左侧菜单结构）"""
    nav = _read_json(NAV_FILE, default=None)
    if nav is None:
        return {'sections': []}
    return nav


def save_nav(nav):
    with _write_lock:
        _write_json(NAV_FILE, nav)
    return True


# ── 页面内容 ──────────────────────────────────────────────────────────────

def page_path(slug):
    return os.path.join(CONTENT_DIR, f'{slug}.json')


def load_page(slug):
    """读取单个页面内容；不存在返回 None"""
    if not SLUG_RE.match(slug or ''):
        return None
    return _read_json(page_path(slug), default=None)


def save_page(slug, data):
    """保存单个页面内容"""
    if not SLUG_RE.match(slug or ''):
        raise ValueError('非法 slug')
    with _write_lock:
        _write_json(page_path(slug), data)
    return True


def delete_page(slug):
    if not SLUG_RE.match(slug or ''):
        raise ValueError('非法 slug')
    p = page_path(slug)
    if os.path.exists(p):
        with _write_lock:
            os.remove(p)
        return True
    return False


def list_pages():
    """列出所有已存在的页面 slug"""
    if not os.path.isdir(CONTENT_DIR):
        return []
    return sorted(
        f[:-5] for f in os.listdir(CONTENT_DIR)
        if f.endswith('.json')
    )


# ── 每日动态（按日期归档）────────────────────────────────────────────────

NEWS_DIR = os.path.join(CONTENT_DIR, 'news')


def news_path(date_str):
    return os.path.join(NEWS_DIR, f'{date_str}.json')


def load_news(date_str):
    """读取某天的动态；不存在返回 None"""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str or ''):
        return None
    return _read_json(news_path(date_str), default=None)


def save_news(date_str, data):
    with _write_lock:
        _write_json(news_path(date_str), data)
    return True


def list_news_dates():
    """列出所有动态日期（倒序，最新在前）"""
    if not os.path.isdir(NEWS_DIR):
        return []
    dates = [f[:-5] for f in os.listdir(NEWS_DIR)
             if f.endswith('.json') and re.match(r'^\d{4}-\d{2}-\d{2}\.json$', f)]
    return sorted(dates, reverse=True)


def prune_news(retention_days=30):
    """删除超出保留期的动态文件，返回被删除的日期列表"""
    from datetime import date, timedelta
    if not os.path.isdir(NEWS_DIR):
        return []
    cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
    removed = []
    for d in list_news_dates():
        if d < cutoff:
            try:
                with _write_lock:
                    os.remove(news_path(d))
                removed.append(d)
            except OSError:
                pass
    return removed


# ── 已收录条目去重索引 ───────────────────────────────────────────────────

DEDUP_FILE = os.path.join(BASE_DIR, 'data', 'seen.json')


def load_seen():
    """读取去重索引：{条目key: 首次收录日期}"""
    return _read_json(DEDUP_FILE, default={}) or {}


def save_seen(seen):
    with _write_lock:
        _write_json(DEDUP_FILE, seen)
    return True


# ── 合并进技术点页 ───────────────────────────────────────────────────────

MERGE_HEADING = '📌 最新进展'


def merge_into_page(slug, entry):
    """把一条动态合并进技术点页的『📌 最新进展』区块。

    entry 结构：{title_zh, title_en, url, source, date, note}
    返回 True 表示写入成功，False 表示跳过（页面不存在 / 已收录）。
    """
    page = load_page(slug)
    if page is None:
        return False

    key = entry.get('url') or entry.get('title_en') or ''
    if not key:
        return False

    sections = page.setdefault('sections', [])
    target = None
    for s in sections:
        if s.get('heading') == MERGE_HEADING:
            target = s
            break
    if target is None:
        target = {
            'heading': MERGE_HEADING,
            'tldr': '自动同步的技术动态（每日 03:00 任务写入），随领域进展更新。',
            'body': '<p>以下条目由每日同步任务自动追加，按时间倒序排列，'
                    '<strong>原文链接</strong>可直达原论文/原文。</p>',
            'points': [],
        }
        sections.append(target)

    # 同一条目不重复合并
    if any(p.startswith(f"[{entry.get('date', '')}]") and key in p
           for p in target.get('points', [])):
        return False

    line = (f"[{entry.get('date', '')}] **{entry.get('title_zh', '')}**"
            f"（{entry.get('source', '')}）— {entry.get('note', '')} "
            f"原文：{key}")

    points = target.setdefault('points', [])
    points.insert(0, line)

    # 限制单页合并条数，超出的从尾部丢弃
    max_n = 8
    if len(points) > max_n:
        del points[max_n:]

    page['updated_at'] = entry.get('date', '')
    save_page(slug, page)
    return True


# ── 导航树遍历工具 ────────────────────────────────────────────────────────

def flatten_nav(nav):
    """把导航树拍平成 [(slug, title, depth, path)]，用于渲染和查找"""
    out = []

    def walk(items, depth=0, prefix=''):
        for it in items:
            slug = it.get('slug')
            title = it.get('title', slug or '')
            cur_prefix = f'{prefix}/{slug}' if slug else prefix
            out.append({
                'slug': slug,
                'title': title,
                'depth': depth,
                'path': cur_prefix,
                'kind': it.get('kind', 'page'),
            })
            if it.get('children'):
                walk(it['children'], depth + 1, cur_prefix)

    walk(nav.get('sections', []))
    return out


def find_nav(nav, slug):
    """按 slug 在导航树里查找节点，返回 (节点, 父节点列表, 完整路径)"""
    target = None
    trail = []

    def walk(items, parents):
        nonlocal target
        for it in items:
            if target is not None:
                return
            s = it.get('slug')
            if s == slug:
                target = (it, list(parents))
                return
            if it.get('children'):
                walk(it['children'], parents + [it])

    walk(nav.get('sections', []), [])
    if target is None:
        return None
    node, parents = target
    path = '/' + '/'.join(
        [p.get('slug') for p in parents if p.get('slug')] +
        ([node.get('slug')] if node.get('slug') else [])
    )
    return {'node': node, 'parents': parents, 'path': path}
