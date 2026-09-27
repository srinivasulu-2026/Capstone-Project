import os
import re
import sqlite3
import urllib.parse
import pandas as pd
import requests
from bs4 import BeautifulSoup

# --- CONSTANTS ---
BASE_URL = "https://books.toscrape.com/"
FIXED_GBP_TO_INR = 105.50  # Required fixed baseline conversion rate
DB_FILE = "books_database.db"

# Mapping star ratings from word to integer
RATING_MAP = {
    "One": 1,
    "Two": 2,
    "Three": 3,
    "Four": 4,
    "Five": 5
}

CATEGORIES = [
    {"name": "Travel", "url": urllib.parse.urljoin(BASE_URL, "catalogue/category/books/travel_2/index.html")},
    {"name": "Mystery", "url": urllib.parse.urljoin(BASE_URL, "catalogue/category/books/mystery_3/index.html")},
    {"name": "Historical Fiction", "url": urllib.parse.urljoin(BASE_URL, "catalogue/category/books/historical-fiction_4/index.html")},
]


# --- STEP 1: SCRAPING & CLEANING ---
def scrape_and_clean_data():
    raw_books = []

    for cat in CATEGORIES:
        category_name = cat["name"]
        current_url = cat["url"]
        count = 0

        print(f"Scraping category: {category_name}...")

        while current_url and count < 20:
            response = requests.get(current_url)
            if response.status_code != 200:
                print(f"Failed to fetch {current_url}")
                break

            soup = BeautifulSoup(response.text, "html.parser")
            articles = soup.find_all("article", class_="product_pod")

            for article in articles:
                if count >= 20:
                    break

                # Title
                title_tag = article.h3.find("a")
                title = title_tag["title"] if "title" in title_tag.attrs else title_tag.text

                # Raw Fields
                raw_price = article.find("p", class_="price_color").text
                star_tag = article.find("p", class_="star-rating")
                raw_rating = star_tag["class"][1] if star_tag and len(star_tag["class"]) > 1 else None
                raw_availability = article.find("p", class_="instock availability").text.strip()

                # --- CLEANING & PARSING ---
                # 1. Price GBP
                price_match = re.search(r"[\d.]+", raw_price)
                price_gbp = float(price_match.group()) if price_match else None

                # 2. Rating Integer (1-5)
                rating = RATING_MAP.get(raw_rating, None)

                # 3. Availability Boolean
                in_stock = 1 if "in stock" in raw_availability.lower() else 0

                raw_books.append({
                    "category_name": category_name,
                    "title": title,
                    "price_gbp": price_gbp,
                    "rating": rating,
                    "in_stock": in_stock
                })
                count += 1

            # Pagination
            next_button = soup.find("li", class_="next")
            if next_button and count < 20:
                next_page_rel = next_button.find("a")["href"]
                current_url = urllib.parse.urljoin(current_url, next_page_rel)
            else:
                current_url = None

    df = pd.DataFrame(raw_books)

    # --- ERROR HANDLING & IMPUTATION CHOICE JUSTIFICATION ---
    # Choice: Numeric fields (price_gbp, rating) are imputed with the median to avoid losing scraped rows.
    # Rows missing a critical structural field (like category_name or title) are dropped.
    if df['price_gbp'].isnull().any():
        median_price = df['price_gbp'].median()
        df['price_gbp'].fillna(median_price, inplace=True)
    
    if df['rating'].isnull().any():
        median_rating = int(df['rating'].median())
        df['rating'].fillna(median_rating, inplace=True)

    df.dropna(subset=['title', 'category_name'], inplace=True)

    # Calculate price_inr using fixed baseline rate
    df['price_inr'] = (df['price_gbp'] * FIXED_GBP_TO_INR).round(2)

    return df


