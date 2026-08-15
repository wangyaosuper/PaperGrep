#!/usr/bin/env python3

import sys
import os
import sqlite3
import json
import argparse
import webbrowser
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "db")
DEFAULT_DB_PATH = os.path.join(DB_DIR, "papergrep.db")

PAGE_SIZE = 10


def get_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def clear_screen():
    os.system('clear' if os.name == 'posix' else 'cls')


def print_separator(char='=', width=78):
    print(char * width)


def print_header(title):
    print_separator()
    print(f"  {title}")
    print_separator()


def safe_str(s, max_len=None):
    if s is None:
        return ""
    s = str(s)
    if max_len and len(s) > max_len:
        return s[:max_len - 3] + "..."
    return s


def wrap_text(text, width=76, indent=0):
    if not text:
        return ""
    lines = []
    for paragraph in str(text).split('\n'):
        if not paragraph.strip():
            lines.append('')
            continue
        current = ' ' * indent
        for word in paragraph.split(' '):
            if len(current) + len(word) + 1 > width:
                lines.append(current.rstrip())
                current = ' ' * indent + word
            else:
                if current.strip():
                    current += ' ' + word
                else:
                    current = ' ' * indent + word
        if current.strip():
            lines.append(current.rstrip())
    return '\n'.join(lines)


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


def get_db_stats(conn):
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
    return {
        'paper_count': paper_count,
        'comment_count': comment_count,
        'run_count': run_count,
        'total_likes': total_likes,
        'total_views': total_views,
    }


