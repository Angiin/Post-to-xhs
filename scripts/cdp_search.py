"""
CDP-based Xiaohongshu search.

Connects to a Chrome instance via Chrome DevTools Protocol to search
notes on Xiaohongshu (RED) and extract structured feed results from the DOM.

CLI usage:
    python cdp_search.py search --keyword "关键词"
    python cdp_search.py search --keyword "关键词" --tab video
    python cdp_search.py search --keyword "关键词" --sort-by 最新
    python cdp_search.py search --keyword "关键词" --note-type 视频 --publish-time 一周内
    python cdp_search.py search --keyword "关键词" --sort-by 最多点赞 --limit 5

Library usage:
    from cdp_search import XiaohongshuSearcher

    searcher = XiaohongshuSearcher()
    searcher.connect()
    feeds = searcher.search("关键词", tab="all", filter_option=FilterOption(sort_by="最新"))
    searcher.disconnect()
"""

import json
import os
import re
import sys
import time
from typing import Any
from urllib.parse import urlencode, parse_qs, urlparse

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Add scripts dir to path so sibling modules can be imported
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from cdp_publish import XiaohongshuPublisher, CDPError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

XHS_SEARCH_URL = "https://www.xiaohongshu.com/search_result"

SEARCH_PAGE_LOAD_WAIT = 5  # seconds to wait after navigating to search page
FILTER_APPLY_WAIT = 3  # seconds to wait after applying filters
TAB_SWITCH_WAIT = 3  # seconds to wait after switching tab
DOM_SETTLE_WAIT = 2  # seconds to wait for DOM to settle
INITIAL_STATE_TIMEOUT = 15  # max seconds to wait for __INITIAL_STATE__
LOGIN_MODAL_CHECK_WAIT = 2  # seconds to wait before checking for login modal

DEFAULT_LIMIT = 20  # default number of results to return


# ---------------------------------------------------------------------------
# Channel tabs
# ---------------------------------------------------------------------------

CHANNEL_TABS = {
    "all": "全部",
    "image": "图文",
    "video": "视频",
    "user": "用户",
}


# ---------------------------------------------------------------------------
# Filter options
# ---------------------------------------------------------------------------

class FilterOption:
    """Search filter options for Xiaohongshu.

    All fields are optional. Only non-empty values will be applied.
    """

    SORT_BY_VALUES = ("综合", "最新", "最多点赞", "最多评论", "最多收藏")
    NOTE_TYPE_VALUES = ("不限", "视频", "图文")
    PUBLISH_TIME_VALUES = ("不限", "一天内", "一周内", "半年内")
    SEARCH_SCOPE_VALUES = ("不限", "已看过", "未看过", "已关注")
    LOCATION_VALUES = ("不限", "同城", "附近")

    def __init__(
        self,
        sort_by: str = "",
        note_type: str = "",
        publish_time: str = "",
        search_scope: str = "",
        location: str = "",
    ):
        self.sort_by = sort_by
        self.note_type = note_type
        self.publish_time = publish_time
        self.search_scope = search_scope
        self.location = location

    def is_empty(self) -> bool:
        return not any([
            self.sort_by, self.note_type, self.publish_time,
            self.search_scope, self.location,
        ])


# Filter group labels (0-indexed, matching DOM .filters order)
_FILTER_GROUPS = [
    {"name": "排序依据", "values": ("综合", "最新", "最多点赞", "最多评论", "最多收藏")},
    {"name": "笔记类型", "values": ("不限", "视频", "图文")},
    {"name": "发布时间", "values": ("不限", "一天内", "一周内", "半年内")},
    {"name": "搜索范围", "values": ("不限", "已看过", "未看过", "已关注")},
    {"name": "位置距离", "values": ("不限", "同城", "附近")},
]


def _build_filter_clicks(option: FilterOption) -> list[tuple[int, int, str]]:
    """Convert a FilterOption to a list of (group_index, tag_index, text).

    group_index is 0-based (nth-child(N+1) in CSS).
    tag_index is 0-based within the .tag-container children (nth-child(N+1)).
    """
    fields = [
        (0, option.sort_by),
        (1, option.note_type),
        (2, option.publish_time),
        (3, option.search_scope),
        (4, option.location),
    ]
    result: list[tuple[int, int, str]] = []
    for group_idx, value in fields:
        if not value:
            continue
        group = _FILTER_GROUPS[group_idx]
        if value not in group["values"]:
            valid = ", ".join(group["values"])
            raise ValueError(
                f"{group['name']}: '{value}' 无效，可选值: {valid}"
            )
        tag_idx = group["values"].index(value)
        result.append((group_idx, tag_idx, value))
    return result


