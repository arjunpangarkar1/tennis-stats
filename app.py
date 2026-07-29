import sys
import streamlit as st
import anthropic
import duckdb

st.set_page_config(page_title="Tennis Clutch Engine", page_icon="🎾")

st.title("🎾 Ask the Tennis Data")
st.caption("ATP singles matches, 2000–2026. Every answer comes from real match data.")

import os
import subprocess

@st.cache_resource
def get_connection():
    if not os.path.exists("tennis.db"):
        subprocess.run([sys.executable, "load_data.py"], check=True)
    return duckdb.connect("tennis.db")

con = get_connection()
client = anthropic.Anthropic()

SCHEMA = """
Table: matches — one row per ATP match, 2000 through 2026 (~78,000 matches).

Key columns:
- year (int): season, 2000-2026
- winner_name, loser_name (text)
- tourney_name (text): tournament name, e.g. 'Wimbledon', 'Roland Garros', 'Us Open', 'Australian Open', 'Indian Wells'
- tourney_level (text): 'G' = Grand Slam, 'M' = Masters 1000, 'A' = ATP tour, 'F' = Tour Finals
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
   - Questions about a SPECIFIC TOURNAMENT (e.g. "at Wimbledon", "at the US Open")
     must filter on tourney_name, NOT on surface. Wimbledon ≠ all grass courts.

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

if "count" not in st.session_state:
    st.session_state.count = 0

if st.session_state.count >= 10:
    st.warning("You've hit the question limit for this session. Refresh to reset — but be kind, this runs on my API credits!")
    st.stop()
question = st.text_input("Ask away:", placeholder="Who is the most clutch player on clay?")
if question:
    st.session_state.count += 1

if question:
    with st.spinner("Checking the question..."):
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

Reply with: SUBJECTIVE
ONLY for these narrow cases:
- "Who is the GOAT / greatest of all time?"
- "Is [player X] better than [player Y]?" (direct player-vs-player greatness comparison)
- Questions with no measurable target at all.

EVERYTHING ELSE IS ANSWERABLE. In particular, "who is the best at [tournament]",
"who is the best on [surface]", "who is the best server" are ALL ANSWERABLE —
they map to win rate or a specific stat. Default to ANSWERABLE when in doubt.""",
            messages=[{"role": "user", "content": question}],
        )
        verdict = check.content[0].text.strip()

    if verdict.startswith("SUBJECTIVE"):
        st.warning("That's a debate, not a stat — the data can inform it, but can't settle it.")
        st.write(verdict.replace("SUBJECTIVE", "").strip())
        st.stop()

    error_feedback = ""
    result_text = None

    with st.spinner("Querying the data..."):
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
                st.error("I can only answer questions that read the data. Try rephrasing!")
                st.stop()

            try:
                result = con.sql(sql)
                result_df = result.df()
                result_text = str(result)
                break
            except Exception as e:
                error_feedback = str(e)

    if result_text is None:
        st.error("I couldn't build a working query for that. Try rephrasing!")
        st.stop()

    with st.spinner("Writing the answer..."):
        answer = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
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
            messages=[{"role": "user", "content": f"Question: {question}\n\nData:\n{result_text}"}],
        )

    st.success(answer.content[0].text)

    st.dataframe(result_df, hide_index=True)

    with st.expander("See the SQL that produced this"):
        st.code(sql, language="sql")