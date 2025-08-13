import streamlit as st
import pandas as pd

st.set_page_config(layout="wide", page_title="Dynasty League Draft Tracker")

# ======================
# DATA SETUP
# ======================

teams_data = {
    "Ryan Goldfarb": [
        ("Joe Burrow", "EXT"), ("Trevor Lawrence", 1), ("Drake Maye", 4), ("Cooper Rush", 0),
        ("Drake London", 1), ("Devonta Smith", 0), ("Ladd McConkey", 2), ("Tyler Lockett", 0),
        ("Adonai Mitchell", 3), ("Quentin Johnston", 0), ("Christian McCaffrey", 0),
        ("Rachaad White", 0), ("Jaylen Warren", 1), ("Trey Benson", 1), ("Jaylen Wright", 2),
        ("Isaac Guerendo", 0), ("Trey McBride", "EXT"), ("Jonnu Smith", 0)
    ],
    "Daniel Ensign": [
        ("Patrick Mahomes", 0), ("Lamar Jackson", 2), ("Mack Jones", 0), ("Garrett Wilson", 0),
        ("Jaxson Smith-Njigba", 2), ("Terry McLaurin", 1), ("Xavier Worthy", 3), ("Darnell Mooney", 0),
        ("A.J. Brown", "EXT"), ("Marquise Brown", 1), ("Kyren Williams", "EXT"), ("David Montgomery", 0),
        ("Tyjae Spears", 2), ("Blake Corum", 3), ("Gus Edwards", 1), ("Brock Bowers", 2)
    ],
    "Jared Ruff": [
        ("C.J. Stroud", 2), ("Matthew Stafford", 1), ("Russell Wilson", 0), ("Tyler Huntler", 0),
        ("Amon-Ra St. Brown", "EXT"), ("Mike Evans", "EXT"), ("Rashee Rice", 1), ("Rome Odunze", 3),
        ("Jakobi Meyers", 0), ("Jerry Jeudy", 0), ("Saquon Barkley", 0), ("Kenneth Walker III", 1),
        ("Joe Mixon", 0), ("James Conner", 0), ("Jonathan Brooks", 3), ("MarShawn Lloyd", 3)
    ],
    "Brian Heller / Nick DiFlorio": [
        ("Dak Prescott", 1), ("Kyler Murray", 1), ("Tua Tagovailoa", 0), ("Tyreek Hill", 0),
        ("Ja'Marr Chase", 0), ("Chris Godwin", 0), ("Chris Olave", 1), ("Ricky Pearsall", 4),
        ("Adam Thielen", 0), ("Jahmyr Gibbs", 2), ("Derrick Henry", 0), ("James Cook", 1),
        ("Josh Jacobs", 2), ("J.K. Dobbins", 1), ("Jordan Mason", 0), ("Elijah Mitchell", 0)
    ],
    "Adam Orchant / Brandon Wendel": [
        ("Jalen Hurts", 0), ("Justin Herbert", 0), ("Aaron Rodgers", 1), ("Michael Penix Jr.", 4),
        ("Justin Jefferson", "EXT"), ("Romeo Doubs", 2), ("Cooper Kupp", 2), ("Marvin Harrison Jr.", 3),
        ("Stefon Diggs", 1), ("Xavier Legette", 3), ("Amari Cooper", 0), ("Breece Hall", 1),
        ("De'Von Achane", 1), ("Alvin Kamara", 0), ("Rhamondre Stevenson", 0), ("Javonte Williams", 2),
        ("Nick Chubb", 1), ("Mark Andrews", 1), ("Travis Kelce", 1)
    ],
    "Adam Parkes / Adam Balagia": [
        ("Anthony Richardson", 2), ("Brock Purdy", 2), ("Geno Smith", "EXT"), ("Marvin Harrison Jr.", 3),
        ("Michael Pittman Jr.", 2), ("Jauan Jennings", 0), ("Christian Watson", 2), ("Jayden Reed", 1),
        ("Christian Kirk", 0), ("Wan'Dale Robinson", 1), ("Jonathan Taylor", "EXT"), ("Austin Ekeler", 1),
        ("D'Andre Swift", 1), ("Tony Pollard", 3), ("Zach Charbonnet", 2), ("Travis Etienne Jr.", 0),
        ("T.J. Hockenson", 1), ("Dallas Goedert", 1), ("Sam LaPorta", 2)
    ],
    "BULLS LLC": [
        ("Jayden Daniels", 3), ("Jordan Love", 2), ("Derek Carr", 1), ("CeeDee Lamb", "EXT"),
        ("Tee Higgins", 2), ("Keon Coleman", 3), ("Courtland Sutton", 1), ("Josh Palmer", 3),
        ("Dontayvion Wicks", 2), ("Tyrone Tracy Jr.", 2), ("Andrei Iosivas", 0), ("Sincere McCormick", 0),
        ("Chase Brown", 1), ("Isiah Pacheco", 1), ("Aaron Jones", 0), ("Tucker Kraft", 1),
        ("Dalton Schultz", 2)
    ],
    "Keith Markowitz / Brett Markowitz": [
        ("Josh Allen", 1), ("Baker Mayfield", 2), ("Kirk Cousins", 0), ("DK Metcalf", 2),
        ("Brandin Cooks", 1), ("Jameson Williams", 2), ("Rashid Shaheed", 1), ("George Pickens", 1),
        ("DJ Moore", 0), ("Tank Dell", 2), ("Calvin Ridley", 2), ("Tim Patrick", 0),
        ("Chubba Hubbard", 2), ("Bucky Irving", 1), ("Rico Dowdle", 1), ("Ray Davis", 3),
        ("Kendre Miller", 0), ("Jake Ferguson", 2), ("Brian Robinson Jr.", 1)
    ],
    "Josh Orchant / Mike Levy": [
        ("Caleb Williams", 4), ("Sam Darnold", 1), ("Bryce Young", 0), ("Brandon Allen", 0),
        ("Brian Thomas Jr.", 3), ("Brandon Aiyuk", 2), ("George Pickens", 1), ("DJ Moore", 0),
        ("Tank Dell", 2), ("Calvin Ridley", 2), ("Tim Patrick", 0), ("Ray Davis", 3),
        ("Kendre Miller", 0), ("Jake Ferguson", 2)
    ],
    "Mack Levine / Jeff Levine": [
        ("Bo Nix", 2), ("Aidan O'Connell", 0), ("Spencer Rattler", 0), ("Skylar Thompson", 0),
        ("Malik Nabers", 3), ("Zay Flowers", 1), ("Deebo Samuel", 0), ("Jaylen Waddle", 0),
        ("DeAndre Hopkins", 0), ("Bijan Robinson", 2), ("Najee Harris", 1), ("Tyler Allgeier", 1),
        ("Dalton Kincaid", 2)
    ]
}

# Rookies available
rookies = [
    "Quinshon Judkins", "TreVeyon Henderson", "Emeka Egbuka", "Xavier Worthy (FA)",
    "Michael Pratt", "Braelon Allen"
]

# ======================
# STREAMLIT APP
# ======================

st.title("Dynasty League Draft Tracker")

tab1, tab2 = st.tabs(["Team Rosters", "Available Players"])

# Team rosters
with tab1:
    for team, roster in teams_data.items():
        st.subheader(team)
        df = pd.DataFrame(roster, columns=["Player", "Contract"])
        st.dataframe(df, hide_index=True)

# Available players
with tab2:
    all_players = [player for team in teams_data.values() for player, _ in team]
    available_players = rookies
    df_avail = pd.DataFrame(available_players, columns=["Available Player"])
    st.dataframe(df_avail, hide_index=True)