def show_main_menu(conn, db_path):
    stats = get_db_stats(conn)
    while True:
        clear_screen()
        print_header("PaperGrep 数据库浏览器")
        print(f"  数据库文件: {db_path}")
        print(f"  连接时间:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print_separator('-')
        print(f"  📄 论文总数:     {stats['paper_count']}")
        print(f"  💬 评论总数:     {stats['comment_count']}")
        print(f"  🏃 运行记录数:   {stats['run_count']}")
        print(f"  👍 总点赞数:     {stats['total_likes']:,}")
        print(f"  👁  总浏览数:     {stats['total_views']:,}")
        print_separator()
        print("  请选择操作:")
        print()
        print("    [1] 浏览论文列表")
        print("    [2] 搜索论文 (标题/摘要)")
        print("    [3] 浏览评论列表")
        print("    [4] 浏览运行记录")
        print("    [5] 显示数据库 Schema")
        print()
        print("    [0] 退出")
        print_separator()
        choice = input("  请输入选项 [0-5]: ").strip()

        if choice == '1':
            browse_papers(conn)
        elif choice == '2':
            search_papers(conn)
        elif choice == '3':
            browse_comments(conn)
        elif choice == '4':
            browse_runs(conn)
        elif choice == '5':
            show_schema(conn)
        elif choice == '0' or choice.lower() == 'q' or choice.lower() == 'quit':
            print()
            print("  再见!")
            sys.exit(0)
        else:
            input(f"  无效选项 '{choice}'，按 Enter 继续...")


def browse_papers(conn):
    c = conn.cursor()
    sort_options = [
        ("likes DESC, first_seen ASC", "按点赞数降序 (默认)"),
        ("first_seen DESC", "【最新增加】按论文新增进入DB的时间降序"),
        ("last_updated DESC", "【最新更新】按内容/点赞/评论/翻译等任意更新时间降序"),
        ("views DESC, first_seen ASC", "按浏览数降序"),
        ("comment_count DESC, first_seen ASC", "按评论数降序"),
        ("published_date DESC", "按发布时间降序"),
        ("published_date ASC", "按发布时间升序"),
    ]

    while True:
        clear_screen()
        print_header("选择论文排序方式")
        for i, (_, desc) in enumerate(sort_options, 1):
            print(f"    [{i}] {desc}")
        print(f"    [0] 返回主菜单")
        print_separator()
        choice = input(f"  请选择排序方式 [0-{len(sort_options)}] (默认 1): ").strip()
        if choice == '0':
            return
        if not choice:
            choice = '1'
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(sort_options):
                order_by = sort_options[idx][0]
                break
        except ValueError:
            pass
        input("  无效选择，按 Enter 继续...")

    c.execute(f"SELECT COUNT(*) FROM papers")
    total = c.fetchone()[0]
    if total == 0:
        input("  数据库中暂无论文，按 Enter 返回...")
        return

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = 0

    while True:
        offset = page * PAGE_SIZE
        c.execute(f"""
            SELECT paper_id, url, title_en, title_zh, likes, views, comment_count,
                   published_date, authors_json, first_seen, last_updated
            FROM papers ORDER BY {order_by} LIMIT ? OFFSET ?
        """, (PAGE_SIZE, offset))
        rows = c.fetchall()

        clear_screen()
        print_header(f"论文列表 - 第 {page + 1}/{total_pages} 页 (共 {total} 篇)")
        print(f"  {'#':>4}  {'ID':<14}  {'点赞':>5} {'浏览':>6} {'评论':>4}  标题")
        print_separator('-')
        for i, row in enumerate(rows, 1):
            global_idx = offset + i
            title = safe_str(row['title_zh'] or row['title_en'] or row['paper_id'], 50)
            print(f"  {global_idx:>4}  {row['paper_id']:<14}  {row['likes']:>5} {row['views']:>6} {row['comment_count']:>4}  {title}")
        print_separator()
        print("  操作:")
        print("    [编号] 查看论文详情 (输入列表中的 # 号)")
        print("    [n] 下一页    [p] 上一页")
        print("    [g N] 跳转到第 N 页")
        print("    [s] 换排序方式    [0] 返回主菜单")
        print_separator()
        cmd = input("  请输入操作: ").strip().lower()

        if cmd == '0' or cmd == 'q':
            return
        elif cmd == 's':
            return browse_papers(conn)
        elif cmd == 'n':
            if page < total_pages - 1:
                page += 1
            else:
                input("  已是最后一页，按 Enter 继续...")
        elif cmd == 'p':
            if page > 0:
                page -= 1
            else:
                input("  已是第一页，按 Enter 继续...")
        elif cmd.startswith('g '):
            try:
                n = int(cmd.split()[1])
                if 1 <= n <= total_pages:
                    page = n - 1
                else:
                    input(f"  页码超出范围 (1-{total_pages})，按 Enter 继续...")
            except (ValueError, IndexError):
                input("  无效格式，按 Enter 继续...")
        else:
            try:
                idx = int(cmd)
                if 1 <= idx <= total:
                    target_offset = idx - 1
                    c.execute(f"""
                        SELECT paper_id FROM papers ORDER BY {order_by} LIMIT 1 OFFSET ?
                    """, (target_offset,))
                    pid_row = c.fetchone()
                    if pid_row:
                        show_paper_detail(conn, pid_row['paper_id'])
                else:
                    input(f"  编号超出范围 (1-{total})，按 Enter 继续...")
            except ValueError:
                input("  无效输入，按 Enter 继续...")


def show_paper_detail(conn, paper_id):
    c = conn.cursor()
    c.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,))
    row = c.fetchone()
    if not row:
        input(f"  未找到论文 {paper_id}，按 Enter 继续...")
        return

    show_abs_en = False
    show_ov_en = False

    while True:
        clear_screen()
        print_header(f"论文详情 - {paper_id}")
        d = dict(row)

        title_zh = d.get('title_zh') or ''
        title_en = d.get('title_en') or ''
        print(f"  📌 Paper ID:     {d['paper_id']}")
        print(f"  🔗 URL:          {d['url'] or 'N/A'}")
        arxiv_url = f"https://arxiv.org/abs/{d['paper_id']}"
        print(f"  📄 ArXiv:        {arxiv_url}")
        print()

        if title_zh:
            print(f"  🇨🇳 中文标题:")
            print(wrap_text(title_zh, indent=6))
            print()
        if title_en:
            print(f"  🇬🇧 英文标题:")
            print(wrap_text(title_en, indent=6))
            print()

        print(f"  ✍️  作者:         {format_authors(d.get('authors_json'))}")
        print(f"  📅 发布时间:      {d.get('published_date') or 'N/A'}")
        print(f"  🔄 修改时间:      {d.get('modified_date') or 'N/A'}")
        print(f"  👀 首次入库:      {d.get('first_seen') or 'N/A'}")
        print(f"  🆙 最后更新:      {d.get('last_updated') or 'N/A'}")
        print()
        print(f"  👍 点赞数:        {d.get('likes', 0)}")
        print(f"  👁  浏览数:        {d.get('views', 0)}")
        print(f"  💬 评论数:        {d.get('comment_count', 0)}")
        print()

        abs_zh = d.get('abstract_zh') or ''
        abs_en = d.get('abstract_en') or ''
        if abs_zh or abs_en:
            print_separator('-')
            print("  📝 论文摘要")
            print_separator('-')
            if abs_zh:
                print("  中文摘要:")
                print(wrap_text(abs_zh, indent=4))
                print()
            if abs_en:
                if show_abs_en:
                    print("  英文摘要:")
                    print(wrap_text(abs_en, indent=4))
                    print()
                else:
                    print(f"  英文摘要: (存在 {len(abs_en)} 字符，输入 [ae] 查看)")
                    print()

        ov_zh = d.get('ai_overview_zh') or ''
        ov_en = d.get('ai_overview_en') or ''
        if ov_zh or ov_en:
            print_separator('-')
            print("  🤖 AI 概述")
            print_separator('-')
            if ov_zh:
                print(f"  中文概述（约 {len(ov_zh)} 字）:")
                print(wrap_text(ov_zh, indent=4))
                print()
            if ov_en:
                if show_ov_en:
                    print("  英文概述:")
                    print(wrap_text(ov_en, indent=4))
                    print()
                else:
                    print(f"  英文概述: (存在 {len(ov_en)} 字符，输入 [oe] 查看)")
                    print()

        c.execute("SELECT COUNT(*) FROM comments WHERE paper_id = ?", (paper_id,))
        comment_total = c.fetchone()[0]
        print_separator('-')
        print(f"  💬 相关评论: {comment_total} 条")
        print_separator()
        print("  操作:")
        print("    [c] 查看/浏览评论")
        print("    [ae] 切换显示英文摘要   [oe] 切换显示英文 AI 概述")
        print("    [o] 在浏览器中打开 alphaXiv 论文页面")
        print("    [h] 显示哈希信息 (SHA1)  [t] 显示翻译状态")
        print("    [r] 打印完整原始 JSON 行")
        print("    [0] 返回列表")
        print_separator()
        cmd = input("  请输入操作: ").strip().lower()

        if cmd == '0' or cmd == 'q':
            return
        elif cmd == 'c':
            browse_comments(conn, paper_id_filter=paper_id)
        elif cmd == 'ae':
            show_abs_en = not show_abs_en
        elif cmd == 'oe':
            show_ov_en = not show_ov_en
        elif cmd == 'o':
            url = d.get('url') or f"https://www.alphaxiv.org/abs/{d['paper_id']}"
            print(f"  正在打开浏览器: {url}")
            try:
                webbrowser.open(url)
                print(f"  ✅ 已发送到浏览器")
            except Exception as e:
                print(f"  ❌ 打开失败: {e}")
            print()
            input("  按 Enter 继续...")
        elif cmd == 'h':
            print()
            print(f"  title_hash:        {d.get('title_hash') or '(空)'}")
            print(f"  abstract_hash:     {d.get('abstract_hash') or '(空)'}")
            print(f"  ai_overview_hash:  {d.get('ai_overview_hash') or '(空)'}")
            print()
            input("  按 Enter 继续...")
        elif cmd == 't':
            try:
                tf = json.loads(d.get('translated_fields') or '{}')
            except Exception:
                tf = {}
            print()
            print(f"  title_zh 已翻译:      {'✓' if tf.get('title_zh') else '-'}  (当前: {'有' if d.get('title_zh') else '无'})")
            print(f"  abstract_zh 已翻译:   {'✓' if tf.get('abstract_zh') else '-'}  (当前: {'有' if d.get('abstract_zh') else '无'})")
            print(f"  ai_overview_zh 已翻译:{'✓' if tf.get('ai_overview_zh') else '-'}  (当前: {'有' if d.get('ai_overview_zh') else '无'})")
            print()
            input("  按 Enter 继续...")
        elif cmd == 'r':
            print()
            out = {}
            for k, v in d.items():
                if isinstance(v, str) and len(v) > 100:
                    out[k] = v[:100] + f"... (len={len(v)})"
                else:
                    out[k] = v
            print(json.dumps(out, ensure_ascii=False, indent=2))
            print()
            input("  按 Enter 继续...")


