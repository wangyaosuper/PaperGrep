#!/usr/bin/env python3

import sys
import os
import re
import json
import sqlite3
import hashlib
import plistlib
import argparse
from datetime import datetime, timedelta, timezone
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from dashscope import Generation
import dashscope
from openai import OpenAI


def _create_retry_session(retries=5, backoff_factor=1, status_forcelist=(500, 502, 503, 504), timeout=30):
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session._timeout = timeout
    return session


_retry_session = _create_retry_session()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "db")
WORK_DIR = os.path.join(BASE_DIR, "work")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
TRASH_DIR = os.path.join(BASE_DIR, "trash")
DB_PATH = os.path.join(DB_DIR, "papergrep.db")


def ensure_dirs():
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(TRASH_DIR, exist_ok=True)


# ============================================================
# Database
# ============================================================

def get_db():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS papers (
        paper_id TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        title_en TEXT,
        title_zh TEXT,
        abstract_en TEXT,
        abstract_zh TEXT,
        ai_overview_en TEXT,
        ai_overview_zh TEXT,
        ai_overview_summary_zh TEXT,
        authors_json TEXT,
        published_date TEXT,
        modified_date TEXT,
        likes INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0,
        comment_count INTEGER DEFAULT 0,
        title_hash TEXT,
        abstract_hash TEXT,
        ai_overview_hash TEXT,
        translated_fields TEXT DEFAULT '{}',
        first_seen TEXT,
        last_updated TEXT,
        is_read INTEGER DEFAULT 0,
        is_favorite INTEGER DEFAULT 0,
        is_disliked INTEGER DEFAULT 0,
        is_shared INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_id TEXT NOT NULL,
        external_id TEXT,
        author_name TEXT,
        content_en TEXT,
        content_zh TEXT,
        published_at TEXT,
        content_hash TEXT,
        is_updated INTEGER DEFAULT 0,
        FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE,
        UNIQUE(paper_id, external_id)
    );

    CREATE INDEX IF NOT EXISTS idx_comments_paper ON comments(paper_id);

    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_time TEXT NOT NULL,
        new_papers_json TEXT DEFAULT '[]',
        updated_papers_json TEXT DEFAULT '[]',
        new_comments_json TEXT DEFAULT '[]',
        ranking_before_json TEXT DEFAULT '[]',
        ranking_after_json TEXT DEFAULT '[]',
        summary TEXT
    );
    """)
    # Migrate: add ai_overview_summary_zh column if not exists
    try:
        c.execute("ALTER TABLE papers ADD COLUMN ai_overview_summary_zh TEXT")
        conn.commit()
    except Exception:
        pass
    # Migrate: add user marking columns
    for col, dflt in [
        ("is_read", "0"),
        ("is_favorite", "0"),
        ("is_disliked", "0"),
        ("is_shared", "0"),
    ]:
        try:
            c.execute(f"ALTER TABLE papers ADD COLUMN {col} INTEGER DEFAULT {dflt}")
            conn.commit()
        except Exception:
            pass
    conn.commit()
    return conn


# ============================================================
# WebArchive / HTML Parsing
# ============================================================

def extract_html_from_webarchive(webarchive_path):
    """Extract main HTML content from a .webarchive file."""
    try:
        with open(webarchive_path, 'rb') as f:
            plist = plistlib.load(f)
        if 'WebMainResource' in plist and 'WebResourceData' in plist['WebMainResource']:
            data = plist['WebMainResource']['WebResourceData']
            if isinstance(data, bytes) and data[:8] == b'bplist00':
                try:
                    nested = plistlib.loads(data)
                    if isinstance(nested, dict) and '$objects' in nested:
                        for obj in nested['$objects']:
                            if isinstance(obj, str) and len(obj) > 500 and ('<html' in obj.lower() or '<!doctype' in obj.lower()):
                                return obj
                            elif isinstance(obj, (bytes, bytearray)):
                                for enc in ('utf-8', 'latin-1'):
                                    try:
                                        txt = obj.decode(enc)
                                        if '<html' in txt.lower() or '<!doctype' in txt.lower():
                                            return txt
                                    except Exception:
                                        pass
                except Exception:
                    pass
            for enc in ('utf-8', 'latin-1', 'utf-16'):
                try:
                    html = data.decode(enc)
                    if '<' in html:
                        return html
                except Exception:
                    pass
    except Exception as e:
        print(f"[ERROR] Failed to parse webarchive {webarchive_path}: {e}")
    return ""


def extract_jsonld_papers(html_content):
    """Extract papers from the JSON-LD <script> tag in alphaXiv HTML."""
    papers = []
    soup = BeautifulSoup(html_content, 'html.parser')
    script_tags = soup.find_all('script', type='application/ld+json')
    for tag in script_tags:
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        raw = raw.strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = []
        if isinstance(data, dict) and '@graph' in data:
            items = data['@graph']
        elif isinstance(data, list):
            items = data
        for node in items:
            if not isinstance(node, dict):
                continue
            if node.get('@type') == 'ItemList':
                for elem in node.get('itemListElement', []) or []:
                    if isinstance(elem, dict) and elem.get('@type') == 'ListItem':
                        item = elem.get('item')
                        if isinstance(item, dict) and item.get('@type') == 'Article':
                            p = _parse_article(item, elem.get('position'))
                            if p:
                                papers.append(p)
            elif node.get('@type') == 'Article':
                p = _parse_article(node)
                if p:
                    papers.append(p)
    seen = set()
    deduped = []
    for p in papers:
        if p['paper_id'] in seen:
            continue
        seen.add(p['paper_id'])
        deduped.append(p)
    return deduped


def _parse_k_number(s):
    """Parse '1.2K' '3M' '123' etc. to int, return None if unparseable."""
    if s is None:
        return None
    s = str(s).strip().replace(',', '')
    if not s:
        return None
    try:
        mult = 1
        if s[-1].lower() == 'k':
            mult = 1000
            s = s[:-1]
        elif s[-1].lower() == 'm':
            mult = 1_000_000
            s = s[:-1]
        return int(float(s) * mult)
    except Exception:
        return None


def extract_dom_papers(html_content):
    """Extract papers directly from the rendered HTML card DOM (alphaXiv uses tailwind cards).
    This catches papers that are visually rendered but not included in the limited JSON-LD ItemList."""
    papers = []
    soup = BeautifulSoup(html_content, 'html.parser')
    card_sel = (
        'div.relative.rounded-xl.cursor-pointer.px-4.py-3.backdrop-blur-sm'
    )
    cards = soup.select(card_sel)
    if len(cards) < 20:
        loose = []
        seen_card_ids = set()
        for a in soup.select('a[href*="/abs/"]'):
            href = a.get('href', '')
            m = re.search(r'/abs/(\d{4}\.\d{4,5}(?:v\d+)?)', href)
            if not m:
                continue
            p = a.parent
            found = None
            for _ in range(6):
                if p is None:
                    break
                cls = ' '.join(p.get('class', [])) if isinstance(p.get('class'), list) else str(p.get('class', ''))
                if 'rounded-xl' in cls and 'px-4' in cls and 'py-3' in cls:
                    found = p
                    break
                p = p.parent
            if found and id(found) not in seen_card_ids:
                seen_card_ids.add(id(found))
                loose.append(found)
        cards = loose

    for idx, card in enumerate(cards, 1):
        paper_id = None
        url = ''
        a_tag = card.select_one('a[href*="/abs/"]')
        if a_tag:
            href = a_tag.get('href', '')
            m = re.search(r'/abs/(\d{4}\.\d{4,5}(?:v\d+)?)', href)
            if m:
                paper_id = m.group(1)
                url = 'https://www.alphaxiv.org' + href if href.startswith('/') else href
            else:
                m2 = re.search(r'(\d{4}\.\d{4,5})', href)
                if m2:
                    paper_id = m2.group(1)
                    url = 'https://www.alphaxiv.org' + href if href.startswith('/') else href
        if not paper_id:
            continue

        title_el = card.select_one('h2.font-title')
        title = ''
        if title_el:
            title = title_el.get_text(strip=True)
        if not title:
            for h in card.select('h1,h2,h3,h4,a[href*="/abs/"]'):
                t = h.get_text(strip=True)
                if 10 <= len(t) <= 300:
                    title = t
                    break

        date_str = ''
        date_el = card.select_one('span.text-sm.font-medium.whitespace-nowrap')
        if date_el:
            dt_txt = date_el.get_text(strip=True)
            m = re.search(r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})', dt_txt, re.I)
            if m:
                try:
                    from datetime import datetime as _dt
                    d = _dt.strptime(m.group(0), '%d %b %Y')
                    date_str = d.strftime('%Y-%m-%d')
                except Exception:
                    date_str = m.group(0)

        authors = []
        auth_spans = card.select('div.flex.items-center.gap-4 span.font-normal')
        for s in auth_spans:
            nm = s.get_text(strip=True)
            if nm and 2 <= len(nm) <= 80:
                authors.append(nm)
        if not authors:
            for line in list(card.stripped_strings)[2:10]:
                words = line.split()
                if 1 <= len(words) <= 8 and 2 <= len(line) <= 80 and not re.search(r'\d{4}', line):
                    authors.append(line)
                    break

        abstract = ''
        abs_el = card.select_one('p.line-clamp-6')
        if abs_el:
            abstract = abs_el.get_text('\n', strip=True)
        if not abstract:
            best = ''
            for s in card.stripped_strings:
                if 60 <= len(s) <= 5000 and len(s) > len(best):
                    best = s
            abstract = best

        likes = 0
        views = 0
        like_btn = card.select_one('button[aria-label*="Like" i]')
        if like_btn:
            num_span = like_btn.select_one('span.inline-block')
            if num_span:
                n = _parse_k_number(num_span.get_text(strip=True))
                if n is not None:
                    likes = n
        if not likes:
            for s in list(card.stripped_strings):
                sl = s.lower()
                if 'like' in sl and 'bookmark' not in sl:
                    n = _parse_k_number(sl.replace('like', '').strip())
                    if n is not None:
                        likes = n
                        break

        papers.append({
            'paper_id': paper_id,
            'url': url,
            'title_en': str(title).strip(),
            'abstract_en': str(abstract).strip(),
            'ai_overview_en': '',
            'authors': authors,
            'published_date': date_str,
            'modified_date': date_str,
            'likes': likes,
            'views': views,
            'comment_count': 0,
            'trending_position': idx,
            'comments': [],
        })

    seen = set()
    deduped = []
    for p in papers:
        if p['paper_id'] in seen:
            continue
        seen.add(p['paper_id'])
        deduped.append(p)
    return deduped


def extract_all_papers(html_content):
    """Extract papers from both JSON-LD and DOM, merge and dedupe.
    JSON-LD entries win for overlapping IDs (they usually have richer interactionStatistic data)."""
    jsonld = extract_jsonld_papers(html_content)
    dom = extract_dom_papers(html_content)
    merged = {}
    for p in jsonld:
        merged[p['paper_id']] = p
    for p in dom:
        if p['paper_id'] not in merged:
            merged[p['paper_id']] = p
        else:
            existing = merged[p['paper_id']]
            if not existing.get('title_en') and p.get('title_en'):
                existing['title_en'] = p['title_en']
            if not existing.get('abstract_en') and p.get('abstract_en'):
                existing['abstract_en'] = p['abstract_en']
            if not existing.get('authors') and p.get('authors'):
                existing['authors'] = p['authors']
            if not existing.get('published_date') and p.get('published_date'):
                existing['published_date'] = p['published_date']
            if not existing.get('modified_date') and p.get('modified_date'):
                existing['modified_date'] = p['modified_date']
            if existing.get('likes', 0) == 0 and p.get('likes', 0) > 0:
                existing['likes'] = p['likes']
            if existing.get('trending_position') is None and p.get('trending_position') is not None:
                existing['trending_position'] = p['trending_position']
    result = list(merged.values())
    result.sort(key=lambda x: (x.get('trending_position') or 10**9, x['paper_id']))
    return result


def _parse_article(article, position=None):
    url = article.get('url', '')
    m = re.search(r'/abs/(\d{4}\.\d{4,5}(?:v\d+)?)', url)
    paper_id = m.group(1) if m else None
    if not paper_id:
        m2 = re.search(r'(\d{4}\.\d{4,5})', url)
        paper_id = m2.group(1) if m2 else None
    if not paper_id:
        return None
    title = article.get('headline') or article.get('name') or ''
    abstract = article.get('description') or ''
    date_pub = article.get('datePublished') or ''
    date_mod = article.get('dateModified') or ''
    authors = []
    for a in article.get('author', []) or []:
        if isinstance(a, dict):
            authors.append(a.get('name', ''))
        elif isinstance(a, str):
            authors.append(a)
    authors = [x for x in authors if x]
    likes = 0
    views = 0
    for stat in article.get('interactionStatistic', []) or []:
        if not isinstance(stat, dict):
            continue
        itype = stat.get('interactionType')
        if isinstance(itype, dict):
            itype = itype.get('@type') or itype.get('url') or str(itype)
        itype_str = str(itype).lower()
        count = int(stat.get('userInteractionCount', 0) or 0)
        if 'like' in itype_str:
            likes = count
        elif 'view' in itype_str:
            views = count
    return {
        'paper_id': paper_id,
        'url': url,
        'title_en': str(title).strip(),
        'abstract_en': str(abstract).strip(),
        'ai_overview_en': '',
        'authors': authors,
        'published_date': date_pub,
        'modified_date': date_mod,
        'likes': likes,
        'views': views,
        'comment_count': 0,
        'trending_position': position,
        'comments': [],
    }


def fetch_paper_details(paper_url, timeout=20):
    """Fetch individual paper detail page to extract AI Overview, comments, full abstract."""
    result = {
        'ai_overview_en': '',
        'abstract_en_full': '',
        'comments': [],
        'comment_count': 0,
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    try:
        resp = _retry_session.get(paper_url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return result
        resp.encoding = resp.apparent_encoding or 'utf-8'
        html = resp.text
        soup = BeautifulSoup(html, 'html.parser')

        # Full abstract: look for "View More" pattern or longer abstract sections
        abstract_candidates = []
        for sel in [
            'div[class*="abstract"]', 'section[class*="abstract"]',
            'article [class*="abstract"]', '[data-testid*="abstract"]',
            'meta[name="description"]', 'meta[property="og:description"]'
        ]:
            el = soup.select_one(sel)
            if el:
                if el.name == 'meta':
                    txt = el.get('content', '')
                else:
                    txt = el.get_text('\n', strip=True)
                if txt and len(txt) > 50:
                    abstract_candidates.append(txt)
        if abstract_candidates:
            best = max(abstract_candidates, key=len)
            result['abstract_en_full'] = best

        # AI Overview: search for keywords "AI Overview", "Summary", etc.
        overview_keywords = ['ai overview', 'ai summary', 'overview', 'tl;dr', 'summary', 'ai take']
        for text_node in soup.find_all(string=re.compile(r'AI\s*(Overview|Summary)', re.I)):
            parent = text_node.parent
            container = None
            for _ in range(5):
                if parent is None:
                    break
                txt = parent.get_text('\n', strip=True)
                if len(txt) > 80:
                    container = parent
                    break
                parent = parent.parent
            if container:
                # strip label
                full = container.get_text('\n', strip=True)
                for kw in overview_keywords:
                    idx = full.lower().find(kw)
                    if idx != -1:
                        after = full[idx + len(kw):]
                        after = re.sub(r'^[\s:：\-—•\.]+', '', after)
                        if len(after) > 30:
                            result['ai_overview_en'] = after
                            break
                if not result['ai_overview_en'] and len(full) > 80:
                    result['ai_overview_en'] = full
                break

        # Fallback: look for JSON-LD in detail page
        if not result['ai_overview_en'] or not result['abstract_en_full']:
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(script.string or script.get_text())
                    graph = data.get('@graph', [data]) if isinstance(data, dict) else data
                    for n in graph if isinstance(graph, list) else []:
                        if isinstance(n, dict) and n.get('@type') == 'Article':
                            if not result['abstract_en_full']:
                                desc = n.get('description', '')
                                if len(desc) > len(result['abstract_en_full']):
                                    result['abstract_en_full'] = desc
                except Exception:
                    pass

        # Comments
        comments = _extract_comments(soup, html)
        if comments:
            result['comments'] = comments
            result['comment_count'] = len(comments)

    except Exception:
        pass
    return result


def _extract_comments(soup, html_raw):
    """Attempt to extract comments from soup; alphaXiv may also embed them in JSON."""
    comments = []
    seen_ids = set()
    # JSON embedded comments search
    for pat in [r'"comments"\s*:\s*(\[.*?\])', r'"discussions"\s*:\s*(\[.*?\])', r'"replies"\s*:\s*(\[.*?\])']:
        for m in re.finditer(pat, html_raw, re.DOTALL):
            try:
                arr = json.loads(m.group(1))
                if isinstance(arr, list):
                    for c in arr:
                        _add_comment_from_dict(c, comments, seen_ids)
            except Exception:
                pass
    # DOM-based fallback
    for sel in [
        '[class*="comment"]', '[class*="discussion"]', '[id*="comment"]',
        'article.comment', 'div.comment', 'li.comment'
    ]:
        for el in soup.select(sel):
            txt = el.get_text('\n', strip=True)
            if not txt or len(txt) < 15:
                continue
            author_el = el.select_one('[class*="author"], [class*="user"], [class*="name"]')
            author = author_el.get_text(strip=True) if author_el else ''
            time_el = el.select_one('time, [class*="time"], [class*="date"]')
            ts = ''
            if time_el:
                ts = time_el.get('datetime') or time_el.get_text(strip=True)
            cid = el.get('id') or el.get('data-id') or hashlib.md5(txt.encode('utf-8')).hexdigest()[:12]
            key = (cid or '', txt[:80])
            if key in seen_ids:
                continue
            seen_ids.add(key)
            comments.append({
                'id': cid,
                'author': author,
                'content': txt,
                'published_at': ts,
            })
    return comments


def _add_comment_from_dict(c, comments, seen_ids):
    if not isinstance(c, dict):
        return
    content = c.get('body') or c.get('content') or c.get('text') or c.get('message') or ''
    if not content or len(str(content)) < 10:
        return
    cid = str(c.get('id') or c.get('comment_id') or hashlib.md5(str(content).encode('utf-8')).hexdigest()[:12])
    if cid in seen_ids:
        return
    seen_ids.add(cid)
    author = ''
    a = c.get('author') or c.get('user') or c.get('creator')
    if isinstance(a, dict):
        author = a.get('name') or a.get('username') or ''
    elif isinstance(a, str):
        author = a
    ts = c.get('created_at') or c.get('published_at') or c.get('timestamp') or c.get('date') or ''
    comments.append({
        'id': cid,
        'author': str(author),
        'content': str(content),
        'published_at': str(ts),
    })
    for ch_key in ('replies', 'children', 'comments'):
        children = c.get(ch_key)
        if isinstance(children, list):
            for ch in children:
                _add_comment_from_dict(ch, comments, seen_ids)


def find_webarchive_files(directory):
    files = []
    for root, _, filenames in os.walk(directory):
        for fn in filenames:
            if fn.endswith('.webarchive'):
                files.append(os.path.join(root, fn))
    return sorted(files)


def collect_papers_from_source(args):
    """Return list of paper dicts from sources (webarchive files or dir)."""
    all_papers = []
    sources = []
    if args.dir:
        sources.extend(find_webarchive_files(args.dir))
    if not sources:
        print("[ERROR] No webarchive files found in the specified directory.")
        return all_papers

    seen_ids = set()
    for src in sources:
        print(f"[INFO] Processing webarchive: {src}")
        html = extract_html_from_webarchive(src)
        if not html:
            print(f"  -> Failed to extract HTML")
            continue
        papers = extract_all_papers(html)
        print(f"  -> Extracted {len(papers)} papers (JSON-LD + DOM merged)")
        for p in papers:
            if p['paper_id'] in seen_ids:
                continue
            seen_ids.add(p['paper_id'])
            all_papers.append(p)
    return all_papers


# ============================================================
# Time Filtering
# ============================================================

def parse_time_filter(s, end_of_day=False):
    if not s:
        return None
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(s, fmt)
            if end_of_day and fmt == '%Y-%m-%d':
                dt = dt + timedelta(days=1) - timedelta(microseconds=1)
            return dt
        except ValueError:
            continue
    return None


def _strip_tz(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_iso(ts):
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return _strip_tz(dt)
    except Exception:
        for fmt in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z',
                    '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S',
                    '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                dt = datetime.strptime(s, fmt)
                return _strip_tz(dt)
            except ValueError:
                continue
    return None


def paper_in_range(pub_ts, after_dt, before_dt):
    dt = _parse_iso(pub_ts)
    if dt is None:
        return True
    a = _strip_tz(after_dt)
    b = _strip_tz(before_dt)
    if a and dt < a:
        return False
    if b and dt > b:
        return False
    return True


# ============================================================
# Hashing / Translation helpers
# ============================================================

def content_hash(s):
    if s is None:
        return None
    return hashlib.sha1(str(s).encode('utf-8')).hexdigest()


# ============================================================
# LLM Translation (DashScope / OpenAI compatible)
# ============================================================

def call_qwen_plus(prompt, model='qwen-plus', timeout=600, max_retries=2, verbose=False):
    """调用阿里云大模型（使用原生SDK，参考 AnalysisGrepOutput.py 方式）"""
    api_key = os.environ.get('DASHSCOPE_API_KEY')
    if not api_key:
        raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")
    dashscope.api_key = api_key
    if verbose:
        print(f"[DEBUG][LLM] call_qwen_plus: prompt_len={len(prompt)} chars, model={model}, timeout={timeout}s")
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            response = Generation.call(
                model=model,
                prompt=prompt,
                max_tokens=32000,
                temperature=0.7,
                timeout=timeout,
            )
        except Exception as e:
            last_exc = e
            print(f"[DEBUG][LLM] call_qwen_plus EXCEPTION attempt={attempt}/{max_retries}: type={type(e).__name__}, msg={str(e)[:500]}")
            if attempt < max_retries:
                sleep_sec = 10 * attempt
                print(f"[DEBUG][LLM] call_qwen_plus: retry after {sleep_sec}s ...")
                import time
                time.sleep(sleep_sec)
                continue
            else:
                raise
        if verbose:
            print(f"[DEBUG][LLM] call_qwen_plus: status_code={response.status_code}")
        if response.status_code == 200:
            out = response.output.text or ''
            if verbose:
                print(f"[DEBUG][LLM] call_qwen_plus: response output_len={len(out)} chars, first_300={out[:300]!r}")
            return out
        else:
            print(f"[DEBUG][LLM] call_qwen_plus ERROR: message={response.message}")
            if hasattr(response, 'request_id'):
                print(f"[DEBUG][LLM] call_qwen_plus request_id={response.request_id}")
            raise Exception(f"API调用失败: {response.message}")
    if last_exc:
        raise last_exc


def call_model_via_openai(prompt, model, system_message, timeout=600, max_retries=2, verbose=False):
    """通过OpenAI兼容接口调用模型（支持deepseek-v4-pro、qwen3.6-plus等，参考 AnalysisGrepOutput.py 方式）"""
    api_key = os.environ.get('DASHSCOPE_API_KEY')
    if not api_key:
        raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")
    if verbose:
        print(f"[DEBUG][LLM] call_model_via_openai: prompt_len={len(prompt)} chars, system_len={len(system_message or '')}, model={model}, timeout={timeout}s")
    client = OpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout=timeout,
    )
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt}
                ],
                stream=False
            )
        except Exception as e:
            last_exc = e
            print(f"[DEBUG][LLM] call_model_via_openai EXCEPTION attempt={attempt}/{max_retries}: type={type(e).__name__}, msg={str(e)[:800]}")
            if attempt < max_retries:
                sleep_sec = 10 * attempt
                print(f"[DEBUG][LLM] call_model_via_openai: retry after {sleep_sec}s ...")
                import time
                time.sleep(sleep_sec)
                continue
            else:
                raise
        content = completion.choices[0].message.content or ''
        if verbose:
            print(f"[DEBUG][LLM] call_model_via_openai: response_len={len(content)} chars, first_300={content[:300]!r}")
        return content
    if last_exc:
        raise last_exc


class LLMTranslator:
    PROTOCOL_JSON = 'json'
    PROTOCOL_XML = 'xml'

    def __init__(self, model_name=None, protocol=None, verbose=None):
        self.model = model_name or os.environ.get('PAPERGREP_MODEL', 'qwen-plus')
        self.api_key = os.environ.get('DASHSCOPE_API_KEY')
        env_proto = (os.environ.get('PAPERGREP_PROTOCOL') or '').strip().lower()
        if protocol is None:
            protocol = env_proto
        if protocol not in (self.PROTOCOL_JSON, self.PROTOCOL_XML):
            protocol = self.PROTOCOL_JSON
        self.protocol = protocol
        if verbose is None:
            env_v = (os.environ.get('PAPERGREP_LLM_VERBOSE') or '').strip().lower()
            verbose = env_v in ('1', 'true', 'yes', 'on')
        self.verbose = bool(verbose)
        print(f"[INFO] LLMTranslator init: model={self.model!r}, protocol={self.protocol!r}, verbose={self.verbose}")

    def _debug(self, msg):
        if self.verbose:
            print(f"[DEBUG][LLM] {msg}")

    def available(self):
        return bool(self.api_key)

    def _call_model(self, prompt, system_message):
        self._debug(f"_call_model enter: model={self.model}, prompt_len={len(prompt)}, system_len={len(system_message or '')}")
        t0 = datetime.now()
        try:
            if self.model == 'qwen-plus':
                merged = f"{system_message}\n\n{prompt}" if system_message else prompt
                result = call_qwen_plus(merged, self.model, verbose=self.verbose)
            else:
                result = call_model_via_openai(prompt, self.model, system_message, verbose=self.verbose)
        except Exception as e:
            dt = (datetime.now() - t0).total_seconds()
            print(f"[DEBUG][LLM] _call_model FAILED after {dt:.2f}s, re-raise")
            raise
        dt = (datetime.now() - t0).total_seconds()
        self._debug(f"_call_model OK in {dt:.2f}s, result_len={len(result or '')}")
        return result

    def _build_metas(self, items):
        metas = []
        for (k, t) in items:
            prefix = k.partition('::')[0]
            if prefix in ('TITLE', 'ABSTRACT', 'COMMENT'):
                metas.append({'kind': prefix, 'mode': 'translate'})
            elif prefix in ('OVERVIEW_B', 'OVERVIEW_S', 'OVERVIEW_F', 'OVERVIEW'):
                metas.append({'kind': 'OVERVIEW', 'mode': 'summary'})
            else:
                metas.append({'kind': 'OTHER', 'mode': 'translate'})
        return metas

    def _build_prompt(self, chunk_items, chunk_metas, source_lang, target_lang):
        protocol = self.protocol
        if protocol == self.PROTOCOL_XML:
            return self._build_prompt_xml(chunk_items, chunk_metas, source_lang, target_lang)
        return self._build_prompt_json(chunk_items, chunk_metas, source_lang, target_lang)

    def _build_prompt_json(self, chunk_items, chunk_metas, source_lang, target_lang):
        prompt_parts = [
            f"You are a professional translator and research paper summarizer. Process the following texts from {source_lang} into {target_lang}.",
            "Preserve technical terms (ML/AI/CS terminology) as-is when appropriate, ensure accuracy, keep Markdown/formatting intact.",
            "",
            "For each input i, output JSON value at position i:",
            "  - For TITLE / ABSTRACT / COMMENT inputs: output a single STRING (the full Chinese translation).",
            "  - For AI OVERVIEW inputs: output a single STRING — a Chinese summary of about 1500 characters, covering: 研究问题与动机, 相关工作与痛点, 核心方法与技术细节, 关键创新点, 主要实验设置, 实验结果与分析, 局限性与未来方向, 结论与应用价值。Do NOT output a full word-by-word translation (formulas and pseudocode rarely translate well); just produce a structured, readable ~1500-character Chinese overview.",
            "",
            "Wrap all outputs in a single top-level JSON object in the format: {\"0\": value0, \"1\": value1, ...}.",
            "Output JSON ONLY, no prose, no markdown fences.",
            "",
            "INPUTS with per-item mode:",
        ]
        for idx, ((k, text), meta) in enumerate(zip(chunk_items, chunk_metas)):
            clean = str(text).replace('\r', ' ').replace('\n', '\\n').replace('\"', '\\"')
            if meta['kind'] == 'OVERVIEW':
                mode = 'AI OVERVIEW → 返回字符串（中文约 1500 字结构化概述，非逐字翻译）'
            else:
                mode = f"{meta['kind']} → 返回字符串（中文完整翻译）"
            prompt_parts.append(f"{idx}. [MODE: {mode}] {clean}")
        return "\n".join(prompt_parts)

    def _build_prompt_xml(self, chunk_items, chunk_metas, source_lang, target_lang):
        prompt_parts = [
            f"You are a professional translator and research paper summarizer. Process the following texts from {source_lang} into {target_lang}.",
            "Preserve technical terms (ML/AI/CS terminology) as-is when appropriate, ensure accuracy, keep Markdown/formatting intact.",
            "",
            "OUTPUT FORMAT — use XML-style delimiter tags to wrap each translated item. DO NOT wrap your whole answer in any global JSON object.",
            "For each input i, output EXACTLY:",
            "  <item_i>",
            "    <your translated content for item i>",
            "  </item_i>",
            "  - For TITLE / ABSTRACT / COMMENT inputs: content inside the tags is a plain Chinese string (full translation).",
            "  - For AI OVERVIEW inputs: content inside the tags is a plain Chinese string — a structured overview of about 1500 characters, covering: 研究问题与动机, 相关工作与痛点, 核心方法与技术细节, 关键创新点, 主要实验设置, 实验结果与分析, 局限性与未来方向, 结论与应用价值。Do NOT output a full word-by-word translation (formulas and pseudocode rarely translate well); just produce a readable ~1500-character Chinese overview.",
            "",
            "CRITICAL RULES for the XML delimiter format:",
            "  1. NEVER write the literal strings '</item_' anywhere inside the translated content itself. If the source contains them, rewrite slightly or insert a zero-width space.",
            "  2. Do not indent the closing tag with tabs; write </item_i> on its own line.",
            "  3. Do not output any extra commentary before or after the tagged items.",
            "  4. If an item's translation must contain a real XML tag (e.g., `<br>`), escape it as `&lt;br&gt;` or write `< item>` with a space so our parser ignores it.",
            "",
            "INPUTS with per-item mode:",
        ]
        for idx, ((k, text), meta) in enumerate(zip(chunk_items, chunk_metas)):
            clean = str(text).replace('\r', ' ').replace('\n', '\\n')
            if meta['kind'] == 'OVERVIEW':
                mode = 'AI OVERVIEW → 标签内填中文（约 1500 字结构化概述，非逐字翻译）'
            else:
                mode = f"{meta['kind']} → 标签内填中文（纯文本，完整翻译）"
            prompt_parts.append(f"{idx}. [MODE: {mode}] {clean}")
        return "\n".join(prompt_parts)

    def _try_parse_json(self, text, label):
        """Try progressively more lenient strategies to parse JSON text.
        Returns (parsed_dict_or_None, last_error_or_None)."""
        # Pass 1: strict, as-is
        try:
            return json.loads(text), None
        except Exception as e:
            last_err = e
        # Pass 2: strict=False (tolerates control chars inside strings)
        try:
            return json.loads(text, strict=False), None
        except Exception as e:
            last_err = e
        # Pass 3: sanitize illegal escapes (e.g. \x.. \U.. \?, \$, \', \", etc.)
        #   Keep valid escapes: \" \\ \/ \b \f \n \r \t \uXXXX
        #   Strategy: walk char-by-char, keep valid ones; turn invalid backslash sequences
        #   into the literal escaped character (\\X) so JSON reads it as a plain backslash+char.
        valid_after_bs = set('"\\/bfnrtuUx')

        def sanitize_escapes(s):
            out = []
            i = 0
            n = len(s)
            while i < n:
                ch = s[i]
                if ch != '\\' or i == n - 1:
                    out.append(ch)
                    i += 1
                    continue
                nxt = s[i + 1]
                if nxt in valid_after_bs:
                    handled = False
                    if nxt == 'u' and i + 5 < n:
                        hex4 = s[i + 2:i + 6]
                        if re.fullmatch(r'[0-9a-fA-F]{4}', hex4):
                            out.append(s[i:i + 6])
                            i += 6
                            handled = True
                    elif nxt == 'U' and i + 9 < n:
                        hex8 = s[i + 2:i + 10]
                        if re.fullmatch(r'[0-9a-fA-F]{8}', hex8):
                            out.append(s[i:i + 10])
                            i += 10
                            handled = True
                    elif nxt == 'x' and i + 3 < n:
                        hex2 = s[i + 2:i + 4]
                        if re.fullmatch(r'[0-9a-fA-F]{2}', hex2):
                            # \xHH is not valid JSON - keep the two hex chars as literal, escape the backslash itself
                            out.append('\\\\x' + hex2)
                            i += 4
                            handled = True
                    if handled:
                        continue
                    # \u / \U / \x prefixes but with invalid/incomplete hex: treat as literal backslash + prefix chars
                    # e.g. "\uXYZ" -> "\\uXYZ"  (2 chars \,u escaped to literal \\ then append XYZ)
                    if nxt in ('u', 'U', 'x'):
                        out.append('\\\\')
                        out.append(nxt)
                        i += 2
                        continue
                    # other valid short escapes: \" \\ \/ \b \f \n \r \t
                    out.append(ch)
                    out.append(nxt)
                    i += 2
                else:
                    # Illegal escape like \k, \., \!, \,, etc.
                    # Emit \\ + char so the JSON parser sees a literal backslash followed by the char.
                    out.append('\\\\')
                    out.append(nxt)
                    i += 2
            return ''.join(out)

        try:
            sanitized = sanitize_escapes(text)
            if sanitized != text:
                self._debug(f"_try_parse_json ({label}): sanitized escapes, before_len={len(text)} after_len={len(sanitized)}")
            return json.loads(sanitized), None
        except Exception as e:
            last_err = e
        # Pass 4: try strict=False on sanitized
        try:
            return json.loads(sanitized, strict=False), None
        except Exception as e:
            last_err = e
        # Pass 5: 部分救援提取——顶层 JSON 损坏时，用正则按 idx 单独提取每个键值对
        partial = self._rescue_extract_json_values(text)
        if partial:
            self._debug(f"_try_parse_json ({label}): full parse failed, but PARTIAL rescue extracted {len(partial)} keys via regex.")
            return partial, None
        return None, last_err

    def _rescue_extract_json_values(self, text):
        """Last-resort: regex extract values for keys "0".."N-1" from a broken JSON top-level object.
        Returns dict[str, Any] where values are best-effort parsed (fallback to raw string)."""
        if not text:
            return {}
        # Trim the outer {...} wrapper if present to make inner scanning easier
        m = re.search(r'\{.*\}', text, re.DOTALL)
        inner = m.group(0)[1:-1] if m else text
        result = {}
        # Pattern for a top-level JSON key that is our numeric index (possibly with surrounding whitespace and commas)
        #   Match: ,opt ws "N" ws : ws <capture value up until next ,"N": or end>
        # We do this incrementally by scanning for positions of /"\d+"\s*:/ and then extracting the value region between.
        anchors = []  # list of (idx:int, key_start, colon_end)
        for am in re.finditer(r'"(\d+)"\s*:', inner):
            try:
                idx = int(am.group(1))
            except ValueError:
                continue
            anchors.append((idx, am.start(), am.end()))
        # For each consecutive anchor, slice text between them as the "value region".
        for i_anchor, (idx, _ks, val_start) in enumerate(anchors):
            if i_anchor + 1 < len(anchors):
                next_start = anchors[i_anchor + 1][1]
                val_region = inner[val_start:next_start]
            else:
                val_region = inner[val_start:]
            # Strip trailing comma and whitespace that would have separated this entry from the next
            val_region = val_region.rstrip().rstrip(',').rstrip()
            if not val_region:
                continue
            # Try to parse the value region as JSON value first (strict, then lenient)
            parsed_val = None
            region = val_region.strip()
            # Strategy A: parse as JSON value (string, object, array, number)
            for strict_p in (True, False):
                try:
                    parsed_val = json.loads(region, strict=strict_p)
                    break
                except Exception:
                    parsed_val = None
            if parsed_val is None:
                # Strategy B: if it looks like a JSON string ("..." wrapper), strip quotes and unescape manually
                if len(region) >= 2 and region[0] == '"' and region[-1] == '"':
                    body = region[1:-1]
                    try:
                        # Try json.loads to get proper unescaping, but with our sanitizer pre-applied
                        sanitized_body = '"' + self._sanitize_escapes_inline(body) + '"'
                        parsed_val = json.loads(sanitized_body, strict=False)
                    except Exception:
                        parsed_val = body.replace('\\"', '"').replace('\\\\', '\\').replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
                else:
                    # Strategy C: looks like { ... } (OVERVIEW object) but malformed, try extract summary/full via naive key scan
                    if region.startswith('{'):
                        obj = {}
                        for kk in ('summary', 'full'):
                            sm = re.search(rf'"{kk}"\s*:\s*"((?:[^"\\]|\\.)*)"', region, re.DOTALL)
                            if sm:
                                try:
                                    obj[kk] = json.loads('"' + sm.group(1) + '"', strict=False)
                                except Exception:
                                    obj[kk] = sm.group(1)
                        if obj:
                            parsed_val = obj
                    if parsed_val is None:
                        parsed_val = region
            result[str(idx)] = parsed_val
        return result

    def _sanitize_escapes_inline(self, s):
        """Inline variant of sanitize_escapes used by rescue for JSON string bodies."""
        valid_after_bs = set('"\\/bfnrtuUx')
        out = []
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch != '\\' or i == n - 1:
                out.append(ch)
                i += 1
                continue
            nxt = s[i + 1]
            if nxt in valid_after_bs:
                handled = False
                if nxt == 'u' and i + 5 < n:
                    hex4 = s[i + 2:i + 6]
                    if re.fullmatch(r'[0-9a-fA-F]{4}', hex4):
                        out.append(s[i:i + 6])
                        i += 6
                        handled = True
                elif nxt == 'U' and i + 9 < n:
                    hex8 = s[i + 2:i + 10]
                    if re.fullmatch(r'[0-9a-fA-F]{8}', hex8):
                        out.append(s[i:i + 10])
                        i += 10
                        handled = True
                elif nxt == 'x' and i + 3 < n:
                    hex2 = s[i + 2:i + 4]
                    if re.fullmatch(r'[0-9a-fA-F]{2}', hex2):
                        out.append('\\\\x' + hex2)
                        i += 4
                        handled = True
                if handled:
                    continue
                if nxt in ('u', 'U', 'x'):
                    out.append('\\\\')
                    out.append(nxt)
                    i += 2
                    continue
                out.append(ch)
                out.append(nxt)
                i += 2
            else:
                out.append('\\\\')
                out.append(nxt)
                i += 2
        return ''.join(out)

    def _parse_response(self, content, chunk_items, chunk_metas, results_accum):
        if content is None:
            self._debug("_parse_response: content None, skip chunk")
            return
        self._debug(f"_parse_response: raw_response_len={len(content)} chars")
        self._debug(f"_parse_response: raw_response_first_500={content[:500]!r}")
        self._debug(f"_parse_response: raw_response_last_300={content[-300:]!r}")
        if self.protocol == self.PROTOCOL_XML:
            self._parse_response_xml(content, chunk_items, chunk_metas, results_accum)
            pre_len = len(results_accum)
            xml_fill = sum(1 for (k, _) in chunk_items if k in results_accum)
            if xml_fill == 0:
                self._debug("_parse_response: XML extraction got 0 hits, fallback try JSON on same response...")
                self._parse_response_json(content, chunk_items, chunk_metas, results_accum)
            return
        self._parse_response_json(content, chunk_items, chunk_metas, results_accum)

    def _parse_response_json(self, content, chunk_items, chunk_metas, results_accum):
        parsed, _ = self._try_parse_json(content, "first-pass raw")
        if parsed is not None:
            self._debug("_parse_response: first-pass JSON parse SUCCESS")
        else:
            self._debug("_parse_response: first-pass JSON parse FAILED. Try regex extraction.")
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if m:
                extracted = m.group(0)
                self._debug(f"_parse_response: regex extracted json_len={len(extracted)}, first_300={extracted[:300]!r}")
                parsed, last_err = self._try_parse_json(extracted, "regex-extracted")
                if parsed is not None:
                    self._debug("_parse_response: regex-extracted JSON parse SUCCESS (with lenient strategies)")
                else:
                    print(f"[WARN] _parse_response: regex-extracted JSON parse FAILED after all lenient passes, last_err={last_err}")
                    parsed = {}
            else:
                self._debug("_parse_response: no {...} found by regex, skip chunk")
                return
        if not isinstance(parsed, dict):
            print(f"[WARN] LLM JSON 顶层不是 object，无法解析。实际类型={type(parsed).__name__}, value={str(parsed)[:300]!r}")
            return
        self._debug(f"_parse_response: parsed keys count={len(parsed)}, keys_sample={list(parsed.keys())[:20]}")
        self._apply_parsed_to_results(parsed, chunk_items, chunk_metas, results_accum)

    def _apply_parsed_to_results(self, parsed, chunk_items, chunk_metas, results_accum):
        """Common logic: given parsed {idx_str -> raw_value} dict, fill results_accum
        with properly normalized values. OVERVIEW now is a plain ~1000-char Chinese overview string."""
        miss_count = 0
        for idx, (key, _) in enumerate(chunk_items):
            raw = parsed.get(str(idx))
            if raw is None:
                raw = parsed.get(idx)
            if raw is None:
                miss_count += 1
                self._debug(f"_parse_response: MISSING result for idx={idx} (key={key!r}), both str({idx}) and int({idx}) missing in response keys")
                continue
            meta = chunk_metas[idx]
            if meta['kind'] == 'OVERVIEW':
                if isinstance(raw, dict):
                    text = str(raw.get('summary') or raw.get('full') or '')
                else:
                    text = str(raw)
                text = text.replace('\\n', '\n').strip()
                if text.startswith('{'):
                    sub_p, _ = self._try_parse_json(text, f"overview-inner-{idx}")
                    if isinstance(sub_p, dict):
                        text = str(sub_p.get('summary') or sub_p.get('full') or text)
                        text = text.replace('\\n', '\n').strip()
                results_accum[key] = text
            else:
                results_accum[key] = str(raw).replace('\\n', '\n').strip()
        chunk_got = len(chunk_items) - miss_count
        self._debug(f"_parse_response: chunk results got={chunk_got}/{len(chunk_items)} (missed={miss_count})")
        if chunk_got == 0 and len(chunk_items) > 0:
            print(f"[WARN] _parse_response: 整个分块全部解析失败（{miss_count}/{len(chunk_items)} 条丢失）。可能原因：LLM 返回 JSON/XML 格式异常、键名不以 0..n-1 编号，或响应被截断。请检查 raw_response_first_500 / raw_response_last_300 调试日志。")

    def _parse_response_xml(self, content, chunk_items, chunk_metas, results_accum):
        """Parse XML-delimited response: <item_0> ... </item_0> <item_1> ... </item_1> ...
        All values (including OVERVIEW) are plain strings now."""
        self._debug(f"_parse_response_xml: using XML-delimited protocol for {len(chunk_items)} items")
        parsed = {}
        expected_indices = list(range(len(chunk_items)))
        tag_matches = []
        for om in re.finditer(r'<\s*item_(\d+)\s*>', content):
            try:
                i = int(om.group(1))
            except ValueError:
                continue
            tag_matches.append((True, i, om.start(), om.end()))
        for cm in re.finditer(r'<\s*/\s*item_(\d+)\s*>', content):
            try:
                i = int(cm.group(1))
            except ValueError:
                continue
            tag_matches.append((False, i, cm.start(), cm.end()))
        by_idx_open = {}
        by_idx_close = {}
        for (is_open, i, s, e) in tag_matches:
            if is_open:
                if i not in by_idx_open or s < by_idx_open[i][0]:
                    by_idx_open[i] = (s, e)
            else:
                if i not in by_idx_close or e > by_idx_close[i][1]:
                    by_idx_close[i] = (s, e)
        for i in expected_indices:
            if i not in by_idx_open or i not in by_idx_close:
                self._debug(f"_parse_response_xml: idx={i} missing tag pair (open={i in by_idx_open}, close={i in by_idx_close})")
                continue
            os_, oe = by_idx_open[i]
            cs_, ce_ = by_idx_close[i]
            if oe >= cs_:
                self._debug(f"_parse_response_xml: idx={i} tag overlap, skip")
                continue
            body = content[oe:cs_]
            body = body.strip('\r\n')
            body = body.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&').replace('&quot;', '"').replace('&apos;', "'")
            parsed[str(i)] = body.strip()
        self._debug(f"_parse_response_xml: extracted values for {len(parsed)}/{len(chunk_items)} indices")
        self._apply_parsed_to_results(parsed, chunk_items, chunk_metas, results_accum)

    def _split_chunks(self, items, max_items_per_chunk=50, max_chars_per_chunk=150000):
        """Split items into chunks by both item count and estimated char budget."""
        chunks = []
        cur_chunk = []
        cur_chars = 0
        base_overhead = 2500  # estimated prompt header/meta overhead
        for (k, t) in items:
            est = len(str(t)) + len(k) + 80  # per-item overhead (mode prefix, format, idx)
            if (cur_chunk and (len(cur_chunk) >= max_items_per_chunk or (cur_chars + est + base_overhead) > max_chars_per_chunk)):
                chunks.append(cur_chunk)
                cur_chunk = []
                cur_chars = 0
            cur_chunk.append((k, t))
            cur_chars += est
        if cur_chunk:
            chunks.append(cur_chunk)
        return chunks

    def translate_batch(self, items, source_lang='English', target_lang='Chinese'):
        """items: list of (key, text). key 前缀 TITLE_/ABSTRACT_/OVERVIEW_*/COMMENT_ 用来识别类型。
        返回 dict key -> value：均为字符串，其中 OVERVIEW 是约 1500 字中文结构化概述。"""
        items = [(k, t) for (k, t) in items if t and str(t).strip()]
        if not items:
            self._debug("translate_batch: items empty, return {}")
            return {}
        if not self.available():
            print(f"[WARN] No LLM API key configured. Skip translation for {len(items)} items.")
            return {}

        cnt_title = sum(1 for (k, _) in items if k.startswith('TITLE::'))
        cnt_abs = sum(1 for (k, _) in items if k.startswith('ABSTRACT::'))
        cnt_comment = sum(1 for (k, _) in items if k.startswith('COMMENT::'))
        cnt_ov = sum(1 for (k, _) in items if k.startswith('OVERVIEW'))
        print(f"[INFO] 翻译任务：共 {len(items)} 条（标题 {cnt_title}，摘要 {cnt_abs}，Overview {cnt_ov}，评论 {cnt_comment}）")
        if self.verbose:
            for k, t in items:
                print(f"[DEBUG][LLM]   - task key={k!r:24s}  text_len={len(str(t))}")

        chunks = self._split_chunks(items)
        n_chunks = len(chunks)
        self._debug(f"translate_batch: split into {n_chunks} chunks (sizes: {[len(c) for c in chunks]})")

        results = {}
        failed_chunks = []
        system_message = "You are a precise translator that outputs strict valid JSON."

        for ci, chunk in enumerate(chunks, 1):
            print(f"[INFO]   ===== 翻译分块 {ci}/{n_chunks}，条目 {len(chunk)} 条 =====")
            chunk_metas = self._build_metas(chunk)
            prompt = self._build_prompt(chunk, chunk_metas, source_lang, target_lang)
            self._debug(f"translate_batch chunk {ci}: assembled prompt_len={len(prompt)} chars")
            chunk_ok = True
            try:
                content = self._call_model(prompt, system_message)
                self._parse_response(content, chunk, chunk_metas, results)
            except Exception as e:
                import traceback
                chunk_ok = False
                failed_chunks.append((ci, len(chunk), str(e)))
                print(f"[WARN] LLM translation chunk {ci}/{n_chunks} error: {e}")
                print(f"[DEBUG][LLM] translate_batch chunk {ci} EXCEPTION traceback:\n{traceback.format_exc()}")
            got_so_far = len(results)
            if chunk_ok:
                print(f"[INFO]   分块 {ci}/{n_chunks} 完成，累计成功 {got_so_far}/{len(items)}")
            else:
                print(f"[WARN]   分块 {ci}/{n_chunks} 失败，累计仍为 {got_so_far}/{len(items)}（丢失约 {len(chunk)} 条）")

        if failed_chunks:
            lost_est = sum(n for (_, n, _) in failed_chunks)
            ids = ', '.join(f"#{ci}(-{n})" for (ci, n, _) in failed_chunks)
            print(f"[WARN] translate_batch: 共有 {len(failed_chunks)}/{n_chunks} 个分块失败（{ids}），估计丢失 {lost_est}/{len(items)} 条翻译结果。")
            for (ci, n, err) in failed_chunks:
                print(f"[WARN]   - chunk {ci}/{n_chunks} (size={n}): {err}")
        print(f"[INFO] 翻译返回：{len(results)}/{len(items)} 条结果")
        if self.verbose:
            for k, v in results.items():
                print(f"[DEBUG][LLM]   ok key={k!r:24s}  result_len={len(v)}  first_80={str(v)[:80]!r}")
        return results


# ============================================================
# Database Sync Logic
# ============================================================

def _clip(s, n):
    s = str(s).strip().replace('\r', '').replace('\n', '  ')
    return (s[:n] + '…') if len(s) > n else s


def sync_papers(conn, fetched_papers, translator, fetch_details=True):
    """Insert/update papers, detect changes, trigger translations when needed.
    Returns (new_paper_ids, updated_papers_info, new_comments_info, likes_ranking_before, likes_ranking_after, translation_updates, comment_translations)"""
    print(f"[INFO] [4/7] 同步 {len(fetched_papers)} 篇论文到数据库 …")
    now = datetime.now().isoformat(timespec='seconds')
    c = conn.cursor()

    # Ranking before (by likes, desc)
    c.execute("SELECT paper_id, title_en, title_zh, likes FROM papers ORDER BY likes DESC, first_seen ASC LIMIT 50")
    ranking_before = [dict(r) for r in c.fetchall()]

    new_paper_ids = []
    updated = []
    new_comments = []

    # 1) Per-paper fetch details (optional) in parallel
    if fetch_details:
        to_fetch = [(p['paper_id'], p['url']) for p in fetched_papers]
        detail_results = {}
        n = len(to_fetch)
        print(f"[INFO]   并发抓取 {n} 篇论文详情页 (AI Overview / 完整摘要 / 评论) …")
        done_cnt = 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            fut_map = {ex.submit(fetch_paper_details, url): pid for (pid, url) in to_fetch}
            for fut in as_completed(fut_map):
                pid = fut_map[fut]
                try:
                    detail_results[pid] = fut.result()
                except Exception:
                    detail_results[pid] = {}
                done_cnt += 1
                if done_cnt % 5 == 0 or done_cnt == n:
                    print(f"[INFO]     详情页进度 {done_cnt}/{n}")
    else:
        detail_results = {}
        print(f"[INFO]   --no-details: 跳过详情页抓取")

    print(f"[INFO]   写入/对比 {len(fetched_papers)} 篇论文元数据 …")
    for i, p in enumerate(fetched_papers, 1):
        pid = p['paper_id']
        details = detail_results.get(pid, {})
        abstract_full_incoming = details.get('abstract_en_full') or p['abstract_en']
        ai_overview_incoming = details.get('ai_overview_en') or ''
        comments = details.get('comments', [])
        cc = details.get('comment_count', len(comments)) if details else 0
        title = p['title_en']
        authors_json = json.dumps(p['authors'], ensure_ascii=False)

        c.execute("SELECT * FROM papers WHERE paper_id = ?", (pid,))
        row = c.fetchone()

        new_title_hash = content_hash(title)
        new_abstract_hash = content_hash(abstract_full_incoming)
        new_ai_overview_hash = content_hash(ai_overview_incoming)

        if row is None:
            abstract_full = abstract_full_incoming
            ai_overview = ai_overview_incoming
            c.execute("""
                INSERT INTO papers (paper_id, url, title_en, abstract_en, ai_overview_en, authors_json,
                    published_date, modified_date, likes, views, comment_count,
                    title_hash, abstract_hash, ai_overview_hash, translated_fields,
                    first_seen, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                pid, p['url'], title, abstract_full, ai_overview, authors_json,
                p['published_date'], p['modified_date'], p['likes'], p['views'],
                max(cc, 0),
                new_title_hash, new_abstract_hash, new_ai_overview_hash, '{}',
                now, now
            ))
            new_paper_ids.append(pid)
            _sync_comments_for(conn, pid, comments, new_comments, now)
            continue

        # UPDATE path: use hash to decide content updates; likes/views always refreshed
        prev = dict(row)
        changes = []
        translated_fields = json.loads(prev['translated_fields'] or '{}')
        translated = dict(translated_fields)

        if str(prev['likes']) != str(p['likes']):
            changes.append({'field': 'likes', 'before': prev['likes'], 'after': p['likes']})
        if str(prev['views']) != str(p['views']):
            changes.append({'field': 'views', 'before': prev['views'], 'after': p['views']})
        if str(prev['comment_count']) != str(max(cc, 0)):
            changes.append({'field': 'comment_count', 'before': prev['comment_count'], 'after': cc})

        if prev['title_hash'] != new_title_hash and title:
            changes.append({'field': 'title_en', 'before_len': len(prev['title_en'] or ''), 'after_len': len(title)})
            translated.pop('title_zh', None)
            store_title = title
            store_title_hash = new_title_hash
        else:
            store_title = prev['title_en']
            store_title_hash = prev['title_hash']

        abstract_incoming_nonempty = bool(abstract_full_incoming and str(abstract_full_incoming).strip())
        abstract_prev_nonempty = bool(prev['abstract_en'] and str(prev['abstract_en']).strip())
        abstract_hash_diff = (prev['abstract_hash'] or '') != (new_abstract_hash or '')
        abstract_need_update = False
        if abstract_incoming_nonempty and abstract_hash_diff:
            abstract_need_update = True
        elif abstract_incoming_nonempty and not abstract_prev_nonempty:
            abstract_need_update = True
        if abstract_need_update:
            if (prev['abstract_en'] or '') != (abstract_full_incoming or ''):
                changes.append({'field': 'abstract_en', 'before_len': len(prev['abstract_en'] or ''), 'after_len': len(abstract_full_incoming)})
            translated.pop('abstract_zh', None)
            store_abstract = abstract_full_incoming
            store_abstract_hash = new_abstract_hash
        else:
            store_abstract = prev['abstract_en']
            store_abstract_hash = prev['abstract_hash']

        overview_incoming_nonempty = bool(ai_overview_incoming and str(ai_overview_incoming).strip())
        overview_prev_nonempty = bool(prev['ai_overview_en'] and str(prev['ai_overview_en']).strip())
        overview_hash_diff = (prev['ai_overview_hash'] or '') != (new_ai_overview_hash or '')
        overview_need_update = False
        if overview_incoming_nonempty and overview_hash_diff:
            overview_need_update = True
        elif overview_incoming_nonempty and not overview_prev_nonempty:
            overview_need_update = True
        if overview_need_update:
            if (prev['ai_overview_en'] or '') != (ai_overview_incoming or ''):
                changes.append({'field': 'ai_overview_en', 'before_len': len(prev['ai_overview_en'] or ''), 'after_len': len(ai_overview_incoming)})
            translated.pop('ai_overview_zh', None)
            store_overview = ai_overview_incoming
            store_overview_hash = new_ai_overview_hash
        else:
            store_overview = prev['ai_overview_en']
            store_overview_hash = prev['ai_overview_hash']

        if prev['modified_date'] != p['modified_date']:
            changes.append({'field': 'modified_date'})

        c.execute("""
            UPDATE papers SET
                url=?, title_en=?, abstract_en=?, ai_overview_en=?, authors_json=?,
                published_date=?, modified_date=?, likes=?, views=?, comment_count=?,
                title_hash=?, abstract_hash=?, ai_overview_hash=?, translated_fields=?,
                last_updated=?
            WHERE paper_id=?
        """, (
            p['url'], store_title, store_abstract, store_overview, authors_json,
            p['published_date'], p['modified_date'], p['likes'], p['views'], max(cc, 0),
            store_title_hash, store_abstract_hash, store_overview_hash,
            json.dumps(translated, ensure_ascii=False), now, pid
        ))

        if changes:
            updated.append({'paper_id': pid, 'title_en': title, 'title_zh': prev.get('title_zh') or '', 'changes': changes})

        _sync_comments_for(conn, pid, comments, new_comments, now)

        if i % 10 == 0 or i == len(fetched_papers):
            print(f"[INFO]     元数据写入 {i}/{len(fetched_papers)} (新增 {len(new_paper_ids)}，更新 {len(updated)})")

    conn.commit()
    print(f"[INFO]   元数据写入完成：新增 {len(new_paper_ids)}，更新 {len(updated)}，新评论 {len(new_comments)}")

    # 2) Collect translation tasks (batch)
    #   集合 A：本次新入库或字段更新的论文（recent_set）
    #   集合 B：本次 fetched_papers（因为内容可能刷新但 hash 未变，仍需检查缺译）
    #   集合 C：整个 papers 表中翻译字段不全的所有历史论文（补全历史遗漏）—— 限制最多 500 篇，避免一次性开销过大
    recent_set = set(new_paper_ids) | {u['paper_id'] for u in updated}
    fetched_ids = [p['paper_id'] for p in fetched_papers]
    c.execute("""
        SELECT paper_id FROM papers
        WHERE (translated_fields IS NULL OR json_extract(translated_fields, '$.title_zh') IS NULL AND title_en IS NOT NULL AND title_en != '')
           OR (translated_fields IS NULL OR json_extract(translated_fields, '$.abstract_zh') IS NULL AND abstract_en IS NOT NULL AND abstract_en != '')
           OR (translated_fields IS NULL OR (
                 json_extract(translated_fields, '$.ai_overview_zh') IS NULL
                 AND ai_overview_en IS NOT NULL AND ai_overview_en != ''
              ))
        ORDER BY first_seen DESC
        LIMIT 500
    """)
    history_ids = [r[0] for r in c.fetchall()]
    all_ids_to_check = list(dict.fromkeys(list(recent_set) + fetched_ids + history_ids))
    rows = []
    row_map = {}
    if all_ids_to_check:
        placeholders = ','.join('?' * len(all_ids_to_check))
        c.execute(f"SELECT * FROM papers WHERE paper_id IN ({placeholders})", all_ids_to_check)
        for r in c.fetchall():
            d = dict(r)
            rows.append(d)
            row_map[d['paper_id']] = d
    history_candidates_only = [pid for pid in history_ids if pid not in recent_set and pid not in set(fetched_ids)]
    if history_candidates_only:
        print(f"[INFO]   发现 {len(history_candidates_only)} 篇历史论文翻译字段不全，将一并纳入本次补全队列")

    tasks = []
    for r in rows:
        translated = json.loads(r['translated_fields'] or '{}')
        if r['title_en'] and not translated.get('title_zh'):
            tasks.append((f"TITLE::{r['paper_id']}", r['title_en']))
        if r['abstract_en'] and not translated.get('abstract_zh'):
            tasks.append((f"ABSTRACT::{r['paper_id']}", r['abstract_en']))
        # AI Overview: 生成约 1500 字中文结构化概述（覆盖 8 个维度：动机/相关工作/方法细节/创新点/实验设置/结果分析/局限与未来/应用价值；不做逐字翻译避免公式/伪代码失真）
        if r['ai_overview_en'] and not translated.get('ai_overview_zh'):
            tasks.append((f"OVERVIEW::{r['paper_id']}", r['ai_overview_en']))

    # Also collect untranslated comments (补全所有历史缺译评论，不限制条数以保证数据库一致性)
    c.execute("""
        SELECT c.id, c.paper_id, c.content_en FROM comments c
        WHERE (c.content_zh IS NULL OR c.content_zh = '') AND c.content_en IS NOT NULL AND c.content_en != ''
    """)
    comment_rows = c.fetchall()
    if comment_rows:
        print(f"[INFO]   发现 {len(comment_rows)} 条未翻译评论，将一并补全")
    for cr in comment_rows:
        tasks.append((f"COMMENT::{cr['id']}", cr['content_en']))

    print(f"[INFO] [5/7] 调用大模型翻译：共 {len(tasks)} 项待翻译 (model={translator.model}) …")
    if tasks:
        route = 'DashScope 原生 SDK' if translator.model == 'qwen-plus' else 'OpenAI 兼容接口'
        overview_cnt = sum(1 for (k, _) in tasks if k.startswith('OVERVIEW'))
        print(f"[INFO]   使用路由：{route}  批量条目：{len(tasks)}（其中 AI Overview ~1500字中文结构化概述：{overview_cnt}）")
        translated_map = translator.translate_batch(tasks)
        got = len(translated_map)
        print(f"[INFO]   翻译返回：{got}/{len(tasks)} 条结果")
        # Apply translations
        by_paper_updates = {}
        comment_updates = []
        for key, zh in translated_map.items():
            if not zh:
                continue
            kind, _, rest = key.partition('::')
            if kind == 'TITLE':
                pid = rest
                prev = by_paper_updates.get(pid, {'title_zh': None, 'abstract_zh': None, 'ai_overview_zh': None, 'translated': json.loads(row_map[pid]['translated_fields'] or '{}')})
                prev['title_zh'] = zh
                prev['translated']['title_zh'] = True
                by_paper_updates[pid] = prev
            elif kind == 'ABSTRACT':
                pid = rest
                prev = by_paper_updates.get(pid, {'title_zh': None, 'abstract_zh': None, 'ai_overview_zh': None, 'translated': json.loads(row_map[pid]['translated_fields'] or '{}')})
                prev['abstract_zh'] = zh
                prev['translated']['abstract_zh'] = True
                by_paper_updates[pid] = prev
            elif kind == 'OVERVIEW_B' or kind == 'OVERVIEW_S' or kind == 'OVERVIEW_F' or kind == 'OVERVIEW':
                # OVERVIEW 是纯字符串：约 1500 字中文结构化概述（非逐字翻译；覆盖 8 个维度从动机到应用价值）
                pid = rest
                prev = by_paper_updates.get(pid, {'title_zh': None, 'abstract_zh': None, 'ai_overview_zh': None, 'translated': json.loads(row_map[pid]['translated_fields'] or '{}')})
                overview_text = str(zh).strip()
                if overview_text:
                    prev['ai_overview_zh'] = overview_text
                    prev['translated']['ai_overview_zh'] = True
                by_paper_updates[pid] = prev
            elif kind == 'COMMENT':
                try:
                    cid = int(rest)
                except Exception:
                    continue
                comment_updates.append((zh, cid))

        for pid, up in by_paper_updates.items():
            r = row_map[pid]
            title_zh = up['title_zh'] if up['title_zh'] is not None else r['title_zh']
            abstract_zh = up['abstract_zh'] if up['abstract_zh'] is not None else r['abstract_zh']
            overview_zh = up['ai_overview_zh'] if up['ai_overview_zh'] is not None else r['ai_overview_zh']
            overview_s_zh = overview_zh
            c.execute("""
                UPDATE papers SET title_zh=?, abstract_zh=?, ai_overview_zh=?, ai_overview_summary_zh=?, translated_fields=?, last_updated=?
                WHERE paper_id=?
            """, (
                title_zh, abstract_zh, overview_zh, overview_s_zh,
                json.dumps(up['translated'], ensure_ascii=False), now, pid
            ))
        for zh, cid in comment_updates:
            c.execute("UPDATE comments SET content_zh=? WHERE id=?", (zh, cid))
            # 同步标记 new_comments 里已翻译并保存 content_zh
            updated_markers = []
            for nc in new_comments:
                if nc.get('comment_db_id') == cid:
                    nc2 = dict(nc)
                    nc2['translated'] = True
                    nc2['content_zh'] = zh
                    updated_markers.append(nc2)
                else:
                    updated_markers.append(nc)
            new_comments[:] = updated_markers
        conn.commit()
        overview_cnt = sum(1 for up in by_paper_updates.values() if up.get('ai_overview_zh'))
        title_tr_cnt = sum(1 for up in by_paper_updates.values() if up.get('title_zh'))
        abs_tr_cnt = sum(1 for up in by_paper_updates.values() if up.get('abstract_zh'))
        print(f"[INFO]   翻译写入完成：论文字段 {len(by_paper_updates)} 条"
              f"（标题翻译 {title_tr_cnt}，摘要翻译 {abs_tr_cnt}，AI Overview ~1500字结构化概述 {overview_cnt}），"
              f"评论 {len(comment_updates)} 条")
    else:
        print(f"[INFO]   没有需要翻译的内容（全部已翻译或英文无变化），跳过 LLM 调用")

    # ---- Collect translation updates for updated_papers_json & report ----
    translation_updates = []
    for pid, up in by_paper_updates.items():
        r = row_map.get(pid) or {}
        paper_titles = {
            'title_en': r.get('title_en', ''),
            'title_zh': r.get('title_zh', ''),
        }
        fields_changed = {}
        if up.get('title_zh') is not None:
            fields_changed['title_zh'] = {
                'before_len': len(r.get('title_zh') or ''),
                'after_len': len(up['title_zh']),
                'preview': _clip(up['title_zh'], 80),
            }
        if up.get('abstract_zh') is not None:
            fields_changed['abstract_zh'] = {
                'before_len': len(r.get('abstract_zh') or ''),
                'after_len': len(up['abstract_zh']),
                'preview': _clip(up['abstract_zh'], 160),
            }
        if up.get('ai_overview_zh') is not None:
            fields_changed['ai_overview_zh'] = {
                'before_len': len(r.get('ai_overview_zh') or ''),
                'after_len': len(up['ai_overview_zh']),
                'preview': _clip(up['ai_overview_zh'], 160),
            }
        if fields_changed:
            translation_updates.append({
                'paper_id': pid,
                'title_en': paper_titles['title_en'],
                'title_zh': paper_titles['title_zh'],
                'fields': fields_changed,
            })
            existing = next((u for u in updated if u['paper_id'] == pid), None)
            if existing is None:
                updated.append({
                    'paper_id': pid,
                    'title_en': paper_titles['title_en'],
                    'title_zh': paper_titles['title_zh'],
                    'changes': [{'field': f, **v} for f, v in fields_changed.items()],
                    '_translation_only': True,
                })
            else:
                for f, v in fields_changed.items():
                    existing['changes'].append({'field': f, **v})
                existing['_has_translation'] = True

    comment_translations = []
    for zh, cid in comment_updates:
        crs = c.execute("SELECT paper_id, author_name, content_en FROM comments WHERE id=?", (cid,)).fetchone()
        if crs:
            comment_translations.append({
                'comment_id': cid,
                'paper_id': crs['paper_id'],
                'author': crs['author_name'] or '',
                'before_len': len(crs['content_en'] or ''),
                'after_len': len(zh),
                'preview_en': _clip(crs['content_en'] or '', 160),
                'preview_zh': _clip(zh, 160),
            })
            for nc in new_comments:
                if nc.get('comment_db_id') == cid:
                    nc['content_zh'] = zh
                    nc['translated'] = True

    # Ranking after
    c.execute("SELECT paper_id, title_en, title_zh, likes FROM papers ORDER BY likes DESC, first_seen ASC LIMIT 50")
    ranking_after = [dict(r) for r in c.fetchall()]

    return (new_paper_ids, updated, new_comments, ranking_before, ranking_after,
            translation_updates, comment_translations)


