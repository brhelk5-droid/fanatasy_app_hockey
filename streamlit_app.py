"""
Fantasy Hockey Assistant - Streamlit App
=========================================
Three tools in one app:
  1. Draft Helper  - rank the full player pool before/during your draft,
                      using real 2026-27 projections (uploaded spreadsheet)
                      if provided, falling back to ESPN free-agent data.
                      Check players off as they're picked (by you or others).
  2. Waiver Wire    - rank current free agents after the season starts
  3. My Team        - category strengths/weaknesses vs. league average

Built for Smith's Hockey League (ESPN, Head to Head Points scoring):
  Skaters: G, A, +/-, PPP, SHP, SOG, HIT, BLK  (+ DEF bonus for defensemen)
  Goalies: W, GA, SV, SO, OTL
"""

import streamlit as st
import pandas as pd
import os
from espn_api.hockey import League

st.set_page_config(page_title="Fantasy Hockey Assistant", layout="wide")

SKATER_CATEGORIES = ["G", "A", "+/-", "PPP", "SHP", "SOG", "HIT", "BLK"]
GOALIE_CATEGORIES = ["W", "GA", "SV", "SO", "OTL"]

# Your league's actual point values per stat (Head to Head Points scoring),
# taken directly from League Settings > Scoring.
POINT_VALUES = {
    "G": 6, "A": 4, "+/-": 2, "PPP": 2, "SHP": 4, "SOG": 0.9, "HIT": 0.6, "BLK": 1,
    # DEF (Defensemen Points) is handled separately in calc_fantasy_points --
    # it's a defenseman's own G+A scored again at 0.5 pts/point, not a
    # standalone stat, so it isn't a flat per-stat multiplier here.
    "W": 5, "GA": -3, "SV": 0.6, "SO": 5, "OTL": 2,
}


# ---------------- Shared helpers ----------------
def is_defenseman_label(pos):
    return str(pos).strip().upper() in ("D", "DEFENSE", "DEFENSEMAN")


def calc_fantasy_points(df, categories):
    """Compute each player's total fantasy points using the league's real point values.

    Defensemen get an extra DEF bonus: their own Goals + Assists, scored again
    at 0.5 pts/point (ESPN's "Defensemen Points" category applies only to D
    and is worth 0 for forwards).
    """
    df = df.copy()
    if df.empty:
        df["value"] = pd.Series(dtype=float)
        return df
    df["value"] = sum(df[cat] * POINT_VALUES[cat] for cat in categories)
    if "G" in categories and "A" in categories:  # skater table
        is_def = df["position"].apply(is_defenseman_label)
        def_bonus = (df["G"] + df["A"]) * 0.5
        df["value"] = df["value"] + def_bonus.where(is_def, 0)
    return df.sort_values("value", ascending=False).reset_index(drop=True)


def is_goalie(player):
    pos = str(getattr(player, "position", "")).strip().lower()
    return pos in ("g", "goalie", "goaltender")


def get_stats_dict(player, year):
    """Return the stat totals dict to use for ESPN-based projections.

    Since a season with no games played yet has all-zero 'total' stats
    (true for any league before its draft/season start), this prefers the
    previous season's actual totals as the projection baseline, falling
    back to whatever 'total'-like key is available.
    """
    stats = getattr(player, "stats", {}) or {}
    prev_year = int(year) - 1
    for key in stats.keys():
        if str(prev_year) in str(key) and "total" in str(key).lower():
            return stats[key] or {}
    for key in stats.keys():
        if "total" in str(key).lower():
            return stats[key] or {}
    return stats.get("total", {}) or {}


def player_stat(stats_dict, stat_key):
    try:
        return float(stats_dict.get(stat_key, 0) or 0)
    except (AttributeError, TypeError):
        return 0.0


