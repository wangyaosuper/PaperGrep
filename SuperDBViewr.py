#!/usr/bin/env python3

import sys
import os
import re
import json
import sqlite3
import argparse
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "db")
DEFAULT_DB_PATH = os.path.join(DB_DIR, "papergrep.db")

PAGE_SIZE = 50

VALID_CATEGORIES = ["self-driving", "robot"]


def _parse_categories_json(s):
    try:
        return json.loads(s or '[]') or []
    except Exception:
        return []


# ============================================================
# Database helpers
# ============================================================

def get_db(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_db(conn)
    return conn


def _migrate_db(conn):
    c = conn.cursor()
    for col, dflt in [
        ("ai_overview_summary_zh", "TEXT DEFAULT NULL"),
        ("is_read", "INTEGER DEFAULT 0"),
        ("is_favorite", "INTEGER DEFAULT 0"),
        ("is_disliked", "INTEGER DEFAULT 0"),
        ("is_shared", "INTEGER DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE papers ADD COLUMN {col} {dflt}")
            conn.commit()
        except Exception:
            pass
    try:
        c.execute("ALTER TABLE papers ADD COLUMN categories_json TEXT DEFAULT '[]'")
        conn.commit()
    except Exception:
        pass


def row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def format_authors(authors_json_str, max_display=5):
    try:
        authors = json.loads(authors_json_str or '[]')
        if not authors:
            return "N/A"
        if len(authors) <= max_display:
            return ', '.join(authors)
        return ', '.join(authors[:max_display]) + f' 等 {len(authors)} 人'
    except Exception:
        return "N/A"


# ============================================================
# API Handlers
# ============================================================

def api_get_stats(conn):
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM papers")
    paper_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM comments")
    comment_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM runs")
    run_count = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(likes),0), COALESCE(SUM(views),0) FROM papers")
    row = c.fetchone()
    total_likes = row[0]
    total_views = row[1]
    c.execute("SELECT COUNT(*) FROM papers WHERE is_favorite=1")
    fav_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM papers WHERE is_disliked=1")
    disl_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM papers WHERE is_shared=1")
    shared_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM papers WHERE is_read=1")
    read_count = c.fetchone()[0]
    category_counts = {}
    for cat in VALID_CATEGORIES:
        safe_cat = cat.replace("'", "''")
        c.execute(
            f"SELECT COUNT(*) FROM papers WHERE EXISTS ("
            f"SELECT 1 FROM json_each(COALESCE(categories_json,'[]')) "
            f"WHERE value = '{safe_cat}')"
        )
        category_counts[cat] = c.fetchone()[0]
    return {
        "paper_count": paper_count,
        "comment_count": comment_count,
        "run_count": run_count,
        "total_likes": total_likes,
        "total_views": total_views,
        "favorite_count": fav_count,
        "disliked_count": disl_count,
        "shared_count": shared_count,
        "read_count": read_count,
        "unread_count": max(0, paper_count - read_count),
        "valid_categories": VALID_CATEGORIES,
        "category_counts": category_counts,
    }


SORT_OPTIONS = {
    "likes": ("likes DESC, first_seen ASC", "按点赞数降序"),
    "first_seen": ("first_seen DESC", "最新入库"),
    "last_updated": ("last_updated DESC", "最近更新"),
    "views": ("views DESC, first_seen ASC", "按浏览数降序"),
    "comments": ("comment_count DESC, first_seen ASC", "按评论数降序"),
    "pub_desc": ("published_date DESC", "发布时间降序"),
    "pub_asc": ("published_date ASC", "发布时间升序"),
}


def _parse_filters(query_dict):
    where_parts = []
    params = []
    fav = query_dict.get('filter_fav', [None])[0]
    disl = query_dict.get('filter_disliked', [None])[0]
    shared = query_dict.get('filter_shared', [None])[0]
    unread = query_dict.get('filter_unread', [None])[0]
    exclude_disl = query_dict.get('filter_exclude_disliked', [None])[0]
    cat_mode = query_dict.get('filter_cat_mode', ['any'])[0] or 'any'
    cat_raw = query_dict.get('filter_categories', [''])[0] or ''
    if fav == '1':
        where_parts.append("is_favorite=1")
    if disl == '1':
        where_parts.append("is_disliked=1")
    if shared == '1':
        where_parts.append("is_shared=1")
    if unread == '1':
        where_parts.append("is_read=0")
    if exclude_disl == '1':
        where_parts.append("(is_disliked IS NULL OR is_disliked=0)")
    cats = [c for c in cat_raw.split(',') if c in VALID_CATEGORIES] if cat_raw else []
    if cats:
        cat_where = []
        safe_cats = {}
        for c in cats:
            safe_c = c.replace("'", "''")
            safe_cats[c] = safe_c
            cat_where.append(f"json_each_cat.value = '{safe_c}'")
        if cat_mode == 'any':
            cat_clause = (
                f"EXISTS (SELECT 1 FROM json_each(COALESCE(categories_json,'[]')) AS json_each_cat "
                f"WHERE {' OR '.join(cat_where)})"
            )
        else:
            all_parts = []
            for c in cats:
                sc = safe_cats[c]
                all_parts.append(
                    f"EXISTS (SELECT 1 FROM json_each(COALESCE(categories_json,'[]')) AS json_each_cat WHERE json_each_cat.value = '{sc}')"
                )
            cat_clause = " AND ".join(all_parts)
        where_parts.append(f"({cat_clause})")
    return where_parts, params


def api_list_papers(conn, query_dict):
    c = conn.cursor()
    page = int(query_dict.get('page', ['0'])[0] or 0)
    size = int(query_dict.get('size', [str(PAGE_SIZE)])[0] or PAGE_SIZE)
    sort_key = (query_dict.get('sort', ['likes'])[0] or 'likes')
    order_by = SORT_OPTIONS.get(sort_key, SORT_OPTIONS['likes'])[0]
    search = (query_dict.get('search', [''])[0] or '').strip()

    where_parts = []
    params = []
    if search:
        like = f"%{search}%"
        where_parts.append(
            "(title_en LIKE ? OR title_zh LIKE ? OR abstract_en LIKE ? OR abstract_zh LIKE ? OR paper_id LIKE ?)"
        )
        params.extend([like, like, like, like, like])

    user_filters, user_params = _parse_filters(query_dict)
    where_parts.extend(user_filters)
    params.extend(user_params)

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    c.execute(f"SELECT COUNT(*) FROM papers {where_sql}", params)
    total = c.fetchone()[0]
    total_pages = max(1, (total + size - 1) // size)
    if page >= total_pages:
        page = total_pages - 1
    if page < 0:
        page = 0
    offset = page * size

    c.execute(f"""
        SELECT paper_id, url, title_en, title_zh, likes, views, comment_count,
               published_date, authors_json, first_seen, last_updated,
               is_read, is_favorite, is_disliked, is_shared, categories_json
        FROM papers {where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?
    """, params + [size, offset])
    rows = [row_to_dict(r) for r in c.fetchall()]
    for r in rows:
        r['authors_display'] = format_authors(r.get('authors_json'), 3)
        r['title_display'] = r.get('title_zh') or r.get('title_en') or r.get('paper_id') or ''
        r['categories'] = _parse_categories_json(r.pop('categories_json', None))

    cat_raw = query_dict.get('filter_categories', [''])[0] or ''
    cat_mode = query_dict.get('filter_cat_mode', ['any'])[0] or 'any'
    return {
        "papers": rows,
        "total": total,
        "page": page,
        "size": size,
        "total_pages": total_pages,
        "sort_options": {k: v[1] for k, v in SORT_OPTIONS.items()},
        "sort_key": sort_key,
        "search": search,
        "valid_categories": VALID_CATEGORIES,
        "filter_categories": [c for c in cat_raw.split(',') if c in VALID_CATEGORIES] if cat_raw else [],
        "filter_cat_mode": cat_mode,
    }


def api_get_paper(conn, paper_id):
    c = conn.cursor()
    c.execute("UPDATE papers SET is_read=1 WHERE paper_id=? AND (is_read IS NULL OR is_read=0)", (paper_id,))
    conn.commit()
    c.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,))
    row = c.fetchone()
    if not row:
        return None
    d = row_to_dict(row)
    d['authors_display'] = format_authors(d.get('authors_json'))
    d['categories'] = _parse_categories_json(d.pop('categories_json', None))
    d['valid_categories'] = VALID_CATEGORIES
    try:
        d['translated_fields_obj'] = json.loads(d.get('translated_fields') or '{}')
    except Exception:
        d['translated_fields_obj'] = {}
    return d


def api_mark_paper(conn, paper_id, payload):
    c = conn.cursor()
    c.execute("SELECT is_favorite, is_disliked, is_shared FROM papers WHERE paper_id=?", (paper_id,))
    row = c.fetchone()
    if not row:
        return None
    cur = row_to_dict(row)
    fav = payload.get('favorite')
    disl = payload.get('disliked')
    shared = payload.get('shared')

    if fav is not None:
        fav = bool(fav)
        if fav:
            cur['is_favorite'] = 1
            cur['is_disliked'] = 0
        else:
            cur['is_favorite'] = 0
    if disl is not None:
        disl = bool(disl)
        if disl:
            cur['is_disliked'] = 1
            cur['is_favorite'] = 0
        else:
            cur['is_disliked'] = 0
    if shared is not None:
        cur['is_shared'] = 1 if bool(shared) else 0

    c.execute(
        "UPDATE papers SET is_favorite=?, is_disliked=?, is_shared=? WHERE paper_id=?",
        (cur['is_favorite'], cur['is_disliked'], cur['is_shared'], paper_id)
    )
    conn.commit()
    return cur


def api_update_paper_categories(conn, paper_id, payload):
    c = conn.cursor()
    c.execute("SELECT 1 FROM papers WHERE paper_id=?", (paper_id,))
    if not c.fetchone():
        return None
    cats_raw = payload.get('categories')
    if not isinstance(cats_raw, list):
        cats_raw = []
    seen = set()
    cats = []
    for v in cats_raw:
        if isinstance(v, str) and v in VALID_CATEGORIES and v not in seen:
            seen.add(v)
            cats.append(v)
    c.execute(
        "UPDATE papers SET categories_json=? WHERE paper_id=?",
        (json.dumps(cats, ensure_ascii=False), paper_id)
    )
    conn.commit()
    return {'paper_id': paper_id, 'categories': cats, 'valid_categories': VALID_CATEGORIES}


