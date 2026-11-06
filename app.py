"""
大模型综述 —— 独立 Flask 服务

设计要点：
  * 独立目录、独立端口（默认 8090），不与 english-quiz 耦合
  * 左侧固定菜单（带返回按钮），右侧正文
  * 每个页面右上角有「编辑」按钮，支持在线改内容并保存到 JSON
  * 静态资源挂在 /llm/static/
"""
import hmac
import os
import sys
import traceback

from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, abort, send_from_directory, session)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, 'templates'),
            static_folder=os.path.join(BASE_DIR, 'static'),
            static_url_path='/static')
app.secret_key = os.environ.get('LLM_REVIEW_SECRET', 'llm-review-secret-2026')

# Caddy 反代时前缀是 /llm，用它拼绝对 URL
SCRIPT_NAME = os.environ.get('LLM_REVIEW_SCRIPT_NAME', '/llm')

# ── 编辑鉴权 ──────────────────────────────────────────────────────────────
# 编辑接口是写操作，公网暴露时必须校验，避免任何人改内容。
# ADMIN_TOKEN 通过环境变量注入；未设置时编辑功能整体关闭（只读模式）。
ADMIN_TOKEN = os.environ.get('LLM_REVIEW_ADMIN_TOKEN', '').strip()
EDIT_ENABLED = bool(ADMIN_TOKEN)


def _token_ok(candidate):
    """常量时间比较，避免时序侧信道"""
    if not ADMIN_TOKEN or not candidate:
        return False
    return hmac.compare_digest(str(candidate), ADMIN_TOKEN)


def _require_edit_auth():
    """校验编辑权限，返回 None 表示通过，否则返回 (响应, 状态码)"""
    if not EDIT_ENABLED:
        return jsonify({
            'ok': False,
            'error': '编辑功能未开启（未配置 LLM_REVIEW_ADMIN_TOKEN）'
        }), 403
    token = (request.headers.get('X-Admin-Token')
             or request.args.get('token')
             or '')
    if not _token_ok(token):
        return jsonify({'ok': False, 'error': '编辑令牌无效'}), 401
    return None


class StripPrefixMiddleware:
    """剥掉请求路径的 /llm 前缀。

    Caddy 用的是 `handle /llm/*`（不 strip 前缀），所以 Flask 收到的是
    /llm/page/xxx 而非 /page/xxx。这里在 WSGI 层统一剥掉前缀，
    让路由只注册一份（/page/xxx），模板里仍用 script_name 拼绝对 URL。
    直接访问 8090 端口（无前缀）时行为不变。
    """

    def __init__(self, wsgi_app, prefix='/llm'):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip('/')

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == self.prefix:
            environ['PATH_INFO'] = '/'
        elif path.startswith(self.prefix + '/'):
            environ['PATH_INFO'] = path[len(self.prefix):]
        return self.wsgi_app(environ, start_response)


@app.context_processor
def inject_globals():
    return {'script_name': SCRIPT_NAME, 'edit_enabled': EDIT_ENABLED}


# ── 页面渲染 ──────────────────────────────────────────────────────────────

def _render_page(slug, editable=True):
    nav = store.load_nav()
    page = store.load_page(slug)
    if page is None:
        abort(404)

    flat = store.flatten_nav(nav)
    # 计算当前页在扁平列表中的下标，用于「上一页/下一页」
    idx = next((i for i, it in enumerate(flat) if it['slug'] == slug), None)
    prev_item = flat[idx - 1] if idx is not None and idx > 0 else None
    next_item = flat[idx + 1] if idx is not None and idx < len(flat) - 1 else None

    found = store.find_nav(nav, slug)
    parents = found['parents'] if found else []

    return render_template('page.html',
                           nav=nav,
                           flat=flat,
                           page=page,
                           slug=slug,
                           parents=parents,
                           prev_item=prev_item,
                           next_item=next_item,
                           editable=editable)


@app.route('/')
def index():
    """综述首页"""
    nav = store.load_nav()
    flat = store.flatten_nav(nav)
    counts = {s: 1 for s in store.list_pages()}
    return render_template('index.html', nav=nav, flat=flat, counts=counts)


@app.route('/page/<slug>')
def page(slug):
    return _render_page(slug)


@app.route('/news')
@app.route('/news/')
def news_index():
    """每日动态列表页（按日期倒序）"""
    nav = store.load_nav()
    dates = store.list_news_dates()
    entries = []
    for d in dates:
        data = store.load_news(d) or {}
        entries.append({
            'date': d,
            'items': data.get('items', []),
            'news_items': data.get('news_items', []),
        })
    total = sum(len(e['items']) + len(e['news_items']) for e in entries)
    return render_template('news.html', nav=nav, entries=entries,
                           dates=dates, total=total)