def build_stat_table_from_espn(players, categories, year):
    columns = ["name", "position"] + categories
    rows = []
    for p in players:
        stats_dict = get_stats_dict(p, year)
        row = {"name": p.name, "position": getattr(p, "position", "")}
        for cat in categories:
            row[cat] = player_stat(stats_dict, cat)
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def get_ranked_pool_from_espn(league, year, size=1000):
    """Fallback: full player pool ranked from ESPN's own stats (used when no
    projections spreadsheet has been uploaded)."""
    pool = league.free_agents(size=size)
    skaters = [p for p in pool if not is_goalie(p)]
    goalies = [p for p in pool if is_goalie(p)]
    skater_df = calc_fantasy_points(build_stat_table_from_espn(skaters, SKATER_CATEGORIES, year), SKATER_CATEGORIES)
    goalie_df = calc_fantasy_points(build_stat_table_from_espn(goalies, GOALIE_CATEGORIES, year), GOALIE_CATEGORIES)
    return skater_df, goalie_df


# Your league's actual roster construction (used to compute a Utility-aware
# replacement level -- see compute_utility_adjusted_vorp below).
LEAGUE_TEAMS = 14
DEDICATED_SKATER_SLOTS = {"C": 2, "LW": 2, "RW": 2, "D": 4}  # starters per team
UTIL_SLOTS_PER_TEAM = 2  # Utility: any skater position is eligible


def eligible_positions(position_str):
    return [p.strip().upper() for p in str(position_str).split(",") if p.strip()]


def compute_utility_adjusted_vorp(skater_df):
    """Replacement level per position, accounting for Utility slots.

    The projections sheet's own VORP column assumes only dedicated
    position slots exist (2 C / 2 LW / 2 RW / 4 D per team) and has no
    concept of Utility -- but this league also starts 2 Utility skaters
    per team (any position eligible), which is 28 extra league-wide skater
    slots the sheet's replacement level never accounts for. Ignoring them
    makes the replacement level too shallow a talent pool overall, which
    understates true draft value -- defensemen most visibly, since they're
    already scarcer at their dedicated slots.

    This does a simple greedy fill: rank all skaters by raw fantasy points,
    assign each to their scarcest open dedicated position first, then to a
    Utility slot if no dedicated room remains, then whatever's left over
    marks the replacement level for each position it's eligible for.
    """
    df = skater_df.sort_values("fp", ascending=False).reset_index(drop=True)
    dedicated_capacity = {p: n * LEAGUE_TEAMS for p, n in DEDICATED_SKATER_SLOTS.items()}
    util_capacity = UTIL_SLOTS_PER_TEAM * LEAGUE_TEAMS
    filled = {p: 0 for p in dedicated_capacity}
    filled_util = 0
    replacement_fp = {}

    for _, row in df.iterrows():
        elig = [p for p in eligible_positions(row["position"]) if p in dedicated_capacity]
        open_positions = sorted(
            (p for p in elig if filled[p] < dedicated_capacity[p]),
            key=lambda p: dedicated_capacity[p] - filled[p],
        )
        if open_positions:
            filled[open_positions[0]] += 1
        elif filled_util < util_capacity:
            filled_util += 1
        else:
            for p in elig:
                if p not in replacement_fp:
                    replacement_fp[p] = row["fp"]
        if len(replacement_fp) == len(dedicated_capacity):
            break

    def vorp_for(row):
        elig = [p for p in eligible_positions(row["position"]) if p in replacement_fp]
        if not elig:
            return row["fp"]  # shouldn't happen, but don't crash
        return row["fp"] - min(replacement_fp[p] for p in elig)

    df["vorp"] = df.apply(vorp_for, axis=1)
    return df, replacement_fp


