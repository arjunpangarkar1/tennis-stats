import duckdb
import urllib.request
import os

con = duckdb.connect("tennis.db")

# Start fresh so we don't double-load
con.sql("DROP TABLE IF EXISTS matches")

base = "https://raw.githubusercontent.com/Tennismylife/TML-Database/master/"

for year in range(2000, 2027):
    filename = f"{year}.csv"
    if not os.path.exists(filename):
        print(f"Downloading {year}...")
        urllib.request.urlretrieve(base + filename, filename)

    # First year creates the table; the rest append
    reader = f"read_csv('{filename}', null_padding=true, ignore_errors=true)"
    if year == 2000:
        con.sql(f"CREATE TABLE matches AS SELECT *, {year} AS year FROM {reader}")
    else:
        con.sql(f"INSERT INTO matches SELECT *, {year} AS year FROM {reader}")
    print(f"Loaded {year}")

total = con.sql("SELECT count(*) FROM matches").fetchone()[0]
print(f"\nDone. {total} total matches across all years.")