def api_clear_ai_overview_zh(conn, paper_id):
    c = conn.cursor()
    c.execute("SELECT translated_fields FROM papers WHERE paper_id=?", (paper_id,))
    row = c.fetchone()
    if not row:
        return None
    try:
        translated = json.loads(row[0] or '{}') or {}
    except Exception:
        translated = {}
    translated.pop('ai_overview_zh', None)
    c.execute(
        "UPDATE papers SET ai_overview_zh=NULL, ai_overview_summary_zh=NULL, translated_fields=?, last_updated=? WHERE paper_id=?",
        (json.dumps(translated, ensure_ascii=False), datetime.now().isoformat(timespec='seconds'), paper_id)
    )
    conn.commit()
    return {'paper_id': paper_id, 'cleared': True}


def api_update_ai_overview_zh(conn, paper_id, payload):
    c = conn.cursor()
    c.execute("SELECT translated_fields FROM papers WHERE paper_id=?", (paper_id,))
    row = c.fetchone()
    if not row:
        return None
    new_content = payload.get('ai_overview_zh')
    if not isinstance(new_content, str):
        new_content = None
    if new_content is not None:
        new_content = new_content.strip()
        if not new_content:
            new_content = None
    try:
        translated = json.loads(row[0] or '{}') or {}
    except Exception:
        translated = {}
    if new_content:
        translated['ai_overview_zh'] = True
    else:
        translated.pop('ai_overview_zh', None)
    c.execute(
        "UPDATE papers SET ai_overview_zh=?, ai_overview_summary_zh=?, translated_fields=?, last_updated=? WHERE paper_id=?",
        (new_content, new_content, json.dumps(translated, ensure_ascii=False), datetime.now().isoformat(timespec='seconds'), paper_id)
    )
    conn.commit()
    return {'paper_id': paper_id, 'ai_overview_zh': new_content, 'updated': True}


