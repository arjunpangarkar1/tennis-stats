import anthropic
import duckdb
con = duckdb.connect("tennis.db")

client = anthropic.Anthropic()

SCHEMA = """
Table: matches — one row per ATP match, 2000 through 2026 (~78,000 matches).

Key columns:
- year (int): season, 2000-2026
- winner_name, loser_name (text)
- surface (text): 'Hard', 'Clay', 'Grass', or 'Carpet' (Carpet only appears in older years, phased out after ~2009)
- round (text): 'F' final, 'SF' semi, 'QF' quarter, 'R16', 'R32', etc.
- score (text)
- w_bpSaved, w_bpFaced (int): break points saved/faced BY THE WINNER of that match
- l_bpSaved, l_bpFaced (int): break points saved/faced BY THE LOSER of that match
- w_ace, w_df, l_ace, l_df (int): aces and double faults

IMPORTANT RULES:
1. A player's stats are split across two column sets: when they WIN, their data is in
   the w_ columns; when they LOSE, it's in the l_ columns.

   CRITICAL: You MUST union the RAW MATCH ROWS FIRST in a subquery, and ONLY THEN
   apply GROUP BY / HAVING / aggregation on the combined result. Never GROUP BY inside
   each half of the UNION and then union the summaries — that produces two separate
   rows per player and cherry-picks their wins.

   CORRECT pattern:

   SELECT player, sum(saved) * 100.0 / sum(faced) AS rate, sum(faced) AS faced
   FROM (
     SELECT winner_name AS player, year, surface, w_bpSaved AS saved, w_bpFaced AS faced FROM matches
     UNION ALL
     SELECT loser_name AS player, year, surface, l_bpSaved AS saved, l_bpFaced AS faced FROM matches
   )
   GROUP BY player
   HAVING sum(faced) >= 150

   WRONG (never do this):
   SELECT winner_name, sum(w_bpSaved)... FROM matches GROUP BY winner_name
   UNION ALL
   SELECT loser_name, sum(l_bpSaved)... FROM matches GROUP BY loser_name

2. METRIC SELECTION — choose the metric that matches the question:

   - "best" / "greatest" / "most dominant" on a surface or overall
     → WIN RATE and match wins. Compute using:

       SELECT player, sum(won) AS wins, count(*) AS matches,
              sum(won) * 100.0 / count(*) AS win_pct
       FROM (
         SELECT winner_name AS player, surface, 1 AS won FROM matches
         UNION ALL
         SELECT loser_name AS player, surface, 0 AS won FROM matches
       )
       GROUP BY player
       HAVING count(*) >= 50

   - "clutch" / "under pressure" / "saves break points"
     → break point save rate = sum(saved) * 100.0 / sum(faced)

   - "most wins", "most matches won" → simple count of wins

   Do NOT use break point save rate for "best" questions. Break point save rate
   measures serving under pressure — it is NOT a measure of overall quality.

3. ALWAYS apply a minimum sample size with HAVING (e.g. HAVING sum(faced) >= 150),
   otherwise players with tiny samples show fake 100% rates.

4. Use * 100.0 (not * 100) to avoid integer division.

5. For "best X per group" questions (e.g. most clutch player per surface, per year),
   do NOT use LIMIT — that only returns rows from one group. Use QUALIFY with a window
   function, placed BEFORE the ORDER BY:

   QUALIFY ROW_NUMBER() OVER (PARTITION BY surface ORDER BY clutch_rate DESC) = 1
"""

question = input("Ask a tennis question: ")
check = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=200,
    system="""You classify tennis questions for a stats app that queries ATP match data
(2000-2026) with columns for wins, losses, surfaces, rounds, aces, double faults, and
break points saved/faced.

This app DOES have working definitions for:
- "clutch" = break point save rate (sum saved / sum faced)
- "best on surface X" = win-loss record on that surface
- "most wins / titles / dominance" = counts of matches won
- any question about counts, rates, records, or comparisons of specific players

Reply with exactly: ANSWERABLE
for any question that can be computed from those columns, even if it uses words like
"clutch" or "best" — as long as it maps to a measurable stat.

Reply with: SUBJECTIVE (followed by a brief note on what stats could inform it)
ONLY for questions that cannot be settled by any single statistic — e.g. "who is the
GOAT?", "is Federer better than Nadal?", "who is the greatest player ever?" — where the
answer depends on which achievements one values.""",
    messages=[{"role": "user", "content": question}],
)

verdict = check.content[0].text.strip()

if verdict.startswith("SUBJECTIVE"):
    print("\nThat's a debate, not a stat — the data can inform it, but can't settle it.\n")
    print(verdict.replace("SUBJECTIVE", "").strip())
    print("\nTry asking something measurable, like 'who saved the most break points on clay?'")
    exit()

error_feedback = ""
result_text = None

for attempt in range(3):
    user_msg = question
    if error_feedback:
        user_msg = f"{question}\n\nYour previous SQL failed:\n{error_feedback}\nFix it and return only the corrected SQL."

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=f"You write DuckDB SQL queries. Schema:\n{SCHEMA}\nReturn ONLY the SQL, no explanation, no markdown.",
        messages=[{"role": "user", "content": user_msg}],
    )

    sql = message.content[0].text
    sql = sql.replace("```sql", "").replace("```", "").strip()

    if not sql.lower().startswith("select") and not sql.lower().startswith("with"):
        print("Sorry, I can only answer questions that read the data. Try rephrasing your question!")
        exit()

    print(f"Attempt {attempt + 1} — SQL:\n{sql}\n")

    try:
        result = con.sql(sql)
        result_text = str(result)
        break
    except Exception as e:
        error_feedback = str(e)
        print(f"Failed: {error_feedback}\n")

if result_text is None:
    print("Sorry, I couldn't build a working query for that. Try rephrasing!")
    exit()

answer = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=200,
    system="""You explain tennis query results clearly and honestly.

Rules:
- State the answer, and ALWAYS name the metric you measured (e.g. "highest break point
  SAVE rate" — not "best" or "most clutch" without qualification).
- If the top results are within ~1 percentage point of each other, say they are
  statistically too close to separate rather than declaring a single winner.
- Note relevant caveats briefly (e.g. break point save rate measures serving under
  pressure, not overall clutchness).
- ONLY state numbers that appear in the data provided. Never add facts (like titles
  or rankings) that are not in the result.
  - The data given to you is the FINAL, correct result of a query that already did the
  ranking and filtering. If it contains one row, that row IS the answer — state it
  confidently. Never say you need more data or cannot compare.
- Two or three sentences maximum.""",
    messages=[
        {"role": "user", "content": f"Question: {question}\n\nData:\n{result_text}"}
    ],
)

print(answer.content[0].text)