def search_papers(conn):
    c = conn.cursor()
    while True:
        clear_screen()
        print_header("搜索论文")
        print("  输入关键词 (支持中文/英文，匹配标题和摘要)")
        print("  空输入返回主菜单")
        print_separator()
        keyword = input("  关键词: ").strip()
        if not keyword:
            return
        like = f"%{keyword}%"
        c.execute("""
            SELECT COUNT(*) FROM papers
            WHERE title_en LIKE ? OR title_zh LIKE ?
               OR abstract_en LIKE ? OR abstract_zh LIKE ?
               OR paper_id LIKE ?
        """, (like, like, like, like, like))
        total = c.fetchone()[0]
        if total == 0:
            input(f"  未找到匹配 '{keyword}' 的论文，按 Enter 继续...")
            continue

        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        page = 0

        while True:
            offset = page * PAGE_SIZE
            c.execute("""
                SELECT paper_id, url, title_en, title_zh, likes, views, comment_count,
                       published_date, authors_json
                FROM papers
                WHERE title_en LIKE ? OR title_zh LIKE ?
                   OR abstract_en LIKE ? OR abstract_zh LIKE ?
                   OR paper_id LIKE ?
                ORDER BY likes DESC LIMIT ? OFFSET ?
            """, (like, like, like, like, like, PAGE_SIZE, offset))
            rows = c.fetchall()

            clear_screen()
            print_header(f"搜索: '{keyword}' - 第 {page + 1}/{total_pages} 页 (共 {total} 条)")
            print(f"  {'#':>4}  {'ID':<14}  {'点赞':>5}  标题")
            print_separator('-')
            for i, row in enumerate(rows, 1):
                global_idx = offset + i
                title = safe_str(row['title_zh'] or row['title_en'] or row['paper_id'], 55)
                print(f"  {global_idx:>4}  {row['paper_id']:<14}  {row['likes']:>5}  {title}")
            print_separator()
            print("  操作: [编号] 查看详情  [n] 下一页  [p] 上一页  [g N] 跳页  [s] 重新搜索  [0] 主菜单")
            print_separator()
            cmd = input("  请输入操作: ").strip().lower()

            if cmd == '0' or cmd == 'q':
                return
            elif cmd == 's':
                break
            elif cmd == 'n':
                if page < total_pages - 1:
                    page += 1
                else:
                    input("  已是最后一页，按 Enter 继续...")
            elif cmd == 'p':
                if page > 0:
                    page -= 1
                else:
                    input("  已是第一页，按 Enter 继续...")
            elif cmd.startswith('g '):
                try:
                    n = int(cmd.split()[1])
                    if 1 <= n <= total_pages:
                        page = n - 1
                    else:
                        input(f"  页码超出范围 (1-{total_pages})，按 Enter 继续...")
                except (ValueError, IndexError):
                    input("  无效格式，按 Enter 继续...")
            else:
                try:
                    idx = int(cmd)
                    if 1 <= idx <= total:
                        target_offset = idx - 1
                        c.execute("""
                            SELECT paper_id FROM papers
                            WHERE title_en LIKE ? OR title_zh LIKE ?
                               OR abstract_en LIKE ? OR abstract_zh LIKE ?
                               OR paper_id LIKE ?
                            ORDER BY likes DESC LIMIT 1 OFFSET ?
                        """, (like, like, like, like, like, target_offset))
                        pid_row = c.fetchone()
                        if pid_row:
                            show_paper_detail(conn, pid_row['paper_id'])
                except ValueError:
                    input("  无效输入，按 Enter 继续...")


