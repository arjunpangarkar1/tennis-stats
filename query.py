import duckdb

con = duckdb.connect("tennis.db")

result = con.sql("""
    SELECT player,
           sum(saved) * 100.0 / sum(faced) AS bp_save_pct,
           sum(faced) AS faced
    FROM (
      SELECT winner_name AS player, w_bpSaved AS saved, w_bpFaced AS faced FROM matches
      UNION ALL
      SELECT loser_name AS player, l_bpSaved AS saved, l_bpFaced AS faced FROM matches
    )
    GROUP BY player
    HAVING sum(faced) >= 150
    ORDER BY bp_save_pct DESC
    LIMIT 10
""")

print(result)