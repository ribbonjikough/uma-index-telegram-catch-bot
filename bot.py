import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field

import httpx
from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright
from telegram import (
    ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes,
)

# =====================================================================
# LOAD ENVIRONMENT VARIABLES (.env)
# =====================================================================
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN! Create a .env file with TELEGRAM_BOT_TOKEN=your_token")

UMA_INDEX_URL = "https://www.uma-index.top/?mode=twitter"
DB_PATH = "uma_seen.db"

# =====================================================================
# PAGE WINDOW & ANTI-JAM RATE LIMIT SETTINGS
# =====================================================================
MAX_PAGES = max(1, int(os.getenv("MAX_PAGES", "3")))  # Pages to keep synced for every user (.env, default 3)

POLL_INTERVAL_SECONDS = 60 * 60  # Check the window every 60 minutes
SLEEP_BETWEEN_POSTS = 4.5        # Minimum delay (seconds) between posts
SECONDS_PER_MEDIA_ITEM = 4       # Ensures multi-image albums stay strictly < 20 msgs/min
SLEEP_BETWEEN_USERS = 0.4        # Delay between sending to different users

# =====================================================================
# COLORED LOGGING (COLORS ONLY TIMESTAMP + LEVEL), TOKEN REDACTED
# =====================================================================
if os.name == "nt":
    os.system("")  # enable ANSI colors on Windows terminals

SUCCESS = 25  # between INFO and WARNING
logging.addLevelName(SUCCESS, "SUCCESS")
logging.Logger.success = lambda self, msg, *a: self.log(SUCCESS, msg, *a)
COLORS = {
    logging.DEBUG: "\033[36m",       # Cyan
    logging.INFO: "\033[34m",        # Dark Blue
    SUCCESS: "\033[32m",             # Green
    logging.WARNING: "\033[33m",     # Yellow
    logging.ERROR: "\033[31m",       # Red
    logging.CRITICAL: "\033[1;35m",  # Bold Magenta
}


class ColorFormatter(logging.Formatter):
    def format(self, record):
        record.c = COLORS.get(record.levelno, "")
        return super().format(record).replace(BOT_TOKEN, "[REDACTED_TOKEN]")


