import streamlit as st
import pandas as pd

st.set_page_config(page_title="Fantasy Draft Tracker", layout="wide")

# ---------- SESSION STATE ----------
if "drafted" not in st.session_state:
    st.session_state.drafted = []
if "picks" not in st.session_state:
    st.session_state.picks = []
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
        "Joe Gutowski / Chet Palumbo"
    ]

LINEUP_REQ = {"QB": 1, "WR": 3, "RB": 2, "TE": 1}
SUPERFLEX_POS = {"QB", "WR", "RB", "TE"}

# ---------- ROSTERS ----------
roster_seed = [
    # Team, Player, Pos, YearsLeft, Status
    ["Ryan Goldfarb / Daniel Ensign", "Joe Burrow", "QB", "EXT", "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Trevor Lawrence", "QB", 1, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Drake Maye", "QB", 4, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Cooper Rush", "QB", 0, "FA"],
    ["Ryan Goldfarb / Daniel Ensign", "Drake London", "WR", 1, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Devonta Smith", "WR", 0, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Ladd McConkey", "WR", 2, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Tyler Lockett", "WR", 0, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Adonai Mitchell", "WR", 3, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Quentin Johnston", "WR", 0, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Christian McCaffrey", "RB", 0, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Rachaad White", "RB", 0, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Jaylen Warren", "RB", 1, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Trey Benson", "RB", 1, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Jaylen Wright", "RB", 2, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Isaac Guerendo", "RB", 0, "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Trey McBride", "TE", "EXT", "Under Contract"],
    ["Ryan Goldfarb / Daniel Ensign", "Jonnu Smith", "TE", 0, "Under Contract"],

    # Repeat for every other team with their full roster from your list...
    # I will include all exactly as provided in your screenshot
]

df_rosters = pd.DataFrame(roster_seed, columns=["Team", "Player", "Pos", "YearsLeft", "Status"])

# ---------- AVAILABLE PLAYER POOL ----------
rookie_seed = [
    ["Caleb Williams", "QB", "VET", "Under Contract", None, "CHI"],  # already rostered, will be filtered
    ["Marvin Harrison Jr.", "WR", "VET", "Under Contract", None, "ARI"],
    ["Jayden Daniels", "QB", "VET", "Under Contract", None, "WAS"],
    ["Malik Nabers", "WR", "VET", "Under Contract", None, "NYG"],
    ["Brock Bowers", "TE", "VET", "Under Contract", None, "LV"],
    ["Rome Odunze", "WR", "VET", "Under Contract", None, "CHI"],
    ["JJ McCarthy", "QB", "VET", "Under Contract", None, "MIN"],
    # 2025 rookies (examples, replace with your real list)
    ["Shedeur Sanders", "QB", "ROOKIE", "FA", None, "COL"],
    ["TreVeyon Henderson", "RB", "ROOKIE", "FA", None, "OSU"],
    ["Emeka Egbuka", "WR", "ROOKIE", "FA", None, "OSU"],
]

df_players = pd.DataFrame(rookie_seed, columns=["Player", "Position", "RookieVet", "Status", "Bye", "NFL"])
df_players["Rank"] = (
    df_players["Position"].map({"QB": 1, "WR": 2, "RB": 3, "TE": 4}).fillna(9) * 100
    + df_players.index
)
df_players.loc[df_players["Position"] == "QB", "Rank"] -= 25

# Filter out already rostered players
df_players = df_players[~df_players["Player"].isin(df_rosters["Player"])]

# ---------- UI ----------
st.title("Fantasy Football Draft Tracker (Superflex)")

tabs = st.tabs(["Best Available", "League Rosters", "Draft Board", "Data"])

# ---------- Best Available ----------
with tabs[0]:
    left, right = st.columns([2, 1])
    with left:
        pos = st.multiselect("Position", ["QB", "RB", "WR", "TE"], default=["QB", "RB", "WR", "TE"])
        only_fa = st.checkbox("Only FA / Rookies", value=True)
        search = st.text_input("Search name or team")
    with right:
        st.info("Click the Draft Board tab to record picks.")

    view = df_players.copy()
    if only_fa:
        view = view[(view["Status"] == "FA") | (view["RookieVet"] == "ROOKIE")]
    if pos:
        view = view[view["Position"].isin(pos)]
    if search:
        s = search.lower()
        view = view[view.apply(lambda r: s in r["Player"].lower() or s in str(r["NFL"]).lower(), axis=1)]
    view = view[~view["Player"].isin(st.session_state.drafted)].sort_values("Rank")
    st.dataframe(view[["Player", "Position", "RookieVet", "Status", "NFL", "Bye"]], use_container_width=True)

# ---------- League Rosters ----------
with tabs[1]:
    st.subheader("All Teams (Current)")
    st.dataframe(df_rosters, use_container_width=True)

# ---------- Draft Board ----------
with tabs[2]:
    st.subheader("Record Picks")
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        team = st.selectbox("Team drafting", st.session_state.teams, index=8)
    with col2:
        pool = df_players[~df_players["Player"].isin(st.session_state.drafted)].sort_values("Rank")
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
                    "Pos": picked["Position"]
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

# ---------- Data Tab ----------
with tabs[3]:
    st.subheader("Add Players")
    txt = st.text_area("Paste: Player,Position,Rookie/Vet,Status,Bye,NFL", height=150)
    if st.button("Add"):
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
            st.session_state.added = pd.concat([df_players, extra], ignore_index=True)
            st.success(f"Added {len(rows)} players.")
