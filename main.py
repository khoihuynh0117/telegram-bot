"""Telegram bot: tìm IC STK4026 và loa Yamaha MS202II trên các trang bán hàng,
chỉ gửi những kết quả MỚI (chưa gửi trước đó, không trùng) về Telegram.

Nguồn tìm: các trang bán hàng (qua tìm kiếm web) + Chợ Tốt (API trực tiếp).
"""
import html
import json
import logging
import os
import re
import time
from urllib.parse import quote, urlsplit

import requests
from ddgs import DDGS

# ====== ĐIỀN 2 DÒNG NÀY (giữ nguyên dấu ngoặc kép) ======
TOKEN = "9298916796:AAGUipWdrWPxW1GU_efvpp--ginHJLbd92w"
CHAT_ID = "6500543897"
# ========================================================

INTERVAL = 50  # giây nghỉ giữa 2 lượt quét (50 giây dễ bị chặn, tăng lên nếu thấy lỗi 429)
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
MAX_PER_ITEM = 10  # tối đa số tin gửi mỗi món trong 1 lần quét

# Mỗi món: tên hiển thị, từ khóa tìm, và chuỗi bắt buộc phải xuất hiện trong tiêu đề/link
ITEMS = [
    {
        "name": "IC STK4026",
        "queries": ["STK4026", "IC STK4026"],
        "must": ["stk4026"],
    },
    {
        "name": "Loa Yamaha MS202II",
        "queries": ["Yamaha MS202II", "Yamaha MS202 II"],
        "must": ["ms202"],
    },
]

# Chỉ tìm trên các trang bán hàng này (sửa/thêm tùy ý)
SHOP_SITES = [
    "shopee.vn",
    "lazada.vn",
    "tiki.vn",
    "sendo.vn",
    "aliexpress.com",
    "ebay.com",
    "hshop.vn",
    "nshopvn.com",
    "linhkienx.vn",
    "mualinhkien.vn",
    "thegioiic.com",
    "facebook.com",
]

# Facebook yêu cầu đăng nhập nên bot không tự quét sâu được.
# Bot gửi sẵn link tìm kiếm Facebook để bạn bấm xem trực tiếp.
FB_LINKS = {
    "IC STK4026": "STK4026",
    "Loa Yamaha MS202II": "Yamaha MS202",
}

# Bỏ các link không phải tin bán hàng
BAD_URL = ["/videos/", "/reel", "/watch", "/hashtag", "/category", "/blog", "/news"]
SALE_WORDS = re.compile(r"\b(mua|bán|giá|sell|selling|price|ship|for sale|in stock)\b", re.I)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for noisy in ("ddgs", "primp", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def norm(text: str) -> str:
    return re.sub(r"[\s\-_]", "", (text or "").lower())


def canon(url: str) -> str:
    """Chuẩn hóa link để cùng một tin luôn ra cùng một khóa."""
    p = urlsplit(url)
    host = re.sub(r"^(www|m)\.", "", p.netloc.lower())
    key = host + p.path.rstrip("/").lower()
    if "facebook.com" in host and p.query:  # bài FB phân biệt bằng tham số
        key += "?" + p.query
    return key


def load_seen() -> set:
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False)


def send(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if not r.ok:
        log.error("Telegram lỗi: %s %s", r.status_code, r.text)


def search_site(query: str, site: str):
    """Tìm trong đúng một trang bán hàng."""
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(f"{query} site:{site}", region="vn-vi", max_results=10):
                url = r.get("href", "")
                if site not in url:
                    continue
                yield r.get("title", ""), url, r.get("body", ""), site
    except Exception as e:  # noqa: BLE001
        log.warning("Tìm trên %s lỗi (%s): %s", site, query, e)


def search_chotot(query: str):
    try:
        r = requests.get(
            "https://gateway.chotot.com/v1/public/ad-listing",
            params={"q": query, "limit": 20},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        r.raise_for_status()
        for ad in r.json().get("ads", []):
            title = ad.get("subject", "")
            price = ad.get("price_string", "")
            region = ad.get("area_name") or ad.get("region_name") or ""
            url = f"https://www.chotot.com/{ad.get('list_id')}.htm"
            yield title, url, f"{price} - {region}".strip(" -"), "Chợ Tốt"
    except Exception as e:  # noqa: BLE001
        log.warning("Chợ Tốt lỗi (%s): %s", query, e)


def run_once(seen: set) -> None:
    for item in ITEMS:
        found = {}  # khóa chuẩn hóa -> (url gốc, tiêu đề, mô tả, nguồn, khóa tiêu đề)
        titles = set()

        def consider(results):
            for title, url, body, src in results:
                if not url:
                    continue
                key = canon(url)
                clean_title = re.sub(r"#\w+", "", title)
                tkey = f"t:{item['name']}:{src}:{norm(clean_title)[:80]}"
                if key in seen or tkey in seen or key in found or tkey in titles:
                    continue
                # Từ khóa phải nằm trong TIÊU ĐỀ hoặc LINK (không tính hashtag, không tính mô tả)
                if not any(m in norm(clean_title + " " + url) for m in item["must"]):
                    continue
                low = url.lower()
                if any(b in low for b in BAD_URL) or re.search(r"/c\d+", low):
                    continue
                # Facebook: chỉ nhận Marketplace hoặc bài có từ khóa mua bán
                if "facebook.com" in low and "/marketplace/item" not in low:
                    if not SALE_WORDS.search(title + " " + body):
                        continue
                found[key] = (url, title, body, src, tkey)
                titles.add(tkey)

        for q in item["queries"]:
            for site in SHOP_SITES:
                consider(search_site(q, site))
                time.sleep(2)  # tránh bị chặn vì gửi quá nhanh
            consider(search_chotot(q))

        log.info("%s: %d kết quả mới", item["name"], len(found))

        # Đánh dấu "đã thấy" TRƯỚC khi gửi, để lỡ lỗi/tắt giữa chừng cũng không gửi lại
        for key, (_, _, _, _, tkey) in found.items():
            seen.add(key)
            seen.add(tkey)
        save_seen(seen)

        for key, (url, title, body, src, _) in list(found.items())[:MAX_PER_ITEM]:
            send(
                f"🔎 <b>{html.escape(item['name'])}</b> · {src}\n"
                f"{html.escape(title)}\n"
                f"{html.escape(body[:200])}\n"
                f"{url}"
            )
            time.sleep(1)


def main() -> None:
    seen = load_seen()
    send(f"✅ Bot đã chạy. Nghỉ {INTERVAL} giây giữa các lượt quét STK4026 và Yamaha MS202II.")
    lines = ["📘 <b>Link tìm trực tiếp trên Facebook</b> (bấm để xem):"]
    for name, kw in FB_LINKS.items():
        q = quote(kw)
        lines.append(
            f"\n<b>{html.escape(name)}</b>\n"
            f"• Marketplace: https://www.facebook.com/marketplace/search/?query={q}\n"
            f"• Bài đăng/nhóm: https://www.facebook.com/search/posts/?q={q}"
        )
    send("\n".join(lines))
    while True:
        try:
            run_once(seen)
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi vòng quét: %s", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
