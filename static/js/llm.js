/* ═══════════════════════════════════════════════════
   大模型综述 · 交互脚本
   菜单高亮 / 移动端抽屉 / 在线编辑
   ═══════════════════════════════════════════════════ */
(function () {
  'use strict';

  var API = window.LLM_API || '/llm';

  /* ── 移动端菜单 ────────────────────────────────── */
  var toggle = document.getElementById('menuToggle');
  var backdrop = document.getElementById('sidebarBackdrop');
  if (toggle) {
    toggle.addEventListener('click', function () {
      document.body.classList.toggle('menu-open');
    });
  }
  if (backdrop) {
    backdrop.addEventListener('click', function () {
      document.body.classList.remove('menu-open');
    });
  }

  /* ── 当前菜单项高亮（服务端已渲染 active，这里兜底） ── */
  var current = document.querySelector('.menu-item[data-current="1"]');
  if (current && current.scrollIntoView) {
    current.scrollIntoView({ block: 'center' });
  }

  /* ── 表格可排序 ────────────────────────────────── */
  document.querySelectorAll('.doc-table').forEach(function (table) {
    var heads = table.querySelectorAll('thead th');
    if (heads.length < 2) return;

    heads.forEach(function (th, idx) {
      th.style.cursor = 'pointer';
      th.style.userSelect = 'none';
      th.title = '点击排序';

      th.addEventListener('click', function () {
        var tbody = table.querySelector('tbody');
        if (!tbody) return;
        var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
        var asc = th.dataset.dir !== 'asc';

        rows.sort(function (a, b) {
          var av = (a.children[idx] ? a.children[idx].textContent : '').trim();
          var bv = (b.children[idx] ? b.children[idx].textContent : '').trim();
          var an = parseFloat(av.replace(/[^0-9.\-]/g, ''));
          var bn = parseFloat(bv.replace(/[^0-9.\-]/g, ''));
          if (!isNaN(an) && !isNaN(bn)) {
            return asc ? an - bn : bn - an;
          }
          return asc ? av.localeCompare(bv, 'zh-CN') : bv.localeCompare(av, 'zh-CN');
        });

        rows.forEach(function (r) { tbody.appendChild(r); });
        heads.forEach(function (h) { delete h.dataset.dir; });
        th.dataset.dir = asc ? 'asc' : 'desc';
      });
    });
  });

  /* ── 编辑器 ────────────────────────────────────── */
  var mask = document.getElementById('editorMask');
  var btnEdit = document.getElementById('btnEdit');
  var btnCancel = document.getElementById('btnCancel');
  var btnSave = document.getElementById('btnSave');
  var btnFormat = document.getElementById('btnFormat');
  var edTitle = document.getElementById('edTitle');
  var edSummary = document.getElementById('edSummary');
  var edSections = document.getElementById('edSections');
  var edSlug = document.getElementById('editorSlug');
  var status = document.getElementById('editorStatus');

  /* ── 令牌弹层 ──────────────────────────────────── */
  var tokenMask = document.getElementById('tokenMask');
  var edToken = document.getElementById('edToken');
  var tokenError = document.getElementById('tokenError');
  var btnTokenOk = document.getElementById('btnTokenOk');
  var btnTokenCancel = document.getElementById('btnTokenCancel');
  var TOKEN_KEY = 'llm_review_token';

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }
  function setToken(t) {
    try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (e) {}
  }
  function showTokenDialog(msg) {
    if (!tokenMask) return;
    tokenMask.hidden = false;
    document.body.style.overflow = 'hidden';
    if (tokenError) tokenError.textContent = msg || '';
    if (edToken) { edToken.value = getToken(); edToken.focus(); edToken.select(); }
  }
  function closeTokenDialog() {
    if (!tokenMask) return;
    tokenMask.hidden = true;
    document.body.style.overflow = '';
  }
  function submitToken() {
    var t = (edToken.value || '').trim();
    if (!t) { if (tokenError) tokenError.textContent = '请输入令牌'; return; }
    setToken(t);
    closeTokenDialog();
    openEditor();
  }
  if (btnTokenOk) btnTokenOk.addEventListener('click', submitToken);
  if (btnTokenCancel) btnTokenCancel.addEventListener('click', closeTokenDialog);
  if (edToken) {
    edToken.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') submitToken();
      if (e.key === 'Escape') closeTokenDialog();
    });
  }
  if (tokenMask) {
    tokenMask.addEventListener('click', function (e) {
      if (e.target === tokenMask) closeTokenDialog();
    });
  }

  if (!mask || !btnEdit) return;

  var currentSlug = window.LLM_SLUG || '';

  function setStatus(msg, kind) {
    if (!status) return;
    status.textContent = msg || '';
    status.className = 'editor-status' + (kind ? ' ' + kind : '');
  }

  function openEditor() {
    var slug = currentSlug;
    if (!slug) { setStatus('首页暂不支持直接编辑', 'err'); return; }

    edSlug.textContent = slug;
    setStatus('加载中…');
    mask.hidden = false;
    document.body.style.overflow = 'hidden';

    fetch(API + '/api/page/' + encodeURIComponent(slug))
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.error || '加载失败');
        var p = res.page || {};
        edTitle.value = p.title || '';
        edSummary.value = p.summary || '';
        edSections.value = JSON.stringify(p.sections || [], null, 2);
        setStatus('');
        edTitle.focus();
      })
      .catch(function (e) {
        setStatus('加载失败：' + e.message, 'err');
      });
  }
  function closeEditor() {
    mask.hidden = true;
    document.body.style.overflow = '';
    setStatus('');
  }

  btnEdit.addEventListener('click', function () {
    // 无令牌时先弹令牌框
    if (!getToken()) { showTokenDialog(); return; }
    openEditor();
  });
  if (btnCancel) btnCancel.addEventListener('click', closeEditor);

  // 点击遮罩空白处关闭
  mask.addEventListener('click', function (e) {
    if (e.target === mask) closeEditor();
  });

  // ESC 关闭
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !mask.hidden) closeEditor();
  });

  if (btnFormat) {
    btnFormat.addEventListener('click', function () {
      try {
        edSections.value = JSON.stringify(JSON.parse(edSections.value || '[]'), null, 2);
        setStatus('JSON 格式已整理', 'ok');
      } catch (e) {
        setStatus('JSON 语法错误：' + e.message, 'err');
      }
    });
  }

  if (btnSave) {
    btnSave.addEventListener('click', function () {
      var sections;
      try {
        sections = JSON.parse(edSections.value || '[]');
        if (!Array.isArray(sections)) throw new Error('sections 必须是数组');
      } catch (e) {
        setStatus('JSON 解析失败：' + e.message, 'err');
        return;
      }

      var payload = {
        title: (edTitle.value || '').trim(),
        summary: (edSummary.value || '').trim(),
        sections: sections,
        updated_at: new Date().toISOString().slice(0, 10)
      };
      if (!payload.title) { setStatus('标题不能为空', 'err'); return; }

      btnSave.disabled = true;
      setStatus('保存中…');

      fetch(API + '/api/page/' + encodeURIComponent(currentSlug), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Admin-Token': getToken()
        },
        body: JSON.stringify(payload)
      })
        .then(function (r) {
          return r.json().then(function (j) { return { ok: r.ok, body: j }; });
        })
        .then(function (res) {
          if (res.body && res.body.error === '编辑令牌无效') {
            setToken('');
            closeEditor();
            showTokenDialog('令牌无效，请重新输入');
            throw new Error('令牌无效');
          }
          if (!res.ok) throw new Error((res.body && res.body.error) || '保存失败');
          setStatus('✅ 已保存，正在刷新…', 'ok');
          setTimeout(function () { location.reload(); }, 700);
        })
        .catch(function (e) {
          setStatus('保存失败：' + e.message, 'err');
        })
        .finally(function () { btnSave.disabled = false; });
    });
  }
})();