_handler = logging.StreamHandler()
_handler.setFormatter(ColorFormatter("%(c)s%(asctime)s | %(levelname)-8s\033[0m | %(name)s | %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
for noisy in ("httpx", "httpcore"):  # kills the "HTTP Request: POST .../getUpdates" spam
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("UmaIndexBot")


# =====================================================================
# DATA MODEL & SQLITE STORE (SUGGESTION B: AUTO-MIGRATION & CACHE PURGE)
# =====================================================================
@dataclass
class UmaComicPost:
    post_id: str
    character: str = "Unknown"
    artist: str = "Unknown"
    translator: str = "Unknown"
    date_str: str = ""
    tweet_url: str | None = None
    image_urls: list[str] = field(default_factory=list)


class PostStore:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.q("CREATE TABLE IF NOT EXISTS posts_cache (post_id TEXT PRIMARY KEY, data TEXT)")
        self.q("CREATE TABLE IF NOT EXISTS subscribers (chat_id TEXT PRIMARY KEY, chat_title TEXT, chat_type TEXT)")
        self.q(
            "CREATE TABLE IF NOT EXISTS user_deliveries (chat_id TEXT, post_id TEXT, status TEXT DEFAULT 'sent', PRIMARY KEY (chat_id, post_id))"
        )
        self._auto_heal_database()

    def q(self, sql: str, *args) -> list:
        with self.db:  # commits on success
            return self.db.execute(sql, args).fetchall()

    def _auto_heal_database(self):
        """
        Suggestion B: Automatic Startup Migration & Poisoned-Cache Purge.
        1. Upgrades existing user_deliveries table with 'status' column if missing.
        2. Purges poisoned cache entries (missing/non-http URLs or old unverified fallbacks).
        3. Resets any 'try_again' rows on startup so they get a fresh attempt on boot,
           while never retrying during the polling loop.
        """
        try:
            self.q("ALTER TABLE user_deliveries ADD COLUMN status TEXT DEFAULT 'sent'")
        except sqlite3.OperationalError:
            pass  # column already exists

        purged = 0
        for post_id, raw_json in self.q("SELECT post_id, data FROM posts_cache"):
            try:
                urls = json.loads(raw_json).get("image_urls", [])
                bad = not urls or any(not u.startswith("http") for u in urls)
            except Exception:
                bad = True
            if bad:
                self.q("DELETE FROM posts_cache WHERE post_id = ?", post_id)
                purged += 1

        # Clear 'try_again' entries from previous runs so this startup gives them 1 clean retry
        with self.db:
            reset_rows = self.db.execute("DELETE FROM user_deliveries WHERE status = 'try_again'").rowcount

        if purged or reset_rows:
            logger.info(
                f"Startup Auto-Heal: purged {purged} poisoned cache entries, queued {reset_rows} 'try_again' posts for fresh retry."
            )

    # --- Post Cache Management ---
    def get_cached_post(self, post_id: str) -> UmaComicPost | None:
        rows = self.q("SELECT data FROM posts_cache WHERE post_id = ?", post_id)
        return UmaComicPost(**json.loads(rows[0][0])) if rows else None

    def get_cached_ids(self) -> list[str]:
        return [r[0] for r in self.q("SELECT post_id FROM posts_cache")]

    def save_post_to_cache(self, post: UmaComicPost):
        self.q("INSERT OR REPLACE INTO posts_cache VALUES (?, ?)", post.post_id, json.dumps(asdict(post)))

    def count_cached_posts(self) -> int:
        return self.q("SELECT COUNT(*) FROM posts_cache")[0][0]

    # --- Per-User Delivery Tracking ---
    def is_delivered(self, chat_id, post_id: str) -> bool:
        """
        True if status is 'sent' OR 'try_again'.
        Because 'try_again' counts here, the polling loop will NEVER
        repeatedly hammer failed posts during the same session.
        """
        return bool(self.q("SELECT 1 FROM user_deliveries WHERE chat_id = ? AND post_id = ?", str(chat_id), post_id))

    def mark(self, chat_id, post_id: str, status: str = "sent"):
        if status == "try_again":  # purge its cache entry too, so the retry re-resolves fresh image URLs
            self.q("DELETE FROM posts_cache WHERE post_id = ?", post_id)
            logger.warning(f"Post {post_id} for chat {chat_id} labelled 'try_again' in DB (skipped for polls).")
        self.q(
            "INSERT OR REPLACE INTO user_deliveries (chat_id, post_id, status) VALUES (?, ?, ?)",
            str(chat_id), post_id, status,
        )

    def clear_try_again_for_chat(self, chat_id):
        """Allows /start to also give 'try_again' posts a fresh attempt."""
        self.q("DELETE FROM user_deliveries WHERE chat_id = ? AND status = 'try_again'", str(chat_id))

    def count_user_deliveries(self, chat_id, status: str = "sent") -> int:
        return self.q("SELECT COUNT(*) FROM user_deliveries WHERE chat_id = ? AND status = ?", str(chat_id), status)[0][0]

    # --- Subscribers / Channels Management ---
    def add_subscriber(self, chat_id, title: str = "User", chat_type: str = "private"):
        self.q("INSERT OR REPLACE INTO subscribers VALUES (?, ?, ?)", str(chat_id), title, chat_type)
        logger.info(f"Subscriber active: {title} ({chat_id}) [{chat_type}]")

    def remove_subscriber(self, chat_id):
        self.q("DELETE FROM subscribers WHERE chat_id = ?", str(chat_id))
        logger.info(f"Subscriber removed: {chat_id}")

    def get_subscribers(self) -> list[str]:
        return [r[0] for r in self.q("SELECT chat_id FROM subscribers")]


# =====================================================================
# SOLUTION #1 & #2: DIRECT SITE SCRAPER + IN-MEMORY IMAGE DOWNLOADER
# =====================================================================
TWEET_ID_REGEX = re.compile(r"(?:twitter\.com|x\.com)/[^/]+/status/(\d+)")
IMG_URL_REGEX = re.compile(
    r'https?://[^\s"\'<>]+?(?:pbs\.twimg\.com/media/[^\s"\'<>]+|\.(?:jpg|jpeg|png|webp))'
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.uma-index.top/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}
API_HEADERS = {"User-Agent": UA, "Accept": "application/json"}

RESOLVERS = [
    (  # 1. FxTwitter API
        lambda i: f"https://api.fxtwitter.com/status/{i}",
        lambda d: [p["url"] for p in d.get("tweet", {}).get("media", {}).get("photos", []) if "url" in p],
    ),
    (  # 2. VxTwitter API
        lambda i: f"https://api.vxtwitter.com/Twitter/status/{i}",
        lambda d: [u for u in d.get("mediaURLs", []) if "pbs.twimg.com/media/" in u],
    ),
    (  # 3. X Syndication Embed API
        lambda i: f"https://cdn.syndication.twimg.com/tweet-result?id={i}&token=a",
        lambda d: [p["url"] for p in d.get("photos", []) if "url" in p],
    ),
]


def upgrade_twimg_url(url: str) -> str:
    """Resolves relative uma-index URLs and forces pbs.twimg.com URLs to load original quality."""
    if not url or url.startswith(("blob:", "data:")):
        return ""
    if url.startswith("/"):
        url = f"https://www.uma-index.top{url}"
    if "pbs.twimg.com/media/" in url:
        return f"{url.split('?')[0]}?format=jpg&name=orig"
    return url


async def resolve_tweet_images(client: httpx.AsyncClient, tweet_url: str, expected: int = 1) -> list[str]:
    """
    Resolves every photo of the tweet, in order. A tier that returns fewer photos than
    the site announced (e.g. 1 of 2) is not trusted: the next tier is tried, best result wins.
    """
    match = TWEET_ID_REGEX.search(tweet_url)
    if not match:
        return []
    best: list[str] = []
    for build_url, extract in RESOLVERS:
        try:
            resp = await client.get(build_url(match.group(1)), timeout=12.0)
            if resp.status_code == 200:
                urls = [u for u in dict.fromkeys(map(upgrade_twimg_url, extract(resp.json()))) if u][:4]
                best = max(best, urls, key=len)
                if len(best) >= expected:
                    return best
        except Exception as exc:
            logger.debug(f"Resolver fallback triggered for {tweet_url}: {exc}")
    if not best:
        logger.warning(f"All X image resolvers failed for {tweet_url}")
    return best


async def download_image_bytes(client: httpx.AsyncClient, url: str) -> bytes | None:
    """
    Solution #2: Downloads image bytes into memory in Python with browser headers
    so Telegram never fails with 'Failed to get http url content'.
    Raw bytes (not BytesIO): a retried upload would otherwise re-read an exhausted stream.
    """
    clean_url = upgrade_twimg_url(url)
    if not clean_url:
        return None
    for candidate in dict.fromkeys([clean_url, url]):
        try:
            resp = await client.get(candidate, headers=BROWSER_HEADERS, timeout=20.0)
            ctype = resp.headers.get("content-type", "").lower()
            if resp.status_code == 200 and len(resp.content) > 512 and ("image" in ctype or "octet-stream" in ctype):
                return resp.content
        except Exception as exc:
            logger.debug(f"Image byte download failed for {candidate}: {exc}")
    return None


async def extract_cards_from_current_page(page: Page, known: list[str]) -> list[dict]:
    """
    Solution #1: Extracts all comic cards AND clicks the website's own 'READ' button
    for multi-page comics to pull the English-translated images directly from uma-index.top.
    `known` = cached post ids: their READ modal is skipped (already resolved, saves time).
    Only buttons that appear WITH the modal are clicked, so the site's own pagination
    'Next' button is never hit by accident (that used to flip the page mid-scan).
    """
    return await page.evaluate(
        r"""
        async (known) => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            const src = img => img.currentSrc || img.src || img.getAttribute('data-src');
            const btns = () => Array.from(document.querySelectorAll('button, [role="button"]'));
            const results = [];
            const xLinks = Array.from(document.querySelectorAll('a[href*="x.com/"], a[href*="twitter.com/"]'))
                .filter(a => /\/status\/\d+/.test(a.href));

            for (const link of xLinks) {
                let card = link.closest('article, .card, [class*="card"], [class*="item"], [class*="post"], li, div');
                let depth = 0;
                while (card && card.parentElement && depth < 5) {
                    if (card.querySelectorAll('a[href*="/status/"]').length > 1) break;
                    if (card.querySelector('img')) break;
                    card = card.parentElement;
                    depth++;
                }
                if (!card) continue;

                const tid = (link.href.match(/status\/(\d+)/) || [])[1];
                const rawText = card.innerText || '';
                const pageMatch = rawText.match(/\b(\d+)P\b/);
                const expectedPages = pageMatch ? parseInt(pageMatch[1], 10) : 1;
                const cardImgs = Array.from(card.querySelectorAll('img')).map(src).filter(Boolean);

                let modalImgs = [];
                const readBtn = Array.from(card.querySelectorAll('button, a, [role="button"], span'))
                    .find(el => (el.innerText || '').trim().toUpperCase() === 'READ');

                if (readBtn && !known.includes(tid) && (expectedPages > 1 || cardImgs.length === 0)) {
                    try {
                        const beforeImgs = new Set(Array.from(document.querySelectorAll('img')).map(src));
                        const beforeBtns = new Set(btns());
                        readBtn.click();
                        await sleep(350);

                        const collectNewImgs = () => {
                            for (const img of document.querySelectorAll('img')) {
                                const s = src(img);
                                if (s && !beforeImgs.has(s) && !modalImgs.includes(s)) modalImgs.push(s);
                            }
                        };
                        collectNewImgs();

                        for (let p = 1; p < expectedPages; p++) {
                            const nextModalBtn = btns().find(b => {
                                if (beforeBtns.has(b)) return false;
                                const t = (b.innerText || b.getAttribute('aria-label') || '').toUpperCase();
                                return t === '>' || t === '›' || t === '→' || t.includes('NEXT');
                            });
                            if (!nextModalBtn) break;
                            nextModalBtn.click();
                            await sleep(250);
                            collectNewImgs();
                        }

                        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
                        const closeBtn = btns().find(b => !beforeBtns.has(b) &&
                            ['×', '✕', 'X', 'CLOSE'].includes((b.innerText || b.getAttribute('aria-label') || '').trim().toUpperCase()));
                        if (closeBtn) closeBtn.click();
                        await sleep(150);
                    } catch (e) {}
                }

                results.push({
                    tweetUrl: link.href,
                    siteImgs: modalImgs.length ? modalImgs : cardImgs,
                    expectedPages: expectedPages,
                    rawText: rawText
                });
            }
            return results;
        }
        """,
        known,
    )


async def go_to_next_page(page: Page, target_page_num: int, prev_first_url: str) -> bool:
    """
    Clicks the pagination Next / page-number button, then falls back to
    ?mode=twitter&page=N if needed. True once the first card has changed.
    """
    clicked = await page.evaluate(
        """
        (targetPage) => {
            const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], span, li'));
            for (const el of candidates) {
                const txt = (el.innerText || el.textContent || '').trim().toUpperCase();
                const aria = (el.getAttribute('aria-label') || '').toUpperCase();
                if (['NEXT', 'NEXT >', '>', '»', '→'].includes(txt) || aria.includes('NEXT')) {
                    if (!el.disabled && !el.classList.contains('disabled')) { el.click(); return true; }
                }
            }
            for (const el of candidates) {
                if ((el.innerText || el.textContent || '').trim() === String(targetPage) && !el.disabled) {
                    el.click(); return true;
                }
            }
            return false;
        }
        """,
        target_page_num,
    )

    async def first_card_changed() -> bool:
        first_href = await page.evaluate(
            "() => (document.querySelector('a[href*=\"/status/\"]') || {}).href || ''"
        )
        return bool(first_href) and first_href != prev_first_url

    if clicked:
        for _ in range(16):
            await page.wait_for_timeout(500)
            if await first_card_changed():
                return True

    try:
        await page.goto(f"{UMA_INDEX_URL}&page={target_page_num}", wait_until="networkidle", timeout=35000)
        await page.wait_for_timeout(2000)
        return await first_card_changed()
    except Exception as exc:
        logger.warning(f"Fallback navigation to page {target_page_num} failed: {exc}")
        return False


def parse_card(raw_text: str) -> dict:
    """Pulls artist / translator / date / character out of a card's visible text."""
    artist = translator = "Unknown"
    date_str, character_parts = "", []
    for ln in (ln.strip() for ln in raw_text.splitlines() if ln.strip()):
        if ln.startswith("ART"):
            artist = ln[3:].strip()
        elif ln.startswith("TL"):
            translator = ln[2:].strip()
        elif re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}", ln):
            date_str = ln
        elif ln.upper() not in ("READ", "X POST", "TAGS/REPORTS") and not re.match(r"^\d+P$", ln):
            character_parts.append(ln)
    character = ", ".join(dict.fromkeys(character_parts[:2])) or "Uma Musume"
    return dict(character=character, artist=artist, translator=translator, date_str=date_str)


async def scrape_uma_index_pages(store: PostStore) -> list[UmaComicPost]:
    """
    Scrapes pages 1..MAX_PAGES (newest -> oldest). The tweet's own photo list is the
    source of truth for images; what the site showed is only the fallback.
    """
    raw_cards: list[dict] = []
    api_url_map: dict[str, list[str]] = {}
    known = store.get_cached_ids()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context(user_agent=UA)).new_page()

        async def capture_site_json(response):
            if "json" not in response.headers.get("content-type", "").lower():
                return
            try:
                text = await response.text()
                for m in re.finditer(r'"(\d{15,22})"', text):
                    window = text[max(0, m.start() - 300): m.end() + 1200]
                    if found := IMG_URL_REGEX.findall(window):
                        api_url_map[m.group(1)] = [u for u in map(upgrade_twimg_url, dict.fromkeys(found)) if u]
            except Exception:
                pass

        page.on("response", capture_site_json)

        logger.info(f"Scanning up to {MAX_PAGES} pages from {UMA_INDEX_URL} ...")
        await page.goto(UMA_INDEX_URL, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(2500)

        for n in range(1, MAX_PAGES + 1):
            cards = await extract_cards_from_current_page(page, known)
            if not cards:
                logger.warning(f"No cards found on page {n}, stopping pagination.")
                break
            logger.info(f"Page {n}/{MAX_PAGES}: extracted {len(cards)} cards.")
            raw_cards.extend(cards)
            if n < MAX_PAGES and not await go_to_next_page(page, n + 1, cards[0].get("tweetUrl", "")):
                logger.info(f"Reached end of available pagination at page {n}.")
                break
        await browser.close()

    seen: set[str] = set()  # keyed by post id: x.com / twitter.com / ?s=20 variants of one tweet are one post
    posts: list[UmaComicPost] = []

    async with httpx.AsyncClient(follow_redirects=True, headers=API_HEADERS) as client:
        for item in raw_cards:
            tweet_url = (item.get("tweetUrl") or "").strip()
            match = TWEET_ID_REGEX.search(tweet_url)
            post_id = match.group(1) if match else tweet_url
            if not tweet_url or post_id in seen:
                continue
            seen.add(post_id)

            expected_pages = item.get("expectedPages") or 1

            # A cached post with fewer pages than the site announces is stale (single-image bug): re-resolve it
            cached = store.get_cached_post(post_id)
            if cached and len(cached.image_urls) >= expected_pages:
                posts.append(cached)
                continue

            images = await resolve_tweet_images(client, tweet_url, expected_pages)
            if not images:  # X resolvers all down: fall back to what the site itself showed
                images = api_url_map.get(post_id) or [
                    u for u in map(upgrade_twimg_url, item.get("siteImgs") or []) if u
                ]
                if len(images) < expected_pages:  # never send half a comic: skipped and retried next scan
                    logger.warning(f"Post {post_id} looks incomplete ({len(images)}/{expected_pages} pages), retrying next scan.")
                    continue

            if images:
                post = UmaComicPost(
                    post_id=post_id, tweet_url=tweet_url, image_urls=images[:4],
                    **parse_card(item.get("rawText") or ""),
                )
                store.save_post_to_cache(post)
                posts.append(post)
                await asyncio.sleep(0.15)

    return posts


# =====================================================================
# TELEGRAM SENDER (IN-MEMORY BYTE UPLOAD) & "DONE READING" UI
# =====================================================================
def done_keyboard(message_ids: list[int] = ()) -> InlineKeyboardMarkup:
    # Callback data is capped at 64 bytes, so ids are sent as offsets from the first id
    base = message_ids[0] if message_ids else 0
    offsets = ",".join(str(m - base) for m in message_ids)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Done reading", callback_data=f"del:{base}:{offsets}")]]
    )