def api_list_comments(conn, query_dict):
    c = conn.cursor()
    page = int(query_dict.get('page', ['0'])[0] or 0)
    size = int(query_dict.get('size', [str(PAGE_SIZE)])[0] or PAGE_SIZE)
    paper_id_filter = (query_dict.get('paper_id', [None])[0] or None)

    where_parts = []
    params = []
    if paper_id_filter:
        where_parts.append("c.paper_id=?")
        params.append(paper_id_filter)
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    order_by = "c.published_at ASC" if paper_id_filter else "c.published_at DESC"

    c.execute(f"SELECT COUNT(*) FROM comments c {where_sql}", params)
    total = c.fetchone()[0]
    total_pages = max(1, (total + size - 1) // size)
    if page >= total_pages:
        page = total_pages - 1
    if page < 0:
        page = 0
    offset = page * size

    c.execute(f"""
        SELECT c.id, c.paper_id, c.external_id, c.author_name,
               c.content_en, c.content_zh, c.published_at, c.is_updated,
               p.title_en, p.title_zh
        FROM comments c LEFT JOIN papers p ON c.paper_id = p.paper_id
        {where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?
    """, params + [size, offset])
    rows = [row_to_dict(r) for r in c.fetchall()]
    for r in rows:
        r['content_display'] = r.get('content_zh') or r.get('content_en') or ''
        r['paper_title'] = r.get('title_zh') or r.get('title_en') or r.get('paper_id') or ''
    return {
        "comments": rows,
        "total": total,
        "page": page,
        "size": size,
        "total_pages": total_pages,
        "paper_id": paper_id_filter,
    }


def api_get_comment(conn, cid):
    c = conn.cursor()
    c.execute("""
        SELECT c.*, p.title_en, p.title_zh
        FROM comments c LEFT JOIN papers p ON c.paper_id = p.paper_id
        WHERE c.id = ?
    """, (cid,))
    row = c.fetchone()
    if not row:
        return None
    d = row_to_dict(row)
    d['paper_title'] = d.get('title_zh') or d.get('title_en') or d.get('paper_id') or ''
    d['content_zh_display'] = d.get('content_zh') or ''
    d['content_en_display'] = d.get('content_en') or ''
    return d


def api_list_runs(conn, query_dict):
    c = conn.cursor()
    page = int(query_dict.get('page', ['0'])[0] or 0)
    size = int(query_dict.get('size', [str(PAGE_SIZE)])[0] or PAGE_SIZE)
    c.execute("SELECT COUNT(*) FROM runs")
    total = c.fetchone()[0]
    total_pages = max(1, (total + size - 1) // size)
    if page >= total_pages:
        page = total_pages - 1
    if page < 0:
        page = 0
    offset = page * size
    c.execute("""
        SELECT id, run_time, summary,
               json_array_length(new_papers_json) as new_papers,
               json_array_length(updated_papers_json) as updated_papers,
               json_array_length(new_comments_json) as new_comments
        FROM runs ORDER BY id DESC LIMIT ? OFFSET ?
    """, (size, offset))
    rows = [row_to_dict(r) for r in c.fetchall()]
    return {
        "runs": rows,
        "total": total,
        "page": page,
        "size": size,
        "total_pages": total_pages,
    }


def api_get_run(conn, run_id):
    c = conn.cursor()
    c.execute("SELECT * FROM runs WHERE id=?", (run_id,))
    row = c.fetchone()
    if not row:
        return None
    d = row_to_dict(row)
    for k in ('new_papers_json', 'updated_papers_json', 'new_comments_json',
              'ranking_before_json', 'ranking_after_json'):
        try:
            d[k.replace('_json', '_obj')] = json.loads(d.get(k) or '[]')
        except Exception:
            d[k.replace('_json', '_obj')] = []

    new_paper_details = []
    for pid in d.get('new_papers_obj', []):
        c.execute("SELECT paper_id, title_en, title_zh, likes FROM papers WHERE paper_id=?", (pid,))
        pr = c.fetchone()
        if pr:
            pd = row_to_dict(pr)
            pd['title_display'] = pd.get('title_zh') or pd.get('title_en') or pid
            new_paper_details.append(pd)
        else:
            new_paper_details.append({'paper_id': pid, 'title_display': '(已不在DB)'})
    d['new_papers_detail'] = new_paper_details

    rank_before = d.get('ranking_before_obj', [])
    rank_after = d.get('ranking_after_obj', [])
    pos_before = {r.get('paper_id'): i + 1 for i, r in enumerate(rank_before)}
    pos_after_map = {}
    for i, r in enumerate(rank_after[:20]):
        pid = r.get('paper_id', '')
        after_pos = i + 1
        before_pos = pos_before.get(pid)
        likes = r.get('likes', 0)
        title = r.get('title_zh') or r.get('title') or r.get('title_en') or pid
        if before_pos is None:
            delta_str = "NEW"
        else:
            delta = before_pos - after_pos
            if delta > 0:
                delta_str = f"+{delta}"
            elif delta < 0:
                delta_str = f"{delta}"
            else:
                delta_str = "="
        pos_after_map[pid] = {
            'pos': after_pos,
            'likes': likes,
            'title': title,
            'before_pos': before_pos or '',
            'delta': delta_str,
        }
    d['rank_diff_top20'] = pos_after_map
    return d


def api_get_schema(conn):
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in c.fetchall()]
    result = {'tables': {}, 'ddls': []}
    for t in tables:
        c.execute(f"PRAGMA table_info({t})")
        cols = []
        for col in c.fetchall():
            cols.append({
                'cid': col[0],
                'name': col[1],
                'type': col[2] or 'ANY',
                'notnull': bool(col[3]),
                'dflt_value': col[4],
                'pk': bool(col[5]),
            })
        c.execute(f"PRAGMA index_list({t})")
        indices = []
        for idx in c.fetchall():
            indices.append({
                'name': idx[1],
                'unique': bool(idx[2]),
            })
        result['tables'][t] = {'columns': cols, 'indices': indices}
    c.execute("SELECT sql FROM sqlite_master WHERE type IN ('table','index','view') ORDER BY name")
    result['ddls'] = [r[0] for r in c.fetchall() if r[0]]
    return result


# ============================================================
# Frontend HTML / CSS / JS
# ============================================================

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>PaperGrep Super DB Viewer</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; background: #f5f7fa; color: #1f2937; }
  header { background: linear-gradient(135deg, #1e3a5f, #2d5a8b); color: #fff; padding: 12px 24px; display: flex; align-items: center; gap: 24px; flex-wrap: wrap; position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 8px rgba(0,0,0,.15); }
  header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  header h1 .sub { font-size: 13px; opacity: .8; margin-left: 10px; font-weight: 400; }
  nav { display: flex; gap: 6px; flex-wrap: wrap; }
  nav button { background: rgba(255,255,255,.1); color: #fff; border: 1px solid rgba(255,255,255,.25); padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 14px; transition: all .15s; }
  nav button:hover { background: rgba(255,255,255,.22); }
  nav button.active { background: #fff; color: #1e3a5f; font-weight: 600; }
  main { padding: 20px 24px; max-width: 1400px; margin: 0 auto; }
  .card { background: #fff; border-radius: 10px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04); margin-bottom: 18px; }
  .card h2 { margin: 0 0 16px 0; font-size: 18px; color: #1e3a5f; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 14px; }
  .stat-box { background: linear-gradient(135deg, #f0f4ff, #e8edff); border-radius: 10px; padding: 16px; text-align: center; }
  .stat-box .num { font-size: 28px; font-weight: 700; color: #2563eb; }
  .stat-box .lbl { font-size: 13px; color: #64748b; margin-top: 4px; }
  .stat-box.alt { background: linear-gradient(135deg, #fef3c7, #fde68a); }
  .stat-box.alt .num { color: #b45309; }
  .stat-box.ok { background: linear-gradient(135deg, #d1fae5, #a7f3d0); }
  .stat-box.ok .num { color: #047857; }
  .stat-box.bad { background: linear-gradient(135deg, #fee2e2, #fecaca); }
  .stat-box.bad .num { color: #b91c1c; }
  .stat-box.purple { background: linear-gradient(135deg, #ede9fe, #ddd6fe); }
  .stat-box.purple .num { color: #6d28d9; }

  .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
  .toolbar input[type=text], .toolbar select { padding: 7px 11px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px; }
  .toolbar input[type=text] { flex: 1; min-width: 180px; }
  .toolbar button { padding: 7px 14px; border: 1px solid #d1d5db; background: #fff; border-radius: 6px; cursor: pointer; font-size: 14px; }
  .toolbar button:hover { background: #f3f4f6; }
  .toolbar .filter-check { display: inline-flex; align-items: center; gap: 4px; padding: 5px 10px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 13px; cursor: pointer; user-select: none; background: #fff; }
  .toolbar .filter-check input { margin: 0; }
  .toolbar .filter-check.on { background: #dbeafe; border-color: #60a5fa; }

  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  table th, table td { padding: 10px 8px; text-align: left; border-bottom: 1px solid #eef0f3; vertical-align: top; }
  table th { background: #f8fafc; color: #475569; font-weight: 600; position: sticky; top: 0; z-index: 5; }
  table tr:hover td { background: #f9fafb; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; margin-right: 4px; }
  .badge-read { background: #e0e7ff; color: #3730a3; }
  .badge-fav { background: #fef3c7; color: #92400e; }
  .badge-dislike { background: #fee2e2; color: #991b1b; }
  .badge-shared { background: #dcfce7; color: #166534; }
  .badge-unread { background: #fce7f3; color: #9d174d; }
  .badge-cat { background: #ede9fe; color: #5b21b6; cursor: pointer; }
  .badge-cat:hover { background: #ddd6fe; }
  .paper-title { font-weight: 500; color: #1e40af; cursor: pointer; }
  .paper-title:hover { text-decoration: underline; }
  .paper-title-en { font-size: 12px; color: #6b7280; margin-top: 2px; font-weight: 400; line-height: 1.4; }
  .paper-meta { font-size: 12px; color: #6b7280; margin-top: 3px; }
  .action-btn { padding: 3px 9px; border: 1px solid #d1d5db; border-radius: 5px; background: #fff; cursor: pointer; font-size: 12px; margin: 1px 2px; }
  .action-btn.on { background: #2563eb; color: #fff; border-color: #2563eb; }
  .action-btn.bad.on { background: #dc2626; border-color: #dc2626; }
  .action-btn.ok.on { background: #059669; border-color: #059669; }
  .action-btn.warn.on { background: #d97706; border-color: #d97706; }

  .pagination { display: flex; justify-content: center; align-items: center; gap: 6px; margin: 16px 0 4px; flex-wrap: wrap; }
  .pagination button, .pagination span { padding: 5px 11px; border-radius: 6px; border: 1px solid #d1d5db; background: #fff; cursor: pointer; font-size: 13px; }
  .pagination button:disabled { opacity: .4; cursor: not-allowed; }
  .pagination .cur { background: #2563eb; color: #fff; border-color: #2563eb; cursor: default; }

  .detail-section { margin-bottom: 18px; }
  .detail-section h3 { margin: 0 0 10px 0; font-size: 15px; color: #334155; padding: 6px 10px; background: #f1f5f9; border-radius: 6px; border-left: 4px solid #2563eb; }
  .kv-grid { display: grid; grid-template-columns: 140px 1fr; gap: 6px 14px; font-size: 14px; }
  .kv-grid .k { color: #64748b; font-weight: 500; }
  .kv-grid .v { word-break: break-word; }
  .content-block { padding: 12px 14px; background: #fafafa; border-radius: 6px; line-height: 1.7; font-size: 14px; white-space: pre-wrap; word-wrap: break-word; }
  .mark-bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 12px; background: linear-gradient(135deg, #eff6ff, #e0e7ff); border-radius: 8px; margin-bottom: 14px; }
  .mark-bar .lbl { font-weight: 600; color: #3730a3; }
  a.external { color: #2563eb; text-decoration: none; }
  a.external:hover { text-decoration: underline; }
  .taglist { display: flex; gap: 4px; flex-wrap: wrap; }
  .empty { text-align: center; padding: 40px; color: #94a3b8; }
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #e5e7eb; border-top-color: #2563eb; border-radius: 50%; animation: spin 0.6s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .comment-card { padding: 12px 14px; border: 1px solid #e5e7eb; border-radius: 8px; margin-bottom: 10px; background: #fff; }
  .comment-card .chead { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; font-size: 13px; }
  .comment-card .cauthor { font-weight: 600; color: #1e3a8a; }
  .comment-card .cdate { color: #94a3b8; font-size: 12px; }
  .schema-table font { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  pre.ddl { background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 8px; overflow: auto; font-size: 12px; line-height: 1.6; }
  .run-action-group { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
  .run-action-group button { padding: 5px 12px; border: 1px solid #d1d5db; background: #fff; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .run-action-group button.active { background: #2563eb; color: #fff; border-color: #2563eb; }
  .rank-table td.num { font-family: ui-monospace, monospace; }
  .delta-up { color: #059669; font-weight: 600; }
  .delta-down { color: #dc2626; font-weight: 600; }
  .delta-new { color: #2563eb; font-weight: 600; }
  .delta-same { color: #64748b; }
  .breadcrumbs { font-size: 13px; color: #64748b; margin-bottom: 12px; }
  .breadcrumbs a { color: #2563eb; cursor: pointer; }
  .breadcrumbs a:hover { text-decoration: underline; }
  @media (max-width: 720px) {
    header { padding: 10px; gap: 10px; }
    main { padding: 12px; }
    .kv-grid { grid-template-columns: 1fr; }
    table th, table td { padding: 6px 4px; font-size: 13px; }
  }
</style>
</head>
<body>
<header>
  <h1>📚 PaperGrep <span class="sub">Super DB Viewer</span></h1>
  <nav id="topnav">
    <button data-tab="dashboard" class="active">📊 概览</button>
    <button data-tab="papers">📄 论文</button>
    <button data-tab="comments">💬 评论</button>
    <button data-tab="runs">🏃 运行</button>
    <button data-tab="schema">🗂  Schema</button>
  </nav>
</header>
<main id="app"></main>
<script>
// ============================================================
// App state & core helpers
// ============================================================
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => (s == null ? '' : String(s)).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const state = {
  tab: 'dashboard',
  papers: {
    page: 0, sort: 'likes', search: '',
    filter_fav: 0, filter_disliked: 0, filter_shared: 0, filter_unread: 0, filter_exclude_disliked: 0,
    filter_categories: [], filter_cat_mode: 'any',
  },
  comments: { page: 0, paper_id: null },
  runs: { page: 0 },
  runDetail: { view: 'summary' },
  currentPaperId: null,
  currentCommentId: null,
  currentRunId: null,
  scrollPos: { papers: 0, comments: 0, runs: 0 },
};

async function apiGet(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
async function apiPost(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function init() {
  $$('#topnav button').forEach(b => {
    b.addEventListener('click', () => switchTab(b.dataset.tab));
  });
  loadTab();
}

function switchTab(t) {
  state.tab = t;
  state.currentPaperId = null;
  state.currentCommentId = null;
  state.currentRunId = null;
  $$('#topnav button').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  loadTab();
}

function loadTab() {
  const app = $('#app');
  app.innerHTML = `<div class="empty"><span class="spinner"></span> 加载中…</div>`;
  if (state.tab === 'dashboard') return renderDashboard();
  if (state.tab === 'papers') return state.currentPaperId ? renderPaperDetail(state.currentPaperId) : renderPapers();
  if (state.tab === 'comments') return state.currentCommentId ? renderCommentDetail(state.currentCommentId) : (state.comments.paper_id ? renderComments() : renderComments());
  if (state.tab === 'runs') return state.currentRunId ? renderRunDetail(state.currentRunId) : renderRuns();
  if (state.tab === 'schema') return renderSchema();
}

// ============================================================
// Dashboard
// ============================================================
async function renderDashboard() {
  const stats = await apiGet('/api/stats');
  const app = $('#app');
  const unreadPct = stats.paper_count ? Math.round((stats.read_count / stats.paper_count) * 100) : 0;
  const catCounts = stats.category_counts || {};
  const validCats = stats.valid_categories || [];
  const catStatBoxes = validCats.map(cat => `
    <div class="stat-box purple"><div class="num">${catCounts[cat]||0}</div><div class="lbl">🏷 ${esc(cat)}</div></div>
  `).join('');
  const catQuickBtns = validCats.length ? `
      <div style="margin-top:10px; display:flex; flex-wrap:wrap; gap:8px;">
        <span style="font-size:13px;color:#64748b;align-self:center;">类别快速过滤：</span>
        ${validCats.map(cat => `<button onclick="goPapers({filter_categories:['${esc(cat)}'], filter_cat_mode:'any'})">🏷 ${esc(cat)} (${catCounts[cat]||0})</button>`).join('')}
        ${validCats.length>1 ? `<button onclick="goPapers({filter_categories:${JSON.stringify(validCats)}, filter_cat_mode:'all'})">🎯 同时属于所有类别</button>` : ''}
      </div>
  ` : '';
  app.innerHTML = `
    <div class="card"><h2>📊 数据库总览</h2>
      <div class="stats-grid">
        <div class="stat-box"><div class="num">${stats.paper_count}</div><div class="lbl">论文总数</div></div>
        <div class="stat-box alt"><div class="num">${stats.comment_count}</div><div class="lbl">评论总数</div></div>
        <div class="stat-box purple"><div class="num">${stats.run_count}</div><div class="lbl">运行记录</div></div>
        <div class="stat-box"><div class="num">${(stats.total_likes/1000).toFixed(1)}K</div><div class="lbl">总点赞数</div></div>
        <div class="stat-box"><div class="num">${(stats.total_views/1000).toFixed(1)}K</div><div class="lbl">总浏览数</div></div>
        <div class="stat-box ok"><div class="num">${stats.favorite_count}</div><div class="lbl">⭐ 收藏</div></div>
        <div class="stat-box bad"><div class="num">${stats.disliked_count}</div><div class="lbl">👎 不喜欢</div></div>
        <div class="stat-box ok"><div class="num">${stats.shared_count}</div><div class="lbl">📤 已分享</div></div>
        <div class="stat-box alt"><div class="num">${stats.read_count}</div><div class="lbl">✅ 已读 (${unreadPct}%)</div></div>
        <div class="stat-box bad"><div class="num">${stats.unread_count}</div><div class="lbl">📖 未读</div></div>
        ${catStatBoxes}
      </div>
    </div>
    <div class="card"><h2>🚀 快速入口</h2>
      <div class="toolbar">
        <button onclick="goPapers({filter_unread:1})">📖 查看未读论文</button>
        <button onclick="goPapers({filter_fav:1})">⭐ 我的收藏</button>
        <button onclick="goPapers({filter_disliked:1})">👎 不喜欢</button>
        <button onclick="goPapers({filter_shared:1})">📤 已分享</button>
        <button onclick="goPapers({sort:'first_seen'})">🆕 最新入库</button>
        <button onclick="goPapers({sort:'likes'})">🔥 热门排序</button>
        ${catQuickBtns}
      </div>
    </div>
    <div class="card"><h2>📝 使用说明</h2>
      <ul style="line-height:1.9; color:#475569; font-size:14px;">
        <li>点击论文标题查看详情，<b>打开详情即自动标记为已读</b>。</li>
        <li>在论文详情或列表中可以快速标记：⭐收藏 / 👎不喜欢 / 📤已分享。收藏与不喜欢互斥。</li>
        <li>论文列表支持关键词搜索（匹配标题/摘要/paper_id），以及多种排序与筛选。</li>
        <li>运行记录中可以查看每次抓取新增/更新论文、评论以及点赞排名变化对比。</li>
        <li>类别筛选支持多选，并可切换「任一匹配 (any)」或「全部同时拥有 (all)」模式。</li>
      </ul>
    </div>
  `;
}
window.goPapers = (opts) => {
  Object.assign(state.papers, opts, { page: 0 });
  state.currentPaperId = null;
  state.scrollPos.papers = 0;
  switchTab('papers');
};
window.backToPapers = () => {
  state.currentPaperId = null;
  loadTab();
};
window.backToComments = () => {
  state.currentCommentId = null;
  loadTab();
};
window.backToRuns = () => {
  state.currentRunId = null;
  loadTab();
};

// ============================================================
// Paper list + detail
// ============================================================
function markBadges(p) {
  const parts = [];
  if (!p.is_read) parts.push(`<span class="badge badge-unread">未读</span>`);
  else parts.push(`<span class="badge badge-read">已读</span>`);
  if (p.is_favorite) parts.push(`<span class="badge badge-fav">⭐收藏</span>`);
  if (p.is_disliked) parts.push(`<span class="badge badge-dislike">👎不喜欢</span>`);
  if (p.is_shared) parts.push(`<span class="badge badge-shared">📤已分享</span>`);
  const cats = Array.isArray(p.categories) ? p.categories : [];
  for (const c of cats) {
    parts.push(`<span class="badge badge-cat" data-cat="${esc(c)}">🏷 ${esc(c)}</span>`);
  }
  return parts.join(' ');
}

function quickMarkButtons(p) {
  const favCls = 'action-btn warn ' + (p.is_favorite ? 'on' : '');
  const dislCls = 'action-btn bad ' + (p.is_disliked ? 'on' : '');
  const sharedCls = 'action-btn ok ' + (p.is_shared ? 'on' : '');
  return `
    <button class="${favCls}" data-pid="${p.paper_id}" data-mark="favorite" title="收藏 / 取消收藏">⭐</button>
    <button class="${dislCls}" data-pid="${p.paper_id}" data-mark="disliked" title="不喜欢 / 取消">👎</button>
    <button class="${sharedCls}" data-pid="${p.paper_id}" data-mark="shared" title="已分享 / 取消">📤</button>
  `;
}

async function renderPapers() {
  const app = $('#app');
  const catList = Array.isArray(state.papers.filter_categories) ? state.papers.filter_categories : [];
  const qp = new URLSearchParams({
    page: state.papers.page, size: ${PAGE_SIZE}, sort: state.papers.sort,
    search: state.papers.search,
    filter_fav: state.papers.filter_fav,
    filter_disliked: state.papers.filter_disliked,
    filter_shared: state.papers.filter_shared,
    filter_unread: state.papers.filter_unread,
    filter_exclude_disliked: state.papers.filter_exclude_disliked,
    filter_categories: catList.join(','),
    filter_cat_mode: state.papers.filter_cat_mode || 'any',
  });
  const data = await apiGet('/api/papers?' + qp);
  const sortOpts = data.sort_options || {};
  const validCats = data.valid_categories || [];
  const selCats = Array.isArray(data.filter_categories) ? data.filter_categories : [];
  const catMode = data.filter_cat_mode || 'any';
  const filterCrumbs = selCats.length ? ` · 类别[${catMode.toUpperCase()}]: ${selCats.join(' + ')}` : '';
  const breadcrumbs = `<div class="breadcrumbs">📄 论文列表 ${data.search ? '· 搜索: ' + esc(data.search) : ''} ${filterCrumbs} ${data.total > 0 ? `· 共 ${data.total} 篇` : ''}</div>`;
  const catCheckboxes = validCats.map(cat => {
    const on = selCats.includes(cat);
    return `<label class="filter-check ${on?'on':''}" title="类别筛选">
      <input type="checkbox" class="cat-chk" data-cat="${esc(cat)}" ${on?'checked':''}/> 🏷 ${esc(cat)}
    </label>`;
  }).join('');
  const catModeSel = `
    <select id="catModeSel" title="类别匹配模式">
      <option value="any" ${catMode==='any'?'selected':''}>类别: 任一匹配 (OR)</option>
      <option value="all" ${catMode==='all'?'selected':''}>类别: 全部同时拥有 (AND)</option>
    </select>
  `;
  const toolbar = `
    <div class="toolbar">
      <input type="text" id="searchInput" placeholder="🔍 搜索标题/摘要/ID ..." value="${esc(data.search)}" />
      <select id="sortSel">
        ${Object.entries(sortOpts).map(([k,v]) => `<option value="${k}" ${k===data.sort_key?'selected':''}>排序: ${esc(v)}</option>`).join('')}
      </select>
      <label class="filter-check ${state.papers.filter_unread?'on':''}">
        <input type="checkbox" id="fUnread" ${state.papers.filter_unread?'checked':''}/> 📖 未读
      </label>
      <label class="filter-check ${state.papers.filter_fav?'on':''}">
        <input type="checkbox" id="fFav" ${state.papers.filter_fav?'checked':''}/> ⭐ 收藏
      </label>
      <label class="filter-check ${state.papers.filter_disliked?'on':''}">
        <input type="checkbox" id="fDis" ${state.papers.filter_disliked?'checked':''}/> 👎 不喜欢
      </label>
      <label class="filter-check ${state.papers.filter_shared?'on':''}">
        <input type="checkbox" id="fSha" ${state.papers.filter_shared?'checked':''}/> 📤 已分享
      </label>
      <label class="filter-check ${state.papers.filter_exclude_disliked?'on':''}">
        <input type="checkbox" id="fExDis" ${state.papers.filter_exclude_disliked?'checked':''}/> 🚫 排除不喜欢
      </label>
      ${catModeSel}
      ${catCheckboxes}
      <button id="btnSearch">搜索</button>
      <button id="btnReset">重置</button>
    </div>
  `;
  let rowsHtml = '';
  if (!data.papers.length) {
    rowsHtml = `<div class="empty">暂无匹配的论文</div>`;
  } else {
    rowsHtml = `
      <table>
        <thead><tr>
          <th style="width:48px">#</th>
          <th style="width:120px">Paper ID</th>
          <th>标题 / 作者</th>
          <th style="width:100px">点赞</th>
          <th style="width:100px">评论</th>
          <th style="width:130px">发布日期</th>
          <th style="width:180px">标记 / 操作</th>
        </tr></thead><tbody>
        ${data.papers.map((p, i) => `
          <tr>
            <td>${data.page * data.size + i + 1}</td>
            <td><code style="font-size:12px">${esc(p.paper_id)}</code></td>
            <td>
              ${(p.title_zh && p.title_en && p.title_zh !== p.title_en)
                ? ('<div class="paper-title" data-pid="' + p.paper_id + '">' + esc(p.title_zh) + '</div><div class="paper-title-en">' + esc(p.title_en) + '</div>')
                : ('<div class="paper-title" data-pid="' + p.paper_id + '">' + esc(p.title_zh || p.title_en || p.paper_id) + '</div>')
              }
              <div class="paper-meta">${esc(p.authors_display || '')}</div>
              <div class="paper-meta" style="margin-top:4px;">${markBadges(p)}</div>
            </td>
            <td class="num">${p.likes || 0}</td>
            <td class="num">${p.comment_count || 0}</td>
            <td style="font-size:12px;color:#64748b">${esc((p.published_date||'').slice(0,10))}</td>
            <td>${quickMarkButtons(p)}</td>
          </tr>
        `).join('')}
        </tbody>
      </table>
    `;
  }
  const pager = paginate(data.page, data.total_pages, (p) => { state.papers.page = p; state.scrollPos.papers = 0; renderPapers(); });
  app.innerHTML = `<div class="card">${breadcrumbs}${toolbar}${rowsHtml}${pager}</div>`;

  $('#searchInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
  $('#btnSearch').addEventListener('click', doSearch);
  $('#btnReset').addEventListener('click', () => {
    state.papers = {
      page: 0, sort: 'likes', search: '',
      filter_fav: 0, filter_disliked: 0, filter_shared: 0, filter_unread: 0, filter_exclude_disliked: 0,
      filter_categories: [], filter_cat_mode: 'any',
    };
    state.scrollPos.papers = 0;
    renderPapers();
  });
  $('#sortSel').addEventListener('change', (e) => { state.papers.sort = e.target.value; state.papers.page = 0; state.scrollPos.papers = 0; renderPapers(); });
  for (const [id, key] of [['fUnread','filter_unread'],['fFav','filter_fav'],['fDis','filter_disliked'],['fSha','filter_shared'],['fExDis','filter_exclude_disliked']]) {
    $(`#${id}`).addEventListener('change', (e) => { state.papers[key] = e.target.checked ? 1 : 0; state.papers.page = 0; state.scrollPos.papers = 0; renderPapers(); });
  }
  if ($('#catModeSel')) {
    $('#catModeSel').addEventListener('change', (e) => { state.papers.filter_cat_mode = e.target.value; state.papers.page = 0; state.scrollPos.papers = 0; renderPapers(); });
  }
  $$('.cat-chk').forEach(el => el.addEventListener('change', (e) => {
    const cat = el.dataset.cat;
    const cur = new Set(state.papers.filter_categories || []);
    if (el.checked) cur.add(cat); else cur.delete(cat);
    state.papers.filter_categories = [...cur];
    state.papers.page = 0;
    state.scrollPos.papers = 0;
    renderPapers();
  }));
  $$('.badge-cat[data-cat]').forEach(el => el.addEventListener('click', (e) => {
    e.stopPropagation();
    const cat = el.dataset.cat;
    state.papers.filter_categories = [cat];
    state.papers.filter_cat_mode = 'any';
    state.papers.page = 0;
    state.scrollPos.papers = 0;
    renderPapers();
  }));
  $$('.paper-title').forEach(el => el.addEventListener('click', () => {
    state.scrollPos.papers = window.scrollY || document.documentElement.scrollTop || 0;
    state.currentPaperId = el.dataset.pid; loadTab();
  }));
  $$('.action-btn[data-mark]').forEach(el => el.addEventListener('click', async (ev) => {
    ev.stopPropagation();
    const pid = el.dataset.pid; const mark = el.dataset.mark;
    const curOn = el.classList.contains('on');
    const payload = {}; payload[mark] = !curOn;
    const res = await apiPost(`/api/paper/${encodeURIComponent(pid)}/mark`, payload);
    if (res) {
      const tr = el.closest('tr');
      tr.querySelectorAll('.action-btn.warn').forEach(b => b.classList.toggle('on', !!res.is_favorite));
      tr.querySelectorAll('.action-btn.bad').forEach(b => b.classList.toggle('on', !!res.is_disliked));
      tr.querySelectorAll('.action-btn.ok').forEach(b => b.classList.toggle('on', !!res.is_shared));
      const badges = tr.querySelector('.paper-meta + .paper-meta') || tr.querySelectorAll('.paper-meta')[1];
      if (badges) {
        const paper = data.papers.find(x => x.paper_id === pid);
        if (paper) {
          paper.is_favorite = res.is_favorite;
          paper.is_disliked = res.is_disliked;
          paper.is_shared = res.is_shared;
          badges.innerHTML = markBadges(paper);
        }
      }
    }
  }));
  const restoreScroll = state.scrollPos.papers > 0;
  if (restoreScroll) {
    const pos = state.scrollPos.papers;
    state.scrollPos.papers = 0;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => window.scrollTo({ top: pos, behavior: 'auto' }));
    });
  }
}
function doSearch() {
  state.papers.search = $('#searchInput').value.trim();
  state.papers.page = 0;
  state.scrollPos.papers = 0;
  renderPapers();
}

async function renderPaperDetail(pid) {
  const app = $('#app');
  app.innerHTML = `<div class="empty"><span class="spinner"></span> 加载论文详情…</div>`;
  const p = await apiGet(`/api/paper/${encodeURIComponent(pid)}`);
  if (!p) {
    app.innerHTML = `<div class="empty">未找到论文 ${esc(pid)}</div>`;
    return;
  }
  const tf = p.translated_fields_obj || {};
  const arxiv = `https://arxiv.org/abs/${p.paper_id}`;
  const alphaurl = p.url || `https://www.alphaxiv.org/abs/${p.paper_id}`;

  const crumbs = `<div class="breadcrumbs">
    <a onclick="window.backToPapers();">📄 返回论文列表</a>
    › 详情 · <code>${esc(p.paper_id)}</code>
  </div>`;

  const backBtn = `<div style="margin-bottom:14px;">
    <button class="action-btn on" style="background:#2563eb;color:#fff;border-color:#2563eb;padding:8px 18px;font-size:14px;" onclick="window.backToPapers();">
      ← 返回论文列表
    </button>
  </div>`;

  const markBar = `
    <div class="mark-bar">
      <span class="lbl">标记:</span>
      <button class="action-btn warn ${p.is_favorite?'on':''}" id="btnFav">⭐ 收藏 ${p.is_favorite?'(已标记)':''}</button>
      <button class="action-btn bad ${p.is_disliked?'on':''}" id="btnDis">👎 不喜欢 ${p.is_disliked?'(已标记)':''}</button>
      <button class="action-btn ok ${p.is_shared?'on':''}" id="btnSha">📤 已分享 ${p.is_shared?'(已标记)':''}</button>
      <span style="margin-left:auto;font-size:12px;color:#64748b;">
        ${p.is_read ? '<span class="badge badge-read">已读 ✓</span>' : '<span class="badge badge-unread">未读</span>'}
      </span>
    </div>
  `;

  const cats = Array.isArray(p.categories) ? p.categories : [];
  const catHtml = cats.length
    ? cats.map(c => `<span class="badge badge-cat" style="cursor:pointer;" onclick="window.goPapers({filter_categories:['${esc(c)}'], filter_cat_mode:'any'});">🏷 ${esc(c)}</span>`).join(' ')
    : '<span style="color:#94a3b8;">(未归类)</span>';

  const validCats = Array.isArray(p.valid_categories) ? p.valid_categories : [];
  const catEditSection = validCats.length ? `
    <div class="detail-section">
      <h3>🏷 类别管理</h3>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
        ${validCats.map(cat => {
          const on = cats.includes(cat);
          return `<label class="filter-check ${on?'on':''}">
            <input type="checkbox" class="cat-edit-chk" data-cat="${esc(cat)}" ${on?'checked':''}/> 🏷 ${esc(cat)}
          </label>`;
        }).join('')}
        <span style="font-size:12px;color:#64748b;margin-left:8px;">勾选 / 取消后自动保存</span>
      </div>
    </div>
  ` : '';

  const metaGrid = `
    <div class="kv-grid">
      <div class="k">Paper ID</div><div class="v"><code>${esc(p.paper_id)}</code></div>
      <div class="k">作者</div><div class="v">${esc(p.authors_display || 'N/A')}</div>
      <div class="k">类别</div><div class="v" id="catCell">${catHtml}</div>
      <div class="k">发布日期</div><div class="v">${esc(p.published_date || 'N/A')}</div>
      <div class="k">修改日期</div><div class="v">${esc(p.modified_date || 'N/A')}</div>
      <div class="k">首次入库</div><div class="v">${esc(p.first_seen || 'N/A')}</div>
      <div class="k">最后更新</div><div class="v">${esc(p.last_updated || 'N/A')}</div>
      <div class="k">点赞 / 浏览 / 评论</div><div class="v">👍 ${p.likes||0} · 👁 ${p.views||0} · 💬 ${p.comment_count||0}</div>
      <div class="k">alphaXiv 链接</div><div class="v"><a class="external" href="${esc(alphaurl)}" target="_blank">${esc(alphaurl)}</a></div>
      <div class="k">arXiv 链接</div><div class="v"><a class="external" href="${esc(arxiv)}" target="_blank">${esc(arxiv)}</a></div>
    </div>
  `;

  const titleSection = p.title_zh || p.title_en ? `
    <div class="detail-section"><h3>📌 标题</h3>
      ${p.title_zh ? `<div class="content-block" style="background:#fffbeb; border:1px solid #fde68a; font-size:16px; font-weight:500;">${esc(p.title_zh)}</div>` : ''}
      ${p.title_en ? `<div class="content-block" style="margin-top:8px;">${esc(p.title_en)}</div>` : ''}
    </div>
  ` : '';

  const abstractSection = (p.abstract_zh || p.abstract_en) ? `
    <div class="detail-section"><h3>📝 摘要</h3>
      ${p.abstract_zh ? `<h4 style="margin:6px 0 4px; font-size:13px; color:#64748b;">中文</h4><div class="content-block">${esc(p.abstract_zh)}</div>` : ''}
      ${p.abstract_en ? `<h4 style="margin:10px 0 4px; font-size:13px; color:#64748b;">English</h4><div class="content-block">${esc(p.abstract_en)}</div>` : ''}
    </div>
  ` : '';

  const zhOverviewExists = !!(p.ai_overview_zh && p.ai_overview_zh.trim());
  const overviewSection = `
    <div class="detail-section"><h3>🤖 AI 概述</h3>
      ${zhOverviewExists ? `
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px;">
          <h4 style="margin:0; font-size:13px; color:#64748b;">中文概述（约 ${p.ai_overview_zh.length} 字）</h4>
          <div style="margin-left:auto;display:flex;gap:6px;">
            <button class="action-btn bad" id="btnClearOverviewZh" title="清除当前中文概述，下次 PaperGrep --refill 时会重新生成">🗑 清除中文概述</button>
            <button class="action-btn ok" id="btnEditOverviewZh" title="手动修改/补充中文概述内容">✏️ 编辑中文概述</button>
          </div>
        </div>
        <div id="overviewZhView"><div class="content-block">${esc(p.ai_overview_zh)}</div></div>
        <div id="overviewZhEdit" style="display:none;">
          <textarea id="overviewZhTextarea" style="width:100%;min-height:240px;padding:12px 14px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;line-height:1.7;font-family:inherit;resize:vertical;">${esc(p.ai_overview_zh)}</textarea>
          <div style="display:flex;gap:8px;margin-top:8px;justify-content:flex-end;">
            <button class="action-btn" id="btnCancelEditOverviewZh">取消</button>
            <button class="action-btn ok on" id="btnSaveOverviewZh" style="background:#059669;color:#fff;border-color:#059669;">💾 保存修改</button>
          </div>
        </div>
      ` : `
        <div style="padding:12px 14px;background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;font-size:14px;color:#92400e;">
          ⚠️ 暂无中文 AI 概述。您可以：<br/>
          1) 运行 <code>PaperGrep.py --refill</code> 自动从 alphaxiv.org 获取信息并调用大模型生成<br/>
          2) 点击下方按钮手动输入/粘贴概述内容：
          <div style="margin-top:10px;">
            <button class="action-btn ok" id="btnEditOverviewZh" title="手动添加中文概述内容">✏️ 添加中文概述</button>
          </div>
        </div>
        <div id="overviewZhEdit" style="display:none;margin-top:10px;">
          <textarea id="overviewZhTextarea" style="width:100%;min-height:240px;padding:12px 14px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;line-height:1.7;font-family:inherit;resize:vertical;" placeholder="在此输入中文 AI 概述内容..."></textarea>
          <div style="display:flex;gap:8px;margin-top:8px;justify-content:flex-end;">
            <button class="action-btn" id="btnCancelEditOverviewZh">取消</button>
            <button class="action-btn ok on" id="btnSaveOverviewZh" style="background:#059669;color:#fff;border-color:#059669;">💾 保存</button>
          </div>
        </div>
      `}
      ${p.ai_overview_en ? `<h4 style="margin:10px 0 4px; font-size:13px; color:#64748b;">English Overview</h4><div class="content-block">${esc(p.ai_overview_en)}</div>` : ''}
    </div>
  `;

  const hashes = `
    <div class="kv-grid" style="font-size:12px;">
      <div class="k">title_hash</div><div class="v"><code>${esc(p.title_hash || '(空)')}</code></div>
      <div class="k">abstract_hash</div><div class="v"><code>${esc(p.abstract_hash || '(空)')}</code></div>
      <div class="k">ai_overview_hash</div><div class="v"><code>${esc(p.ai_overview_hash || '(空)')}</code></div>
    </div>
  `;
  const trans = `
    <div class="kv-grid" style="font-size:13px;">
      <div class="k">title_zh 已翻译</div><div class="v">${tf.title_zh ? '✅ 是' : '— 否'} ${p.title_zh ? '(当前有值)' : ''}</div>
      <div class="k">abstract_zh 已翻译</div><div class="v">${tf.abstract_zh ? '✅ 是' : '— 否'} ${p.abstract_zh ? '(当前有值)' : ''}</div>
      <div class="k">ai_overview_zh 已翻译</div><div class="v">${tf.ai_overview_zh ? '✅ 是' : '— 否'} ${p.ai_overview_zh ? '(当前有值)' : ''}</div>
    </div>
  `;

  app.innerHTML = `<div class="card">${crumbs}${backBtn}${markBar}
    ${titleSection}
    <div class="detail-section"><h3>🏷 元数据</h3>${metaGrid}</div>
    ${catEditSection}
    ${abstractSection}
    ${overviewSection}
    <div class="detail-section"><h3>🔗 哈希 / 翻译状态</h3>
      ${hashes}
      <div style="height:12px"></div>
      ${trans}
    </div>
    <div class="detail-section"><h3>💬 相关评论</h3>
      <button class="action-btn ok on" id="btnViewComments">查看该论文的评论 (${p.comment_count||0} 条) →</button>
    </div>
  </div>`;

  const refreshCatCell = () => {
    const cell = $('#catCell');
    if (!cell) return;
    cell.innerHTML = cats.length
      ? cats.map(c => `<span class="badge badge-cat" style="cursor:pointer;" onclick="window.goPapers({filter_categories:['${esc(c)}'], filter_cat_mode:'any'});">🏷 ${esc(c)}</span>`).join(' ')
      : '<span style="color:#94a3b8;">(未归类)</span>';
  };
  $$('.cat-edit-chk').forEach(el => el.addEventListener('change', async () => {
    const cat = el.dataset.cat;
    const cur = new Set(cats);
    if (el.checked) cur.add(cat); else cur.delete(cat);
    const newCats = [...cur];
    try {
      const res = await apiPost(`/api/paper/${encodeURIComponent(pid)}/categories`, { categories: newCats });
      if (res && Array.isArray(res.categories)) {
        cats.length = 0;
        cats.push(...res.categories);
        $$('.cat-edit-chk').forEach(c => {
          const on = cats.includes(c.dataset.cat);
          c.checked = on;
          const lbl = c.closest('.filter-check');
          if (lbl) lbl.classList.toggle('on', on);
        });
        refreshCatCell();
      }
    } catch (e) {
      // 还原勾选状态
      el.checked = !el.checked;
      const lbl = el.closest('.filter-check');
      if (lbl) lbl.classList.toggle('on', el.checked);
      alert('保存类别失败: ' + (e && e.message ? e.message : e));
    }
  }));

  const doMark = async (mark) => {
    const el = mark === 'favorite' ? $('#btnFav') : mark === 'disliked' ? $('#btnDis') : $('#btnSha');
    const cur = (mark === 'favorite') ? !!p.is_favorite : (mark === 'disliked') ? !!p.is_disliked : !!p.is_shared;
    const payload = {}; payload[mark] = !cur;
    const res = await apiPost(`/api/paper/${encodeURIComponent(pid)}/mark`, payload);
    if (res) {
      p.is_favorite = res.is_favorite; p.is_disliked = res.is_disliked; p.is_shared = res.is_shared;
      $('#btnFav').innerHTML = `⭐ 收藏 ${res.is_favorite?'(已标记)':''}`;
      $('#btnFav').classList.toggle('on', !!res.is_favorite);
      $('#btnDis').innerHTML = `👎 不喜欢 ${res.is_disliked?'(已标记)':''}`;
      $('#btnDis').classList.toggle('on', !!res.is_disliked);
      $('#btnSha').innerHTML = `📤 已分享 ${res.is_shared?'(已标记)':''}`;
      $('#btnSha').classList.toggle('on', !!res.is_shared);
    }
  };
  $('#btnFav').onclick = () => doMark('favorite');
  $('#btnDis').onclick = () => doMark('disliked');
  $('#btnSha').onclick = () => doMark('shared');
  $('#btnViewComments').onclick = () => {
    state.tab = 'comments';
    state.comments.paper_id = pid;
    state.comments.page = 0;
    state.currentCommentId = null;
    loadTab();
  };

  const enterEditMode = () => {
    const viewEl = document.getElementById('overviewZhView');
    const editEl = document.getElementById('overviewZhEdit');
    if (viewEl) viewEl.style.display = 'none';
    if (editEl) editEl.style.display = 'block';
    const ta = document.getElementById('overviewZhTextarea');
    if (ta) { ta.focus(); ta.scrollTop = 0; }
  };
  const exitEditMode = () => {
    const viewEl = document.getElementById('overviewZhView');
    const editEl = document.getElementById('overviewZhEdit');
    if (viewEl) viewEl.style.display = 'block';
    if (editEl) editEl.style.display = 'none';
  };
  const btnClear = document.getElementById('btnClearOverviewZh');
  if (btnClear) {
    btnClear.onclick = async () => {
      if (!confirm('确定要清除这篇论文的中文 AI 概述吗？清除后可通过 PaperGrep.py --refill 重新生成。')) return;
      try {
        btnClear.disabled = true;
        btnClear.textContent = '清除中…';
        const res = await apiPost(`/api/paper/${encodeURIComponent(pid)}/ai_overview_zh/clear`, {});
        if (res && res.cleared) {
          alert('✅ 中文 AI 概述已清除！下次运行 PaperGrep.py --refill 时会重新生成。');
          loadTab();
        }
      } catch (e) {
        alert('清除失败: ' + (e && e.message ? e.message : e));
        btnClear.disabled = false;
        btnClear.textContent = '🗑 清除中文概述';
      }
    };
  }
  const btnEdit = document.getElementById('btnEditOverviewZh');
  if (btnEdit) {
    btnEdit.onclick = enterEditMode;
  }
  const btnCancelEdit = document.getElementById('btnCancelEditOverviewZh');
  if (btnCancelEdit) {
    btnCancelEdit.onclick = exitEditMode;
  }
  const btnSaveEdit = document.getElementById('btnSaveOverviewZh');
  if (btnSaveEdit) {
    btnSaveEdit.onclick = async () => {
      const ta = document.getElementById('overviewZhTextarea');
      const val = ta ? ta.value : '';
      if (!val || !val.trim()) {
        if (!confirm('内容为空，保存会清除现有概述，是否继续？')) return;
      }
      try {
        btnSaveEdit.disabled = true;
        btnSaveEdit.textContent = '保存中…';
        const res = await apiPost(`/api/paper/${encodeURIComponent(pid)}/ai_overview_zh/update`, { ai_overview_zh: val });
        if (res && res.updated) {
          alert('✅ 保存成功！');
          loadTab();
        }
      } catch (e) {
        alert('保存失败: ' + (e && e.message ? e.message : e));
        btnSaveEdit.disabled = false;
        btnSaveEdit.textContent = zhOverviewExists ? '💾 保存修改' : '💾 保存';
      }
    };
  }
}

// ============================================================
// Comments list + detail
// ============================================================
async function renderComments() {
  const app = $('#app');
  const qp = new URLSearchParams({ page: state.comments.page, size: ${PAGE_SIZE} });
  if (state.comments.paper_id) qp.set('paper_id', state.comments.paper_id);
  const data = await apiGet('/api/comments?' + qp);
  const crumbs = `<div class="breadcrumbs">
    ${state.comments.paper_id
      ? `<a onclick="state.tab='papers'; state.currentPaperId='${esc(state.comments.paper_id)}'; loadTab();">⬅ 返回论文详情</a> › ` : ''
    }💬 评论列表 ${state.comments.paper_id ? `· 论文 ${esc(state.comments.paper_id)}` : ''} · 共 ${data.total} 条
  </div>`;
  const toolbar = state.comments.paper_id ? '' : `
    <div class="toolbar">
      <button onclick="state.comments.paper_id=null; state.comments.page=0; state.scrollPos.comments=0; renderComments();">全部评论</button>
    </div>
  `;
  let body = '';
  if (!data.comments.length) {
    body = `<div class="empty">暂无评论</div>`;
  } else {
    body = data.comments.map(c => `
      <div class="comment-card" data-cid="${c.id}">
        <div class="chead">
          <span>
            <span class="cauthor">${esc(c.author_name || '(匿名)')}</span>
            ${c.is_updated ? ' <span class="badge badge-shared">UPD</span>' : ''}
          </span>
          <span class="cdate">${esc((c.published_at||'').replace('T',' ').slice(0,19))}</span>
        </div>
        ${!state.comments.paper_id ? `<div class="paper-meta" style="margin-bottom:6px;">论文: <a class="external" data-gopaper="${c.paper_id}">${esc(c.paper_title || c.paper_id)}</a></div>` : ''}
        <div style="line-height:1.6; white-space:pre-wrap; word-wrap:break-word; font-size:14px;">${esc(c.content_display)}</div>
      </div>
    `).join('');
  }
  const pager = paginate(data.page, data.total_pages, (p) => { state.comments.page = p; state.scrollPos.comments = 0; renderComments(); });
  app.innerHTML = `<div class="card">${crumbs}${toolbar}${body}${pager}</div>`;
  $$('.comment-card').forEach(el => el.addEventListener('click', () => {
    state.scrollPos.comments = window.scrollY || document.documentElement.scrollTop || 0;
    state.currentCommentId = el.dataset.cid; loadTab();
  }));
  $$('[data-gopaper]').forEach(el => el.addEventListener('click', (e) => {
    e.stopPropagation();
    state.tab = 'papers'; state.currentPaperId = el.dataset.gopaper; loadTab();
  }));
  const restoreScroll = state.scrollPos.comments > 0;
  if (restoreScroll) {
    const pos = state.scrollPos.comments;
    state.scrollPos.comments = 0;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => window.scrollTo({ top: pos, behavior: 'auto' }));
    });
  }
}

async function renderCommentDetail(cid) {
  const app = $('#app');
  app.innerHTML = `<div class="empty"><span class="spinner"></span> 加载评论…</div>`;
  const c = await apiGet(`/api/comment/${cid}`);
  if (!c) { app.innerHTML = `<div class="empty">未找到评论 #${esc(cid)}</div>`; return; }
  const crumbs = `<div class="breadcrumbs">
    <a onclick="window.backToComments();">💬 返回评论列表</a> › #${c.id}
  </div>`;
  const backBtn = `<div style="margin-bottom:14px;">
    <button class="action-btn on" style="background:#2563eb;color:#fff;border-color:#2563eb;padding:8px 18px;font-size:14px;" onclick="window.backToComments();">
      ← 返回评论列表
    </button>
  </div>`;
  app.innerHTML = `<div class="card">${crumbs}${backBtn}
    <div class="detail-section"><h3>💬 评论详情</h3>
      <div class="kv-grid">
        <div class="k">评论 ID</div><div class="v">${c.id}</div>
        <div class="k">Paper ID</div><div class="v"><a class="external" data-gopaper="${esc(c.paper_id)}">${esc(c.paper_id)}</a> - ${esc(c.paper_title || '')}</div>
        <div class="k">外部 ID</div><div class="v">${esc(c.external_id || 'N/A')}</div>
        <div class="k">作者</div><div class="v">${esc(c.author_name || '(匿名)')}</div>
        <div class="k">发布时间</div><div class="v">${esc(c.published_at || 'N/A')}</div>
        <div class="k">已更新</div><div class="v">${c.is_updated ? '✅ 是' : '否'}</div>
        <div class="k">内容哈希</div><div class="v"><code>${esc(c.content_hash || 'N/A')}</code></div>
      </div>
    </div>
    ${c.content_zh_display ? `<div class="detail-section"><h3>🇨🇳 中文内容</h3><div class="content-block">${esc(c.content_zh_display)}</div></div>` : ''}
    ${c.content_en_display ? `<div class="detail-section"><h3>🇬🇧 英文内容</h3><div class="content-block">${esc(c.content_en_display)}</div></div>` : ''}
  </div>`;
  $$('[data-gopaper]').forEach(el => el.addEventListener('click', () => {
    state.tab = 'papers'; state.currentPaperId = el.dataset.gopaper; loadTab();
  }));
}

// ============================================================
// Runs list + detail
// ============================================================
async function renderRuns() {
  const app = $('#app');
  const qp = new URLSearchParams({ page: state.runs.page, size: ${PAGE_SIZE} });
  const data = await apiGet('/api/runs?' + qp);
  const crumbs = `<div class="breadcrumbs">🏃 运行记录 · 共 ${data.total} 条</div>`;
  let body = '';
  if (!data.runs.length) {
    body = `<div class="empty">暂无运行记录</div>`;
  } else {
    body = `<table>
      <thead><tr><th>#</th><th>ID</th><th>运行时间</th><th>新增论文</th><th>更新论文</th><th>新评论</th><th>摘要</th><th></th></tr></thead>
      <tbody>${data.runs.map((r,i) => `
        <tr>
          <td>${data.page*data.size + i + 1}</td>
          <td>#${r.id}</td>
          <td style="font-size:12px;">${esc((r.run_time||'').replace('T',' ').slice(0,19))}</td>
          <td class="num">${r.new_papers||0}</td>
          <td class="num">${r.updated_papers||0}</td>
          <td class="num">${r.new_comments||0}</td>
          <td style="max-width:360px;">${esc((r.summary||'').slice(0,90))}</td>
          <td><button class="action-btn ok on" data-rid="${r.id}">详情 →</button></td>
        </tr>
      `).join('')}</tbody>
    </table>`;
  }
  const pager = paginate(data.page, data.total_pages, (p) => { state.runs.page = p; state.scrollPos.runs = 0; renderRuns(); });
  app.innerHTML = `<div class="card">${crumbs}${body}${pager}</div>`;
  $$('[data-rid]').forEach(b => b.addEventListener('click', () => {
    state.scrollPos.runs = window.scrollY || document.documentElement.scrollTop || 0;
    state.currentRunId = b.dataset.rid; loadTab();
  }));
  const restoreScroll = state.scrollPos.runs > 0;
  if (restoreScroll) {
    const pos = state.scrollPos.runs;
    state.scrollPos.runs = 0;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => window.scrollTo({ top: pos, behavior: 'auto' }));
    });
  }
}

async function renderRunDetail(rid) {
  const app = $('#app');
  app.innerHTML = `<div class="empty"><span class="spinner"></span> 加载运行记录…</div>`;
  const r = await apiGet(`/api/run/${rid}`);
  if (!r) { app.innerHTML = `<div class="empty">未找到运行记录 #${esc(rid)}</div>`; return; }
  const crumbs = `<div class="breadcrumbs">
    <a onclick="window.backToRuns();">🏃 返回运行列表</a> › #${r.id}
  </div>`;
  const backBtn = `<div style="margin-bottom:14px;">
    <button class="action-btn on" style="background:#2563eb;color:#fff;border-color:#2563eb;padding:8px 18px;font-size:14px;" onclick="window.backToRuns();">
      ← 返回运行列表
    </button>
  </div>`;
  const view = state.runDetail.view || 'summary';
  const newPapersList = r.new_papers_detail && r.new_papers_detail.length ? r.new_papers_detail.map(p => `
    <div class="comment-card"><b style="color:#1e40af">${esc(p.paper_id)}</b> · 👍 ${p.likes||0} · ${esc(p.title_display || '')}</div>
  `).join('') : `<div class="empty">本次无新增论文</div>`;
  const updatedList = (r.updated_papers_obj && r.updated_papers_obj.length) ? r.updated_papers_obj.map(u => {
    const pid = u.paper_id || '?';
    const title = u.title_zh || u.title_en || u.title || '';
    const changes = (u.changes || []).map(ch => {
      let s = ch.field || '?';
      if ('before' in ch && 'after' in ch) s += `: ${String(ch.before||'').slice(0,18)} → ${String(ch.after||'').slice(0,18)}`;
      else if ('before_len' in ch) s += ` (${ch.before_len||0}→${ch.after_len||0} chars)`;
      return s;
    }).join(' · ') || '(无细节)';
    return `<div class="comment-card"><b>${esc(pid)}</b> ${esc(title)}<div style="font-size:12px;color:#64748b;margin-top:4px;">变更: ${esc(changes)}</div></div>`;
  }).join('') : `<div class="empty">本次无更新论文</div>`;
  const commentList = (r.new_comments_obj && r.new_comments_obj.length) ? r.new_comments_obj.slice(0, 200).map((nc,i) => {
    const pid = nc.paper_id || '?';
    const author = nc.author || '';
    const content = nc.content_zh || nc.content || '';
    const flag = nc.updated ? ' <span class="badge badge-shared">UPD</span>' : '';
    return `<div class="comment-card">#${i+1}${flag} <b>${esc(pid)}</b> · ${esc(author)}<div style="font-size:13px;margin-top:4px;">${esc(String(content).slice(0,300))}</div></div>`;
  }).join('') + (r.new_comments_obj.length > 200 ? `<div class="empty">... 还有 ${r.new_comments_obj.length - 200} 条省略</div>` : '') : `<div class="empty">本次无新评论</div>`;

  let rankHtml = '';
  if (r.rank_diff_top20 && Object.keys(r.rank_diff_top20).length) {
    const entries = Object.values(r.rank_diff_top20).sort((a,b) => a.pos - b.pos);
    const deltaCls = (d) => d === 'NEW' ? 'delta-new' : d.startsWith('+') ? 'delta-up' : d.startsWith('-') ? 'delta-down' : 'delta-same';
    rankHtml = `<table class="rank-table"><thead>
      <tr><th>#</th><th>Paper ID</th><th>标题</th><th class="num">点赞</th><th class="num">之前</th><th class="num">变化</th></tr>
    </thead><tbody>${entries.map(e => `
      <tr>
        <td class="num">${e.pos}</td>
        <td><code>${esc(Object.keys(r.rank_diff_top20).find(k => r.rank_diff_top20[k] === e) || '')}</code></td>
        <td>${esc(e.title || '').slice(0, 60)}</td>
        <td class="num">${e.likes||0}</td>
        <td class="num">${esc(String(e.before_pos || '-'))}</td>
        <td class="num"><span class="${deltaCls(e.delta)}">${esc(e.delta)}</span></td>
      </tr>
    `).join('')}</tbody></table>`;
  } else rankHtml = `<div class="empty">无排名对比数据</div>`;

  const bodyMap = {
    summary: `
      <div class="kv-grid">
        <div class="k">运行时间</div><div class="v">${esc(r.run_time || 'N/A')}</div>
        <div class="k">新增论文</div><div class="v">${r.new_papers_obj ? r.new_papers_obj.length : 0} 篇</div>
        <div class="k">更新论文</div><div class="v">${r.updated_papers_obj ? r.updated_papers_obj.length : 0} 篇</div>
        <div class="k">新评论</div><div class="v">${r.new_comments_obj ? r.new_comments_obj.length : 0} 条</div>
        <div class="k">排名对比</div><div class="v">${r.ranking_before_obj ? r.ranking_before_obj.length : 0} → ${r.ranking_after_obj ? r.ranking_after_obj.length : 0}</div>
      </div>
      ${r.summary ? `<div class="detail-section"><h3>📋 运行摘要</h3><div class="content-block">${esc(r.summary)}</div></div>` : ''}
    `,
    new_papers: newPapersList,
    updated: updatedList,
    comments: commentList,
    rank: rankHtml,
  };

  app.innerHTML = `<div class="card">${crumbs}${backBtn}
    <div class="detail-section"><h3>🏃 运行记录 #${r.id}</h3>
      <div class="run-action-group">
        <button class="${view==='summary'?'active':''}" data-view="summary">📋 摘要</button>
        <button class="${view==='new_papers'?'active':''}" data-view="new_papers">🆕 新增论文 (${r.new_papers_obj ? r.new_papers_obj.length : 0})</button>
        <button class="${view==='updated'?'active':''}" data-view="updated">🔄 更新论文 (${r.updated_papers_obj ? r.updated_papers_obj.length : 0})</button>
        <button class="${view==='comments'?'active':''}" data-view="comments">💬 新评论 (${r.new_comments_obj ? r.new_comments_obj.length : 0})</button>
        <button class="${view==='rank'?'active':''}" data-view="rank">📊 排名变化 TOP20</button>
      </div>
      ${bodyMap[view] || bodyMap.summary}
    </div>
  </div>`;
  $$('[data-view]').forEach(b => b.addEventListener('click', () => {
    state.runDetail.view = b.dataset.view; renderRunDetail(rid);
  }));
}

// ============================================================
// Schema view
// ============================================================
async function renderSchema() {
  const app = $('#app');
  const data = await apiGet('/api/schema');
  const tables = Object.keys(data.tables).sort();
  const tabsHtml = tables.map(t => {
    const cols = data.tables[t].columns;
    const colsHtml = `<table style="font-size:13px;">
      <thead><tr>
        <th>字段名</th><th>类型</th><th>PK</th><th>NOT NULL</th><th>默认值</th>
      </tr></thead><tbody>${cols.map(c => `
        <tr>
          <td><code>${esc(c.name)}</code></td>
          <td>${esc(c.type)}</td>
          <td style="text-align:center;">${c.pk ? '✓' : ''}</td>
          <td style="text-align:center;">${c.notnull ? '✓' : ''}</td>
          <td><code style="font-size:12px;">${esc(c.dflt_value == null ? '' : String(c.dflt_value))}</code></td>
        </tr>
      `).join('')}</tbody>
    </table>`;
    const indicesHtml = data.tables[t].indices && data.tables[t].indices.length
      ? `<h4 style="margin:12px 0 6px; font-size:14px;">索引</h4><ul style="font-size:13px;">${data.tables[t].indices.map(i => `<li><code>${esc(i.name)}</code>${i.unique ? ' <span class="badge badge-shared">UNIQUE</span>' : ''}</li>`).join('')}</ul>`
      : '';
    return `<div class="detail-section"><h3>🗂  表: ${esc(t)}</h3>${colsHtml}${indicesHtml}</div>`;
  }).join('');
  const ddlHtml = `<div class="detail-section"><h3>🧾 原始 DDL</h3><pre class="ddl">${esc(data.ddls.join('\n\n'))}</pre></div>`;
  app.innerHTML = `<div class="card">
    <div class="breadcrumbs">🗂 数据库 Schema · 共 ${tables.length} 张表</div>
    ${tabsHtml}
    ${ddlHtml}
  </div>`;
}

// ============================================================
// Pagination helper
// ============================================================
function paginate(page, totalPages, onChange) {
  if (totalPages <= 1) return '';
  const pageSlots = [];
  const add = (p, label, active, disabled) => {
    if (p !== null) pageSlots.push({ p, label, active, disabled });
  };
  add(0, '«', false, page === 0);
  add(page - 1, '‹', false, page === 0);
  const windowSize = 5;
  const half = Math.floor(windowSize / 2);
  let start = Math.max(0, page - half);
  let end = Math.min(totalPages - 1, start + windowSize - 1);
  start = Math.max(0, end - windowSize + 1);
  for (let p = start; p <= end; p++) add(p, String(p + 1), p === page, false);
  add(page + 1, '›', false, page === totalPages - 1);
  add(totalPages - 1, '»', false, page === totalPages - 1);
  window.__pgCb = window.__pgCb || {};
  const cbId = 'cb' + Math.random().toString(36).slice(2, 8);
  window.__pgCb[cbId] = onChange;
  return `<div class="pagination">
    ${pageSlots.map(({p, label, active, disabled}) => active
      ? `<span class="cur">${label}</span>`
      : `<button ${disabled?'disabled':''} onclick="window.__pgCb['${cbId}'](${p})">${label}</button>`
    ).join('')}
    <span style="border:none;background:transparent;">第 ${page+1} / ${totalPages} 页</span>
  </div>`;
}

init();
</script>
</body>
</html>
""".replace('${PAGE_SIZE}', str(PAGE_SIZE))


# ============================================================
# HTTP Handler
# ============================================================

class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB_PATH

    def log_message(self, format, *args):
        # quieter
        return

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, code=200):
        body = html.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_notfound(self):
        self._send_html("<h1>404 Not Found</h1>", 404)

    def _read_body_json(self):
        length = int(self.headers.get('Content-Length', '0') or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            return {}

    def _parse_query(self):
        parts = self.path.split('?', 1)
        qs = parts[1] if len(parts) > 1 else ''
        return urllib.parse.parse_qs(qs, keep_blank_values=True)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        query = self._parse_query()
        conn = get_db(self.db_path)
        try:
            if path == '/' or path == '/index.html':
                return self._send_html(INDEX_HTML)
            if path.startswith('/api/'):
                if path == '/api/stats':
                    return self._send_json(api_get_stats(conn))
                if path == '/api/papers':
                    return self._send_json(api_list_papers(conn, query))
                if path == '/api/comments':
                    return self._send_json(api_list_comments(conn, query))
                if path == '/api/runs':
                    return self._send_json(api_list_runs(conn, query))
                if path == '/api/schema':
                    return self._send_json(api_get_schema(conn))
                m = re.match(r'^/api/paper/([^/]+)$', path)
                if m:
                    pid = urllib.parse.unquote(m.group(1))
                    res = api_get_paper(conn, pid)
                    if res is None:
                        return self._send_json({'error': 'not found'}, 404)
                    return self._send_json(res)
                m = re.match(r'^/api/comment/(\d+)$', path)
                if m:
                    cid = int(m.group(1))
                    res = api_get_comment(conn, cid)
                    if res is None:
                        return self._send_json({'error': 'not found'}, 404)
                    return self._send_json(res)
                m = re.match(r'^/api/run/(\d+)$', path)
                if m:
                    rid = int(m.group(1))
                    res = api_get_run(conn, rid)
                    if res is None:
                        return self._send_json({'error': 'not found'}, 404)
                    return self._send_json(res)
                return self._send_json({'error': 'unknown api'}, 404)
            return self._send_notfound()
        finally:
            conn.close()

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        payload = self._read_body_json()
        conn = get_db(self.db_path)
        try:
            m = re.match(r'^/api/paper/([^/]+)/mark$', path)
            if m:
                pid = urllib.parse.unquote(m.group(1))
                res = api_mark_paper(conn, pid, payload)
                if res is None:
                    return self._send_json({'error': 'not found'}, 404)
                return self._send_json(res)
            m = re.match(r'^/api/paper/([^/]+)/categories$', path)
            if m:
                pid = urllib.parse.unquote(m.group(1))
                res = api_update_paper_categories(conn, pid, payload)
                if res is None:
                    return self._send_json({'error': 'not found'}, 404)
                return self._send_json(res)
            m = re.match(r'^/api/paper/([^/]+)/ai_overview_zh/clear$', path)
            if m:
                pid = urllib.parse.unquote(m.group(1))
                res = api_clear_ai_overview_zh(conn, pid)
                if res is None:
                    return self._send_json({'error': 'not found'}, 404)
                return self._send_json(res)
            m = re.match(r'^/api/paper/([^/]+)/ai_overview_zh/update$', path)
            if m:
                pid = urllib.parse.unquote(m.group(1))
                res = api_update_ai_overview_zh(conn, pid, payload)
                if res is None:
                    return self._send_json({'error': 'not found'}, 404)
                return self._send_json(res)
            return self._send_json({'error': 'unknown api'}, 404)
        finally:
            conn.close()


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="PaperGrep Super DB Viewer (Web UI at 127.0.0.1:8888)",
    )
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH,
                        help=f"数据库文件路径 (默认: {DEFAULT_DB_PATH})")
    parser.add_argument("--host", type=str, default="127.0.0.1",
                        help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8888,
                        help="监听端口 (默认 8888)")
    parser.add_argument("--no-browser", action="store_true",
                        help="启动后不自动打开浏览器")
    args = parser.parse_args()

    db_path = args.db
    if not os.path.exists(db_path):
        print(f"[WARN] 数据库文件不存在: {db_path}")
        print(f"       仍将启动服务器，访问时会自动创建。")
    try:
        conn = get_db(db_path)
        stats = api_get_stats(conn)
        conn.close()
    except Exception as e:
        print(f"[ERROR] 数据库连接/初始化失败: {e}")
        sys.exit(1)

    Handler.db_path = db_path
    max_attempts = 10
    server = None
    used_port = args.port
    last_err = None
    for offset in range(max_attempts):
        test_port = args.port + offset
        try:
            server = HTTPServer((args.host, test_port), Handler)
            used_port = test_port
            break
        except OSError as e:
            last_err = e
            if e.errno == 48 or 'Address already in use' in str(e):
                if offset < max_attempts - 1:
                    print(f"[WARN] 端口 {test_port} 已被占用，尝试 {test_port + 1} ...")
                continue
            raise
    if server is None:
        print(f"[ERROR] 启动失败：从端口 {args.port} 开始连续 {max_attempts} 个端口均被占用。")
        print(f"        最后一个错误: {last_err}")
        print(f"        请先 kill 占用端口的进程 (如: lsof -nP -iTCP:{args.port} -sTCP:LISTEN  然后 kill -9 <PID>)")
        print(f"        或使用 --port <端口号> 手动指定其它端口。")
        sys.exit(2)
    port_used_note = '' if used_port == args.port else f' (原 {args.port} 被占用，已自动使用 {used_port})'
    url = f"http://{args.host}:{used_port}/"
    pid = os.getpid()
    print()
    print(f"  ╔══════════════════════════════════════════════════════╗")
    print(f"  ║  🚀  PaperGrep Super DB Viewer 已启动{port_used_note:<26s}║")
    print(f"  ║  🌐  浏览器访问: {url:<43s} ║")
    print(f"  ║  🖥  IP: {args.host:<17s}  端口: {used_port:<6d}  PID: {pid:<7d}       ║")
    print(f"  ║  💾  数据库: {os.path.abspath(db_path):<50s} ║")
    print(f"  ║  📊  论文: {stats['paper_count']:<6d}  评论: {stats['comment_count']:<6d}  运行: {stats['run_count']:<5d}    ║")
    print(f"  ║  ⏹  按 Ctrl+C 停止服务器                             ║")
    print(f"  ╚══════════════════════════════════════════════════════╝")
    print()

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  用户中断，停止服务器。再见!")
        server.server_close()


if __name__ == "__main__":
    main()
