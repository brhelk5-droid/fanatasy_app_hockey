"""
Fantasy Hockey Assistant - Streamlit App
=========================================
Three tools in one app:
  1. Draft Helper  - rank the full player pool before/during your draft,
                      check players off as they're picked (by you or others)
  2. Waiver Wire    - rank current free agents after the season starts
  3. My Team        - category strengths/weaknesses vs. league average

Built for an ESPN category-based league:
  Skaters: G, A, +/-, PPP, SHP, SOG, HIT, BLK
  Goalies: W, GA, SV, SO, OTL   (GA and OTL are "lower is better")
"""

import streamlit as st
import pandas as pd
from espn_api.hockey import League

st.set_page_config(page_title="Fantasy Hockey Assistant", layout="wide")

SKATER_CATEGORIES = ["G", "A", "+/-", "PPP", "SHP", "SOG", "HIT", "BLK"]
GOALIE_CATEGORIES = ["W", "GA", "SV", "SO", "OTL"]
INVERT_CATEGORIES = {"GA", "OTL"}

# ---------------- Sidebar: league connection ----------------
st.sidebar.header("League Connection")
league_id = st.sidebar.text_input("League ID", value="")
year = st.sidebar.number_input("Season year", value=2027, step=1)
swid = st.sidebar.text_input("SWID (private leagues only)", value="", type="password")
espn_s2 = st.sidebar.text_input("espn_s2 (private leagues only)", value="", type="password")
my_team_name = st.sidebar.text_input("Your exact team name", value="")

connect_clicked = st.sidebar.button("Connect")


@st.cache_resource(show_spinner="Connecting to ESPN...")
def connect_league(league_id, year, swid, espn_s2):
    if swid and espn_s2:
        return League(league_id=int(league_id), year=int(year), swid=swid, espn_s2=espn_s2)
    return League(league_id=int(league_id), year=int(year))


if "league" not in st.session_state:
    st.session_state.league = None
if "drafted_names" not in st.session_state:
    st.session_state.drafted_names = set()

if connect_clicked and league_id:
    try:
        st.session_state.league = connect_league(league_id, year, swid, espn_s2)
        st.sidebar.success("Connected!")
    except Exception as e:
        st.sidebar.error(f"Connection failed: {e}")

league = st.session_state.league


# ---------------- Shared helpers ----------------
def player_stat(player, stat_key):
    try:
        return float(player.stats.get("total", {}).get(stat_key, 0) or 0)
    except (AttributeError, TypeError):
        return 0.0


def build_stat_table(players, categories):
    columns = ["name", "position"] + categories
    rows = []
    for p in players:
        row = {"name": p.name, "position": getattr(p, "position", "")}
        for cat in categories:
            row[cat] = player_stat(p, cat)
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def zscore_rank(df, categories):
    df = df.copy()
    if df.empty:
        for cat in categories:
            df[f"z_{cat}"] = pd.Series(dtype=float)
        df["value"] = pd.Series(dtype=float)
        return df
    for cat in categories:
        mean, std = df[cat].mean(), df[cat].std(ddof=0)
        if std == 0 or pd.isna(std):
            df[f"z_{cat}"] = 0.0
            continue
        z = (df[cat] - mean) / std
        if cat in INVERT_CATEGORIES:
            z = -z
        df[f"z_{cat}"] = z
    df["value"] = df[[f"z_{c}" for c in categories]].sum(axis=1)
    return df.sort_values("value", ascending=False).reset_index(drop=True)


def is_goalie(player):
    pos = str(getattr(player, "position", "")).strip().lower()
    return pos in ("g", "goalie", "goaltender")


def get_ranked_pool(league, size=1000):
    """Full player pool (used for draft prep -- most players are 'free agents' pre-draft)."""
    pool = league.free_agents(size=size)
    skaters = [p for p in pool if not is_goalie(p)]
    goalies = [p for p in pool if is_goalie(p)]
    skater_df = zscore_rank(build_stat_table(skaters, SKATER_CATEGORIES), SKATER_CATEGORIES)
    goalie_df = zscore_rank(build_stat_table(goalies, GOALIE_CATEGORIES), GOALIE_CATEGORIES)
    return skater_df, goalie_df