def format_caption(post: UmaComicPost) -> str:
    parts = [f"🏇 <b>{post.character}</b>"]
    if post.artist != "Unknown":
        parts.append(f"🎨 <b>Artist:</b> {post.artist}")
    if post.translator != "Unknown":
        parts.append(f"🌐 <b>TL:</b> {post.translator}")
    if post.date_str:
        parts.append(f"📅 {post.date_str}")
    if post.tweet_url:
        parts.append(f"🔗 <a href='{post.tweet_url}'>Original X Post</a>")
    return "\n".join(parts)


async def safe_telegram_call(coro_func, *args, **kwargs):
    """
    Retries ONLY on RetryAfter (429 FloodWait: the message was definitely not sent).
    TimedOut is deliberately NOT retried: the upload may already have reached Telegram,
    and a blind retry is what posted the same comic several times.
    """
    for _ in range(5):
        try:
            return await coro_func(*args, **kwargs)
        except RetryAfter as exc:
            logger.warning(f"Telegram FloodWait triggered! Pausing for {exc.retry_after + 2:.1f}s...")
            await asyncio.sleep(exc.retry_after + 2.0)


async def send_comic_post(
    bot, store: PostStore, http_client: httpx.AsyncClient, chat_id, post: UmaComicPost
) -> int:
    """
    Downloads image bytes locally and uploads them to Telegram.
    Returns messages sent (>0), 0 for transient failure, or -1 to label as 'try_again'.
    """
    caption = format_caption(post)

    downloaded = await asyncio.gather(*(download_image_bytes(http_client, u) for u in post.image_urls[:10]))
    if any(photo is None for photo in downloaded):
        logger.warning(f"Could not download all image bytes for post {post.post_id}")
        return -1
    photos = [photo for photo in downloaded if photo is not None]
    if not photos:
        logger.warning(f"Could not download image bytes for post {post.post_id}: {post.image_urls}")
        return -1

    try:
        if len(photos) == 1:
            sent = await safe_telegram_call(
                bot.send_photo, chat_id=chat_id, photo=photos[0], caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=done_keyboard(),
            )
            count = 1 if sent else 0
        else:
            media = [
                InputMediaPhoto(photo, caption=caption if i == 0 else None,
                                parse_mode=ParseMode.HTML if i == 0 else None)
                for i, photo in enumerate(photos)
            ]
            sent = await safe_telegram_call(bot.send_media_group, chat_id=chat_id, media=media)
            count = 0
            if sent:
                ids = [m.message_id for m in sent]
                count = len(photos)
                await asyncio.sleep(1.2)
                try:
                    control = await safe_telegram_call(
                        bot.send_message, chat_id=chat_id,
                        text=f"📖 <i>{post.character} ({len(photos)}P)</i> — Tap below when finished:",
                        parse_mode=ParseMode.HTML, reply_to_message_id=ids[0],
                        reply_markup=done_keyboard(ids),
                    )
                    if control:
                        count += 1
                except Exception as exc:
                    logger.warning(f"Media group delivered for post {post.post_id}, but control message failed: {exc}")
        if count:
            logger.success(f"Sent [{post.character}] ({post.post_id}, {len(photos)}P) -> chat {chat_id}")
        return count
    except BadRequest as exc:
        logger.warning(f"Skipping post {post.post_id}: Telegram BadRequest ({exc})")
        return -1
    except Forbidden:
        logger.warning(f"Bot blocked or kicked from {chat_id}. Removing subscriber.")
        store.remove_subscriber(chat_id)
    except Exception as exc:  # includes TimedOut: not marked delivered, so it is retried next scan
        logger.error(f"Failed to send post {post.post_id} to {chat_id}: {exc!r}")
    return 0


