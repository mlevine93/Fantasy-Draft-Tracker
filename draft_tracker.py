import streamlit as st
import pandas as pd

st.set_page_config(page_title="Fantasy Draft Lab — Full League", layout="wide")

# SESSION STATE STORAGE
if "drafted" not in st.session_state:
    st.session_state.drafted = []
if "picks" not in st.session_state:
    st.session_state.picks = []
if "players" not in st.session_state:
    # ---- Full draft pool with FantasyPros rankings ----
    full = [
        # (Player, Pos, Status, Bye, NFL, Rank)
        ("Joe Burrow", "QB", "FA", None, "CIN", 5),
        ("CeeDee Lamb", "WR", "FA", None, "DAL", 11),
        ("Justin Jefferson", "WR", "Under Contract", None, "MIN", 10),
        ("Josh Allen", "QB", "Under Contract", None, "BUF", 1),
        ("Lamar Jackson", "QB", "Under Contract", None, "BAL", 2),
        ("Jayden Daniels", "QB", "ROOKIE", None, "WAS", 3),
        ("Ja'Marr Chase", "WR", "Under Contract", None, "CIN", 6),
        ("Bijan Robinson", "RB", "Under Contract", None, "ATL", 3),
        ("Malik Nabers", "WR", "ROOKIE", None, "NYG", 12),
        ("Caleb Williams", "QB", "ROOKIE", None, "CHI", 17),
        ("Puka Nacua", "WR", "Under Contract", None, "LAR", 18),
        ("Ashton Jeanty", "RB", "ROOKIE", None, "LV", 21),
        ("Amon-Ra St. Brown", "WR", "Under Contract", None, "DET", 16),
        ("Tetairoa McMillan", "WR", "ROOKIE", None, "CAR", 42),
        ("Travis Hunter", "WR", "ROOKIE", None, "JAX", 44),
    ]
    df = pd.DataFrame(full, columns=["Player","Position","Status","Bye","NFL","Rank"])
    st.session_state.players = df

# TITLE & TABS
st.title("Fantasy Draft Lab — Full League")
tabs = st.tabs(["Best Available","League Rosters","Draft Board"])

# Best Available Tab
with tabs[0]:
    st.subheader("Best Available (Auto-Ranked)")
    pos = st.multiselect("Position", ["QB","RB","WR","TE"], default=["QB","RB","WR","TE"])
    only_fa = st.checkbox("Only FA / Rookies", value=True)
    df = st.session_state.players.copy()
    if only_fa:
        df = df[df["Status"].isin(["FA","ROOKIE"])]
    if pos:
        df = df[df["Position"].isin(pos)]
    df = df[~df.Player.isin(st.session_state.drafted)].sort_values("Rank")
    st.dataframe(df)

# League Rosters Tab
with tabs[1]:
    st.subheader("All League Rosters")
    rosters = {
        "Mack": [("Caleb Williams", "QB", 4), ("Brandon Aiyuk", "WR", 2)],
        "Ryan Goldfarb": [("Joe Burrow", "QB", 0), ("CeeDee Lamb", "WR", 0)],
        # ...add other teams similarly from your league...
    }
    df_all = pd.DataFrame(
        [(team, p, pos, yrs) for team, pl in rosters.items() for p,pos,yrs in pl],
        columns=["Team","Player","Pos","YearsLeft"]
    )
    st.dataframe(df_all)

# Draft Board Tab
with tabs[2]:
    st.subheader("Record Picks")
    col1, col2 = st.columns(2)
    with col1:
        team = st.selectbox("Drafting Team", list(rosters.keys(),))
    with col2:
        pool = st.session_state.players[~st.session_state.players.Player.isin(st.session_state.drafted)].sort_values("Rank")
        player = st.selectbox("Player to draft", pool.Player)
    if st.button("Draft"):
        if player not in st.session_state.drafted:
            st.session_state.drafted.append(player)
            st.session_state.picks.append({"Team": team, "Player": player})
            st.success(f"{player} drafted to {team}")

    st.markdown("### Picks so Far")
    if st.session_state.picks:
        st.dataframe(pd.DataFrame(st.session_state.picks))
    else:
        st.write("No picks yet.")
