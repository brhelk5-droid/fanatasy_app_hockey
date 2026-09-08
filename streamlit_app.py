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

Field glossary (also shown in-app via the "What do the badges mean?" expander):
  - Name badge (+, ++, -, --): the source spreadsheet's own analyst
    adjustment (analyst_adj column) -- a manual judgment-call tweak on top
    of the statistical model, for cases like breakout candidates or players
    who switched teams. "+" = bumped up a bit, "++" = bumped up more,
    "-"/"--" = bumped down. This is the analysts' opinion, not our math.
  - Value/Reach column (🔥 Value / ⚠️ Reach / plain +N or -N): compares each
    player's ADP to their rank in OUR VORP-based board (adp_diff). Positive
    means they're going later in drafts than our model says they're worth
    (a sleeper); negative means they're going earlier (a reach risk).
"""

import streamlit as st
import pandas as pd
import os
import requests
import datetime
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

# The projections sheet uses a few non-standard team abbreviations
# (dot-style for three-word city names); the NHL's own schedule API uses
# the standard 3-letter codes. This reconciles the two so we can match a
# player's team to whether they're playing on a given date.
TEAM_ABBREV_TO_NHL_API = {
    "L.A": "LAK", "N.J": "NJD", "S.J": "SJS", "T.B": "TBL",
}


def normalize_team_abbrev(team):
    team = str(team).strip().upper()
    return TEAM_ABBREV_TO_NHL_API.get(team, team)


@st.cache_data(show_spinner="Checking NHL schedule...", ttl=3600)
def fetch_teams_playing_on(date_str):
    """Returns the set of (normalized) team abbreviations with a game on the
    given YYYY-MM-DD date, using the NHL's own public schedule API. Returns
    an empty set (and the UI shows a warning) if the request fails, rather
    than crashing the tab.
    """
    try:
        resp = requests.get(f"https://api-web.nhle.com/v1/schedule/{date_str}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return set()
    teams_today = set()
    for week in data.get("gameWeek", []):
        if week.get("date") != date_str:
            continue
        for game in week.get("games", []):
            for side in ("awayTeam", "homeTeam"):
                abbrev = game.get(side, {}).get("abbrev")
                if abbrev:
                    teams_today.add(normalize_team_abbrev(abbrev))
    return teams_today


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


def get_recent_form_stats_dict(player):
    """Best-effort pull of a 'recent form' stat split (last 7/15/30 days) from
    ESPN's Player object. ESPN's fantasy API exposes these splits reliably
    for football/basketball; hockey support in this library is documented as
    still 'in development', so this may return None for every player --
    callers must treat None as "not available" rather than "value of 0".
    """
    stats = getattr(player, "stats", {}) or {}
    for window in ("last_7", "last_15", "last_30", "last7", "last15", "last30"):
        for key in stats.keys():
            if window in str(key).lower():
                return stats[key] or {}
    return None


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


def fetch_espn_adp(league, size=2000):
    """Best-effort pull of ESPN's own Average Draft Position per player.

    ESPN's own football API exposes ADP via a player.ownership dict
    (averageDraftPosition); hockey runs on the same underlying platform, but
    this library doesn't document whether that field is populated the same
    way for hockey players, so this tries a few plausible attribute paths
    and returns whatever it can find -- an empty dict if none work, which
    the UI treats as "ESPN ADP not available" rather than failing.
    """
    adp_by_name = {}
    try:
        players = league.free_agents(size=size)
    except Exception:
        return adp_by_name
    for p in players:
        adp = None
        ownership = getattr(p, "ownership", None)
        if isinstance(ownership, dict):
            adp = ownership.get("averageDraftPosition")
        if adp is None:
            adp = getattr(p, "average_draft_position", None) or getattr(p, "ave_draft_pos", None)
        if adp not in (None, 0):
            adp_by_name[p.name] = adp
    return adp_by_name


GOALIE_STARTER_SLOTS = 2  # per team, no Utility crossover for goalies


def compute_goalie_vorp_fallback(goalie_df):
    """Fallback goalie replacement level when the sheet's own VORP isn't
    available: the projected FP of the best goalie who would NOT be a
    starter across the league (rank = starters*teams + 1). Used only when
    sheet_vorp is missing -- never silently substitute raw FP as "VORP",
    since goalies' raw point totals are on a different scale than
    already-baselined skater VORP and would wrongly float to the top.
    """
    df = goalie_df.sort_values("value", ascending=False).reset_index(drop=True)
    replacement_rank = GOALIE_STARTER_SLOTS * LEAGUE_TEAMS
    if len(df) > replacement_rank:
        replacement_fp = df.loc[replacement_rank, "value"]
    else:
        replacement_fp = df["value"].min() if not df.empty else 0.0
    return df["value"] - replacement_fp


@st.cache_data(show_spinner="Loading player projections...")
def load_projections_from_dataframe(raw_df):
    """Shared parsing logic: takes a raw DataFrame with NAME/POS + stat columns
    (from either the bundled CSV or an uploaded spreadsheet) and returns
    ranked skater/goalie DataFrames plus the Utility-adjusted replacement levels.

    Skaters: VORP is computed here (Utility-adjusted), since the spreadsheet's
    own VORP ignores this league's Utility slots.
    Goalies: no Utility crossover applies to them, so we trust the sheet's own
    VORP directly (it uses a "Draft Based" replacement count reflecting real
    draft behavior, not just raw roster-slot math) rather than recomputing --
    but only if that column is actually present; see compute_goalie_vorp_fallback.
    """
    keep_cols = ["NAME", "POS"] + SKATER_CATEGORIES + GOALIE_CATEGORIES
    optional_cols = ["GP", "sheet_adp", "sheet_vorp", "analyst_adj", "TEAM"]
    missing_optional_cols = [c for c in optional_cols if c not in raw_df.columns]
    df = raw_df.copy()
    for col in keep_cols:
        if col not in df.columns:
            df[col] = 0.0
    for col in optional_cols:
        if col not in df.columns:
            df[col] = None
    df = df[keep_cols + optional_cols].rename(columns={"NAME": "name", "POS": "position"})
    df[SKATER_CATEGORIES + GOALIE_CATEGORIES] = df[SKATER_CATEGORIES + GOALIE_CATEGORIES].fillna(0.0)
    for numeric_col in ["GP", "sheet_adp", "sheet_vorp"]:
        df[numeric_col] = pd.to_numeric(df[numeric_col], errors="coerce")

    is_goalie_row = df["position"].astype(str).str.strip().str.upper() == "G"

    skater_df = calc_fantasy_points(df[~is_goalie_row].copy().reset_index(drop=True), SKATER_CATEGORIES)
    skater_df = skater_df.rename(columns={"value": "fp"})
    skater_df, replacement_levels = compute_utility_adjusted_vorp(skater_df)
    skater_df = skater_df.rename(columns={"fp": "value"})
    skater_df = skater_df.sort_values("vorp", ascending=False).reset_index(drop=True)

    goalie_df = calc_fantasy_points(df[is_goalie_row].copy().reset_index(drop=True), GOALIE_CATEGORIES)
    if goalie_df["sheet_vorp"].notna().any():
        goalie_df["vorp"] = goalie_df["sheet_vorp"].fillna(compute_goalie_vorp_fallback(goalie_df))
    else:
        goalie_df["vorp"] = compute_goalie_vorp_fallback(goalie_df)
    goalie_df = goalie_df.sort_values("vorp", ascending=False).reset_index(drop=True)
    replacement_levels["G"] = None  # sourced from the sheet when available

    return skater_df, goalie_df, replacement_levels, missing_optional_cols


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
proj_missing_cols = []

if os.path.exists(BUNDLED_PROJECTIONS_PATH):
    try:
        proj_skater_df, proj_goalie_df, proj_replacement_levels, proj_missing_cols = load_bundled_projections()
        projections_loaded = True
        st.sidebar.success(f"Loaded {len(proj_skater_df) + len(proj_goalie_df)} players from bundled projections")
        if proj_missing_cols:
            st.sidebar.warning(
                f"Your player_projections.csv is missing columns: {', '.join(proj_missing_cols)}. "
                "This looks like an older version of the file -- re-upload the latest CSV to GitHub "
                "to get ADP, GP, and correct goalie rankings back."
            )
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
            proj_skater_df, proj_goalie_df, proj_replacement_levels, proj_missing_cols = load_projections_from_upload(projections_file.getvalue())
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

espn_adp_by_name = {}
if league is not None:
    espn_adp_by_name = fetch_espn_adp(league)
    if not espn_adp_by_name:
        st.sidebar.info(
            "ESPN ADP not available through this library for hockey -- "
            "falling back to the spreadsheet's own ADP column."
        )


# ---------------- Tabs ----------------
tab1, tab2, tab3, tab4 = st.tabs(["Draft Helper", "Waiver Wire", "My Team", "Streaming"])

with tab1:
    st.subheader("Draft Helper")

    with st.expander("What do the badges mean?"):
        st.markdown(
            "- **Name badge (`+`, `++`, `-`, `--`)** -- the source spreadsheet's "
            "own analyst adjustment: a manual judgment-call tweak on top of the "
            "statistical model, for cases like breakout candidates or players who "
            "switched teams. `+` = bumped up a bit, `++` = bumped up more, "
            "`-`/`--` = bumped down. This reflects the analysts' opinion, not our math.\n"
            "- **Value/Reach column** (🔥 Value / ⚠️ Reach / plain `+N` or `-N`) -- "
            "compares a player's ADP to their rank in *our* VORP-based board. "
            "Positive means they're going later in drafts than our model says "
            "they're worth (a sleeper); negative means they're going earlier "
            "(a reach risk)."
        )

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
                    {pos: round(fp, 1) for pos, fp in sorted(proj_replacement_levels.items()) if fp is not None}
                )
                st.caption(
                    "This is the projected fantasy points of the best player at each "
                    "position who would NOT make a starting lineup across the league, "
                    "once Utility slots are filled too. Lower than the spreadsheet's "
                    "own numbers because Utility slots make the draftable pool deeper. "
                    "Goalies aren't shown here -- they don't share Utility slots with "
                    "skaters, so their replacement level is sourced separately."
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
        skater_df["vorp"] = skater_df["value"]  # no VORP model for the ESPN fallback path
        goalie_df["vorp"] = goalie_df["value"]
        data_source_ready = True
    else:
        st.info("Upload a projections spreadsheet, or connect to your league, in the sidebar.")
        data_source_ready = False

    if data_source_ready:
        # Combine skaters and goalies into one board, ranked on the same VORP scale.
        combined_df = pd.concat([skater_df, goalie_df], ignore_index=True, sort=False)
        combined_df = combined_df.sort_values("vorp", ascending=False).reset_index(drop=True)
        # Display multi-position eligibility as "LW/RW" instead of "LW,RW"
        combined_df["position_display"] = combined_df["position"].astype(str).str.replace(",", "/")

        # ADP: prefer ESPN's own live ADP when available; otherwise fall back
        # to whatever ADP is baked into the projections sheet (Yahoo's, in
        # this case -- clearly labeled so it's never confused for ESPN's).
        if espn_adp_by_name:
            combined_df["adp"] = combined_df["name"].map(espn_adp_by_name)
            combined_df["adp_source"] = combined_df["adp"].apply(lambda v: "ESPN" if pd.notna(v) else None)
            if "sheet_adp" in combined_df.columns:
                missing = combined_df["adp"].isna()
                combined_df.loc[missing, "adp_source"] = combined_df.loc[missing, "sheet_adp"].apply(
                    lambda v: "sheet" if pd.notna(v) else None
                )
                combined_df.loc[missing, "adp"] = combined_df.loc[missing, "sheet_adp"]
        elif "sheet_adp" in combined_df.columns:
            combined_df["adp"] = combined_df["sheet_adp"]
            combined_df["adp_source"] = combined_df["adp"].apply(lambda v: "sheet" if pd.notna(v) else None)
        else:
            combined_df["adp"] = None
            combined_df["adp_source"] = None

        # Value/reach flag: compare each player's rank in OUR VORP-based board
        # (already Utility-adjusted, unlike the sheet's own rank) against their
        # ADP. A player going much later than our rank suggests is a "value";
        # much earlier is a "reach." Same idea as the source spreadsheet's own
        # ADP Difference column, just computed against our corrected ranking.
        combined_df["our_rank"] = combined_df["vorp"].rank(ascending=False, method="min")
        combined_df["adp_diff"] = combined_df["adp"] - combined_df["our_rank"]

        available_df = combined_df[~combined_df["name"].isin(st.session_state.drafted_names)]

        ALL_POSITIONS = ["C", "LW", "RW", "D", "G"]
        selected_positions = st.multiselect(
            "Positions to show", ALL_POSITIONS, default=ALL_POSITIONS
        )
        if selected_positions:
            def matches_position_filter(pos_str):
                elig = [p.strip().upper() for p in str(pos_str).split(",")]
                return any(p in selected_positions for p in elig)
            available_df = available_df[available_df["position"].apply(matches_position_filter)]

        search_term = st.text_input("Search players", value="", placeholder="Type a player name...")
        if search_term:
            available_df = available_df[available_df["name"].str.contains(search_term, case=False, na=False)]

        st.write(f"**{len(available_df)} players {'matching search/filter' if (search_term or len(selected_positions) < len(ALL_POSITIONS)) else 'still available'}**")
        for _, row in available_df.head(40).iterrows():
            cols = st.columns([0.5, 2.3, 1, 1, 1.3, 0.8, 1.2, 3.5])
            with cols[0]:
                if st.button("Draft", key=f"draft_{row['name']}"):
                    st.session_state.drafted_names.add(row["name"])
                    st.rerun()
            with cols[1]:
                adj = row.get("analyst_adj")
                adj_badge = f" {adj.strip()}" if isinstance(adj, str) and adj.strip() else ""
                st.write(f"**{row['name']}**{adj_badge} ({row['position_display']})")
            with cols[2]:
                st.write(f"VORP: {row['vorp']:.1f}")
            with cols[3]:
                st.write(f"FP: {row['value']:.0f}")
            with cols[4]:
                if pd.notna(row.get("adp")):
                    src = row.get("adp_source") or ""
                    st.write(f"ADP: {row['adp']:.1f}" + (f" ({src})" if src else ""))
                else:
                    st.write("ADP: n/a")
            with cols[5]:
                gp = row.get("GP")
                st.write(f"GP: {gp:.0f}" if pd.notna(gp) else "GP: n/a")
            with cols[6]:
                diff = row.get("adp_diff")
                if pd.notna(diff):
                    if diff >= 15:
                        st.write(f"🔥 Value (+{diff:.0f})")
                    elif diff <= -15:
                        st.write(f"⚠️ Reach ({diff:.0f})")
                    else:
                        st.write(f"{diff:+.0f}")
                else:
                    st.write("")
            with cols[7]:
                is_goalie_row = str(row["position"]).strip().upper() == "G"
                cats = GOALIE_CATEGORIES if is_goalie_row else SKATER_CATEGORIES
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

with tab4:
    st.subheader("Streaming Helper")
    st.caption(
        "Shows which available players actually have a game on a given day -- "
        "useful for deciding who to stream on your empty roster/Utility spots."
    )

    if not projections_loaded:
        st.info("Player projections haven't loaded (see the sidebar) -- Streaming needs those to rank players.")
    else:
        pick_date = st.date_input("Date to check", value=datetime.date.today())
        date_str = pick_date.strftime("%Y-%m-%d")
        teams_today = fetch_teams_playing_on(date_str)

        if not teams_today:
            st.warning(
                "Couldn't get today's schedule from the NHL's API (or there are no "
                "games on this date -- normal for an off-day, or if the date is "
                "outside the regular season)."
            )
        else:
            st.write(f"**{len(teams_today)} teams play on {pick_date.strftime('%A, %B %-d')}:** " + ", ".join(sorted(teams_today)))

            combined_stream_df = pd.concat([proj_skater_df, proj_goalie_df], ignore_index=True, sort=False)
            if "TEAM" not in combined_stream_df.columns:
                st.error("Your player_projections.csv doesn't have a TEAM column -- re-upload the latest CSV to use Streaming.")
            else:
                combined_stream_df["team_norm"] = combined_stream_df["TEAM"].apply(normalize_team_abbrev)
                combined_stream_df["position_display"] = combined_stream_df["position"].astype(str).str.replace(",", "/")

                # Per-game rate: for a single stream night, what a player
                # does PER GAME matters more than their season-long total --
                # a player projected for fewer total games can still be the
                # better one-night play if their per-game rate is higher.
                combined_stream_df["value_per_game"] = combined_stream_df.apply(
                    lambda r: r["value"] / r["GP"] if pd.notna(r.get("GP")) and r["GP"] > 0 else float("nan"),
                    axis=1,
                )

                # Figure out who's actually available (not rostered) if we can.
                owned_names = set()
                if league is not None:
                    try:
                        owned_names = {p.name for t in league.teams for p in t.roster}
                    except Exception:
                        owned_names = set()

                playing_df = combined_stream_df[combined_stream_df["team_norm"].isin(teams_today)]
                available_stream_df = playing_df[~playing_df["name"].isin(owned_names)].copy()

                # Best-effort recent-form (last 15 days) pull from ESPN, only
                # for the players actually in play here -- not the whole pool,
                # to keep this fast. Honestly reports if the library doesn't
                # have it for hockey rather than showing misleading zeros.
                recent_form_by_name = {}
                recent_form_checked = False
                if league is not None and not available_stream_df.empty:
                    recent_form_checked = True
                    try:
                        espn_pool = league.free_agents(size=2000)
                        espn_by_name = {p.name: p for p in espn_pool}
                        for _, r in available_stream_df.iterrows():
                            p = espn_by_name.get(r["name"])
                            if p is None:
                                continue
                            form_stats = get_recent_form_stats_dict(p)
                            if form_stats is None:
                                continue
                            cats = GOALIE_CATEGORIES if r["position_display"].strip().upper() == "G" else SKATER_CATEGORIES
                            form_value = sum(player_stat(form_stats, c) * POINT_VALUES[c] for c in cats)
                            recent_form_by_name[r["name"]] = form_value
                    except Exception:
                        pass
                available_stream_df["recent_form"] = available_stream_df["name"].map(recent_form_by_name)
                recent_form_available = len(recent_form_by_name) > 0

                sort_options = ["Per-game rate", "Season VORP"]
                if recent_form_available:
                    sort_options.append("Recent form (last 15 days)")
                sort_choice = st.radio("Rank by", sort_options, horizontal=True)

                if recent_form_checked and not recent_form_available:
                    st.caption(
                        "Tried pulling recent-form (last 15 days) stats from ESPN, "
                        "but this library doesn't appear to expose that split for "
                        "hockey players -- ranking by per-game rate and season VORP instead."
                    )

                sort_col = {"Per-game rate": "value_per_game", "Season VORP": "vorp", "Recent form (last 15 days)": "recent_form"}[sort_choice]
                available_stream_df = available_stream_df.sort_values(sort_col, ascending=False, na_position="last").reset_index(drop=True)

                if league is None:
                    st.caption(
                        "Not connected to your league, so this shows every player "
                        "playing today (can't tell who's actually on waivers vs. "
                        "already rostered) -- connect in the sidebar for a real list."
                    )
                else:
                    st.write(f"**{len(available_stream_df)} available players play today**")

                stream_search = st.text_input("Search players", value="", placeholder="Type a player name...", key="stream_search")
                if stream_search:
                    available_stream_df = available_stream_df[available_stream_df["name"].str.contains(stream_search, case=False, na=False)]

                for _, row in available_stream_df.head(30).iterrows():
                    cols = st.columns([2.3, 1, 1, 1, 1.2])
                    with cols[0]:
                        st.write(f"**{row['name']}** ({row['position_display']}, {row['team_norm']})")
                    with cols[1]:
                        vpg = row.get("value_per_game")
                        st.write(f"FP/GP: {vpg:.1f}" if pd.notna(vpg) else "FP/GP: n/a")
                    with cols[2]:
                        st.write(f"VORP: {row['vorp']:.1f}")
                    with cols[3]:
                        gp = row.get("GP")
                        st.write(f"GP: {gp:.0f}" if pd.notna(gp) else "GP: n/a")
                    with cols[4]:
                        rf = row.get("recent_form")
                        st.write(f"L15: {rf:.1f}" if pd.notna(rf) else "")

                # Cross-check: which of MY rostered players do NOT play today?
                if league is not None and my_team_name:
                    team = next((t for t in league.teams if t.team_name == my_team_name), None)
                    if team is not None:
                        my_names_teams = []
                        for p in team.roster:
                            pro_team = normalize_team_abbrev(getattr(p, "proTeam", ""))
                            my_names_teams.append((p.name, pro_team))
                        sitting_today = [n for n, t in my_names_teams if t and t not in teams_today]
                        if sitting_today:
                            with st.expander(f"Your roster NOT playing today ({len(sitting_today)})"):
                                st.write(", ".join(sitting_today))
                                st.caption(
                                    "These are candidates to bench in favor of one of the "
                                    "available streaming options above, if you have a "
                                    "matching open spot."
                                )

        # ---------------- Weekly Planner ----------------
        st.markdown("---")
        st.subheader("Weekly Planner (Mon-Sun)")
        st.caption(
            "Finds days this week your active roster is short-handed at a "
            "position, then ranks available players by how many of those "
            "gap-days they'd actually cover -- since you only get 7 roster "
            "moves a week, it's worth prioritizing adds that help the most "
            "days rather than just the single best name."
        )

        if league is None or not my_team_name:
            st.info("Connect to your league and enter your team name in the sidebar to use the Weekly Planner.")
        else:
            team = next((t for t in league.teams if t.team_name == my_team_name), None)
            if team is None:
                st.info("Team not found -- check your team name in the sidebar.")
            elif "TEAM" not in proj_skater_df.columns and "TEAM" not in proj_goalie_df.columns:
                st.error("Your player_projections.csv doesn't have a TEAM column -- re-upload the latest CSV to use this.")
            else:
                anchor_date = pick_date if "pick_date" in dir() else datetime.date.today()
                monday = anchor_date - datetime.timedelta(days=anchor_date.weekday())
                week_days = [monday + datetime.timedelta(days=i) for i in range(7)]
                week_schedules = {d: fetch_teams_playing_on(d.strftime("%Y-%m-%d")) for d in week_days}

                # My roster: name, eligible positions, team, goalie flag
                my_roster_info = []
                for p in team.roster:
                    pos_list = eligible_positions(getattr(p, "position", ""))
                    pro_team = normalize_team_abbrev(getattr(p, "proTeam", ""))
                    my_roster_info.append({
                        "name": p.name, "positions": pos_list, "team": pro_team, "is_goalie": is_goalie(p),
                    })

                # Per-day: how many of MY players at each position are playing,
                # and where that falls short of what a full lineup needs.
                day_need = {}
                for d in week_days:
                    teams_playing = week_schedules[d]
                    counts = {"C": 0, "LW": 0, "RW": 0, "D": 0}
                    goalie_count = 0
                    skater_count = 0
                    for pl in my_roster_info:
                        if not teams_playing or pl["team"] not in teams_playing:
                            continue
                        if pl["is_goalie"]:
                            goalie_count += 1
                        else:
                            skater_count += 1
                            for pos in pl["positions"]:
                                if pos in counts:
                                    counts[pos] += 1
                    shortfalls = {pos: max(0, DEDICATED_SKATER_SLOTS[pos] - counts[pos]) for pos in DEDICATED_SKATER_SLOTS}
                    goalie_shortfall = max(0, GOALIE_STARTER_SLOTS - goalie_count)
                    total_skater_slots = sum(DEDICATED_SKATER_SLOTS.values()) + UTIL_SLOTS_PER_TEAM
                    general_skater_shortfall = max(0, total_skater_slots - skater_count)
                    day_need[d] = {
                        "position_shortfalls": shortfalls,
                        "goalie_shortfall": goalie_shortfall,
                        "general_skater_shortfall": general_skater_shortfall,
                        "needs_help": any(v > 0 for v in shortfalls.values()) or goalie_shortfall > 0 or general_skater_shortfall > 0,
                    }

                st.write("**Day-by-day roster check:**")
                day_cols = st.columns(7)
                for i, d in enumerate(week_days):
                    with day_cols[i]:
                        label = d.strftime("%a %-m/%-d")
                        if not week_schedules[d]:
                            st.write(f"?  {label}")
                            st.caption("no schedule data")
                        elif day_need[d]["needs_help"]:
                            gaps = [pos for pos, v in day_need[d]["position_shortfalls"].items() if v > 0]
                            if day_need[d]["goalie_shortfall"] > 0:
                                gaps.append("G")
                            st.write(f"⚠️ **{label}**")
                            st.caption(", ".join(gaps) if gaps else "short-handed")
                        else:
                            st.write(f"✅ {label}")

                need_days = [d for d in week_days if day_need[d]["needs_help"]]
                if not need_days:
                    st.success("Your roster covers every day this week -- no streaming needed based on the current schedule data.")
                else:
                    combined_week_df = pd.concat([proj_skater_df, proj_goalie_df], ignore_index=True, sort=False)
                    combined_week_df["team_norm"] = combined_week_df["TEAM"].apply(normalize_team_abbrev)
                    combined_week_df["position_display"] = combined_week_df["position"].astype(str).str.replace(",", "/")
                    owned_names_week = set()
                    try:
                        owned_names_week = {p.name for t in league.teams for p in t.roster}
                    except Exception:
                        pass
                    combined_week_df = combined_week_df[~combined_week_df["name"].isin(owned_names_week)].copy()

                    def days_and_coverage(row):
                        player_team = row["team_norm"]
                        is_g = row["position_display"].strip().upper() == "G"
                        player_positions = eligible_positions(row["position"])
                        playing_days, gap_days = [], []
                        for d in week_days:
                            if player_team not in week_schedules[d]:
                                continue
                            playing_days.append(d)
                            need = day_need[d]
                            if is_g and need["goalie_shortfall"] > 0:
                                gap_days.append(d)
                            elif not is_g and (
                                any(need["position_shortfalls"].get(pos, 0) > 0 for pos in player_positions)
                                or need["general_skater_shortfall"] > 0
                            ):
                                gap_days.append(d)
                        return len(playing_days), len(gap_days), playing_days

                    results = combined_week_df.apply(days_and_coverage, axis=1, result_type="expand")
                    combined_week_df["days_playing_this_week"] = results[0]
                    combined_week_df["gap_days_covered"] = results[1]
                    combined_week_df["playing_days_list"] = results[2]

                    candidates_df = combined_week_df[combined_week_df["gap_days_covered"] > 0].copy()
                    candidates_df = candidates_df.sort_values(
                        ["gap_days_covered", "vorp"], ascending=[False, False]
                    ).reset_index(drop=True)

                    if candidates_df.empty:
                        st.info("No available players play on your gap days this week.")
                    else:
                        st.write(
                            f"**Top streaming targets for the rest of the week** "
                            f"(you get 7 roster moves/week -- prioritize the top rows first):"
                        )
                        for _, row in candidates_df.head(15).iterrows():
                            cols = st.columns([2.3, 1.2, 1, 3])
                            with cols[0]:
                                st.write(f"**{row['name']}** ({row['position_display']}, {row['team_norm']})")
                            with cols[1]:
                                st.write(f"Gap days covered: {row['gap_days_covered']}")
                            with cols[2]:
                                st.write(f"VORP: {row['vorp']:.1f}")
                            with cols[3]:
                                plays_str = ", ".join(d.strftime("%a") for d in row["playing_days_list"])
                                st.write(f"Plays: {plays_str}")