def _sync_comments_for(conn, paper_id, incoming_comments, new_comments_tracker, now_iso=None):
    if not incoming_comments:
        return
    if now_iso is None:
        now_iso = datetime.now().isoformat(timespec='seconds')
    c = conn.cursor()
    paper_touched = False
    for ic in incoming_comments:
        ext_id = str(ic.get('id') or '')
        content_en = str(ic.get('content') or '').strip()
        if not content_en:
            continue
        chash = content_hash(content_en)
        author = str(ic.get('author') or '')
        pub_at = str(ic.get('published_at') or '')
        if ext_id:
            c.execute("SELECT id, content_hash, content_en FROM comments WHERE paper_id=? AND external_id=?", (paper_id, ext_id))
        else:
            c.execute("SELECT id, content_hash, content_en FROM comments WHERE paper_id=? AND content_hash=?", (paper_id, chash))
        row = c.fetchone()
        if row is None:
            c.execute("""
                INSERT INTO comments (paper_id, external_id, author_name, content_en, published_at, content_hash)
                VALUES (?,?,?,?,?,?)
            """, (paper_id, ext_id, author, content_en, pub_at, chash))
            db_id = c.lastrowid
            new_comments_tracker.append({
                'comment_db_id': db_id,
                'paper_id': paper_id,
                'author': author,
                'content_en': content_en,
                'published_at': pub_at,
                'translated': False,
            })
            paper_touched = True
        else:
            db_id = row['id']
            if row['content_hash'] != chash and len(content_en) > len(row['content_en'] or ''):
                c.execute("""
                    UPDATE comments SET author_name=?, content_en=?, published_at=?, content_hash=?, content_zh=NULL, is_updated=1
                    WHERE id=?
                """, (author, content_en, pub_at, chash, db_id))
                new_comments_tracker.append({
                    'comment_db_id': db_id,
                    'paper_id': paper_id,
                    'author': author,
                    'content_en': content_en,
                    'published_at': pub_at,
                    'updated': True,
                    'translated': False,
                })
                paper_touched = True
    if paper_touched:
        c.execute("UPDATE papers SET last_updated=? WHERE paper_id=?", (now_iso, paper_id))