# =====================================================================
# ROLLING SYNC ENGINE (CRASH-SAFE & DUPLICATE-PROOF)
# =====================================================================
async def sync_to_subscribers(application: Application, target_chat_id=None):
    store: PostStore = application.bot_data["store"]

    async with application.bot_data["sync_lock"]:
        targets = [str(target_chat_id)] if target_chat_id is not None else store.get_subscribers()
        if not targets:
            logger.info("No active subscribers yet. Waiting for /start or channel invite.")
            return

        try:
            posts = await scrape_uma_index_pages(store)
        except Exception as exc:
            logger.error(f"Error scraping {MAX_PAGES} pages of uma-index.top: {exc}")
            return
        if not posts:
            logger.warning(f"No posts extracted during {MAX_PAGES}-page scan.")
            return

        async with httpx.AsyncClient(follow_redirects=True) as http_client:
            for post in reversed(posts):  # oldest first, so chats read in order
                active = set(store.get_subscribers())
                for chat_id in targets:
                    if chat_id not in active or store.is_delivered(chat_id, post.post_id):
                        continue
                    sent = await send_comic_post(application.bot, store, http_client, chat_id, post)
                    if sent > 0:
                        store.mark(chat_id, post.post_id)
                    elif sent == -1:
                        # Labelled 'try_again' in DB: ignored by polls, retried on next bot startup or /start
                        store.mark(chat_id, post.post_id, "try_again")
                    await asyncio.sleep(
                        max(SLEEP_BETWEEN_POSTS, sent * SECONDS_PER_MEDIA_ITEM) if sent > 0 else SLEEP_BETWEEN_USERS
                    )