def browse_comments(conn, paper_id_filter=None):
    c = conn.cursor()
    where_clause = ""
    params = ()
    if paper_id_filter:
        where_clause = "WHERE c.paper_id = ?"
        params = (paper_id_filter,)
        c.execute(f"SELECT COUNT(*) FROM comments c {where_clause}", params)
    else:
        c.execute("SELECT COUNT(*) FROM comments")
    total = c.fetchone()[0]
    if total == 0:
        input("  暂无评论数据，按 Enter 返回...")
        return

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = 0
    order_by = "c.published_at DESC" if not paper_id_filter else "c.published_at ASC"

    while True:
        offset = page * PAGE_SIZE
        query = f"""
            SELECT c.id, c.paper_id, c.external_id, c.author_name,
                   c.content_en, c.content_zh, c.published_at,
                   c.is_updated,
                   p.title_en, p.title_zh
            FROM comments c
            LEFT JOIN papers p ON c.paper_id = p.paper_id
            {where_clause}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
        """
        c.execute(query, params + (PAGE_SIZE, offset))
        rows = c.fetchall()

        clear_screen()
        header_title = f"评论列表 - 论文 {paper_id_filter}" if paper_id_filter else "评论列表"
        print_header(f"{header_title} - 第 {page + 1}/{total_pages} 页 (共 {total} 条)")
        if paper_id_filter:
            pass
        else:
            print(f"  {'#':>4}  {'ID':>5}  {'Paper ID':<14}  作者 / 内容预览")
        print_separator('-')
        for i, row in enumerate(rows, 1):
            global_idx = offset + i
            author = safe_str(row['author_name'] or '(匿名)', 12)
            title = safe_str(row['title_zh'] or row['title_en'] or row['paper_id'], 25)
            preview = safe_str(row['content_zh'] or row['content_en'] or '', 35)
            if paper_id_filter:
                date = safe_str((row['published_at'] or '')[:16], 16)
                updated_flag = " [UPD]" if row['is_updated'] else ""
                print(f"  {global_idx:>4}  {date:<16}  {author:<12}{updated_flag}")
                print(f"        {preview}")
            else:
                print(f"  {global_idx:>4}  {row['id']:>5}  {row['paper_id']:<14}  {author:<12} | {title}")
                print(f"        {preview}")
        print_separator()
        print("  操作: [编号] 查看详情  [n] 下一页  [p] 上一页  [g N] 跳页  [0] 返回")
        print_separator()
        cmd = input("  请输入操作: ").strip().lower()

        if cmd == '0' or cmd == 'q':
            return
        elif cmd == 'n':
            if page < total_pages - 1:
                page += 1
            else:
                input("  已是最后一页，按 Enter 继续...")
        elif cmd == 'p':
            if page > 0:
                page -= 1
            else:
                input("  已是第一页，按 Enter 继续...")
        elif cmd.startswith('g '):
            try:
                n = int(cmd.split()[1])
                if 1 <= n <= total_pages:
                    page = n - 1
                else:
                    input(f"  页码超出范围 (1-{total_pages})，按 Enter 继续...")
            except (ValueError, IndexError):
                input("  无效格式，按 Enter 继续...")
        else:
            try:
                idx = int(cmd)
                if 1 <= idx <= total:
                    target_offset = idx - 1
                    query2 = f"""
                        SELECT c.id FROM comments c
                        {where_clause}
                        ORDER BY {order_by}
                        LIMIT 1 OFFSET ?
                    """
                    c.execute(query2, params + (target_offset,))
                    cid_row = c.fetchone()
                    if cid_row:
                        show_comment_detail(conn, cid_row['id'])
                else:
                    input(f"  编号超出范围 (1-{total})，按 Enter 继续...")
            except ValueError:
                input("  无效输入，按 Enter 继续...")


