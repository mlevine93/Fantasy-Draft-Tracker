# draft_tracker.py
import streamlit as st
import pandas as pd

st.set_page_config(page_title="Fantasy Draft Tracker", layout="wide")

# -------- SESSION STATE --------
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
        "Joe Gutowski / Chet Palumbo",
    ]

LINEUP_REQ = {"QB": 1, "WR": 3, "RB": 2, "TE": 1}
SUPERFLEX_POS = {"QB", "WR", "RB", "TE"}

# -------- FULL LEAGUE ROSTERS --------
roster_data = [
    # Ryan Goldfarb / Daniel Ensign
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

    # Jared Ruff
    ["Jared Ruff", "Patrick Mahomes", "QB", 0, "Under Contract"],
    ["Jared Ruff", "Lamar Jackson", "QB", 2, "Under Contract"],
    ["Jared Ruff", "Mack Jones", "QB", 0, "Under Contract"],
    ["Jared Ruff", "Garrett Wilson", "WR", 0, "Under Contract"],
    ["Jared Ruff", "Jaxson Smith-Njigba", "WR", 2, "Under Contract"],
    ["Jared Ruff", "Xavier Worthy", "WR", 3, "Under Contract"],
    ["Jared Ruff", "Darnell Mooney", "WR", 0, "Under Contract"],
    ["Jared Ruff", "A.J. Brown", "WR", "EXT", "Under Contract"],
    ["Jared Ruff", "Kyren Williams", "RB", "EXT", "Under Contract"],
    ["Jared Ruff", "David Montgomery", "RB", 0, "Under Contract"],
    ["Jared Ruff", "Tyjae Spears", "RB", 2, "Under Contract"],
    ["Jared Ruff", "Blake Corum", "RB", 3, "Under Contract"],
    ["Jared Ruff", "Gus Edwards", "RB", 1, "Under Contract"],
    ["Jared Ruff", "Brock Bowers", "TE", 2, "Under Contract"],

    # Brian Heller / Nick DiFlorio
    ["Brian Heller / Nick DiFlorio", "C.J. Stroud", "QB", 2, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Matthew Stafford", "QB", 1, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Russell Wilson", "QB", 0, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Tyler Huntley", "WR", 0, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Amon-Ra St. Brown", "WR", "EXT", "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Amari Cooper", "WR", 0, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Chris Godwin", "WR", 0, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Adam Thielen", "WR", 0, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Saquon Barkley", "RB", 0, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Derrick Henry", "RB", 0, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Xavier Legette", "WR", 3, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Christian Kirk", "WR", 0, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Josh Downs", "WR", 0, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "Tyrone Tracy Jr.", "RB", 2, "Under Contract"],
    ["Brian Heller / Nick DiFlorio", "DeAndre Hopkins", "WR", 0, "Under Contract"],

    # Adam Orchant / Brandon Wendel
    ["Adam Orchant / Brandon Wendel", "Dak Prescott", "QB", 1, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Kyler Murray", "QB", 1, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Tua Tagovailoa", "QB", 0, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Tyreek Hill", "WR", 0, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Ja'Marr Chase", "WR", 0, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Michael Penix Jr.", "QB", 4, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Jameis Winston", "QB", 0, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "DeVonta Smith", "WR", 0, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Jahmyr Gibbs", "RB", 2, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "A.J. Brown", "WR", "EXT", "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Chris Olave", "WR", 1, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Romeo Doubs", "WR", 2, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Courtland Sutton", "WR", 1, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Khalil Shakir", "WR", 3, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Josh Palmer", "WR", 3, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Travis Etienne Jr.", "RB", 0, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Chase Brown", "RB", 1, "Under Contract"],
    ["Adam Orchant / Brandon Wendel", "Justice Hill", "RB", 0, "Under Contract"],

    # Adam Parkes / Adam Balagia
    ["Adam Parkes / Adam Balagia", "Jaylen Daniels", "QB", 3, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Baker Mayfield", "QB", 2, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Brock Purdy", "QB", 2, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Geno Smith", "QB", "EXT", "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Jordan Love", "QB", 2, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Brandon Aiyuk", "WR", 2, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Nico Collins", "WR", 0, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Keenan Allen", "WR", 1, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Brandin Cooks", "WR", 1, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Jonathan Taylor", "RB", "EXT", "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Tank Dell", "WR", 2, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Calvin Ridley", "WR", 2, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Tim Patrick", "WR", 0, "Under Contract"],
    ["Adam Parkes / Adam Balagia", "Deebo Samuel", "WR", 0, "Under Contract"],

    # BULLS LLC
    ["BULLS LLC", "Jayden Daniels", "QB", 3, "Under Contract"],
    ["BULLS LLC", "Josh Allen", "QB", 1, "Under Contract"],
    ["BULLS LLC", "Baker Mayfield", "QB", 2, "Under Contract"],
    ["BULLS LLC", "Jordan Love", "QB", 2, "Under Contract"],
    ["BULLS LLC", "Brandon Thomas Jr.", "WR", 3, "Under Contract"],
    ["BULLS LLC", "Jonathan Taylor", "RB", "EXT", "Under Contract"],
    ["BULLS LLC", "De’Von Achane", "RB", 1, "Under Contract"],
    ["BULLS LLC", "D’Andre Swift", "RB", 1, "Under Contract"],
    ["BULLS LLC", "Chuba Hubbard", "RB", 2, "Under Contract"],
    ["BULLS LLC", "Puka Nacua", "WR", "EXT", "Under Contract"],
    ["BULLS LLC", "Malik Nabers", "WR", 3, "Under Contract"],

    # Keith Markowitz / Brett Markowitz
    ["Keith Markowitz / Brett Markowitz", "CeeDee Lamb", "WR", "EXT", "Under Contract"],
    ["Keith Markowitz / Brett Markowitz", "DK Metcalf", "WR", 2, "Under Contract"],
    ["Keith Markowitz / Brett Markowitz", "Tee Higgins", "WR", 2, "Under Contract"],
    ["Keith Markowitz / Brett Markowitz", "Rashid Shaheed", "WR", 1, "Under Contract"],

    # Josh Orchant / Mike Levy
    ["Josh Orchant / Mike Levy", "Aidan O’Connell", "QB", 0, "Under Contract"],
    ["Josh Orchant / Mike Levy", "Baker Mayfield", "QB", 2, "Under Contract"],
    ["Josh Orchant / Mike Levy", "Kenny Pickett", "QB", 0, "Under Contract"],
    ["Josh Orchant / Mike Levy", "Brian Thomas Jr.", "WR", 3, "Under Contract"],
    ["Josh Orchant / Mike Levy", "George Pickens", "WR", 1, "Under Contract"],
    ["Josh Orchant / Mike Levy", "DJ Moore", "WR", 0, "Under Contract"],
    ["Josh Orchant / Mike Levy", "Puka Nacua", "WR", "EXT", "Under Contract"],
    ["Josh Orchant / Mike Levy", "Malik Nabers", "WR", 3, "Under Contract"],
    ["Josh Orchant / Mike Levy", "Deebo Samuel", "WR", 0, "Under Contract"],

    # Mack Levine / Jeff Levine
    ["Mack Levine / Jeff Levine", "Caleb Williams", "QB", 4, "Under Contract"],
    ["Mack Levine / Jeff Levine", "Sam Darnold", "QB", 1, "Under Contract"],
    ["Mack Levine / Jeff Levine", "Brandon Aiyuk", "WR", 2, "Under Contract"],
    ["Mack Levine / Jeff Levine", "Brian Thomas Jr.", "WR", 3, "Under Contract"],
    ["Mack Levine / Jeff Levine", "George Pickens", "WR", 1, "Under Contract"],
    ["Mack Levine / Jeff Levine", "DJ Moore", "WR", 0, "Under Contract"],
    ["Mack Levine / Jeff Levine", "Chuba Hubbard", "RB", 2, "Under Contract"],
    ["Mack Levine / Jeff Levine", "Jake Ferguson", "TE", 2, "Under Contract"],

    # Joe Gutowski / Chet Palumbo
    ["Joe Gutowski / Chet Palumbo", "Jared Goff", "QB", 0, "Under Contract"],
    ["Joe Gutowski / Chet Palumbo", "Sam Darnold", "QB", 1, "Under Contract"],
    ["Joe Gutowski / Chet Palumbo", "JJ McCarthy", "QB", 2, "Under Contract"],
    ["Joe Gutowski / Chet Palumbo", "Bo Nix", "QB", 2, "Under Contract"],
    ["Joe Gutowski / Chet Palumbo", "Aidan O’Connell", "QB", 0, "Under Contract"],
    ["Joe Gutowski / Chet Palumbo", "Caleb Williams", "QB", 4, "Under Contract"],
]

df_rosters = pd.DataFrame(roster_data, columns=["Team", "Player", "Pos", "YearsLeft", "Status"])

# ---------- AVAILABLE PLAYERS (FAs + Rookies) ----------
available_players = [
    ["Shedeur Sanders", "QB", "ROOKIE", "FA", None, "COL"],
    ["Quinshon Judkins", "RB", "ROOKIE", "FA", None, "CLE"],
    ["Emeka Egbuka", "WR", "ROOKIE", "FA", None, "HOU"],
    ["Colston Loveland", "TE", "ROOKIE", "FA", None, "LAC"],
]

df_players = pd.DataFrame(available_players, columns=["Player", "Position", "RookieVet", "Status", "Bye", "NFL"])
df_players["Rank"] = (
    df_players["Position"].map({"QB": 1, "WR": 2, "RB": 3, "TE": 4}).fillna(9) * 100
    + df_players.index
)
df_players.loc[df_players["Position"] == "QB", "Rank"] -= 25

# Remove players already rostered
df_players = df_players[~df_players["Player"].isin(df_rosters["Player"])]

# ---------- UI ----------
st.title("Fantasy Football Draft Tracker (Superflex)")

tabs = st.tabs(["Best Available", "League Rosters", "Draft Board", "Data"])

# Best Available
with tabs[0]:
    left, right = st.columns([2, 1])
    with left():
        pos = st.multiselect("Position", ["QB","RB","WR","TE"], default=["QB","RB","WR","TE"])
        search = st.text_input("Search player/team")
    with right():
        st.info("Draft picks populate this automatically.")

    view = df_players.copy()
    if pos:
        view = view[view["Position"].isin(pos)]
    if search:
        s = search.lower()
        view = view[view.apply(lambda r: s in r["Player"].lower() or s in str(r["NFL"]).lower(), axis=1)]
    view = view[~view["Player"].isin(st.session_state.drafted)].sort_values("Rank")
    st.dataframe(view[["Player","Position","RookieVet","Status","NFL","Bye"]], use_container_width=True)

# League Rosters
with tabs[1]:
    st.dataframe(df_rosters, use_container_width=True)

# Draft Board
with tabs[2]:
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        team = st.selectbox("Team drafting", st.session_state.teams, index=8)
    with col2:
        pool = df_players[~df_players["Player"].isin(st.session_state.drafted)].sort_values("Rank")
        player = st.selectbox("Select player to draft", pool["Player"])
    with col3:
        if st.button("Draft"):
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

    if st.button("Undo last pick"):
        last = st.session_state.picks.pop()
        st.session_state.drafted.remove(last["Player"])
        st.info(f"Undid pick: {last['Player']}")

# Data Tab
with tabs[3]:
    st.subheader("Add Players")
    txt = st.text_area("Paste rows as: Player,Position,Rookie/Vet,Status,Bye,NFL", height=150)
    if st.button("Add"):
        rows = [line.split(',') for line in txt.strip().splitlines()]
        extra = pd.DataFrame(rows, columns=["Player","Position","RookieVet","Status","Bye","NFL"])
        extra["Rank"] = extra["Position"].map({"QB":1,"WR":2,"RB":3,"TE":4}).fillna(9)*100 + range(len(extra))
        extra.loc[extra["Position"]=="QB","Rank"] -= 25
        st.session_state.added = pd.concat([df_players, extra], ignore_index=True)
        st.success(f"Added {len(extra)} players.")