# ---------------------------------------------------------------------------
# DOM extraction helpers
# ---------------------------------------------------------------------------

def _parse_note_href(href: str) -> tuple[str, str]:
    """Extract note ID and xsec_token from a search result link href.

    href format: /search_result/{ID}?xsec_token={TOKEN}&xsec_source=...
    Returns (note_id, xsec_token).
    """
    # Extract ID from path
    match = re.search(r"/search_result/([a-f0-9]+)", href)
    note_id = match.group(1) if match else ""

    # Extract xsec_token from query
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    xsec_token = params.get("xsec_token", [""])[0]

    return note_id, xsec_token


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------

class XiaohongshuSearcher:
    """Search Xiaohongshu notes via CDP."""

    def __init__(self, publisher: XiaohongshuPublisher | None = None):
        """Create a searcher, optionally reusing an existing publisher connection."""
        if publisher is not None:
            self._publisher = publisher
            self._owns_publisher = False
        else:
            self._publisher = XiaohongshuPublisher()
            self._owns_publisher = True

    def connect(self):
        """Connect to Chrome, reusing an existing tab (no new tab created)."""
        if self._owns_publisher:
            self._publisher.connect(create_new=False)

    def disconnect(self):
        """Disconnect (only if we created our own publisher)."""
        if self._owns_publisher:
            self._publisher.disconnect()

    def _evaluate(self, expression: str) -> Any:
        return self._publisher._evaluate(expression)

    def _navigate(self, url: str):
        self._publisher._navigate(url)

    # -- Login handling ----------------------------------------------------

    def _check_and_close_login_modal(self) -> bool:
        """Check for login modal and close it if present.

        Returns True if a login modal was found (and closed or attempted to close).
        """
        has_modal = self._evaluate(
            "!!document.querySelector("
            "'div.login-modal, div.reds-modal.login-modal, "
            "[class*=\"login-modal\"]')"
        )
        if not has_modal:
            return False

        print("[search] Login modal detected, closing...", file=sys.stderr)
        self._evaluate("""
            (function() {
                var closeBtn = document.querySelector(
                    'div.login-modal .close-button, '
                    + 'div.login-modal .icon-btn-wrapper.close-button, '
                    + 'div.reds-modal .close-button, '
                    + 'div.login-modal [class*="close"]'
                );
                if (closeBtn) {
                    closeBtn.click();
                    return true;
                }
                var mask = document.querySelector('i.reds-mask');
                if (mask) {
                    mask.click();
                    return true;
                }
                return false;
            })();
        """)
        time.sleep(1)
        return True

    def _check_login_via_cookie(self) -> bool:
        """Check if the user is logged in by looking for auth cookies.

        Uses CDP Network.getCookies since web_session is httpOnly.
        """
        result = self._publisher._send(
            "Network.getCookies",
            {"urls": ["https://www.xiaohongshu.com"]},
        )
        cookies = result.get("cookies", [])
        return any(c.get("name") == "web_session" for c in cookies)

    # -- Tab handling ------------------------------------------------------

    def _switch_tab(self, tab: str):
        """Click a channel tab to switch search type.

        Args:
            tab: One of "all", "image", "video", "user".
        """
        if tab not in CHANNEL_TABS:
            valid = ", ".join(CHANNEL_TABS.keys())
            raise ValueError(f"Invalid tab '{tab}', valid: {valid}")

        label = CHANNEL_TABS[tab]
        print(f"[search] Switching to tab: {label} ({tab})", file=sys.stderr)

        clicked = self._evaluate(f"""
            (function() {{
                var el = document.querySelector('#channel-container #{tab}.channel');
                if (el) {{
                    el.click();
                    return true;
                }}
                return false;
            }})();
        """)
        if not clicked:
            print(f"[search] Warning: Could not click tab '{tab}'", file=sys.stderr)
            return

        time.sleep(TAB_SWITCH_WAIT)

    # -- Filter handling ---------------------------------------------------

    def _apply_filters(self, filter_clicks: list[tuple[int, int, str]]):
        """Apply filter selections on the search page.

        Args:
            filter_clicks: List of (group_index, tag_index, text) from _build_filter_clicks.
        """
        if not filter_clicks:
            return

        print("[search] Opening filter panel...", file=sys.stderr)

        # Hover over the filter button to reveal the filter panel
        self._evaluate("""
            (function() {
                var filterBtn = document.querySelector('div.filter');
                if (filterBtn) {
                    filterBtn.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
                    filterBtn.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                }
            })();
        """)
        time.sleep(1)

        # Wait for filter panel
        panel_ready = False
        for _ in range(5):
            panel_ready = self._evaluate(
                "!!document.querySelector('div.filter-panel')"
            )
            if panel_ready:
                break
            time.sleep(1)

        if not panel_ready:
            print("[search] Warning: Filter panel did not appear.", file=sys.stderr)
            return

        # Click each filter tag by matching text content
        for group_idx, tag_idx, text in filter_clicks:
            group_name = _FILTER_GROUPS[group_idx]["name"]
            print(f"[search]   {group_name}: {text}", file=sys.stderr)

            # Use nth-child selectors based on actual DOM structure:
            # .filters-wrapper > .filters:nth-child(N) > .tag-container > .tags:nth-child(M)
            # nth-child is 1-based
            group_nth = group_idx + 1
            tag_nth = tag_idx + 1
            selector = (
                f".filter-panel .filters-wrapper "
                f".filters:nth-child({group_nth}) "
                f".tag-container .tags:nth-child({tag_nth})"
            )
            clicked = self._evaluate(f"""
                (function() {{
                    var el = document.querySelector('{selector}');
                    if (el) {{ el.click(); return true; }}
                    return false;
                }})();
            """)
            if not clicked:
                print(f"[search]   Warning: Could not click filter '{text}' ({selector})", file=sys.stderr)
            time.sleep(0.5)

        time.sleep(FILTER_APPLY_WAIT)

    # -- DOM extraction ----------------------------------------------------

    def _wait_for_note_items(self, timeout: int = 10) -> bool:
        """Wait for note-item sections to appear in the DOM."""
        for _ in range(timeout):
            count = self._evaluate(
                "document.querySelectorAll('section.note-item').length"
            )
            if count and count > 0:
                return True
            time.sleep(1)
        return False

    def _extract_feeds_from_dom(self) -> list[dict]:
        """Extract search results from DOM note-item sections.

        Returns list of dicts with: id, xsec_token, title,
        user_nickname, publish_time.
        """
        raw = self._evaluate("""
            (function() {
                var sections = document.querySelectorAll('section.note-item');
                var results = [];
                for (var i = 0; i < sections.length; i++) {
                    var s = sections[i];

                    // Extract href from cover link
                    var coverLink = s.querySelector('a.cover');
                    var href = coverLink ? coverLink.getAttribute('href') : '';

                    // Skip if no valid search_result href
                    if (href.indexOf('/search_result/') === -1) continue;

                    // Title
                    var titleEl = s.querySelector('.footer .title span');
                    var title = titleEl ? titleEl.textContent.trim() : '';

                    // Author nickname
                    var nameEl = s.querySelector('.author .name');
                    var nickname = nameEl ? nameEl.textContent.trim() : '';

                    // Publish time
                    var timeEl = s.querySelector('.author .time');
                    var publishTime = timeEl ? timeEl.textContent.trim() : '';

                    results.push({
                        href: href,
                        title: title,
                        user_nickname: nickname,
                        publish_time: publishTime
                    });
                }
                return JSON.stringify(results);
            })();
        """)

        if not raw:
            return []

        try:
            dom_items = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[search] Warning: Failed to parse DOM data: {e}", file=sys.stderr)
            return []

        # Parse href to extract note_id and xsec_token
        feeds = []
        for item in dom_items:
            note_id, xsec_token = _parse_note_href(item["href"])
            if not note_id:
                continue
            feeds.append({
                "id": note_id,
                "xsec_token": xsec_token,
                "title": item["title"],
                "user_nickname": item["user_nickname"],
                "publish_time": item["publish_time"],
            })

        return feeds

    # -- Main search -------------------------------------------------------

    def search(
        self,
        keyword: str,
        tab: str = "all",
        filter_option: FilterOption | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict]:
        """
        Search Xiaohongshu for notes matching the keyword.

        Args:
            keyword: Search keyword.
            tab: Channel tab - "all", "image", "video", "user".
            filter_option: Optional filter settings.
            limit: Max number of results to return (0 = unlimited).

        Returns:
            List of note dicts extracted from the search results DOM.
        """
        # Build search URL
        params = urlencode({
            "keyword": keyword,
            "source": "web_explore_feed",
        })
        search_url = f"{XHS_SEARCH_URL}?{params}"

        # Check login status first
        print("[search] Checking login status...", file=sys.stderr)
        if not self._check_login_via_cookie():
            print(
                "[search] ERROR: Not logged in. "
                "Xiaohongshu requires login to use search.\n"
                "  To log in, run:  python scripts/cdp_publish.py login",
                file=sys.stderr,
            )
            return []

        print(f"[search] Searching for: {keyword}", file=sys.stderr)
        self._navigate(search_url)
        time.sleep(SEARCH_PAGE_LOAD_WAIT)

        # Check and dismiss login modal if it still appears
        time.sleep(LOGIN_MODAL_CHECK_WAIT)
        self._check_and_close_login_modal()

        # Verify we weren't redirected away from search
        current_url = self._evaluate("window.location.href")
        if "search_result" not in current_url:
            print(
                f"[search] ERROR: Redirected away from search page to: {current_url}\n"
                "  This usually means the session has expired. Please log in again:\n"
                "  python scripts/cdp_publish.py login",
                file=sys.stderr,
            )
            return []

        # Wait for note items to render
        if not self._wait_for_note_items():
            print("[search] Warning: No note items found, retrying with reload...", file=sys.stderr)
            self._evaluate("window.location.reload()")
            time.sleep(SEARCH_PAGE_LOAD_WAIT)
            self._check_and_close_login_modal()
            time.sleep(1)
            if not self._wait_for_note_items():
                print("[search] Error: Could not load search results.", file=sys.stderr)
                return []

        # Switch tab if not default
        if tab != "all":
            self._switch_tab(tab)
            # Wait for new results to render
            time.sleep(DOM_SETTLE_WAIT)

        # Apply filters if provided
        if filter_option and not filter_option.is_empty():
            filter_clicks = _build_filter_clicks(filter_option)
            self._apply_filters(filter_clicks)
            # Wait for results to update
            time.sleep(DOM_SETTLE_WAIT)

        # Extract results from DOM
        feeds = self._extract_feeds_from_dom()
        print(f"[search] Found {len(feeds)} result(s).", file=sys.stderr)

        if limit > 0:
            feeds = feeds[:limit]

        return feeds


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    from chrome_launcher import ensure_chrome

    parser = argparse.ArgumentParser(description="Xiaohongshu CDP Search")
    parser.add_argument("--headed", action="store_true",
                        help="Use headed Chrome with GUI (default is headless)")
    parser.add_argument("--account", help="Account name to use")
    sub = parser.add_subparsers(dest="command", required=True)

    # search command
    p_search = sub.add_parser("search", help="Search Xiaohongshu notes")
    p_search.add_argument("--keyword", required=True, help="Search keyword")
    p_search.add_argument("--tab", default="all",
                          choices=["all", "image", "video", "user"],
                          help="Channel tab: all|image|video|user (default: all)")
    p_search.add_argument("--sort-by", default="",
                          help="排序: 综合|最新|最多点赞|最多评论|最多收藏")
    p_search.add_argument("--note-type", default="",
                          help="类型: 不限|视频|图文")
    p_search.add_argument("--publish-time", default="",
                          help="时间: 不限|一天内|一周内|半年内")
    p_search.add_argument("--search-scope", default="",
                          help="范围: 不限|已看过|未看过|已关注")
    p_search.add_argument("--location", default="",
                          help="位置: 不限|同城|附近")
    p_search.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                          help=f"Max number of results (0 = all, default: {DEFAULT_LIMIT})")
    p_search.add_argument("--raw", action="store_true",
                          help="Output raw JSON (no summary)")

    args = parser.parse_args()

    # Redirect stdout to stderr during search so only JSON goes to stdout.
    # This suppresses [cdp_publish] / [chrome_launcher] logs from stdout.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr

    # Default is headless; use --headed for GUI (login, QR verification)
    headless = not args.headed
    account = args.account
    if not ensure_chrome(headless=headless, account=account):
        print("Error: Failed to start Chrome.", file=sys.stderr)
        sys.exit(2)

    searcher = XiaohongshuSearcher()
    try:
        searcher.connect()

        if args.command == "search":
            filter_opt = FilterOption(
                sort_by=args.sort_by,
                note_type=args.note_type,
                publish_time=args.publish_time,
                search_scope=args.search_scope,
                location=args.location,
            )

            feeds = searcher.search(
                keyword=args.keyword,
                tab=args.tab,
                filter_option=filter_opt,
                limit=args.limit,
            )

            if not feeds:
                print("No results found.", file=sys.stderr)
                logged_in = searcher._check_login_via_cookie()
                sys.exit(0 if logged_in else 1)

            # Only JSON output goes to real stdout
            print(json.dumps(feeds, ensure_ascii=False, indent=2), file=real_stdout)
            print(f"SEARCH_STATUS: FOUND_{len(feeds)}_RESULTS", file=sys.stderr)

    except (CDPError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    finally:
        sys.stdout = real_stdout
        searcher.disconnect()


if __name__ == "__main__":
    main()