def show_comment_detail(conn, comment_id):
    c = conn.cursor()
    c.execute("""
        SELECT c.*, p.title_en, p.title_zh
        FROM comments c LEFT JOIN papers p ON c.paper_id = p.paper_id
        WHERE c.id = ?
    """, (comment_id,))
    row = c.fetchone()
    if not row:
        input(f"  未找到评论 id={comment_id}，按 Enter 继续...")
        return

    while True:
        clear_screen()
        d = dict(row)
        print_header(f"评论详情 - ID #{d['id']}")
        print(f"  📄 Paper ID:     {d['paper_id']}")
        title = d.get('title_zh') or d.get('title_en') or ''
        if title:
            print(f"  📌 论文标题:     {safe_str(title, 70)}")
        print(f"  👤 作者:          {d.get('author_name') or '(匿名)'}")
        print(f"  📅 发布时间:      {d.get('published_at') or 'N/A'}")
        print(f"  🔗 外部 ID:       {d.get('external_id') or 'N/A'}")
        print(f"  🔄 已更新:        {'是' if d.get('is_updated') else '否'}")
        print(f"  🧮 内容哈希:      {d.get('content_hash') or 'N/A'}")
        print()

        c_zh = d.get('content_zh') or ''
        c_en = d.get('content_en') or ''
        print_separator('-')
        if c_zh:
            print("  🇨🇳 中文内容:")
            print(wrap_text(c_zh, indent=4))
            print()
        if c_en:
            print("  🇬🇧 英文内容:")
            print(wrap_text(c_en, indent=4))
            print()
        print_separator()
        print("  操作:")
        print("    [p] 跳转查看所属论文详情")
        print("    [0] 返回列表")
        print_separator()
        cmd = input("  请输入操作: ").strip().lower()

        if cmd == '0' or cmd == 'q':
            return
        elif cmd == 'p':
            show_paper_detail(conn, d['paper_id'])