# ============================================================
# Report Generation
# ============================================================

def build_and_save_report(conn, new_paper_ids, updated, new_comments, ranking_before, ranking_after,
                           translation_updates, comment_translations, args):
    print("[INFO] [6/7] 生成运行报告 …")
    c = conn.cursor()
    now = datetime.now()
    ts_file = now.strftime('%Y%m%d_%H%M%S')
    now_iso = now.isoformat(timespec='seconds')

    # Load paper info for new / updated
    placeholders_new = ','.join('?' * len(new_paper_ids)) if new_paper_ids else 'NULL'
    new_papers_full = []
    if new_paper_ids:
        c.execute(f"SELECT * FROM papers WHERE paper_id IN ({placeholders_new}) ORDER BY likes DESC, first_seen ASC", new_paper_ids)
        new_papers_full = [dict(r) for r in c.fetchall()]

    # Ranking changes
    pos_before = {r['paper_id']: i + 1 for i, r in enumerate(ranking_before)}
    pos_after = {r['paper_id']: i + 1 for i, r in enumerate(ranking_after)}
    rank_changes = []
    for i, r in enumerate(ranking_after[:20]):
        pid = r['paper_id']
        before_pos = pos_before.get(pid)
        after_pos = pos_after[pid]
        delta = None
        if before_pos is not None:
            delta = before_pos - after_pos
        rank_changes.append({
            'rank': after_pos,
            'paper_id': pid,
            'title': r['title_en'],
            'title_zh': r.get('title_zh') or '',
            'likes': r['likes'],
            'prev_rank': before_pos,
            'delta': delta,
        })

    # Group new_comments by paper
    comments_by_paper = {}
    for nc in new_comments:
        pid = nc.get('paper_id')
        if not pid:
            continue
        comments_by_paper.setdefault(pid, []).append(nc)

    report_path = os.path.join(WORK_DIR, f"papergrep_report_{ts_file}.md")
    json_path = os.path.join(TRASH_DIR, f"papergrep_report_{ts_file}.json")

    buf = StringIO()
    def w(s=''):
        buf.write(s + '\n')

    # ---- Markdown Header (full Chinese) ----
    w(f"# PaperGrep 报告 — {now.strftime('%Y-%m-%d %H:%M:%S')}")
    w()
    w("> 本报告由 PaperGrep 自动生成。数据来源：alphaXiv.org 趋势论文榜。")
    w()
    w("## 📌 运行概览")
    w()
    w("| 项 | 值 |")
    w("|---|---|")
    w(f"| 翻译模型 | `{args.model}` |")
    w(f"| 开始时间过滤 | `{args.after or '(无)'}` |")
    w(f"| 结束时间过滤 | `{args.before or '(无)'}` |")
    w(f"| 🆕 新增论文 | **{len(new_papers_full)}** |")
    w(f"| 🔄 更新论文 | **{len(updated)}** |")
    w(f"| 💬 新/更新评论 | **{len(new_comments)}** |")
    if translation_updates:
        n_title = sum(1 for t in translation_updates if 'title_zh' in t['fields'])
        n_abs = sum(1 for t in translation_updates if 'abstract_zh' in t['fields'])
        n_ov = sum(1 for t in translation_updates if 'ai_overview_zh' in t['fields'])
        w(f"| 🌐 标题翻译补充 | **{n_title}** 篇 |")
        w(f"| 🌐 摘要翻译补充 | **{n_abs}** 篇 |")
        w(f"| 🌐 AI Overview ~1500 字结构化概述 | **{n_ov}** 篇 |")
    if comment_translations:
        w(f"| 🌐 评论翻译补充 | **{len(comment_translations)}** 条 |")
    w()

    # ---- Section 1: 新增论文 ----
    w("---")
    w(f"## 1. 🆕 新增论文 ({len(new_papers_full)})")
    w()
    if not new_papers_full:
        w("_本次运行没有新增论文。_")
    for i, p in enumerate(new_papers_full, 1):
        authors_list = json.loads(p['authors_json'] or '[]')
        authors = ', '.join(authors_list[:5])
        if len(authors_list) > 5:
            authors += ' 等'
        title_main = p.get('title_zh') or p.get('title_en') or p['paper_id']

        w(f"### {i}. {title_main}")
        w()
        w(f"- **Paper ID**：`{p['paper_id']}`")
        w(f"- **点赞数 / 浏览数**：**{p['likes']}** / {p['views']}")
        w(f"- **发布时间**：{p['published_date'][:16] if p['published_date'] else 'N/A'}")
        w(f"- **作者**：{authors or 'N/A'}")
        arxiv_url = f"https://arxiv.org/abs/{p['paper_id']}"
        w(f"- **原文链接**：[arxiv.org/abs/{p['paper_id']}]({arxiv_url})")
        w()
        if p.get('title_zh'):
            w(f"**标题（中文）**：{p['title_zh']}")
            w()
        if p.get('title_en'):
            w(f"**标题（英文）**：{p['title_en']}")
            w()

        has_abstract = bool(p.get('abstract_en') or p.get('abstract_zh'))
        if has_abstract:
            w("#### 📝 论文摘要")
            w()
            if p.get('abstract_zh'):
                w("**中文摘要**：")
                w()
                w(p['abstract_zh'].strip())
                w()
            if p.get('abstract_en'):
                w("**英文摘要**：")
                w()
                w(p['abstract_en'].strip())
                w()

        has_overview = bool(p.get('ai_overview_zh'))
        if has_overview:
            w("#### 🤖 AI 概述")
            w()
            overview_zh = p.get('ai_overview_zh') or ''
            if overview_zh:
                len_note = f"（约 {len(overview_zh)} 字）"
                w(f"**中文结构化概述** {len_note}：")
                w()
                w(f"> {overview_zh.strip()}")
                w()

    # ---- Section 2: 更新论文 ----
    w("---")
    w(f"## 2. 🔄 更新论文 ({len(updated)})")
    w()
    if not updated:
        w("_本次运行没有检测到论文字段更新。_")
    else:
        w("| # | Paper ID | 标题 | 变更字段 |")
        w("|---|---|---|---|")
        for i, u in enumerate(updated, 1):
            changes_str = '；'.join(
                f"{c['field']}: {c.get('before') or c.get('before_len', '?')} → {c.get('after') or c.get('after_len', '?')}"
                if c.get('field') in ('likes', 'views', 'comment_count') else
                f"{c['field']} 变更"
                for c in u['changes']
            )
            title_s = (u.get('title_zh') or u.get('title_en') or u.get('title') or '')[:60]
            pid_link = f"`{u['paper_id']}`"
            w(f"| {i} | {pid_link} | {title_s} | {changes_str} |")
        w()

    # ---- Section 2B: Translation Supplements ----
    w("---")
    total_tr = len(translation_updates)
    w(f"## 2B. 🌐 翻译补充 / AI Overview 总结补充 ({total_tr} 篇论文 + {len(comment_translations)} 条评论)")
    w()
    if not translation_updates and not comment_translations:
        w("_本次运行未补充翻译（所有字段均已翻译或 LLM 未返回该部分）。_")
    else:
        if translation_updates:
            w("### 📝 论文翻译补充")
            w()
            for i, tr in enumerate(translation_updates, 1):
                title_s = tr.get('title_zh') or tr.get('title_en') or tr['paper_id']
                w(f"#### {i}. {title_s}")
                w()
                w(f"- Paper ID：`{tr['paper_id']}`")
                fs = tr.get('fields') or {}
                for fkey in ('title_zh', 'abstract_zh', 'ai_overview_zh'):
                    if fkey not in fs:
                        continue
                    v = fs[fkey]
                    label_map = {
                        'title_zh': '标题翻译',
                        'abstract_zh': '摘要翻译',
                        'ai_overview_zh': 'AI Overview 结构化概述',
                    }
                    len_before = v.get('before_len') or 0
                    len_after = v.get('after_len') or 0
                    note = f"（{len_before} → {len_after} 字符）" if len_before else f"（新补充，约 {len_after} 字）"
                    w(f"- **{label_map.get(fkey, fkey)}** {note}：")
                    w()
                    c.execute(f"SELECT {fkey} FROM papers WHERE paper_id=?", (tr['paper_id'],))
                    row = c.fetchone()
                    content_to_show = (row and row[0]) or v.get('preview') or ''
                    if fkey == 'title_zh':
                        w(f"> {content_to_show.strip()}")
                        w()
                    else:
                        for para in str(content_to_show).split('\n'):
                            ps = para.strip()
                            if ps:
                                w(f"> {ps}")
                        w()
            w()

        if comment_translations:
            by_p = {}
            for ct in comment_translations:
                by_p.setdefault(ct['paper_id'], []).append(ct)
            w("### 💬 评论翻译补充")
            w()
            for pid, cts in by_p.items():
                c.execute("SELECT title_en, title_zh FROM papers WHERE paper_id=?", (pid,))
                prow = c.fetchone()
                ptitle = (prow and (prow['title_zh'] or prow['title_en'])) or pid
                w(f"#### 📄 {ptitle}")
                w()
                w(f"- Paper ID：`{pid}`  ·  本次补充翻译评论：**{len(cts)}** 条")
                w()
                for j, ct in enumerate(cts, 1):
                    author = ct.get('author') or '匿名用户'
                    w(f"##### {j}. {author} （评论 #{ct['comment_id']}，约 {ct['after_len']} 字）")
                    w()
                    c.execute("SELECT content_en, content_zh FROM comments WHERE id=?", (ct['comment_id'],))
                    cr = c.fetchone()
                    zh = (cr and cr['content_zh']) or ct.get('preview_zh') or ''
                    en = (cr and cr['content_en']) or ct.get('preview_en') or ''
                    if zh:
                        w("**中文翻译**：")
                        w()
                        for para in str(zh).split('\n'):
                            ps = para.strip()
                            if ps:
                                w(f"> {ps}")
                        w()
                    if en and len(en) <= 800:
                        w("<details><summary>展开原文 (English)</summary>")
                        w()
                        for para in str(en).split('\n'):
                            ps = para.strip()
                            if ps:
                                w(f"> {ps}")
                        w()
                        w("</details>")
                        w()
            w()

    # ---- Section 3: 新/更新评论 ----
    w("---")
    w(f"## 3. 💬 新评论 & 更新评论 ({len(new_comments)})")
    w()
    if not new_comments:
        w("_本次运行没有新评论或评论更新。_")
    for pid, cs in comments_by_paper.items():
        c.execute("SELECT title_en, title_zh FROM papers WHERE paper_id=?", (pid,))
        prow = c.fetchone()
        title = (prow['title_zh'] or prow['title_en'] or pid) if prow else pid
        w(f"### 📄 {title}")
        w()
        w(f"- Paper ID：`{pid}`")
        w(f"- 本次评论数：**{len(cs)}**")
        w()
        for j, nc in enumerate(cs, 1):
            tag = "更新" if nc.get('updated') else "新增"
            author = nc.get('author') or '匿名用户'
            time_s = str(nc.get('published_at') or '')[:24] or '未知时间'
            w(f"#### [{tag}] {j}. {author} · {time_s}")
            w()
            zh_s = nc.get('content_zh')
            if not zh_s and nc.get('translated') and nc.get('comment_db_id'):
                c.execute("SELECT content_zh FROM comments WHERE id=?", (nc['comment_db_id'],))
                r = c.fetchone()
                if r and r['content_zh']:
                    zh_s = r['content_zh'].strip()
            if zh_s:
                w("**中文翻译**：")
                w()
                w(zh_s)
                w()
            if nc.get('content_en'):
                w("**原文**：")
                w()
                w(nc['content_en'].strip())
                w()

    # ---- Section 4: 点赞排行 Top 20 ----
    w("---")
    w(f"## 4. 🏆 点赞数排行 Top 20（对比上次排位）")
    w()
    w("| 排名 | 变动 | 上次排名 | 点赞数 | Paper ID | 标题 |")
    w("|---:|---|---:|---:|---|---|")
    for rc in rank_changes:
        if rc['delta'] is None:
            delta_str = '🆕 NEW'
        elif rc['delta'] > 0:
            delta_str = f'⬆️ +{rc["delta"]}'
        elif rc['delta'] < 0:
            delta_str = f'⬇️ {rc["delta"]}'
        else:
            delta_str = '➖ ='
        prev = str(rc['prev_rank']) if rc['prev_rank'] else '-'
        title_s = (rc.get('title_zh') or rc.get('title') or '')[:70]
        w(f"| {rc['rank']} | {delta_str} | {prev} | {rc['likes']} | `{rc['paper_id']}` | {title_s} |")
    w()

    # Summary
    summary_parts = [
        f"新增论文 {len(new_papers_full)} 篇",
        f"更新论文 {len(updated)} 篇",
        f"新/更新评论 {len(new_comments)} 条",
    ]
    if translation_updates:
        n_title = sum(1 for t in translation_updates if 'title_zh' in t['fields'])
        n_abs = sum(1 for t in translation_updates if 'abstract_zh' in t['fields'])
        n_ov = sum(1 for t in translation_updates if 'ai_overview_zh' in t['fields'])
        trans_items = []
        if n_title: trans_items.append(f"标题翻译补充 {n_title} 篇")
        if n_abs: trans_items.append(f"摘要翻译补充 {n_abs} 篇")
        if n_ov: trans_items.append(f"AI Overview 结构化概述 {n_ov} 篇")
        if trans_items:
            summary_parts.append("；".join(trans_items))
    if comment_translations:
        summary_parts.append(f"评论翻译补充 {len(comment_translations)} 条")
    summary = '；'.join(summary_parts)
    w("---")
    w(f"## 📊 本次运行总结")
    w()
    w(f"> **{summary}**")
    w()

    report_text = buf.getvalue()
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_text)

    # Also persist JSON report + run row
    json_report = {
        'run_time': now_iso,
        'args': {
            'model': args.model,
            'after': args.after,
            'before': args.before,
            'dir': args.dir,
        },
        'new_papers': new_papers_full,
        'updated_papers': updated,
        'new_comments': new_comments,
        'translation_updates': translation_updates,
        'comment_translations': comment_translations,
        'ranking_top20': rank_changes,
        'summary': summary,
    }
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(json_report, f, ensure_ascii=False, indent=2)

    c.execute("""
        INSERT INTO runs (run_time, new_papers_json, updated_papers_json, new_comments_json,
                          ranking_before_json, ranking_after_json, summary)
        VALUES (?,?,?,?,?,?,?)
    """, (
        now_iso,
        json.dumps(new_paper_ids, ensure_ascii=False),
        json.dumps(updated, ensure_ascii=False),
        json.dumps(new_comments, ensure_ascii=False, default=str),
        json.dumps(ranking_before, ensure_ascii=False),
        json.dumps(ranking_after, ensure_ascii=False),
        summary,
    ))
    conn.commit()

    # Print a brief console summary (don't dump full MD to keep output readable)
    print()
    print("=" * 80)
    print(f"PaperGrep 运行完成 — {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print(f"  🆕 新增论文       {len(new_papers_full)} 篇")
    print(f"  🔄 更新论文       {len(updated)} 篇")
    print(f"  💬 新/更新评论    {len(new_comments)} 条")
    print(f"  🏆 点赞榜首位     {rank_changes[0]['paper_id'] if rank_changes else '-'}  ({rank_changes[0]['likes'] if rank_changes else 0} 赞)")
    if translation_updates:
        n_t = sum(1 for t in translation_updates if 'title_zh' in t['fields'])
        n_a = sum(1 for t in translation_updates if 'abstract_zh' in t['fields'])
        n_o = sum(1 for t in translation_updates if 'ai_overview_zh' in t['fields'])
        parts = []
        if n_t: parts.append(f"标题翻译 {n_t}")
        if n_a: parts.append(f"摘要翻译 {n_a}")
        if n_o: parts.append(f"AI Overview 中文概述 {n_o}")
        if parts:
            print(f"  🌐 论文翻译补充    {' | '.join(parts)}  篇")
    if comment_translations:
        print(f"  🌐 评论翻译补充    {len(comment_translations)} 条")
    print("=" * 80)
    print(f"  📄 Markdown 报告  {report_path}")
    print(f"  📋 JSON 数据      {json_path}")
    print(f"  🗄️  数据库        {DB_PATH}")
    print("=" * 80)
    print()
    print("[INFO] [7/7] 完成 ✅")
    return report_path, json_path


# ============================================================
# Main
# ============================================================

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog='PaperGrep.py',
        description='Fetch, persist, translate, and report papers from alphaXiv.org',
    )
    p.add_argument('--after', type=str, default=None,
                   help='Only include papers published on/after this datetime. '
                        'Formats: YYYY-MM-DD or "YYYY-MM-DD HH:MM"')
    p.add_argument('--before', type=str, default=None,
                   help='Only include papers published on/before this datetime')
    p.add_argument('--model', type=str, default=os.environ.get('PAPERGREP_MODEL', 'qwen-plus'),
                   help='LLM model name for translation (default: qwen-plus via DashScope)')
    p.add_argument('--protocol', type=str, default=os.environ.get('PAPERGREP_PROTOCOL', 'json'),
                   choices=['json', 'xml'],
                   help='LLM output protocol: "json" (default, strict top-level JSON) or '
                        '"xml" (XML-style <item_N>...</item_N> delimiters, more robust against '
                        'JSON escape / quote issues in translated text)')
    p.add_argument('--llm-verbose', action='store_true',
                   default=(os.environ.get('PAPERGREP_LLM_VERBOSE') or '').strip().lower() in ('1', 'true', 'yes', 'on'),
                   help='Enable verbose DEBUG-level logs for LLM calls, prompt assembly, raw responses, '
                        'and per-item translation results. Default off (only show INFO/WARN).')
    p.add_argument('--dir', type=str, default=None,
                   help='Directory to scan for .webarchive files (default: cache/)')
    p.add_argument('--no-details', action='store_true',
                   help='Skip per-paper detail page fetch (AI overview / comments / full abstract)')
    p.add_argument('--db', type=str, default=None,
                   help='Override DB path (default: db/papergrep.db)')
    p.add_argument('files', nargs='*',
                   help='Optional explicit .webarchive file(s) to process')
    return p


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    # --files take precedence over --dir
    if args.files:
        args.dir = None  # will collect files manually below

    global DB_PATH
    if args.db:
        DB_PATH = os.path.abspath(args.db)

    print("=" * 80)
    print(f"PaperGrep 启动 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print(f"  DB：        {DB_PATH}")
    print(f"  报告目录：  {WORK_DIR}")
    print(f"  缓存目录：  {CACHE_DIR}")
    print(f"  临时目录：  {TRASH_DIR}")
    print(f"  模型：      {args.model}")
    print(f"  协议：      {args.protocol} (json/xml，可用 --protocol xml 改用 XML 标签分隔模式，避免 JSON 转义格式错误)")
    print(f"  时间范围：  after={args.after or '(无)'}  before={args.before or '(无)'}")
    print(f"  详情页：    {'关闭 (--no-details)' if args.no_details else '开启'}")
    print("=" * 80)
    print()

    ensure_dirs()
    init_db()
    conn = get_db()

    after_dt = parse_time_filter(args.after)
    before_dt = parse_time_filter(args.before, end_of_day=True)

    if args.after:
        print(f"[INFO] --after filter: {args.after} -> {after_dt}")
    if args.before:
        print(f"[INFO] --before filter: {args.before} -> {before_dt}")
    if after_dt and before_dt and after_dt > before_dt:
        print("[ERROR] --after must not be later than --before")
        sys.exit(2)

    if not args.files and not args.dir:
        print("[ERROR] No webarchive source specified. Please provide --dir <dir> or explicit .webarchive file(s) as positional arguments.")
        sys.exit(1)

    # Collect papers
    print("[INFO] [1/7] 收集 webarchive 与网页源 …")
    class SourceArgs:
        pass
    sa = SourceArgs()
    sa.dir = args.dir
    papers = collect_papers_from_source(sa)
    # If explicit files, also process them
    if args.files:
        print(f"[INFO]   追加显式指定的 {len(args.files)} 个 webarchive 文件 …")
        for fp in args.files:
            if not os.path.exists(fp):
                print(f"[WARN] File not found: {fp}")
                continue
            html = extract_html_from_webarchive(fp)
            if html:
                ps = extract_all_papers(html)
                seen = {p['paper_id'] for p in papers}
                added = 0
                for p in ps:
                    if p['paper_id'] not in seen:
                        papers.append(p)
                        seen.add(p['paper_id'])
                        added += 1
                print(f"[INFO]     {fp}: 提取 {len(ps)} 篇，新增 {added} 篇去重后）")
    print(f"[INFO] [2/7] 解析 JSON-LD + DOM：共提取 {len(papers)} 篇论文元数据")

    # Time filter
    before_cnt = len(papers)
    if after_dt or before_dt:
        papers = [p for p in papers if paper_in_range(p['published_date'], after_dt, before_dt)]
        print(f"[INFO] [3/7] 时间过滤：保留 {len(papers)}/{before_cnt} 篇论文")
    else:
        print(f"[INFO] [3/7] 无时间过滤：保留全部 {before_cnt} 篇论文")

    if not papers:
        print("[WARN] No papers to process. Exiting.")
        return

    # Set model env for translator
    if args.model:
        os.environ['PAPERGREP_MODEL'] = args.model

    translator = LLMTranslator(model_name=args.model, protocol=args.protocol, verbose=args.llm_verbose)
    route = 'DashScope 原生 SDK (qwen-plus)' if translator.model == 'qwen-plus' else f'OpenAI 兼容接口 ({translator.model})'
    print(f"[INFO] LLM translator：{route}，可用={translator.available()}，协议={translator.protocol!r}，DEBUG 日志={'开启' if translator.verbose else '关闭'}")

    new_ids, updated, new_comments, rank_before, rank_after, trans_updates, comment_trans = sync_papers(
        conn, papers, translator, fetch_details=(not args.no_details)
    )
    build_and_save_report(conn, new_ids, updated, new_comments, rank_before, rank_after,
                          trans_updates, comment_trans, args)
    conn.close()


if __name__ == '__main__':
    main()