# --- STEP 2: SQLITE DATABASE POPULATION ---
def setup_database(df, db_path=DB_FILE):
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Enable Foreign Key Support
    cursor.execute("PRAGMA foreign_keys = ON;")

    # Create Tables (Normalized Schema with PK/FK relationship)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        category_id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_name TEXT UNIQUE NOT NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS books (
        book_id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        price_gbp REAL NOT NULL,
        price_inr REAL NOT NULL,
        rating INTEGER NOT NULL,
        in_stock INTEGER NOT NULL,
        category_id INTEGER NOT NULL,
        FOREIGN KEY (category_id) REFERENCES categories (category_id)
    );
    """)
    conn.commit()

    # Insert Categories
    unique_categories = df['category_name'].unique()
    for cat in unique_categories:
        cursor.execute("INSERT OR IGNORE INTO categories (category_name) VALUES (?);", (cat,))
    conn.commit()

    # Map category names to IDs
    cat_mapping = pd.read_sql("SELECT category_id, category_name FROM categories;", conn)
    df = df.merge(cat_mapping, on='category_name')

    # Insert Books
    books_to_insert = df[['title', 'price_gbp', 'price_inr', 'rating', 'in_stock', 'category_id']]
    books_to_insert.to_sql('books', conn, if_exists='append', index=False)

    conn.close()
    print("Database built and populated successfully.")


# --- STEP 3: EXECUTE SQL QUERIES & COMPARISON ---
def execute_sql_and_pandas_validation(db_path=DB_FILE):
    conn = sqlite3.connect(db_path)

    queries = {
        "Query 1 (SELECT/WHERE)": "SELECT title, price_gbp, rating FROM books WHERE rating >= 4;",
        "Query 2 (ORDER BY / LIMIT)": "SELECT title, price_gbp, price_inr FROM books ORDER BY price_gbp DESC LIMIT 5;",
        "Query 3 (DISTINCT)": "SELECT DISTINCT rating FROM books ORDER BY rating ASC;",
        "Query 4 (BETWEEN / IN)": "SELECT title, price_gbp, rating FROM books WHERE rating IN (3, 5) AND price_gbp BETWEEN 20.0 AND 40.0;",
        "Query 5 (JOIN)": """
            SELECT b.title, c.category_name, b.price_gbp, b.rating 
            FROM books b 
            JOIN categories c ON b.category_id = c.category_id 
            WHERE b.rating = 5 
            ORDER BY c.category_name, b.price_gbp DESC 
            LIMIT 10;
        """
    }

    print("\n=================== EXECUTING SQL QUERIES ===================")
    for q_name, q_sql in queries.items():
        print(f"\n--- {q_name} ---")
        print(f"SQL: {q_sql.strip()}")
        result = pd.read_sql_query(q_sql, conn)
        print(result.to_string(index=False))

    # --- STEP 4: PANDAS READ_SQL VS MERGE COMPARISON ---
    print("\n=================== PANDAS READ_SQL vs MERGE COMPARISON ===================")
    
    # Approach A: pd.read_sql
    sql_join_res = pd.read_sql_query(queries["Query 5 (JOIN)"], conn)

    # Approach B: pd.merge in-memory
    df_books = pd.read_sql_query("SELECT * FROM books;", conn)
    df_categories = pd.read_sql_query("SELECT * FROM categories;", conn)

    merged_res = (
        pd.merge(df_books, df_categories, on='category_id')
        [df_books['rating'] == 5]
        [['title', 'category_name', 'price_gbp', 'rating']]
        .sort_values(by=['category_name', 'price_gbp'], ascending=[True, False])
        .head(10)
        .reset_index(drop=True)
    )

    print("\n1. pd.read_sql Result:")
    print(sql_join_res.to_string(index=False))

    print("\n2. pd.merge Result:")
    print(merged_res.to_string(index=False))

    # Verification check
    are_equal = sql_join_res.equals(merged_res)
    print(f"\nDo pd.read_sql and pd.merge outputs match exactly? -> {are_equal}")

    conn.close()


if __name__ == "__main__":
    df_clean = scrape_and_clean_data()
    setup_database(df_clean)
    execute_sql_and_pandas_validation()