def browse_runs(conn):
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM runs")
    total = c.fetchone()[0]
    if total == 0:
        input("  暂无运行记录，按 Enter 返回...")
        return

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = 0

    while True:
        offset = page * PAGE_SIZE
        c.execute("""
            SELECT id, run_time, summary,
                   json_array_length(new_papers_json) as new_papers,
                   json_array_length(updated_papers_json) as updated_papers,
                   json_array_length(new_comments_json) as new_comments
            FROM runs ORDER BY id DESC LIMIT ? OFFSET ?
        """, (PAGE_SIZE, offset))
        rows = c.fetchall()

        clear_screen()
        print_header(f"运行记录 - 第 {page + 1}/{total_pages} 页 (共 {total} 条)")
        print(f"  {'#':>4}  {'ID':>4}  {'运行时间':<20}  {'新增':>5} {'更新':>5} {'新评':>5}  摘要")
        print_separator('-')
        for i, row in enumerate(rows, 1):
            global_idx = offset + i
            run_time = safe_str((row['run_time'] or '')[:19], 19)
            summary = safe_str(row['summary'] or '(无摘要)', 40)
            print(f"  {global_idx:>4}  {row['id']:>4}  {run_time:<20}  {row['new_papers']:>5} {row['updated_papers']:>5} {row['new_comments']:>5}  {summary}")
        print_separator()
        print("  操作: [编号] 查看详情  [n] 下一页  [p] 上一页  [g N] 跳页  [0] 返回")
        print_separator()
        cmd = input("  请输入操作: ").strip().lower()

        if cmd == '0' or cmd == 'q':
            return
        elif cmd == 'n':
            if page < total_pages - 1:
                page += 1
            else:
                input("  已是最后一页，按 Enter 继续...")
        elif cmd == 'p':
            if page > 0:
                page -= 1
            else:
                input("  已是第一页，按 Enter 继续...")
        elif cmd.startswith('g '):
            try:
                n = int(cmd.split()[1])
                if 1 <= n <= total_pages:
                    page = n - 1
                else:
                    input(f"  页码超出范围 (1-{total_pages})，按 Enter 继续...")
            except (ValueError, IndexError):
                input("  无效格式，按 Enter 继续...")
        else:
            try:
                idx = int(cmd)
                if 1 <= idx <= total:
                    target_offset = idx - 1
                    c.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1 OFFSET ?", (target_offset,))
                    rid_row = c.fetchone()
                    if rid_row:
                        show_run_detail(conn, rid_row['id'])
                else:
                    input(f"  编号超出范围 (1-{total})，按 Enter 继续...")
            except ValueError:
                input("  无效输入，按 Enter 继续...")


