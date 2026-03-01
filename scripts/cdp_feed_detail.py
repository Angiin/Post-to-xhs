"""
CDP-based Xiaohongshu feed detail extractor.

Connects to a Chrome instance via Chrome DevTools Protocol to navigate
to a Xiaohongshu note page and extract its content, comments, and metadata.

CLI usage:
    # Basic feed detail (no comment loading)
    python cdp_feed_detail.py detail --feed-id <ID> --xsec-token <TOKEN>

    # With all comments loaded
    python cdp_feed_detail.py detail --feed-id <ID> --xsec-token <TOKEN> --load-comments

    # With comment options
    python cdp_feed_detail.py detail --feed-id <ID> --xsec-token <TOKEN> \
        --load-comments --click-more-replies --max-replies-threshold 10 \
        --max-comments 50 --scroll-speed normal

Library usage:
    from cdp_feed_detail import XiaohongshuFeedDetail

    detail = XiaohongshuFeedDetail()
    detail.connect()
    result = detail.get_feed_detail(
        feed_id="abc123",
        xsec_token="token",
        load_comments=True,
    )
    detail.disconnect()
"""

import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

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

XHS_EXPLORE_URL = "https://www.xiaohongshu.com/explore"

FEED_PAGE_LOAD_WAIT = 3  # seconds to wait after navigation
DOM_SETTLE_WAIT = 2  # seconds to wait for DOM to settle
QR_VERIFY_TIMEOUT = 120  # max seconds to wait for QR code verification
QR_VERIFY_POLL_INTERVAL = 3  # seconds between polling for QR verification
QR_RETRY_WAIT = 2  # seconds to wait before retrying after QR detection

# Comment loading constants (ported from Go feed_detail.go)
DEFAULT_MAX_ATTEMPTS = 500
STAGNANT_LIMIT = 20
MIN_SCROLL_DELTA = 10
MAX_CLICK_PER_ROUND = 3
LARGE_SCROLL_TRIGGER = 5  # stagnant count before triggering large scroll
BUTTON_CLICK_INTERVAL = 3  # attempt interval for clicking buttons
FINAL_SPRINT_PUSH_COUNT = 15

# Delay ranges (milliseconds)
HUMAN_DELAY = (300, 700)
REACTION_TIME = (300, 800)
HOVER_TIME = (100, 300)
READ_TIME = (500, 1200)
SHORT_READ = (600, 1200)
SCROLL_WAIT = (100, 200)
POST_SCROLL = (300, 500)

