# books-scraper

A web scraper for the [Books to Scrape](https://books.toscrape.com) practice sandbox.

It walks the site the way a person would — home page, category listing, every page of
that listing, then every individual product page — and writes one row per book to a CSV.
The dataset is built entirely by this program; no pre-packaged copy of the data is used.

## Install and run

```bash
pip install -r requirements.txt
python scrape_books.py
```

Output: `data/books.csv` and `data/books.xlsx`.

Useful flags:

```bash
python scrape_books.py --categories Fiction Mystery   # scrape a subset
python scrape_books.py --delay 1.5                    # be more polite
python scrape_books.py --max-books 5                  # quick smoke test
python scrape_books.py --refresh                      # ignore the resume cache
```

Each book is written to `raw/<category>.jsonl` as soon as it is parsed, so an interrupted
run resumes where it stopped instead of re-downloading pages. `--refresh` forces a clean
re-scrape. Every request is separated by a randomised pause (`--delay`, default 0.75s).

## Dataset

296 books across 5 categories, 20 columns, no missing values.

| category | books | mean price | mean rating |
|---|---|---|---|
| Nonfiction | 110 | 34.26 | 2.88 |
| Fiction | 65 | 36.07 | 3.18 |
| Young Adult | 54 | 35.45 | 3.30 |
| Romance | 35 | 33.93 | 2.63 |
| Mystery | 32 | 31.72 | 2.94 |

### Required columns

| column | type | description |
|---|---|---|
| `title` | text | Title of the book |
| `description` | text | Product description |
| `category` | text | Fiction, Nonfiction, Mystery, Romance or Young Adult |
| `price` | float | Book price, GBP |
| `rating` | int 1–5 | Star rating converted from the CSS class |
| `availability` | int | Number of copies shown as available |
| `number_reviews` | int | Number of reviews |
| `upc` | text | Product identifier |
| `product_url` | text | URL of the individual product page |

### Extra columns

`product_id`, `price_excl_tax`, `price_incl_tax`, `tax`, `in_stock`, `availability_text`,
`rating_label`, `product_type`, `image_url`, `category_from_breadcrumb`,
`breadcrumb_matches_category`.

`category` comes from the listing page the book was found on. `category_from_breadcrumb` is
re-read from the product page and `breadcrumb_matches_category` records whether the two
agree (296/296 for this run) — a cheap integrity check on the crawl.

## Two things to know about the data

**`upc` is text, not a number.** The assignment lists it as numerical, but all 296 values
are hex strings such as `a897fe39b1053632`. Casting to a numeric type destroys them.

**`number_reviews` is 0 for all 296 books.** This is the site's real data, not a parse
failure: books.toscrape.com always reports 0 in the product table, and reviews live on
separate `/reviews/` pages. Verified by re-fetching a random sample of rows and comparing
against the live pages (20/20 exact on price, rating and review count).

Some descriptions end in a literal `" ...more"`. That truncation is in the source site,
and the text is stored verbatim rather than silently edited.

## Layout

```
scrape_books.py     the scraper
requirements.txt    dependencies
data/               generated dataset (csv + xlsx)
raw/                resume cache, git-ignored
```
(Add Books to Scrape web scraper and generated dataset)