def show_run_detail(conn, run_id):
    c = conn.cursor()
    c.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
    row = c.fetchone()
    if not row:
        input(f"  未找到运行记录 id={run_id}，按 Enter 继续...")
        return

    while True:
        clear_screen()
        d = dict(row)
        print_header(f"运行记录详情 - ID #{d['id']}")
        print(f"  🕒 运行时间:      {d.get('run_time') or 'N/A'}")
        print()
        try:
            new_papers = json.loads(d.get('new_papers_json') or '[]')
            updated = json.loads(d.get('updated_papers_json') or '[]')
            new_comments = json.loads(d.get('new_comments_json') or '[]')
            rank_before = json.loads(d.get('ranking_before_json') or '[]')
            rank_after = json.loads(d.get('ranking_after_json') or '[]')
        except Exception:
            new_papers, updated, new_comments, rank_before, rank_after = [], [], [], [], []

        print(f"  🆕 新增论文:      {len(new_papers)} 篇")
        print(f"  🔄 更新论文:      {len(updated)} 篇")
        print(f"  💬 新/更新评论:   {len(new_comments)} 条")
        print(f"  📊 排名变化对:    {len(rank_before)} → {len(rank_after)}")
        print()

        if d.get('summary'):
            print_separator('-')
            print("  📋 运行摘要:")
            print(wrap_text(d['summary'], indent=4))
            print()

        print_separator()
        print("  操作:")
        print("    [1] 列出新增论文 ID")
        print("    [2] 列出更新论文及字段变更")
        print("    [3] 列出新增/更新评论")
        print("    [4] 对比点赞排名 (TOP 20 前后变化)")
        print("    [0] 返回列表")
        print_separator()
        cmd = input("  请输入操作: ").strip().lower()

        if cmd == '0' or cmd == 'q':
            return
        elif cmd == '1':
            print()
            if new_papers:
                for pid in new_papers:
                    c.execute("SELECT paper_id, title_en, title_zh, likes FROM papers WHERE paper_id = ?", (pid,))
                    pr = c.fetchone()
                    if pr:
                        title = safe_str(pr['title_zh'] or pr['title_en'] or pid, 60)
                        print(f"    {pid:<14}  👍{pr['likes']:>5}  {title}")
                    else:
                        print(f"    {pid:<14}  (已不在DB)")
            else:
                print("    本次无新增论文")
            print()
            input("  按 Enter 继续...")
        elif cmd == '2':
            print()
            if updated:
                for u in updated:
                    pid = u.get('paper_id', '?')
                    title = safe_str(u.get('title_zh') or u.get('title_en') or u.get('title', ''), 50)
                    changes = u.get('changes', [])
                    change_desc = ", ".join(
                        f"{ch.get('field','?')}" + (
                            f":{ch.get('before','')}→{ch.get('after','')}"
                            if 'before' in ch and 'after' in ch else
                            (f" ({ch.get('before_len',0)}→{ch.get('after_len',0)} chars)"
                             if 'before_len' in ch else '')
                        )
                        for ch in changes[:5]
                    )
                    print(f"    {pid:<14}  {title}")
                    print(f"      变更: {change_desc}")
            else:
                print("    本次无更新论文")
            print()
            input("  按 Enter 继续...")
        elif cmd == '3':
            print()
            if new_comments:
                for i, nc in enumerate(new_comments[:50], 1):
                    pid = nc.get('paper_id', '?')
                    author = safe_str(nc.get('author', ''), 12)
                    content = safe_str(nc.get('content_zh') or nc.get('content_en', ''), 50)
                    flag = " [UPD]" if nc.get('updated') else ""
                    print(f"    {i:>3}. {pid:<14} {author:<12}{flag}  {content}")
                if len(new_comments) > 50:
                    print(f"    ... 还有 {len(new_comments) - 50} 条省略")
            else:
                print("    本次无新/更新评论")
            print()
            input("  按 Enter 继续...")
        elif cmd == '4':
            print()
            pos_before = {r.get('paper_id'): i + 1 for i, r in enumerate(rank_before)}
            pos_after = {r.get('paper_id'): i + 1 for i, r in enumerate(rank_after)}
            print(f"    {'#':>3}  {'ID':<14}  {'点赞':>5}  {'之前':>5}  {'变化':>6}  标题")
            print_separator('-')
            for i, r in enumerate(rank_after[:20], 1):
                pid = r.get('paper_id', '')
                after_pos = i
                before_pos = pos_before.get(pid)
                likes = r.get('likes', 0)
                title = safe_str(r.get('title_zh') or r.get('title') or '', 35)
                if before_pos is None:
                    delta_str = "  NEW"
                else:
                    delta = before_pos - after_pos
                    if delta > 0:
                        delta_str = f"  +{delta}"
                    elif delta < 0:
                        delta_str = f"  {delta}"
                    else:
                        delta_str = "   ="
                print(f"    {after_pos:>3}  {pid:<14}  {likes:>5}  {before_pos or '-':>5}  {delta_str:>6}  {title}")
            print()
            input("  按 Enter 继续...")