# =====================================================================
# CALLBACK HANDLER FOR "DONE READING" BUTTON
# =====================================================================
async def handle_done_reading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id, current_id = query.message.chat_id, query.message.message_id
    base, offsets = query.data.removeprefix("del:").split(":")
    ids = {current_id, *(int(base) + int(o) for o in offsets.split(",") if o)}

    deleted = False
    for msg_id in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted = True
        except (BadRequest, Forbidden) as exc:
            logger.warning(f"Could not delete message {msg_id} in {chat_id}: {exc}")

    if deleted:
        await query.answer("Post removed!")
    else:
        await query.answer(
            "Unable to delete! Ensure the bot is an Admin with 'Delete Messages' permission (and the post is under 48h old).",
            show_alert=True,
        )


# =====================================================================
# SUBSCRIPTION COMMANDS & AUTO-CHANNEL DETECTION
# =====================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, store = update.effective_chat, context.application.bot_data["store"]
    store.add_subscriber(chat.id, chat.title or chat.username or chat.first_name or "Private User", chat.type)
    # Clear any 'try_again' rows for this user when they manually run /start
    store.clear_try_again_for_chat(chat.id)

    await update.message.reply_text(
        f"✅ <b>Subscribed to Uma-Index ({MAX_PAGES}-Page Sync Active)!</b>\n\n"
        f"• <b>Chat ID:</b> <code>{chat.id}</code>\n"
        f"• <b>Posts Already Delivered to You:</b> {store.count_user_deliveries(chat.id)}\n"
        f"• Scanning Pages 1–{MAX_PAGES} now. Any posts you haven't received yet will be sent shortly (paced to respect Telegram rate limits).\n"
        f"• Tap <b>✅ Done reading</b> under any post to delete it.\n"
        f"• Send /stop at any time to pause delivery.",
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(sync_to_subscribers(context.application, target_chat_id=chat.id))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["store"].remove_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "🛑 Unsubscribed! Your delivery history is saved, so if you send /start later, you won't receive duplicate posts."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store: PostStore = context.application.bot_data["store"]
    busy = context.application.bot_data["sync_lock"].locked()
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"✅ <b>Uma-Index {MAX_PAGES}-Page Monitor Status</b>\n"
        f"• <b>Source Window:</b> Pages 1 to {MAX_PAGES}\n"
        f"• <b>Currently Syncing/Sending:</b> {'Yes (In Progress)' if busy else 'Idle'}\n"
        f"• <b>Posts Delivered to This Chat:</b> {store.count_user_deliveries(chat_id)}\n"
        f"• <b>Labelled 'try_again' (Skipped Until Restart):</b> {store.count_user_deliveries(chat_id, 'try_again')}\n"
        f"• <b>Total Cached Posts in DB:</b> {store.count_cached_posts()}\n"
        f"• <b>Active Subscribers/Channels:</b> {len(store.get_subscribers())}\n"
        f"• <b>Poll Interval:</b> {POLL_INTERVAL_SECONDS}s",
        parse_mode=ParseMode.HTML,
    )


