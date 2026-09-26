"""
Web scraper for the "Books to Scrape" practice sandbox (https://books.toscrape.com).

The program navigates the site the same way a person would:

    home page  ->  category listing page  ->  every page of that listing
               ->  every individual product page

One row of the resulting dataset == one book. Nothing is read from a
pre-packaged copy of the data; every field is parsed out of the live HTML.

Usage
-----
    pip install -r requirements.txt
    python scrape_books.py
    python scrape_books.py --categories Fiction Mystery --delay 0.4
    python scrape_books.py --max-books 10 --no-cache      # quick smoke test
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_URL = "https://books.toscrape.com/"

# The five categories required by the assignment. Names are matched against the
# category list that is discovered from the site's own sidebar navigation, so the
# scraper does not hard-code the numeric slugs the site happens to use today.
DEFAULT_CATEGORIES = ["Fiction", "Nonfiction", "Mystery", "Romance", "Young Adult"]

RATING_BY_CLASS = {
    "One": 1,
    "Two": 2,
    "Three": 3,
    "Four": 4,
    "Five": 5,
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

PRICE_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
AVAILABLE_RE = re.compile(r"\((\d+)\s+available\)")
PRODUCT_ID_RE = re.compile(r"_(\d+)/index\.html$")
DIGITS_RE = re.compile(r"(\d+)")


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #


def build_session() -> requests.Session:
    """A session that retries flaky requests instead of dying on one 500."""
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    retries = Retry(
        total=4,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class PoliteClient:
    """GET wrapper that pauses a randomised amount between every request.

    The sandbox is built for scraping practice, but a delay is still polite
    practice (and it keeps you inside any rate limit if you point this at a
    real site).  The jitter avoids a perfectly periodic request pattern.
    """

    def __init__(self, session: requests.Session, delay: float = 0.75) -> None:
        self.session = session
        self.delay = max(delay, 0.0)
        self._last_request_at = 0.0
        self.request_count = 0

    def get(self, url: str) -> str | None:
        target = urljoin(BASE_URL, url)
        elapsed = time.monotonic() - self._last_request_at
        if self.request_count and elapsed < self.delay:
            pause = random.uniform(self.delay * 0.75, self.delay * 1.25)
            time.sleep(max(0.0, pause - elapsed))
        try:
            response = self.session.get(target, timeout=30)
        except requests.RequestException as exc:
            print(f"  ! request failed ({exc.__class__.__name__}): {target}", file=sys.stderr)
            return None
        self._last_request_at = time.monotonic()
        self.request_count += 1
        if response.status_code != 200:
            print(f"  ! HTTP {response.status_code}: {target}", file=sys.stderr)
            return None
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def clean_text(node: Any) -> str:
    """Collapse all whitespace so multi-line HTML becomes one tidy string."""
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def to_float(text: str) -> float | None:
    """'£51.77' -> 51.77.  Returns None when no digits are present."""
    match = PRICE_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def to_int(text: str, default: int = 0) -> int:
    match = DIGITS_RE.search(text or "")
    return int(match.group(1)) if match else default


def first_node(soup: BeautifulSoup, selector: str) -> Any:
    try:
        return soup.select_one(selector)
    except Exception:
        return None


def product_information(soup: BeautifulSoup) -> dict[str, str]:
    """Flatten the 'Product Information' table into {label: value}."""
    info: dict[str, str] = {}
    table = first_node(soup, "table.table")
    if table is None:
        return info
    for row in table.find_all("tr"):
        header = row.find("th")
        cell = row.find("td")
        if header is not None and cell is not None:
            info[clean_text(header)] = clean_text(cell)
    return info


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #


def discover_categories(client: PoliteClient) -> dict[str, str]:
    """Read the sidebar on the home page to map category name -> listing URL."""
    html = client.get(BASE_URL)
    if html is None:
        raise SystemExit("Could not reach https://books.toscrape.com/ - check your connection.")

    soup = BeautifulSoup(html, "html.parser")
    categories: dict[str, str] = {}
    for anchor in soup.select("div.side_categories ul.nav-list li a"):
        name = clean_text(anchor)
        href = anchor.get("href")
        if name and href:
            categories.setdefault(name, urljoin(BASE_URL, href))
    return categories


def total_pages_on_listing(soup: BeautifulSoup) -> int | None:
    """'Page 1 of 4' -> 4.  Used only for progress reporting."""
    current = first_node(soup, "ul.pager li.current")
    if current is None:
        return None
    match = re.search(r"of\s+(\d+)", clean_text(current))
    return int(match.group(1)) if match else None


def listing_page_urls(client: PoliteClient, listing_url: str) -> list[str]:
    """Walk the 'next' pager links and return every page of a category."""
    pages: list[str] = []
    url: str | None = listing_url
    seen: set[str] = set()

    while url and url not in seen:
        seen.add(url)
        html = client.get(url)
        if html is None:
            break
        soup = BeautifulSoup(html, "html.parser")

        if not pages:
            expected = total_pages_on_listing(soup)
            print(f"    listing reports {expected or '?'} page(s)")

        pages.append(urljoin(BASE_URL, url))

        next_anchor = first_node(soup, "ul.pager li.next a")
        url = urljoin(url, next_anchor["href"]) if next_anchor is not None and next_anchor.get("href") else None

    return pages


def product_urls_on_listing(html: str, page_url: str) -> list[str]:
    """Absolute URLs of every product on one category listing page."""
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for anchor in soup.select("article.product_pod h3 a"):
        href = anchor.get("href")
        if href:
            url = urljoin(page_url, href)
            if url not in urls:
                urls.append(url)
    return urls


def breadcrumb_category(html: str) -> str:
    """The category shown in a *product* page breadcrumb.

    A product breadcrumb is:  Home > Books > Poetry > A Light in the Attic
    so the leaf item is the book title and the category is the item before it.
    (On a category listing page the leaf *is* the category.)
    """
    soup = BeautifulSoup(html, "html.parser")
    crumbs = soup.select("ul.breadcrumb li")
    if len(crumbs) < 2:
        return ""
    return clean_text(crumbs[-2])


# --------------------------------------------------------------------------- #
# Product page parsing
# --------------------------------------------------------------------------- #


def parse_product(html: str, url: str, category: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    price_text = clean_text(first_node(soup, "div.product_main p.price_color"))
    star_node = first_node(soup, "div.product_main p.star-rating")
    rating_label = ""
    rating: int | None = None
    if star_node is not None:
        for css_class in star_node.get("class", []):
            if css_class in RATING_BY_CLASS:
                rating_label = css_class
                rating = RATING_BY_CLASS[css_class]
                break

    availability_text = clean_text(first_node(soup, "div.product_main p.availability"))
    available_match = AVAILABLE_RE.search(availability_text)
    availability = int(available_match.group(1)) if available_match else 0

    # The description paragraph is a sibling of the #product_description header,
    # which keeps working even when a book has no description at all.
    description = clean_text(first_node(soup, "div#product_description ~ p"))

    info = product_information(soup)
    upc = info.get("UPC", "").strip()

    product_id_match = PRODUCT_ID_RE.search(urlparse(url).path)
    product_id = int(product_id_match.group(1)) if product_id_match else None

    image_node = first_node(soup, "div.item.active img")
    if image_node is None:
        image_node = first_node(soup, "img.thumbnail")

    price_excl_tax = to_float(info.get("Price (excl. tax)", ""))
    if price_excl_tax is None:
        price_excl_tax = to_float(price_text)
    price_incl_tax = to_float(info.get("Price (incl. tax)", ""))

    crumb_category = breadcrumb_category(html)

    record: dict[str, Any] = {
        "title": clean_text(first_node(soup, "div.product_main h1")),
        "description": description,
        "category": category,
        "price": to_float(price_text),
        "rating": rating,
        "availability": availability,
        "number_reviews": to_int(info.get("Number of reviews", "")),
        "upc": upc,
        "product_url": url,
        # --- extra variables that are useful downstream ------------------- #
        "product_id": product_id,
        "category_from_breadcrumb": crumb_category,
        "breadcrumb_matches_category": (not crumb_category) or crumb_category == category,
        "price_excl_tax": price_excl_tax,
        "price_incl_tax": price_incl_tax,
        "tax": to_float(info.get("Tax", "")),
        "in_stock": bool(available_match),
        "availability_text": availability_text,
        "rating_label": rating_label,
        "product_type": info.get("Product Type", ""),
        "image_url": urljoin(url, image_node["src"]) if image_node is not None and image_node.get("src") else None,
    }
    return record


# --------------------------------------------------------------------------- #
# Cache (makes an interrupted run cheap to restart)
# --------------------------------------------------------------------------- #


def load_cache(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def append_cache(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def scrape_category(
    client: PoliteClient,
    category: str,
    listing_url: str,
    cache_path: Path,
    use_cache: bool = True,
    max_books: int | None = None,
) -> list[dict[str, Any]]:
    print(f"\n[{category}] listing: {listing_url}")

    cached: list[dict[str, Any]] = load_cache(cache_path) if use_cache else []
    already_done = {row.get("product_url") for row in cached}
    print(f"    {len(cached)} book(s) restored from {cache_path.name if cached else 'no cache'}")

    if max_books is not None:
        already_done = set(list(already_done)[:max_books])
        cached = [row for row in cached if row.get("product_url") in already_done]

    records: list[dict[str, Any]] = list(cached)
    seen: set[str] = set(already_done)
    failures: list[str] = []

    pages = listing_page_urls(client, listing_url)
    print(f"    {len(pages)} listing page(s) to walk")

    queued: list[str] = []
    for page_url in pages:
        html = client.get(page_url)
        if html is None:
            failures.append(page_url)
            continue
        queued.extend(product_urls_on_listing(html, page_url))

    wanted = [url for url in dict.fromkeys(queued) if url not in seen]
    if max_books is not None:
        wanted = wanted[: max(0, max_books - len(records))]
    print(f"    {len(queued)} product link(s) found, {len(wanted)} still to fetch")

    for index, url in enumerate(wanted, start=1):
        html = client.get(url)
        if html is None:
            failures.append(url)
            continue
        record = parse_product(html, url, category)
        if not record["title"] or record["rating"] is None:
            failures.append(url)
            continue
        records.append(record)
        append_cache(cache_path, record)
        seen.add(url)
        if index % 20 == 0 or index == len(wanted):
            print(f"    {index}/{len(wanted)} product pages parsed")

    print(f"    done: {len(records)} book(s), {len(failures)} page failure(s)")
    return records


def to_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame.from_records(records)

    if frame.empty:
        return frame

    # Keep every book unique; the product URL is the natural key.
    frame = frame.drop_duplicates(subset=["product_url"], keep="first")

    int_columns = [
        "rating",
        "availability",
        "number_reviews",
        "product_id",
    ]
    for column in int_columns:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")

    float_columns = ["price", "price_excl_tax", "price_incl_tax", "tax"]
    for column in float_columns:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").round(2)

    if "category" in frame.columns:
        frame["category"] = frame["category"].astype("string")

    ordered = [
        "title",
        "description",
        "category",
        "price",
        "rating",
        "availability",
        "number_reviews",
        "upc",
        "product_url",
        "product_id",
        "price_excl_tax",
        "price_incl_tax",
        "tax",
        "in_stock",
        "availability_text",
        "rating_label",
        "product_type",
        "image_url",
        "category_from_breadcrumb",
        "breadcrumb_matches_category",
    ]
    columns = [column for column in ordered if column in frame.columns]
    columns += [column for column in frame.columns if column not in columns]
    frame = frame[columns]
    frame = frame.sort_values(["category", "title"], ignore_index=True)
    return frame


def report(frame: pd.DataFrame) -> None:
    print("\n" + "=" * 68)
    print("DATASET SUMMARY")
    print("=" * 68)
    print(f"rows (books)      : {len(frame)}")
    print(f"columns           : {len(frame.columns)}")
    print(f"unique categories : {frame['category'].nunique()}")
    print("\nbooks per category:")
    print(frame["category"].value_counts().to_string())
    print("\nrating distribution (1-5):")
    print(frame["rating"].value_counts().sort_index().to_string())
    print(f"\nprice  : min {frame['price'].min():.2f}  max {frame['price'].max():.2f}"
          f"  mean {frame['price'].mean():.2f}")
    print(f"stock  : total copies available {int(frame['availability'].sum())}")
    print(f"reviews: total {int(frame['number_reviews'].sum())}")
    if "breadcrumb_matches_category" in frame.columns:
        mismatches = int((~frame["breadcrumb_matches_category"].astype(bool)).sum())
        print(f"check  : breadcrumb agrees with listing category for "
              f"{len(frame) - mismatches}/{len(frame)} books")
    print("\nmissing values per column:")
    missing = frame.isna().sum()
    print(missing[missing > 0].to_string() if missing.any() else "  none")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape books.toscrape.com across five categories.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=DEFAULT_CATEGORIES,
        help="Category names exactly as listed in the site sidebar.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.75,
        help="Minimum seconds to wait between HTTP requests.",
    )
    parser.add_argument(
        "--out",
        default="data/books.csv",
        help="Path of the CSV dataset to write.",
    )
    parser.add_argument(
        "--xlsx",
        default="data/books.xlsx",
        help="Path of the Excel copy to write.",
    )
    parser.add_argument(
        "--cache-dir",
        default="raw",
        help="Directory holding the per-category JSONL resume cache.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore (and do not write) the resume cache.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached rows for the categories being scraped.",
    )
    parser.add_argument(
        "--max-books",
        type=int,
        default=None,
        help="Stop after this many books per category (for testing).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = PoliteClient(build_session(), delay=args.delay)

    print(f"Polite delay: ~{args.delay:.2f}s between requests")
    print("Reading the site navigation ...")
    available = discover_categories(client)
    print(f"  found {len(available)} categories in the sidebar")

    selected: dict[str, str] = {}
    for name in args.categories:
        if name not in available:
            close = [c for c in available if c.lower() == name.lower()]
            if not close:
                print(f"  ! unknown category {name!r}; skipping", file=sys.stderr)
                continue
            name = close[0]
        selected[name] = available[name]

    if not selected:
        print("No valid categories selected.", file=sys.stderr)
        return 1

    cache_dir = Path(args.cache_dir)
    all_records: list[dict[str, Any]] = []

    for category, listing_url in selected.items():
        cache_path = cache_dir / f"{category.replace(' ', '_').lower()}.jsonl"
        use_cache = not (args.no_cache or args.refresh)
        all_records.extend(
            scrape_category(
                client,
                category,
                listing_url,
                cache_path,
                use_cache=use_cache,
                max_books=args.max_books,
            )
        )

    frame = to_frame(all_records)
    if frame.empty:
        print("No books were scraped.", file=sys.stderr)
        return 1

    csv_path = Path(args.out)
    xlsx_path = Path(args.xlsx)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)

    frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
    try:
        frame.to_excel(xlsx_path, index=False)
    except Exception as exc:  # openpyxl is optional
        print(f"  ! Excel export skipped ({exc})", file=sys.stderr)

    report(frame)
    print(f"\nHTTP requests made this run: {client.request_count}")
    print(f"CSV   -> {csv_path.resolve()}")
    if xlsx_path.exists():
        print(f"Excel -> {xlsx_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