@st.cache_data(show_spinner="Loading player projections...")
def load_projections_from_dataframe(raw_df):
    """Shared parsing logic: takes a raw DataFrame with NAME/POS + stat columns
    (from either the bundled CSV or an uploaded spreadsheet) and returns
    ranked skater/goalie DataFrames plus the Utility-adjusted replacement levels.
    """
    keep_cols = ["NAME", "POS"] + SKATER_CATEGORIES + GOALIE_CATEGORIES
    df = raw_df.copy()
    for col in keep_cols:
        if col not in df.columns:
            df[col] = 0.0
    df = df[keep_cols].rename(columns={"NAME": "name", "POS": "position"})
    df[SKATER_CATEGORIES + GOALIE_CATEGORIES] = df[SKATER_CATEGORIES + GOALIE_CATEGORIES].fillna(0.0)

    is_goalie_row = df["position"].astype(str).str.strip().str.upper() == "G"

    skater_df = calc_fantasy_points(df[~is_goalie_row].copy().reset_index(drop=True), SKATER_CATEGORIES)
    skater_df = skater_df.rename(columns={"value": "fp"})
    skater_df, replacement_levels = compute_utility_adjusted_vorp(skater_df)
    skater_df = skater_df.rename(columns={"fp": "value"})
    skater_df = skater_df.sort_values("vorp", ascending=False).reset_index(drop=True)

    goalie_df = calc_fantasy_points(df[is_goalie_row].copy().reset_index(drop=True), GOALIE_CATEGORIES)
    goalie_df["vorp"] = goalie_df["value"]
    goalie_df = goalie_df.sort_values("vorp", ascending=False).reset_index(drop=True)

    return skater_df, goalie_df, replacement_levels


BUNDLED_PROJECTIONS_PATH = "player_projections.csv"


@st.cache_data(show_spinner="Loading player projections...")
def load_bundled_projections():
    raw_df = pd.read_csv(BUNDLED_PROJECTIONS_PATH)
    return load_projections_from_dataframe(raw_df)


def load_projections_from_upload(file_bytes):
    raw_df = pd.read_excel(pd.io.common.BytesIO(file_bytes), sheet_name="The List", header=0)
    return load_projections_from_dataframe(raw_df)


# ---------------- Sidebar: projections ----------------
st.sidebar.header("Player Projections")

projections_loaded = False
proj_skater_df, proj_goalie_df, proj_replacement_levels = pd.DataFrame(), pd.DataFrame(), {}

if os.path.exists(BUNDLED_PROJECTIONS_PATH):
    try:
        proj_skater_df, proj_goalie_df, proj_replacement_levels = load_bundled_projections()
        projections_loaded = True
        st.sidebar.success(f"Loaded {len(proj_skater_df) + len(proj_goalie_df)} players from bundled projections")
    except Exception as e:
        st.sidebar.error(f"Could not read bundled projections: {e}")

with st.sidebar.expander("Update projections (optional)"):
    projections_file = st.file_uploader(
        "Upload a newer projections spreadsheet (.xlsx)",
        type=["xlsx"],
        help="Expects a 'The List' sheet with columns: NAME, POS, G, A, +/-, PPP, SHP, SOG, HIT, BLK, W, GA, SV, SO, OTL",
    )
    if projections_file is not None:
        try:
            proj_skater_df, proj_goalie_df, proj_replacement_levels = load_projections_from_upload(projections_file.getvalue())
            projections_loaded = True
            st.success(f"Loaded {len(proj_skater_df) + len(proj_goalie_df)} players from upload (overriding bundled file)")
        except Exception as e:
            st.error(f"Could not read projections file: {e}")

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

if league is not None:
    with st.sidebar.expander("Debug: league.settings"):
        try:
            settings_dict = vars(league.settings)
            st.write(settings_dict)
        except Exception as e:
            st.write(f"Could not read league.settings: {e}")


# ---------------- Tabs ----------------
tab1, tab2, tab3 = st.tabs(["Draft Helper", "Waiver Wire", "My Team"])