async def cmd_forcecheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔍 Scanning Pages 1–{MAX_PAGES} of uma-index.top for any missed translations...")
    asyncio.create_task(sync_to_subscribers(context.application))


async def track_bot_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-subscribes channels/groups when the bot is added, unsubscribes when removed."""
    result, store = update.my_chat_member, context.application.bot_data["store"]
    chat, status = result.chat, result.new_chat_member.status

    if status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR):
        store.add_subscriber(chat.id, chat.title or chat.username or str(chat.id), chat.type)
        asyncio.create_task(sync_to_subscribers(context.application, target_chat_id=chat.id))
    elif status in (ChatMember.LEFT, ChatMember.BANNED):
        store.remove_subscriber(chat.id)


# =====================================================================
# ENTRY POINT
# =====================================================================
def main():
    store = PostStore(DB_PATH)
    if TARGET_CHAT_ID:
        store.add_subscriber(TARGET_CHAT_ID, title="Configured Target", chat_type="channel")

    # Generous timeouts: the PTB default (5s) is too short for uploading several original-quality
    # images, and every timeout used to trigger a retry that re-posted the same comic.
    app = (
        Application.builder().token(BOT_TOKEN)
        .connect_timeout(30).read_timeout(60).write_timeout(180).pool_timeout(30)
        .build()
    )
    app.bot_data.update(store=store, sync_lock=asyncio.Lock())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("forcecheck", cmd_forcecheck))
    app.add_handler(ChatMemberHandler(track_bot_chats, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(handle_done_reading, pattern=r"^del:"))

    app.job_queue.run_repeating(
        lambda ctx: sync_to_subscribers(ctx.application),
        interval=POLL_INTERVAL_SECONDS, first=5, name="uma_index_poller",
        job_kwargs={"max_instances": 1},
    )

    logger.info("Starting Uma-Index Bot (Direct Modal Scraper + Byte Upload + Startup Auto-Heal)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()