# Page error keywords indicating inaccessible notes
PAGE_ERROR_KEYWORDS = [
    "当前笔记暂时无法浏览",
    "该内容因违规已被删除",
    "该笔记已被删除",
    "内容不存在",
    "笔记不存在",
    "已失效",
    "私密笔记",
    "仅作者可见",
    "因用户设置，你无法查看",
    "因违规无法查看",
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CommentLoadConfig:
    """Configuration for comment loading behavior."""
    click_more_replies: bool = False
    max_replies_threshold: int = 10
    max_comment_items: int = 0
    scroll_speed: str = "normal"  # "slow", "normal", "fast"


@dataclass
class LoadStats:
    """Tracks cumulative click/skip statistics."""
    total_clicked: int = 0
    total_skipped: int = 0
    attempts: int = 0


@dataclass
class LoadState:
    """Tracks scroll and comment count state."""
    last_count: int = 0
    last_scroll_top: int = 0
    stagnant_checks: int = 0


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _sleep_random(min_ms: int, max_ms: int):
    """Sleep for a random duration between min_ms and max_ms milliseconds."""
    if max_ms <= min_ms:
        time.sleep(min_ms / 1000.0)
        return
    delay = min_ms + random.randint(0, max_ms - min_ms)
    time.sleep(delay / 1000.0)


def _get_scroll_interval(speed: str) -> float:
    """Get scroll interval in seconds based on speed setting."""
    if speed == "slow":
        return (1200 + random.randint(0, 300)) / 1000.0
    elif speed == "fast":
        return (300 + random.randint(0, 100)) / 1000.0
    else:  # normal
        return (600 + random.randint(0, 200)) / 1000.0


def _make_feed_detail_url(feed_id: str, xsec_token: str) -> str:
    """Construct the feed detail page URL."""
    return (
        f"{XHS_EXPLORE_URL}/{feed_id}"
        f"?xsec_token={xsec_token}&xsec_source=pc_feed"
    )


# ---------------------------------------------------------------------------
# Feed Detail Extractor
# ---------------------------------------------------------------------------

class XiaohongshuFeedDetail:
    """Extract feed/note detail from Xiaohongshu via CDP."""

    def __init__(self, publisher: XiaohongshuPublisher | None = None):
        """Create a feed detail extractor, optionally reusing an existing publisher."""
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

    # ------------------------------------------------------------------
    # Page accessibility check
    # ------------------------------------------------------------------

    def _check_qr_verification(self) -> bool:
        """Check if the page requires QR code verification.

        Returns True if QR verification page is detected.
        """
        result = self._evaluate("""
            (function() {
                var container = document.querySelector('.access-limit-container');
                if (!container) return false;
                var qrcode = container.querySelector('.qrcode-box, .qrcode-img');
                return !!qrcode;
            })();
        """)
        return bool(result)

    def _wait_for_qr_verification(self) -> bool:
        """Wait for the user to complete QR code verification.

        Returns True if verification completed, False if timed out.
        """
        print(
            "\n[feed_detail] QR code verification required!\n"
            "  Please scan the QR code in the Chrome browser window.\n"
            f"  Waiting up to {QR_VERIFY_TIMEOUT} seconds...\n",
            file=sys.stderr,
        )

        start = time.time()
        while time.time() - start < QR_VERIFY_TIMEOUT:
            time.sleep(QR_VERIFY_POLL_INTERVAL)

            # Check if QR page is gone (verification succeeded)
            still_qr = self._check_qr_verification()
            if not still_qr:
                # Also check if we're no longer on the access-limit page
                has_limit = self._evaluate(
                    "!!document.querySelector('.access-limit-container')"
                )
                if not has_limit:
                    print(
                        "[feed_detail] QR verification completed!",
                        file=sys.stderr,
                    )
                    # Wait for page to fully load after verification
                    time.sleep(FEED_PAGE_LOAD_WAIT)
                    return True

            elapsed = int(time.time() - start)
            if elapsed % 15 == 0 and elapsed > 0:
                print(
                    f"[feed_detail] Still waiting for QR scan... "
                    f"({elapsed}/{QR_VERIFY_TIMEOUT}s)",
                    file=sys.stderr,
                )

        print(
            "[feed_detail] QR verification timed out.",
            file=sys.stderr,
        )
        return False

    def _check_page_error(self) -> str | None:
        """Check for non-QR page errors (deleted, private, blocked, etc).

        Returns None if no error found, or an error message string.
        """
        error_text = self._evaluate("""
            (function() {
                var wrapper = document.querySelector(
                    '.access-wrapper, .error-wrapper, '
                    + '.not-found-wrapper, .blocked-wrapper'
                );
                if (!wrapper) return '';
                // Ignore if it's a QR verification page (handled separately)
                if (wrapper.closest('.access-limit-container')) return '';
                return wrapper.textContent.trim();
            })();
        """)

        if not error_text:
            return None

        for keyword in PAGE_ERROR_KEYWORDS:
            if keyword in error_text:
                return f"笔记不可访问: {keyword}"

        if error_text.strip():
            return f"笔记不可访问: {error_text[:100]}"

        return None

    def _check_page_accessible(
        self,
        feed_id: str,
        xsec_token: str,
        skip_on_verify: bool = False,
    ) -> str | None:
        """Check if the note page is accessible.

        If QR verification is detected, retries by re-navigating to the same URL.
        If QR appears again on retry:
          - skip_on_verify=True: returns error immediately (for batch mode)
          - skip_on_verify=False: waits for user to scan QR code

        Returns None if accessible, or an error message string if not.
        """
        time.sleep(0.5)

        # Check for QR code verification
        if self._check_qr_verification():
            # First attempt: retry by re-navigating to the same URL
            url = _make_feed_detail_url(feed_id, xsec_token)
            print(
                "[feed_detail] QR verification detected, "
                "retrying by re-navigating...",
                file=sys.stderr,
            )
            time.sleep(QR_RETRY_WAIT)
            self._navigate(url)
            time.sleep(FEED_PAGE_LOAD_WAIT)
            time.sleep(DOM_SETTLE_WAIT)

            # Check again after retry
            if self._check_qr_verification():
                if skip_on_verify:
                    print(
                        "[feed_detail] QR verification still required, "
                        "skipping this feed.",
                        file=sys.stderr,
                    )
                    return "需要扫码验证，已跳过"

                # Wait for user to scan QR code
                if not self._wait_for_qr_verification():
                    return "需要扫码验证但超时未完成"
                time.sleep(DOM_SETTLE_WAIT)

        # Check for other page errors
        return self._check_page_error()

    # ------------------------------------------------------------------
    # Comment count helpers
    # ------------------------------------------------------------------

    def _get_comment_count(self) -> int:
        """Get the number of currently loaded parent comments."""
        count = self._evaluate(
            "document.querySelectorAll('.parent-comment').length"
        )
        return count if isinstance(count, int) else 0

    def _get_total_comment_count(self) -> int:
        """Get the total comment count from the page header."""
        result = self._evaluate("""
            (function() {
                var el = document.querySelector('.comments-container .total');
                if (!el) return 0;
                var text = el.textContent;
                var match = text.match(/共(\\d+)条评论/);
                return match ? parseInt(match[1]) : 0;
            })();
        """)
        return result if isinstance(result, int) else 0

    def _check_no_comments(self) -> bool:
        """Check if the page indicates no comments exist."""
        result = self._evaluate("""
            (function() {
                var el = document.querySelector('.no-comments-text');
                if (!el) return false;
                return el.textContent.indexOf('这是一片荒地') !== -1;
            })();
        """)
        return bool(result)

    def _check_end_container(self) -> bool:
        """Check if 'THE END' marker is visible (all comments loaded)."""
        result = self._evaluate("""
            (function() {
                var el = document.querySelector('.end-container');
                if (!el) return false;
                var text = el.textContent.trim().toUpperCase();
                return text.indexOf('THE END') !== -1
                    || text.indexOf('THEEND') !== -1;
            })();
        """)
        return bool(result)

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def _get_scroll_top(self) -> int:
        """Get the current vertical scroll position."""
        result = self._evaluate("""
            (window.pageYOffset
             || document.documentElement.scrollTop
             || document.body.scrollTop
             || 0)
        """)
        return result if isinstance(result, int) else 0

    def _scroll_to_comments_area(self):
        """Scroll to the comments section to start loading."""
        print("[feed_detail] Scrolling to comments area...", file=sys.stderr)
        self._evaluate("""
            (function() {
                var el = document.querySelector('.comments-container');
                if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'});
            })();
        """)
        time.sleep(0.5)

        # Trigger a small scroll to activate lazy loading
        self._smart_scroll(100)

    def _smart_scroll(self, delta: float):
        """Dispatch a wheel event to trigger lazy loading."""
        self._evaluate(f"""
            (function() {{
                var target = document.querySelector('.note-scroller')
                    || document.querySelector('.interaction-container')
                    || document.documentElement;
                var ev = new WheelEvent('wheel', {{
                    deltaY: {delta},
                    deltaMode: 0,
                    bubbles: true,
                    cancelable: true,
                    view: window
                }});
                target.dispatchEvent(ev);
            }})();
        """)

    def _scroll_to_last_comment(self):
        """Scroll to the last loaded parent comment."""
        self._evaluate("""
            (function() {
                var els = document.querySelectorAll('.parent-comment');
                if (els.length > 0) {
                    els[els.length - 1].scrollIntoView(
                        {behavior: 'smooth', block: 'center'}
                    );
                }
            })();
        """)

    def _get_scroll_ratio(self, speed: str) -> float:
        """Get base scroll ratio based on speed."""
        if speed == "slow":
            return 0.5
        elif speed == "fast":
            return 0.9
        return 0.7  # normal

    def _human_scroll(
        self, speed: str, large_mode: bool, push_count: int,
    ) -> tuple[bool, int, int]:
        """Perform human-like scrolling with configurable intensity.

        Returns (scrolled, actual_delta, current_scroll_top).
        """
        before_top = self._get_scroll_top()
        viewport_height = self._evaluate("window.innerHeight") or 800

        base_ratio = self._get_scroll_ratio(speed)
        if large_mode:
            base_ratio *= 2.0

        scrolled = False
        actual_delta = 0
        current_top = before_top

        for i in range(max(1, push_count)):
            scroll_delta = float(viewport_height) * (
                base_ratio + random.random() * 0.2
            )
            if scroll_delta < 400:
                scroll_delta = 400
            scroll_delta += random.randint(-50, 50)

            self._evaluate(f"window.scrollBy(0, {scroll_delta})")
            _sleep_random(*SCROLL_WAIT)

            current_top = self._get_scroll_top()
            delta_this_time = current_top - before_top
            actual_delta += delta_this_time

            if delta_this_time > 5:
                scrolled = True

            before_top = current_top

            if i < push_count - 1:
                _sleep_random(*HUMAN_DELAY)

        # Fallback: scroll to bottom if nothing moved
        if not scrolled and push_count > 0:
            self._evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
            _sleep_random(*POST_SCROLL)
            current_top = self._get_scroll_top()
            actual_delta = current_top - before_top + actual_delta
            scrolled = actual_delta > 5

        return scrolled, actual_delta, current_top

    # ------------------------------------------------------------------
    # Button clicking (show more replies)
    # ------------------------------------------------------------------

    def _click_show_more_buttons(self, max_threshold: int) -> tuple[int, int]:
        """Click 'show more replies' buttons, respecting threshold.

        Returns (clicked_count, skipped_count).
        """
        max_click = MAX_CLICK_PER_ROUND + random.randint(0, MAX_CLICK_PER_ROUND)

        result = self._evaluate(f"""
            (function() {{
                var elements = document.querySelectorAll('.show-more');
                var replyRegex = /展开\\s*(\\d+)\\s*条回复/;
                var maxClick = {max_click};
                var threshold = {max_threshold};
                var clicked = 0;
                var skipped = 0;

                for (var i = 0; i < elements.length; i++) {{
                    if (clicked >= maxClick) break;

                    var el = elements[i];
                    var box = el.getBoundingClientRect();
                    if (box.width === 0 || box.height === 0) continue;

                    var text = el.textContent || '';

                    if (threshold > 0) {{
                        var match = text.match(replyRegex);
                        if (match && parseInt(match[1]) > threshold) {{
                            skipped++;
                            continue;
                        }}
                    }}

                    try {{
                        el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                        el.click();
                        clicked++;
                    }} catch(e) {{
                        // skip failed clicks
                    }}
                }}

                return JSON.stringify({{clicked: clicked, skipped: skipped}});
            }})();
        """)

        if not result:
            return 0, 0

        try:
            data = json.loads(result)
            return data.get("clicked", 0), data.get("skipped", 0)
        except (json.JSONDecodeError, TypeError):
            return 0, 0

    # ------------------------------------------------------------------
    # Comment loader
    # ------------------------------------------------------------------

    def _load_all_comments(self, config: CommentLoadConfig):
        """Load all comments by scrolling and clicking 'show more' buttons."""
        stats = LoadStats()
        state = LoadState()

        max_attempts = (
            config.max_comment_items * 3
            if config.max_comment_items > 0
            else DEFAULT_MAX_ATTEMPTS
        )
        scroll_interval = _get_scroll_interval(config.scroll_speed)

        print("[feed_detail] Starting comment loading...", file=sys.stderr)
        self._scroll_to_comments_area()
        _sleep_random(*HUMAN_DELAY)

        # Check if there are no comments at all
        if self._check_no_comments():
            print(
                "[feed_detail] No comments found (empty area), skipping.",
                file=sys.stderr,
            )
            return

        for attempt in range(max_attempts):
            stats.attempts = attempt

            # Check for end marker
            if self._check_end_container():
                current = self._get_comment_count()
                print(
                    f"[feed_detail] Reached 'THE END' - {current} comments loaded "
                    f"after {attempt + 1} attempts, "
                    f"clicked: {stats.total_clicked}, skipped: {stats.total_skipped}",
                    file=sys.stderr,
                )
                return

            # Click "show more" buttons periodically
            if config.click_more_replies and attempt % BUTTON_CLICK_INTERVAL == 0:
                clicked, skipped = self._click_show_more_buttons(
                    config.max_replies_threshold
                )
                if clicked > 0 or skipped > 0:
                    stats.total_clicked += clicked
                    stats.total_skipped += skipped
                    print(
                        f"[feed_detail] Clicked 'more': {clicked}, "
                        f"skipped: {skipped}, "
                        f"total clicked: {stats.total_clicked}, "
                        f"total skipped: {stats.total_skipped}",
                        file=sys.stderr,
                    )
                    _sleep_random(*READ_TIME)

                    # Retry round
                    clicked2, skipped2 = self._click_show_more_buttons(
                        config.max_replies_threshold
                    )
                    if clicked2 > 0 or skipped2 > 0:
                        stats.total_clicked += clicked2
                        stats.total_skipped += skipped2
                        _sleep_random(*SHORT_READ)

            # Track comment count changes
            current_count = self._get_comment_count()
            total_count = self._get_total_comment_count()

            if current_count != state.last_count:
                print(
                    f"[feed_detail] Comments: {state.last_count} -> {current_count} "
                    f"(+{current_count - state.last_count}), target: {total_count}",
                    file=sys.stderr,
                )
                state.last_count = current_count
                state.stagnant_checks = 0
            else:
                state.stagnant_checks += 1

            # Check if we reached the target
            if (
                config.max_comment_items > 0
                and current_count >= config.max_comment_items
            ):
                print(
                    f"[feed_detail] Reached target: "
                    f"{current_count}/{config.max_comment_items}",
                    file=sys.stderr,
                )
                return

            # Scroll
            if current_count > 0:
                self._scroll_to_last_comment()
                _sleep_random(*POST_SCROLL)

            large_mode = state.stagnant_checks >= LARGE_SCROLL_TRIGGER
            push_count = 1
            if large_mode:
                push_count = 3 + random.randint(0, 2)

            _, scroll_delta, current_top = self._human_scroll(
                config.scroll_speed, large_mode, push_count,
            )

            if scroll_delta < MIN_SCROLL_DELTA or current_top == state.last_scroll_top:
                state.stagnant_checks += 1
            else:
                state.stagnant_checks = 0
                state.last_scroll_top = current_top

            # Handle excessive stagnation
            if state.stagnant_checks >= STAGNANT_LIMIT:
                print(
                    "[feed_detail] Stagnation detected, performing large scroll...",
                    file=sys.stderr,
                )
                self._human_scroll(config.scroll_speed, True, 10)
                state.stagnant_checks = 0

                if self._check_end_container():
                    current = self._get_comment_count()
                    print(
                        f"[feed_detail] Reached bottom, {current} comments.",
                        file=sys.stderr,
                    )
                    return

            time.sleep(scroll_interval)

        # Final sprint
        print("[feed_detail] Max attempts reached, final sprint...", file=sys.stderr)
        self._human_scroll(config.scroll_speed, True, FINAL_SPRINT_PUSH_COUNT)

        final_count = self._get_comment_count()
        has_end = self._check_end_container()
        print(
            f"[feed_detail] Loading complete: {final_count} comments, "
            f"clicked: {stats.total_clicked}, skipped: {stats.total_skipped}, "
            f"reached bottom: {has_end}",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------
    # Data extraction
    # ------------------------------------------------------------------

    def _extract_feed_detail(self, feed_id: str) -> dict | None:
        """Extract feed detail from __INITIAL_STATE__.

        Returns a dict with 'note' and 'comments' keys, or None if not found.
        """
        raw = self._evaluate("""
            (function() {
                if (window.__INITIAL_STATE__
                    && window.__INITIAL_STATE__.note
                    && window.__INITIAL_STATE__.note.noteDetailMap) {
                    return JSON.stringify(
                        window.__INITIAL_STATE__.note.noteDetailMap
                    );
                }
                return '';
            })();
        """)

        if not raw:
            return None

        try:
            note_detail_map = json.loads(raw)
        except json.JSONDecodeError as e:
            print(
                f"[feed_detail] Failed to parse noteDetailMap: {e}",
                file=sys.stderr,
            )
            return None

        detail = note_detail_map.get(feed_id)
        if not detail:
            # Try to find the feed in the map (sometimes key differs)
            for key, value in note_detail_map.items():
                if feed_id in key:
                    detail = value
                    break

        if not detail:
            print(
                f"[feed_detail] Feed {feed_id} not found in noteDetailMap. "
                f"Available keys: {list(note_detail_map.keys())}",
                file=sys.stderr,
            )
            return None

        return {
            "note": detail.get("note", {}),
            "comments": detail.get("comments", {}),
        }

    # ------------------------------------------------------------------
    # Main API
    # ------------------------------------------------------------------

    def get_feed_detail(
        self,
        feed_id: str,
        xsec_token: str,
        load_comments: bool = False,
        config: CommentLoadConfig | None = None,
        skip_on_verify: bool = False,
    ) -> dict | None:
        """Get the full detail of a Xiaohongshu note/feed.

        Navigates in the current tab (no new tab created).

        Args:
            feed_id: The note ID (hex string).
            xsec_token: The xsec_token for authentication.
            load_comments: Whether to scroll and load all comments.
            config: Comment loading configuration (used only if load_comments=True).
            skip_on_verify: If True, skip (return error) when QR verification is
                required instead of waiting for user scan.

        Returns:
            Dict with 'note' and 'comments' keys, or None on failure.
        """
        if config is None:
            config = CommentLoadConfig()

        url = _make_feed_detail_url(feed_id, xsec_token)
        print(f"[feed_detail] Opening feed detail page: {url}", file=sys.stderr)
        print(
            f"[feed_detail] Config: load_comments={load_comments}, "
            f"click_more={config.click_more_replies}, "
            f"threshold={config.max_replies_threshold}, "
            f"max_items={config.max_comment_items}, "
            f"speed={config.scroll_speed}",
            file=sys.stderr,
        )

        self._navigate(url)
        time.sleep(FEED_PAGE_LOAD_WAIT)

        # Wait for DOM to settle
        time.sleep(DOM_SETTLE_WAIT)

        # Check if page is accessible (handles QR retry automatically)
        error = self._check_page_accessible(
            feed_id, xsec_token, skip_on_verify=skip_on_verify,
        )
        if error:
            print(f"[feed_detail] Error: {error}", file=sys.stderr)
            raise CDPError(error)

        # Load all comments if requested
        if load_comments:
            try:
                self._load_all_comments(config)
            except Exception as e:
                print(
                    f"[feed_detail] Warning: Comment loading failed: {e}",
                    file=sys.stderr,
                )

        # Extract feed detail from __INITIAL_STATE__
        result = self._extract_feed_detail(feed_id)
        if result is None:
            raise CDPError(
                f"Could not extract feed detail for {feed_id}. "
                "The page may not have loaded correctly."
            )

        return result

    def get_feed_details_batch(
        self,
        feeds: list[dict],
        load_comments: bool = False,
        config: CommentLoadConfig | None = None,
    ) -> list[dict]:
        """Get details of multiple feeds in a single tab (no new tabs).

        Navigates to each feed URL sequentially in the same tab.

        Args:
            feeds: List of dicts with 'feed_id' and 'xsec_token' keys.
            load_comments: Whether to load comments for each feed.
            config: Comment loading configuration.

        Returns:
            List of result dicts. Each dict has 'feed_id', 'status' ('ok'/'error'),
            and 'data' (feed detail) or 'error' (error message).
        """
        results = []
        total = len(feeds)

        for i, feed in enumerate(feeds):
            feed_id = feed["feed_id"]
            xsec_token = feed["xsec_token"]
            print(
                f"\n[feed_detail] === Batch [{i + 1}/{total}] "
                f"feed_id={feed_id} ===",
                file=sys.stderr,
            )

            try:
                detail = self.get_feed_detail(
                    feed_id=feed_id,
                    xsec_token=xsec_token,
                    load_comments=load_comments,
                    config=config,
                    skip_on_verify=True,
                )
                results.append({
                    "feed_id": feed_id,
                    "status": "ok",
                    "data": detail,
                })
                print(
                    f"[feed_detail] Batch [{i + 1}/{total}] OK",
                    file=sys.stderr,
                )
            except Exception as e:
                print(
                    f"[feed_detail] Batch [{i + 1}/{total}] Error: {e}",
                    file=sys.stderr,
                )
                results.append({
                    "feed_id": feed_id,
                    "status": "error",
                    "error": str(e),
                })

            # Brief pause between fetches to avoid rate limiting
            if i < total - 1:
                _sleep_random(500, 1500)

        return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _add_comment_args(parser):
    """Add comment-related CLI arguments to a parser."""
    parser.add_argument(
        "--load-comments",
        action="store_true",
        help="Scroll to load all comments",
    )
    parser.add_argument(
        "--click-more-replies",
        action="store_true",
        help="Click 'show more replies' buttons during comment loading",
    )
    parser.add_argument(
        "--max-replies-threshold",
        type=int,
        default=10,
        help="Skip 'show more' buttons with reply count above this (default: 10)",
    )
    parser.add_argument(
        "--max-comments",
        type=int,
        default=0,
        help="Stop after loading this many comments (0 = unlimited, default: 0)",
    )
    parser.add_argument(
        "--scroll-speed",
        choices=["slow", "normal", "fast"],
        default="normal",
        help="Comment loading scroll speed (default: normal)",
    )


def _build_comment_config(args) -> CommentLoadConfig:
    """Build a CommentLoadConfig from parsed CLI arguments."""
    return CommentLoadConfig(
        click_more_replies=args.click_more_replies,
        max_replies_threshold=args.max_replies_threshold,
        max_comment_items=args.max_comments,
        scroll_speed=args.scroll_speed,
    )


def main():
    import argparse
    from chrome_launcher import ensure_chrome

    parser = argparse.ArgumentParser(
        description="Xiaohongshu CDP Feed Detail Extractor"
    )
    parser.add_argument(
        "--headed", action="store_true",
        help="Use headed Chrome with GUI (default is headless)",
    )
    parser.add_argument("--account", help="Account name to use")
    sub = parser.add_subparsers(dest="command", required=True)

    # detail command - single feed
    p_detail = sub.add_parser("detail", help="Get feed/note detail")
    p_detail.add_argument(
        "--feed-id", required=True, help="Note/feed ID (hex string)"
    )
    p_detail.add_argument(
        "--xsec-token", required=True, help="xsec_token for the note"
    )
    _add_comment_args(p_detail)

    # batch command - multiple feeds in one tab
    p_batch = sub.add_parser(
        "batch", help="Get multiple feed details in a single tab"
    )
    p_batch.add_argument(
        "--feeds", required=True,
        help=(
            "JSON array of feeds, e.g. "
            '\'[{"feed_id":"abc","xsec_token":"xyz"},...]\' '
            "or path to a JSON file containing such array"
        ),
    )
    _add_comment_args(p_batch)

    args = parser.parse_args()

    # Redirect stdout to stderr during loading so only JSON goes to stdout
    real_stdout = sys.stdout
    sys.stdout = sys.stderr

    # Default is headless; use --headed for GUI (login, QR verification)
    use_headless = not args.headed

    # Ensure Chrome is running
    if not ensure_chrome(headless=use_headless, account=args.account):
        print("Error: Failed to start Chrome.", file=sys.stderr)
        sys.exit(2)

    detail_extractor = XiaohongshuFeedDetail()
    try:
        detail_extractor.connect()

        if args.command == "detail":
            config = _build_comment_config(args)
            result = detail_extractor.get_feed_detail(
                feed_id=args.feed_id,
                xsec_token=args.xsec_token,
                load_comments=args.load_comments,
                config=config,
            )

            if result is None:
                print("[feed_detail] No feed detail found.", file=sys.stderr)
                sys.exit(1)

            print(
                json.dumps(result, ensure_ascii=False, indent=2),
                file=real_stdout,
            )
            print("FEED_DETAIL_STATUS: SUCCESS", file=sys.stderr)

        elif args.command == "batch":
            # Parse feeds: from JSON string or file path
            feeds_input = args.feeds.strip()
            if os.path.isfile(feeds_input):
                with open(feeds_input, "r", encoding="utf-8") as f:
                    feeds = json.load(f)
            else:
                feeds = json.loads(feeds_input)

            if not isinstance(feeds, list) or not feeds:
                print(
                    "Error: --feeds must be a non-empty JSON array",
                    file=sys.stderr,
                )
                sys.exit(1)

            config = _build_comment_config(args)
            results = detail_extractor.get_feed_details_batch(
                feeds=feeds,
                load_comments=args.load_comments,
                config=config,
            )

            print(
                json.dumps(results, ensure_ascii=False, indent=2),
                file=real_stdout,
            )

            ok_count = sum(1 for r in results if r["status"] == "ok")
            print(
                f"FEED_DETAIL_BATCH_STATUS: {ok_count}/{len(results)} OK",
                file=sys.stderr,
            )

    except CDPError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in --feeds: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        sys.stdout = real_stdout
        detail_extractor.disconnect()


if __name__ == "__main__":
    main()