# ---------------- Tabs ----------------
tab1, tab2, tab3 = st.tabs(["Draft Helper", "Waiver Wire", "My Team"])

with tab1:
    st.subheader("Draft Helper")
    st.caption(
        "Ranks players by category value (z-score across your league's scoring "
        "categories) using last season's stats. Check off players as they're "
        "drafted - by anyone - to keep the board current."
    )
    if league is None:
        st.info("Connect to your league in the sidebar first.")
    else:
        skater_df, goalie_df = get_ranked_pool(league)
        pos_filter = st.radio("Position", ["Skaters", "Goalies"], horizontal=True)
        df = skater_df if pos_filter == "Skaters" else goalie_df
        available_df = df[~df["name"].isin(st.session_state.drafted_names)]

        st.write(f"**{len(available_df)} players still available**")
        for _, row in available_df.head(40).iterrows():
            cols = st.columns([0.5, 3, 1, 1, 5])
            with cols[0]:
                if st.button("Draft", key=f"draft_{row['name']}"):
                    st.session_state.drafted_names.add(row["name"])
                    st.rerun()
            with cols[1]:
                st.write(f"**{row['name']}** ({row['position']})")
            with cols[2]:
                st.write(f"Value: {row['value']:.2f}")
            with cols[3]:
                st.write("")
            with cols[4]:
                cats = SKATER_CATEGORIES if pos_filter == "Skaters" else GOALIE_CATEGORIES
                st.write(" | ".join(f"{c}: {row[c]:.0f}" for c in cats))

        if st.session_state.drafted_names:
            with st.expander(f"Drafted so far ({len(st.session_state.drafted_names)})"):
                st.write(", ".join(sorted(st.session_state.drafted_names)))
                if st.button("Reset draft board"):
                    st.session_state.drafted_names = set()
                    st.rerun()

with tab2:
    st.subheader("Waiver Wire Targets")
    st.caption("Same ranking logic, restricted to players currently on waivers/free agency.")
    if league is None:
        st.info("Connect to your league in the sidebar first.")
    else:
        skater_df, goalie_df = get_ranked_pool(league, size=200)
        st.write("**Top Skaters**")
        st.dataframe(skater_df[["name", "position", "value"] + SKATER_CATEGORIES].head(20), use_container_width=True)
        st.write("**Top Goalies**")
        st.dataframe(goalie_df[["name", "position", "value"] + GOALIE_CATEGORIES].head(20), use_container_width=True)

with tab3:
    st.subheader("My Team - Category Report")
    if league is None or not my_team_name:
        st.info("Connect to your league and enter your team name in the sidebar first.")
    else:
        team = next((t for t in league.teams if t.team_name == my_team_name), None)
        if team is None:
            st.error("Team not found. Available teams:")
            st.write([t.team_name for t in league.teams])
        else:
            roster_df = build_stat_table(team.roster, SKATER_CATEGORIES + GOALIE_CATEGORIES)
            all_rosters = [p for t in league.teams for p in t.roster]
            league_df = build_stat_table(all_rosters, SKATER_CATEGORIES + GOALIE_CATEGORIES)

            report_rows = []
            for cat in SKATER_CATEGORIES + GOALIE_CATEGORIES:
                my_total = roster_df[cat].sum()
                league_avg = league_df[cat].sum() / len(league.teams)
                report_rows.append({
                    "Category": cat,
                    "Your Total": round(my_total, 1),
                    "League Avg/Team": round(league_avg, 1),
                    "Note": "lower is better" if cat in INVERT_CATEGORIES else "",
                })
            st.dataframe(pd.DataFrame(report_rows), use_container_width=True)