def show_schema(conn):
    c = conn.cursor()
    while True:
        clear_screen()
        print_header("数据库 Schema")
        c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in c.fetchall()]
        print(f"  共 {len(tables)} 张表: {', '.join(tables)}")
        print_separator()
        for t in tables:
            print(f"\n  📋 表: {t}")
            c.execute(f"PRAGMA table_info({t})")
            cols = c.fetchall()
            print(f"    {'字段名':<28} {'类型':<12} {'PK':>3} {'NOT NULL':>9} 默认值")
            print_separator('-')
            for col in cols:
                pk = "✓" if col[5] else ""
                notnull = "✓" if col[3] else ""
                dflt = safe_str(col[4] or '', 20)
                print(f"    {col[1]:<28} {col[2] or 'ANY':<12} {pk:>3} {notnull:>9} {dflt}")
            c.execute(f"PRAGMA index_list({t})")
            indices = c.fetchall()
            if indices:
                print(f"    索引:")
                for idx in indices:
                    unique = " [UNIQUE]" if idx[2] else ""
                    print(f"      - {idx[1]}{unique}")
        print()
        print_separator()
        c.execute("SELECT sql FROM sqlite_master WHERE type IN ('table','index','view') ORDER BY name")
        print("  原始 DDL 语句:")
        print_separator('-')
        for r in c.fetchall():
            if r[0]:
                print(wrap_text(r[0], indent=4, width=74))
                print()
        print_separator()
        input("  按 Enter 返回主菜单...")
        return


def main():
    parser = argparse.ArgumentParser(
        description="PaperGrep 数据库交互式浏览器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=str,
        default=DEFAULT_DB_PATH,
        help=f"数据库文件路径 (默认: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    db_path = args.db
    if not os.path.exists(db_path):
        print(f"[ERROR] 数据库文件不存在: {db_path}")
        print(f"        请先运行 PaperGrep.py 生成数据库，或使用 --db 指定正确路径。")
        sys.exit(1)

    try:
        conn = get_db(db_path)
    except Exception as e:
        print(f"[ERROR] 连接数据库失败: {e}")
        sys.exit(1)

    try:
        show_main_menu(conn, db_path)
    except KeyboardInterrupt:
        print()
        print("  用户中断，退出。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
