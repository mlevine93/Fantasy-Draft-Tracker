import streamlit as st
import pandas as pd

st.set_page_config(page_title="Fantasy Draft Tracker", layout="wide")

# --------- SESSION STATE ---------
if "drafted" not in st.session_state:
    st.session_state.drafted = []  # list of names
if "teams" not in st.session_state:
    st.session_state.teams = [
        "Ryan Goldfarb / Daniel Ensign",
        "Jared Ruff",
        "Brian Heller / Nick DiFlorio",
        "Adam Orchant / Brandon Wendel",
        "Adam Parkes / Adam Balagia",
        "BULLS LLC",
        "Keith Markowitz / Brett Markowitz",
        "Josh Orchant / Mike Levy",
        "Mack Levine / Jeff Levine",
        "Joe Gutowski / Chet Palumbo",
    ]
if "picks" not in st.session_state:
    st.session_state.picks = []  # dicts: {pick, team, player, pos}
if "players" not in st.session_state:
    st.session_state.players = None  # will hold DataFrame

LINEUP_REQ = {"QB": 1, "WR": 3, "RB": 2, "TE": 1}
SUPERFLEX_POS = {"QB", "WR", "RB", "TE"}

# --------- STARTER DATA ---------
available_seed = [
    ["Joe Burrow", "QB", "VET", "FA", None, "CIN"],
    ["CeeDee Lamb", "WR", "VET", "FA", None, "DAL"],
    ["Justin Jefferson", "WR", "VET", "Under Contract", None, "MIN"],
    ["Christian McCaffrey", "RB", "VET", "Under Contract", None, "SF"],
    ["Amon-Ra St. Brown", "WR", "VET", "Under Contract", None, "DET"],
    ["Puka Nacua", "WR", "VET", "Under Contract", None, "LAR"],
    ["Cam Ward", "QB", "ROOKIE", "FA", None, "TEN"],
    ["Ashton Jeanty", "RB", "ROOKIE", "FA", None, "LV"],
    ["Tetairoa McMillan", "WR", "ROOKIE", "FA", None, "CAR"],
    ["Travis Hunter", "WR", "ROOKIE", "FA", None, "JAX"],
]

df_players = pd.DataFrame(
    available_seed,
    columns=["Player", "Position", "RookieVet", "Status", "Bye", "NFL"]
)
df_players["Rank"] = (
    df_players["Position"].map({"QB": 1, "WR": 2, "RB": 3, "TE": 4}).fillna(9) * 100
    + df_players.index
)
df_players.loc[df_players["Position"] == "QB", "Rank"] -= 25

st.session_state.players = df_players

# --------- UI ----------
st.title("Fantasy Football Draft Tracker (Superflex)")

tabs = st.tabs(["Best Available", "League Rosters", "Draft Board", "Data"])

# ---------- Best Available ----------
with tabs[0]:
    left, right = st.columns([2, 1])
    with left:
        st.subheader("Filter")
        pos = st.multiselect("Position", ["QB", "RB", "WR", "TE"], default=["QB", "RB", "WR", "TE"])
        only_fa = st.checkbox("Only players you can draft (FA / Rookies)", value=True)
        search = st.text_input("Search name/team")
    with right:
        st.info("Tip: Click the **Draft Board** tab to record picks.\n\n"
                "This list updates automatically when you draft someone.")

    view = st.session_state.players.copy()
    if only_fa:
        view = view[(view["Status"] == "FA") | (view["RookieVet"] == "ROOKIE")]
    if pos:
        view = view[view["Position"].isin(pos)]
    if search:
        s = search.lower()
        view = view[view.apply(lambda r: s in r["Player"].lower() or s in str(r["NFL"]).lower(), axis=1)]
    view = view[~view["Player"].isin(st.session_state.drafted)].sort_values("Rank")
    st.dataframe(view[["Player", "Position", "RookieVet", "Status", "NFL", "Bye", "Rank"]], use_container_width=True)

# ---------- League Rosters ----------
with tabs[1]:
    st.subheader("All Teams (current snapshot)")
    st.caption("These are placeholders so the app works now. You can paste/upload full league data in the **Data** tab.")
    roster_seed = [
        ["Mack Levine / Jeff Levine", "Caleb Williams", "QB", 4, "Under Contract"],
        ["Mack Levine / Jeff Levine", "Sam Darnold", "QB", 1, "Under Contract"],
        ["Mack Levine / Jeff Levine", "Brandon Aiyuk", "WR", 2, "Under Contract"],
        ["Ryan Goldfarb / Daniel Ensign", "Joe Burrow", "QB", 0, "FA"],
        ["Keith Markowitz / Brett Markowitz", "CeeDee Lamb", "WR", 0, "FA"],
    ]
    df_rosters = pd.DataFrame(roster_seed, columns=["Team", "Player", "Pos", "YearsLeft", "Status"])
    st.dataframe(df_rosters, use_container_width=True)

# ---------- Draft Board ----------
with tabs[2]:
    st.subheader("Record Picks")
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        team = st.selectbox("Team drafting", st.session_state.teams, index=8)
    with col2:
        pool = st.session_state.players[~st.session_state.players["Player"].isin(st.session_state.drafted)].sort_values("Rank")
        player = st.selectbox("Select player to draft", pool["Player"])
    with col3:
        if st.button("Draft"):
            if player not in st.session_state.drafted:
                st.session_state.drafted.append(player)
                picked = pool[pool["Player"] == player].iloc[0]
                st.session_state.picks.append({
                    "Pick": len(st.session_state.picks) + 1,
                    "Team": team,
                    "Player": player,
                    "Pos": picked["Position"],
                })
                st.success(f"Drafted {player} to {team}")

    st.markdown("### Draft History")
    if st.session_state.picks:
        st.dataframe(pd.DataFrame(st.session_state.picks), use_container_width=True)
    else:
        st.write("No picks yet.")

    if st.button("Undo last pick"):
        if st.session_state.picks:
            last = st.session_state.picks.pop()
            if last["Player"] in st.session_state.drafted:
                st.session_state.drafted.remove(last["Player"])
            st.info(f"Undid pick: {last['Player']}")

# ---------- Data ----------
with tabs[3]:
    st.subheader("Add more players quickly")
    st.caption("Paste rows as: Player,Position,Rookie/Vet,Status(FA/Under Contract),Bye,NFL")
    txt = st.text_area("One per line", height=150, placeholder="Example:\nJalen Hurts,QB,VET,Under Contract,10,PHI")
    if st.button("Add pasted rows"):
        rows = []
        for line in txt.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                rows.append(parts[:6])
        if rows:
            extra = pd.DataFrame(rows, columns=["Player", "Position", "RookieVet", "Status", "Bye", "NFL"])
            extra["Rank"] = (
                extra["Position"].map({"QB": 1, "WR": 2, "RB": 3, "TE": 4}).fillna(9) * 100
                + range(len(extra))
            )
            extra.loc[extra["Position"] == "QB", "Rank"] -= 25
            st.session_state.players = pd.concat([st.session_state.players, extra], ignore_index=True)
            st.success(f"Added {len(rows)} players to available pool.")
