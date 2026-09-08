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
        "useful for deciding who to stream on your empty roster/Utility spots. "
        "Ranked by the same VORP used in Draft Helper (season-long value), not "
        "a day-specific projection, since we don't have per-game projections."
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

                # Figure out who's actually available (not rostered) if we can.
                owned_names = set()
                if league is not None:
                    try:
                        owned_names = {p.name for t in league.teams for p in t.roster}
                    except Exception:
                        owned_names = set()

                playing_df = combined_stream_df[combined_stream_df["team_norm"].isin(teams_today)]
                available_stream_df = playing_df[~playing_df["name"].isin(owned_names)]
                available_stream_df = available_stream_df.sort_values("vorp", ascending=False).reset_index(drop=True)

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
                    cols = st.columns([2.5, 1, 1, 1])
                    with cols[0]:
                        st.write(f"**{row['name']}** ({row['position_display']}, {row['team_norm']})")
                    with cols[1]:
                        st.write(f"VORP: {row['vorp']:.1f}")
                    with cols[2]:
                        st.write(f"FP: {row['value']:.0f}")
                    with cols[3]:
                        gp = row.get("GP")
                        st.write(f"GP: {gp:.0f}" if pd.notna(gp) else "")

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