with tab1:
    st.subheader("Draft Helper")

    if projections_loaded:
        st.caption(
            "Ranks players by VORP (Value Over Replacement Player), computed from "
            "your uploaded 2026-27 projections using your league's exact point "
            "values -- and adjusted for your league's 2 Utility slots per team, "
            "which the spreadsheet's own VORP column doesn't account for. "
            "Check off players as they're drafted - by anyone - to keep the board current."
        )
        if proj_replacement_levels:
            with st.expander("Replacement level by position (Utility-adjusted)"):
                st.write(
                    {pos: round(fp, 1) for pos, fp in sorted(proj_replacement_levels.items())}
                )
                st.caption(
                    "This is the projected fantasy points of the best player at each "
                    "position who would NOT make a starting lineup across the league, "
                    "once Utility slots are filled too. Lower than the spreadsheet's "
                    "own numbers because Utility slots make the draftable pool deeper."
                )
        skater_df, goalie_df = proj_skater_df, proj_goalie_df
        data_source_ready = True
    elif league is not None:
        st.caption(
            "No projections file uploaded, so this is falling back to ESPN's own "
            "stats (last season's totals). Upload a projections spreadsheet in the "
            "sidebar for more accurate, forward-looking rankings."
        )
        skater_df, goalie_df = get_ranked_pool_from_espn(league, year)
        data_source_ready = True
    else:
        st.info("Upload a projections spreadsheet, or connect to your league, in the sidebar.")
        data_source_ready = False

    if data_source_ready:
        pos_filter = st.radio("Position", ["Skaters", "Goalies"], horizontal=True)
        df = skater_df if pos_filter == "Skaters" else goalie_df
        available_df = df[~df["name"].isin(st.session_state.drafted_names)]

        search_term = st.text_input("Search players", value="", placeholder="Type a player name...")
        if search_term:
            available_df = available_df[available_df["name"].str.contains(search_term, case=False, na=False)]

        st.write(f"**{len(available_df)} players {'matching search' if search_term else 'still available'}**")
        for _, row in available_df.head(40).iterrows():
            cols = st.columns([0.5, 3, 1, 1, 5])
            with cols[0]:
                if st.button("Draft", key=f"draft_{row['name']}"):
                    st.session_state.drafted_names.add(row["name"])
                    st.rerun()
            with cols[1]:
                st.write(f"**{row['name']}** ({row['position']})")
            with cols[2]:
                if "vorp" in row:
                    st.write(f"VORP: {row['vorp']:.1f}")
                else:
                    st.write(f"Value: {row['value']:.2f}")
            with cols[3]:
                if "vorp" in row:
                    st.write(f"FP: {row['value']:.0f}")
                else:
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
    st.caption("Ranks players currently on waivers/free agency using ESPN's live stats.")
    if league is None:
        st.info("Connect to your league in the sidebar first.")
    else:
        skater_df, goalie_df = get_ranked_pool_from_espn(league, year, size=200)

        waiver_search = st.text_input("Search players", value="", placeholder="Type a player name...", key="waiver_search")
        if waiver_search:
            skater_df = skater_df[skater_df["name"].str.contains(waiver_search, case=False, na=False)]
            goalie_df = goalie_df[goalie_df["name"].str.contains(waiver_search, case=False, na=False)]

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
            roster_df = build_stat_table_from_espn(team.roster, SKATER_CATEGORIES + GOALIE_CATEGORIES, year)
            all_rosters = [p for t in league.teams for p in t.roster]
            league_df = build_stat_table_from_espn(all_rosters, SKATER_CATEGORIES + GOALIE_CATEGORIES, year)

            report_rows = []
            for cat in SKATER_CATEGORIES + GOALIE_CATEGORIES:
                my_total = roster_df[cat].sum()
                league_avg = league_df[cat].sum() / len(league.teams)
                report_rows.append({
                    "Category": cat,
                    "Points/Stat": POINT_VALUES[cat],
                    "Your Total": round(my_total, 1),
                    "Your Fantasy Pts": round(my_total * POINT_VALUES[cat], 1),
                    "League Avg/Team": round(league_avg, 1),
                })

            is_def = roster_df["position"].apply(is_defenseman_label)
            my_def_bonus = ((roster_df["G"] + roster_df["A"]) * 0.5 * is_def).sum()
            league_is_def = league_df["position"].apply(is_defenseman_label)
            league_def_bonus_avg = ((league_df["G"] + league_df["A"]) * 0.5 * league_is_def).sum() / len(league.teams)
            report_rows.append({
                "Category": "DEF (bonus)",
                "Points/Stat": 0.5,
                "Your Total": round(my_def_bonus / 0.5, 1) if my_def_bonus else 0,
                "Your Fantasy Pts": round(my_def_bonus, 1),
                "League Avg/Team": round(league_def_bonus_avg, 1),
            })

            st.dataframe(pd.DataFrame(report_rows), use_container_width=True)