@app.route('/news/<date_str>')
def news_day(date_str):
    """某一天的动态详情"""
    nav = store.load_nav()
    data = store.load_news(date_str)
    if data is None:
        abort(404)
    dates = store.list_news_dates()
    idx = dates.index(date_str) if date_str in dates else 0
    prev_date = dates[idx + 1] if idx + 1 < len(dates) else None
    next_date = dates[idx - 1] if idx > 0 else None
    return render_template('news_day.html', nav=nav, day=data,
                           date_str=date_str, prev_date=prev_date,
                           next_date=next_date)


@app.route('/section/<path:path>')
def section(path):
    """阶段/目录节点页：渲染该节点自身的页（若存在），否则给个空壳"""
    nav = store.load_nav()
    parts = [p for p in path.split('/') if p]
    if not parts:
        return redirect(url_for('index'))

    # 在导航树里逐级下钻，找到最深匹配
    cur_items = nav.get('sections', [])
    node = None
    trail = []
    for part in parts:
        nxt = None
        for it in cur_items:
            if it.get('slug') == part:
                nxt = it
                break
        if nxt is None:
            abort(404)
        node = nxt
        trail.append(nxt)
        cur_items = nxt.get('children', [])

    slug = node.get('slug')
    page_data = store.load_page(slug) if slug else None
    if page_data is None:
        page_data = {
            'title': node.get('title', parts[-1]),
            'summary': node.get('summary', '（待补充）'),
            'sections': [],
        }

    flat = store.flatten_nav(nav)
    idx = next((i for i, it in enumerate(flat) if it['slug'] == slug), None)
    prev_item = flat[idx - 1] if idx is not None and idx > 0 else None
    next_item = flat[idx + 1] if idx is not None and idx < len(flat) - 1 else None

    return render_template('page.html',
                           nav=nav, flat=flat, page=page_data, slug=slug,
                           parents=trail[:-1], prev_item=prev_item,
                           next_item=next_item, editable=True)


# ── 编辑 API ──────────────────────────────────────────────────────────────

@app.route('/api/page/<slug>', methods=['GET'])
def api_get_page(slug):
    page = store.load_page(slug)
    if page is None:
        return jsonify({'ok': False, 'error': '页面不存在'}), 404
    return jsonify({'ok': True, 'page': page})


@app.route('/api/page/<slug>', methods=['POST', 'PUT'])
def api_save_page(slug):
    """保存页面内容。前端 POST {title, summary, sections:[...]}"""
    denied = _require_edit_auth()
    if denied:
        return denied

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': '请求体必须是 JSON 对象'}), 400

    # 简单字段校验
    if not isinstance(data.get('title'), str) or not data['title'].strip():
        return jsonify({'ok': False, 'error': '标题不能为空'}), 400
    if not isinstance(data.get('sections', []), list):
        return jsonify({'ok': False, 'error': 'sections 必须是数组'}), 400

    try:
        store.save_page(slug, data)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({'ok': False, 'error': f'保存失败：{e}'}), 500

    return jsonify({'ok': True, 'slug': slug})


@app.route('/api/page/<slug>', methods=['DELETE'])
def api_delete_page(slug):
    denied = _require_edit_auth()
    if denied:
        return denied
    ok = store.delete_page(slug)
    return jsonify({'ok': ok})


@app.route('/api/nav', methods=['GET'])
def api_get_nav():
    return jsonify({'ok': True, 'nav': store.load_nav()})


@app.route('/api/nav', methods=['POST', 'PUT'])
def api_save_nav():
    denied = _require_edit_auth()
    if denied:
        return denied
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': '请求体必须是 JSON 对象'}), 400
    store.save_nav(data)
    return jsonify({'ok': True})


@app.route('/api/pages', methods=['GET'])
def api_list_pages():
    return jsonify({'ok': True, 'pages': store.list_pages()})


# ── 静态资源 ──────────────────────────────────────────────────────────────

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)


@app.errorhandler(404)
def _404(e):
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'not found'}), 404
    return render_template('error.html', code=404,
                           message='页面不存在，可能是链接输错了'), 404


@app.errorhandler(500)
def _500(e):
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': '服务器内部错误'}), 500
    return render_template('error.html', code=500,
                           message='服务器内部错误'), 500


if __name__ == '__main__':
    PORT = int(os.environ.get('LLM_REVIEW_PORT', '8090'))
    # 挂上剥前缀中间件（Caddy 反代时路径带 /llm 前缀）
    app.wsgi_app = StripPrefixMiddleware(app.wsgi_app, SCRIPT_NAME)
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
