#!/usr/bin/env python3
"""
Kickoff Lab — data pipeline.
Reads whatever scripts/fetch_data.py downloaded into data/raw/ and turns it
into one dist/fpl_data.json for the page to load.

Variable names below use "prev"/"cur" for last season / this season. A couple
of aggregates keep an internal "25"/"26" suffix purely as short labels
(inherited from when this was first built for the 2025-26 -> 2026-27
handover) — they do not hardcode a year; fetch_data.py already re-points them
at whatever two seasons are current each time this runs.
"""
import argparse, pandas as pd, numpy as np, json, glob, os, math, datetime as dt

ap = argparse.ArgumentParser()
ap.add_argument('--raw', default='data/raw')
ap.add_argument('--out', default='dist/fpl_data.json')
args = ap.parse_args()

RAW = args.raw
OUT = args.out
os.makedirs(os.path.dirname(OUT) or '.', exist_ok=True)
P = lambda f: os.path.join(RAW, f)

with open(P('season.txt')) as fh:
    THIS_SEASON, LAST_SEASON, _ = [l.strip() for l in fh.readlines()]
SEASON_LABEL = THIS_SEASON.replace('-', '/')[:4] + '/' + THIS_SEASON[-2:]  # '2026-2027' -> '2026/27'
print(f'Building for season {THIS_SEASON} (previous: {LAST_SEASON})')

# ---------------------------------------------------------------- load
players   = pd.read_csv(P('cur/players.csv'))
pstats    = pd.read_csv(P('cur/playerstats.csv'))
teams     = pd.read_csv(P('cur/teams.csv'))
prev_teams = pd.read_csv(P('prev/teams.csv'))       # elo fallback source (see below)
gwsum     = pd.read_csv(P('cur/gameweek_summaries.csv')).sort_values('id')
fixtures  = pd.read_csv(P('fixtures.csv'))          # vaastav: full 380 fixture list + FDR
vteams    = pd.read_csv(P('teams_vaastav.csv'))     # vaastav teams (id <-> code)

p25       = pd.read_csv(P('prev/players.csv'))
ps25all   = pd.read_csv(P('prev/playerstats.csv'), low_memory=False)
pms25     = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(P('prev/gw/gw*_pms.csv')))], ignore_index=True)
m25       = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(P('prev/gw/gw*_matches.csv')))], ignore_index=True)

cur_pms_files = sorted(glob.glob(P('cur/gw/gw*_pms.csv')), key=lambda p: int(''.join(filter(str.isdigit, os.path.basename(p)))))
cur_m_files = sorted(glob.glob(P('cur/gw/gw*_matches.csv')), key=lambda p: int(''.join(filter(str.isdigit, os.path.basename(p)))))
# Every finished gameweek so far this season — NOT just GW1. This is what makes
# re-running the pipeline mid-season actually pick up newer matches instead of
# silently freezing on the first one.
pms26 = pd.concat([pd.read_csv(f) for f in cur_pms_files], ignore_index=True) if cur_pms_files else pd.DataFrame()
m26 = pd.concat([pd.read_csv(f) for f in cur_m_files], ignore_index=True) if cur_m_files else pd.DataFrame()
print(f'current season match files loaded: {len(cur_m_files)} gameweek(s)')

def _poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))

def _poisson_cdf(k, lam):
    return sum(_poisson_pmf(i, lam) for i in range(0, int(k) + 1))

POSMAP = {'Goalkeeper': 1, 'Defender': 2, 'Midfielder': 3, 'Forward': 4}
POSNAME = {1: 'GKP', 2: 'DEF', 3: 'MID', 4: 'FWD'}
GOAL_PTS = {1: 10, 2: 6, 3: 5, 4: 4}
CS_PTS = {1: 4, 2: 4, 3: 1, 4: 0}
DC_THRESH = {1: 99, 2: 10, 3: 12, 4: 12}

# ---------------------------------------------------------------- season state
gwsum['deadline_dt'] = pd.to_datetime(gwsum['deadline_time'], utc=True, format='mixed')
finished_gws = gwsum[gwsum.finished == True].id.tolist()
LAST_GW = max(finished_gws) if finished_gws else 0
NEXT_GW = LAST_GW + 1
GWS_PLAYED = LAST_GW
print(f'Season state: GW{LAST_GW} finished, next = GW{NEXT_GW}')

# ---------------------------------------------------------------- team frames
teams = teams.copy()
code2id = dict(zip(teams.code, teams.id))
id2code = dict(zip(teams.id, teams.code))
code2short = dict(zip(teams.code, teams.short_name))
code2name = dict(zip(teams.code, teams.name))
TEAM_CODES = list(teams.code)

# ---------------------------------------------------------------- team attack / defence ratings
def match_rows(mdf, weight_base, is_prev_season):
    rows = []
    if mdf.empty:
        return rows
    for _, r in mdf.iterrows():
        if not r.get('finished', False):
            continue
        h, a = r['home_team'], r['away_team']
        if pd.isna(h) or pd.isna(a):
            continue
        hxg, axg = r.get('home_expected_goals_xg'), r.get('away_expected_goals_xg')
        hg, ag = r.get('home_score'), r.get('away_score')
        # blend xG with actual goals (xG is more stable, goals carry finishing/luck info)
        hv = 0.75 * hxg + 0.25 * hg if pd.notna(hxg) and pd.notna(hg) else (hxg if pd.notna(hxg) else hg)
        av = 0.75 * axg + 0.25 * ag if pd.notna(axg) and pd.notna(ag) else (axg if pd.notna(axg) else ag)
        if pd.isna(hv) or pd.isna(av):
            continue
        gw = r.get('gameweek', 38)
        w = weight_base * (0.55 + 0.45 * (gw / 38.0)) if is_prev_season else weight_base
        rows.append(dict(att=int(h), dfn=int(a), venue='H', val=float(hv), w=w))
        rows.append(dict(att=int(a), dfn=int(h), venue='A', val=float(av), w=w))
    return rows

obs = match_rows(m25, 1.0, True) + match_rows(m26, 5.0, False)
obs = pd.DataFrame(obs)
mu_H = np.average(obs[obs.venue == 'H'].val, weights=obs[obs.venue == 'H'].w)
mu_A = np.average(obs[obs.venue == 'A'].val, weights=obs[obs.venue == 'A'].w)
MU = {'H': mu_H, 'A': mu_A}
print(f'League baseline expected goals  home={mu_H:.2f}  away={mu_A:.2f}')

# Elo prior -> attack / defence multipliers
# Early in a new season the source hasn't refreshed this season's teams.csv
# with Elo yet (every value comes back NaN for a few weeks) — fall back to
# last season's Elo for those clubs, and to the fallback average for anyone
# missing there too (e.g. a club promoted straight into this file).
elo_cur = dict(zip(teams.code, pd.to_numeric(teams.elo, errors='coerce')))
elo_prev = dict(zip(prev_teams.code, pd.to_numeric(prev_teams.elo, errors='coerce')))
prev_vals = [v for v in elo_prev.values() if pd.notna(v)]
fallback_elo = float(np.mean(prev_vals)) if prev_vals else 1800.0
elo = {}
for c, v in elo_cur.items():
    if pd.notna(v):
        elo[c] = float(v)
    elif c in elo_prev and pd.notna(elo_prev[c]):
        elo[c] = float(elo_prev[c])
    else:
        elo[c] = fallback_elo
elo_mean, elo_sd = np.mean(list(elo.values())), np.std(list(elo.values()))
if elo_sd == 0:
    elo_sd = 1.0

# relegated clubs still appear as last season's opponents: fit them too, report only current clubs
ALL_CODES = sorted(set(obs.att) | set(obs.dfn) | set(TEAM_CODES))
ATT = {c: 1.0 for c in ALL_CODES}
DEF = {c: 1.0 for c in ALL_CODES}

for it in range(120):
    # attack step
    newA = {}
    for c in ALL_CODES:
        sub = obs[obs.att == c]
        if len(sub) == 0:
            newA[c] = ATT[c]; continue
        num = (sub.val * sub.w).sum()
        den = sum(r.w * MU[r.venue] * DEF[r.dfn] for r in sub.itertuples())
        newA[c] = num / den if den > 0 else 1.0
    m = np.mean(list(newA.values()))
    ATT = {c: v / m for c, v in newA.items()}
    # defence step
    newD = {}
    for c in ALL_CODES:
        sub = obs[obs.dfn == c]
        if len(sub) == 0:
            newD[c] = DEF[c]; continue
        num = (sub.val * sub.w).sum()
        den = sum(r.w * MU[r.venue] * ATT[r.att] for r in sub.itertuples())
        newD[c] = num / den if den > 0 else 1.0
    m = np.mean(list(newD.values()))
    DEF = {c: v / m for c, v in newD.items()}

# fit log(rating) ~ elo on current clubs with a real sample, then shrink everyone toward that prior
games_played = {c: obs[obs.att == c].w.sum() for c in ALL_CODES}
est = [c for c in TEAM_CODES if games_played[c] > 8]
x = np.array([(elo[c] - elo_mean) / elo_sd for c in est])
ba = np.polyfit(x, np.log([ATT[c] for c in est]), 1)
bd = np.polyfit(x, np.log([DEF[c] for c in est]), 1)
print(f'Elo->attack slope {ba[0]:+.3f}, Elo->defence slope {bd[0]:+.3f} (fit on {len(est)} clubs)')

PRIOR_W = 9.0   # equivalent games of Elo prior
ATTf, DEFf = dict(ATT), dict(DEF)
for c in TEAM_CODES:
    z = (elo[c] - elo_mean) / elo_sd
    pa, pd_ = math.exp(np.polyval(ba, z)), math.exp(np.polyval(bd, z))
    n = games_played[c]
    ATTf[c] = (n * ATT[c] + PRIOR_W * pa) / (n + PRIOR_W)
    DEFf[c] = (n * DEF[c] + PRIOR_W * pd_) / (n + PRIOR_W)
# normalise so the average *current* Premier League club sits at 1.00
ma = np.mean([ATTf[c] for c in TEAM_CODES]); md = np.mean([DEFf[c] for c in TEAM_CODES])
ATT = {c: v / ma for c, v in ATTf.items()}
DEF = {c: v / md for c, v in DEFf.items()}

def team_xg(att_code, def_code, venue):
    return MU[venue] * ATT[att_code] * DEF[def_code]

TEAM_AVG_XG = {c: 0.5 * (MU['H'] + MU['A']) * ATT[c] for c in TEAM_CODES}
TEAM_AVG_XGC = {c: 0.5 * (MU['H'] + MU['A']) * DEF[c] for c in TEAM_CODES}

# observed team attack/defence tables (for the team analysis tab)
team_obs = {}
for c in TEAM_CODES:
    a25 = m25[(m25.home_team == c) | (m25.away_team == c)]
    def agg(df, code):
        n = len(df)
        if n == 0:
            return dict(gp=0)
        h = df[df.home_team == code]; a = df[df.away_team == code]
        return dict(
            gp=n,
            gf=float(h.home_score.sum() + a.away_score.sum()),
            ga=float(h.away_score.sum() + a.home_score.sum()),
            xg=float(h.home_expected_goals_xg.sum() + a.away_expected_goals_xg.sum()),
            xgc=float(h.away_expected_goals_xg.sum() + a.home_expected_goals_xg.sum()),
            shots=float(h.home_total_shots.sum() + a.away_total_shots.sum()),
            shots_c=float(h.away_total_shots.sum() + a.home_total_shots.sum()),
            bc=float(h.home_big_chances.sum() + a.away_big_chances.sum()),
            bc_c=float(h.away_big_chances.sum() + a.home_big_chances.sum()),
            poss=float((h.home_possession.sum() + a.away_possession.sum()) / n),
            cs=int((h.away_score == 0).sum() + (a.home_score == 0).sum()),
        )
    m26_c = m26[(m26.home_team == c) | (m26.away_team == c)] if not m26.empty else m26
    team_obs[c] = {'s25': agg(a25, c), 's26': agg(m26_c, c)}

# ---------------------------------------------------------------- fixtures
fx = fixtures[['event', 'kickoff_time', 'team_h', 'team_a', 'team_h_difficulty', 'team_a_difficulty']].dropna(subset=['event'])
fx['event'] = fx['event'].astype(int)
fx['hc'] = fx.team_h.map(id2code)
fx['ac'] = fx.team_a.map(id2code)

# per team -> per gw list of fixtures (handles blanks and doubles)
team_fix = {c: {} for c in TEAM_CODES}
for _, r in fx.iterrows():
    gw = int(r.event)
    for me, opp, venue, fdr in ((r.hc, r.ac, 'H', r.team_h_difficulty), (r.ac, r.hc, 'A', r.team_a_difficulty)):
        xgf = team_xg(me, opp, venue)
        xga = team_xg(opp, me, 'A' if venue == 'H' else 'H')
        team_fix[me].setdefault(gw, []).append(dict(
            opp=int(opp), h=1 if venue == 'H' else 0, fdr=int(fdr),
            xg=round(xgf, 3), xgc=round(xga, 3),
            # own difficulty scale 1(easy)-5(hard) derived from expected goals conceded by opponent
            ko=str(r.kickoff_time)[:16],
        ))

# custom fixture difficulty (1 easy .. 5 hard) from model expected goals
all_xg = [f['xg'] for c in TEAM_CODES for gw in team_fix[c] for f in team_fix[c][gw]]
all_xgc = [f['xgc'] for c in TEAM_CODES for gw in team_fix[c] for f in team_fix[c][gw]]
qa = np.quantile(all_xg, [.2, .4, .6, .8])
qd = np.quantile(all_xgc, [.2, .4, .6, .8])
for c in TEAM_CODES:
    for gw in team_fix[c]:
        for f in team_fix[c][gw]:
            f['adif'] = int(5 - np.searchsorted(qa, f['xg']))       # attacking difficulty
            f['ddif'] = int(1 + np.searchsorted(qd, f['xgc']))      # defensive difficulty

# ---------------------------------------------------------------- last season player aggregates
ps25_38 = ps25all.sort_values('gw').groupby('id').tail(1).set_index('id')
gw_count25 = ps25all.groupby('id').gw.nunique().to_dict()

pms25['dc'] = pms25['defensive_contributions'].fillna(0)
agg25 = pms25.groupby('player_id').agg(
    sq=('minutes_played', 'size'), mins=('minutes_played', 'sum'),
    xg=('xg', 'sum'), xa=('xa', 'sum'), xgot=('xgot', 'sum'),
    shots=('total_shots', 'sum'), sot=('shots_on_target', 'sum'),
    bcm=('big_chances_missed', 'sum'), cc=('chances_created', 'sum'),
    tob=('touches_opposition_box', 'sum'), touches=('touches', 'sum'),
    f3p=('final_third_passes', 'sum'), drib=('successful_dribbles', 'sum'),
    tkl=('tackles', 'sum'), intc=('interceptions', 'sum'), clr=('clearances', 'sum'),
    blk=('blocks', 'sum'), rec=('recoveries', 'sum'), dc=('dc', 'sum'),
    saves=('saves', 'sum'), gp=('goals_prevented', 'sum'),
    goals=('goals', 'sum'), assists=('assists', 'sum'),
    pens=('penalties_scored', 'sum'),
)
# appearances = matches with >0 minutes; official `starts` column is authoritative for starts
apps25 = pms25[pms25.minutes_played > 0].groupby('player_id').size()
# DC threshold hit-rate from real match logs
dc_hits = {}
for pid, g in pms25[pms25.minutes_played >= 55].groupby('player_id'):
    dc_hits[pid] = (g['dc'].values, len(g))

# ---------------------------------------------------------------- this season aggregates
if not pms26.empty:
    pms26['dc'] = pms26['defensive_contributions'].fillna(0)
    agg26 = pms26.groupby('player_id').agg(
        sq=('minutes_played', 'size'), mins=('minutes_played', 'sum'),
        xg=('xg', 'sum'), xa=('xa', 'sum'), xgot=('xgot', 'sum'),
        shots=('total_shots', 'sum'), sot=('shots_on_target', 'sum'),
        bcm=('big_chances_missed', 'sum'), cc=('chances_created', 'sum'),
        tob=('touches_opposition_box', 'sum'), touches=('touches', 'sum'),
        f3p=('final_third_passes', 'sum'), drib=('successful_dribbles', 'sum'),
        tkl=('tackles', 'sum'), intc=('interceptions', 'sum'), clr=('clearances', 'sum'),
        blk=('blocks', 'sum'), rec=('recoveries', 'sum'), dc=('dc', 'sum'),
        saves=('saves', 'sum'), gp=('goals_prevented', 'sum'),
        goals=('goals', 'sum'), assists=('assists', 'sum'),
        pens=('penalties_scored', 'sum'),
    )
    apps26 = pms26[pms26.minutes_played > 0].groupby('player_id').size()
else:
    agg26 = pd.DataFrame(columns=['sq', 'mins', 'xg', 'xa', 'xgot', 'shots', 'sot', 'bcm', 'cc', 'tob',
                                   'touches', 'f3p', 'drib', 'tkl', 'intc', 'clr', 'blk', 'rec', 'dc',
                                   'saves', 'gp', 'goals', 'assists', 'pens'])
    apps26 = pd.Series(dtype=int)

# link seasons via stable player_code
code25 = dict(zip(p25.player_code, p25.player_id))
team25 = dict(zip(p25.player_id, p25.team_code))

# ---------------------------------------------------------------- positional priors
# playerstats.csv is a per-gameweek snapshot history (one row per player per GW
# so far, cumulative totals as of that GW) rather than one row per player —
# keep only each player's most recent snapshot before joining onto players.
if 'gw' in pstats.columns:
    pstats = pstats.sort_values('gw').drop_duplicates('id', keep='last')
pstats = pstats.set_index('id')
players = players.set_index('player_id')
base = players.join(pstats[[c for c in pstats.columns if c not in ('web_name', 'first_name', 'second_name')]], how='left')
base['pos'] = base.position.map(POSMAP)

def hist(pid_now, pcode):
    pid25 = code25.get(pcode)
    h = {}
    if pid25 is not None and pid25 in agg25.index:
        h.update(agg25.loc[pid25].to_dict())
        h['apps'] = int(apps25.get(pid25, 0))
        h['avail_gw'] = gw_count25.get(pid25, 0)
        h['dclog'] = dc_hits.get(pid25, (np.array([]), 0))
        if pid25 in ps25_38.index:
            r = ps25_38.loc[pid25]
            h['starts'] = float(r.starts); h['subs'] = max(0.0, h['apps'] - float(r.starts))
            h['mins_off'] = float(r.minutes)
            h['pts'] = float(r.total_points); h['bonus'] = float(r.bonus); h['bps'] = float(r.bps)
            h['cs'] = float(r.clean_sheets); h['gc'] = float(r.goals_conceded)
            h['yc'] = float(r.yellow_cards); h['rc'] = float(r.red_cards)
            h['og'] = float(r.own_goals); h['ppg'] = float(r.points_per_game)
            h['team25'] = int(team25.get(pid25, 0))
    return h

def cur(pid):
    c = {}
    if pid in agg26.index:
        c.update(agg26.loc[pid].to_dict())
        c['apps'] = int(apps26.get(pid, 0))
    return c

# positional rate priors (per 90) from last season, minutes-weighted
prior = {}
for pos in (1, 2, 3, 4):
    ids = [pid25 for pcode, pid25 in code25.items() if pid25 in agg25.index]
    pos_ids = [i for i in ids if p25.set_index('player_id').position.get(i) == {v: k for k, v in POSMAP.items()}[pos]]
    sub = agg25.loc[[i for i in pos_ids if i in agg25.index]]
    sub = sub[sub.mins >= 450]
    tm = sub.mins.sum() / 90.0
    prior[pos] = {k: float(sub[k].sum() / tm) if tm > 0 else 0.0
                  for k in ['xg', 'xa', 'shots', 'sot', 'cc', 'tob', 'dc', 'saves', 'goals', 'assists']}
    prior[pos]['n'] = len(sub)
print('positional per-90 priors:', {POSNAME[k]: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in prior.items()})

# bonus / cards priors from last season season-totals
bp = ps25_38[ps25_38.minutes >= 450]
prior_bonus90 = float(bp.bonus.sum() / (bp.minutes.sum() / 90))
prior_yc90 = float(bp.yellow_cards.sum() / (bp.minutes.sum() / 90))

W_PREV = 0.75      # last season minute weight for rate estimation
W_CUR_GW = 3.0     # recency weight per current-season gameweek
K_GW = 2.5         # prior strength in gameweeks for minutes model
K90 = 7.0          # prior strength in 90s for rate shrinkage

def blend_rate(cur_sum, prev_sum, cur_min, prev_min, prior90, k=K90):
    s = cur_sum + W_PREV * prev_sum
    n90 = (cur_min + W_PREV * prev_min) / 90.0
    return (s + k * prior90) / (n90 + k)

rows = []
for pid, r in base.iterrows():
    pcode = int(r.player_code)
    pos = int(r.pos)
    h = hist(pid, pcode)
    c = cur(pid)
    price = float(r.now_cost)
    tcode = int(r.team_code)

    cm = float(r.minutes or 0)                       # official FPL minutes, this season
    pm = float(h.get('mins_off', h.get('mins', 0)))  # official FPL minutes, last season
    cstarts, pstarts = float(r.starts or 0), float(h.get('starts', 0))
    capps, papps = float(c.get('apps', 0)), float(h.get('apps', 0))
    csubs, psubs = max(0.0, capps - cstarts), max(0.0, papps - pstarts)
    prev_gw = float(h.get('avail_gw', 0))

    # ---- availability
    status = str(r.status) if pd.notna(r.status) else 'a'
    cop = r.chance_of_playing_next_round
    if pd.notna(cop):
        avail = float(cop) / 100.0
    elif status == 'a':
        avail = 1.0
    elif status == 'd':
        avail = 0.5
    else:
        avail = 0.0

    # ---- minutes model -------------------------------------------------
    # Beta prior on "starts next match". Prior mean mixes price (a proxy for squad
    # status) with last season's start rate; observations are this season's starts.
    price_prior = float(np.clip(0.08 + (price - 4.0) * 0.115, 0.08, 0.92))
    if prev_gw >= 8:
        hist_start = float(np.clip(pstarts / max(prev_gw, 1), 0, 1))
        m0 = 0.55 * price_prior + 0.45 * hist_start
        hist_sub = float(np.clip(psubs / max(prev_gw, 1), 0, 1))
    else:                       # new arrival / no Premier League history
        m0 = price_prior
        hist_sub = 0.22
    K0 = 2.5                    # prior worth ~2.5 gameweeks; fades as the season runs
    p_start = float(np.clip((cstarts + K0 * m0) / (GWS_PLAYED + K0), 0.01, 0.97))
    sub_rate = float(np.clip((csubs + K0 * hist_sub) / (GWS_PLAYED + K0), 0, 0.9))
    p_sub_given_nostart = float(np.clip(sub_rate / max(1e-6, 1 - p_start), 0.0, 0.85))

    # minutes in a match he starts, shrunk toward 78
    cur_start_mins = max(0.0, cm - 20 * csubs)
    prev_start_mins = max(0.0, pm - 20 * psubs)
    W_REC = 4.0
    mins_if_start = (W_REC * cur_start_mins + prev_start_mins + 4 * 78) / \
                    (W_REC * cstarts + pstarts + 4)
    mins_if_start = float(np.clip(mins_if_start, 50, 90))

    xmins = avail * (p_start * mins_if_start + (1 - p_start) * p_sub_given_nostart * 21.0)
    p_play = avail * (p_start + (1 - p_start) * p_sub_given_nostart)
    p60_if_start = 0.94 if mins_if_start >= 80 else (0.85 if mins_if_start >= 70 else 0.60)
    p60 = avail * (p_start * p60_if_start + (1 - p_start) * p_sub_given_nostart * 0.09)

    # ---- attacking rates per 90
    xg90 = blend_rate(c.get('xg', 0), h.get('xg', 0), cm, pm, prior[pos]['xg'])
    xa90 = blend_rate(c.get('xa', 0), h.get('xa', 0), cm, pm, prior[pos]['xa'])
    g90 = blend_rate(c.get('goals', 0), h.get('goals', 0), cm, pm, prior[pos]['goals'])
    a90 = blend_rate(c.get('assists', 0), h.get('assists', 0), cm, pm, prior[pos]['assists'])
    sh90 = blend_rate(c.get('shots', 0), h.get('shots', 0), cm, pm, prior[pos]['shots'])
    sot90 = blend_rate(c.get('sot', 0), h.get('sot', 0), cm, pm, prior[pos]['sot'])
    cc90 = blend_rate(c.get('cc', 0), h.get('cc', 0), cm, pm, prior[pos]['cc'])
    tob90 = blend_rate(c.get('tob', 0), h.get('tob', 0), cm, pm, prior[pos]['tob'])
    dc90 = blend_rate(c.get('dc', 0), h.get('dc', 0), cm, pm, prior[pos]['dc'])
    sv90 = blend_rate(c.get('saves', 0), h.get('saves', 0), cm, pm, prior[pos]['saves'])
    bonus90 = blend_rate(float(r.bonus or 0), h.get('bonus', 0), cm, pm, prior_bonus90, k=8)
    yc90 = blend_rate(float(r.yellow_cards or 0), h.get('yc', 0), cm, pm, prior_yc90, k=8)

    # goal / assist expectation: mostly underlying, partly finished output
    ge90 = 0.75 * xg90 + 0.25 * g90
    ae90 = 0.70 * xa90 + 0.30 * a90

    # penalty bump: on-pens players get their team's penalty share
    pen_order = r.penalties_order
    on_pens = int(pen_order) if pd.notna(pen_order) else 0

    # ---- defensive contribution hit rate (real match logs, shrunk)
    thr = DC_THRESH[pos]
    if pos == 1:
        dc_rate = 0.0
    else:
        pois = float(np.clip(1 - _poisson_cdf(thr - 1, dc90) if dc90 > 0 else 0, 0, 0.95))
        dclog, ndc = h.get('dclog', (np.array([]), 0))
        if ndc >= 5:
            hit = float((np.asarray(dclog) >= thr).mean())
            dc_rate = (hit * ndc + 5 * pois) / (ndc + 5)   # observed hit-rate shrunk to Poisson
        else:
            dc_rate = pois

    rows.append(dict(
        id=int(pid), code=pcode, name=str(r.web_name),
        full=f"{r.first_name} {r.second_name}".strip(),
        team=tcode, pos=pos, price=price,
        status=status, news=str(r.news) if pd.notna(r.news) else '',
        chance=float(cop) if pd.notna(cop) else None,
        sel=float(r.selected_by_percent or 0), form=float(r.form or 0),
        tin=int(r.transfers_in_event or 0), tout=int(r.transfers_out_event or 0),
        tin_tot=int(r.transfers_in or 0), tout_tot=int(r.transfers_out or 0),
        cost_change=float(r.cost_change_start or 0),
        # ---- current season totals (official FPL)
        pts=float(r.total_points or 0), ppg=float(r.points_per_game or 0),
        mins=float(r.minutes or 0), starts=float(r.starts or 0),
        g=float(r.goals_scored or 0), a=float(r.assists or 0),
        cs=float(r.clean_sheets or 0), gc=float(r.goals_conceded or 0),
        bonus=float(r.bonus or 0), bps=float(r.bps or 0),
        yc=float(r.yellow_cards or 0), rc=float(r.red_cards or 0),
        og=float(r.own_goals or 0), saves=float(r.saves or 0),
        pen_miss=float(r.penalties_missed or 0), pen_saved=float(r.penalties_saved or 0),
        xg=float(r.expected_goals or 0), xa=float(r.expected_assists or 0),
        xgi=float(r.expected_goal_involvements or 0), xgc=float(r.expected_goals_conceded or 0),
        ict=float(r.ict_index or 0), infl=float(r.influence or 0),
        creat=float(r.creativity or 0), threat=float(r.threat or 0),
        dc=float(r.defensive_contribution or 0),
        pen_order=on_pens,
        ck_order=int(r.corners_and_indirect_freekicks_order) if pd.notna(r.corners_and_indirect_freekicks_order) else 0,
        fk_order=int(r.direct_freekicks_order) if pd.notna(r.direct_freekicks_order) else 0,
        # ---- last season totals
        h_pts=h.get('pts', 0), h_mins=pm, h_starts=pstarts, h_g=h.get('goals', 0), h_a=h.get('assists', 0),
        h_xg=round(float(h.get('xg', 0)), 2), h_xa=round(float(h.get('xa', 0)), 2),
        h_cs=h.get('cs', 0), h_bonus=h.get('bonus', 0), h_bps=h.get('bps', 0),
        h_shots=h.get('shots', 0), h_sot=h.get('sot', 0), h_cc=h.get('cc', 0),
        h_tob=h.get('tob', 0), h_bcm=h.get('bcm', 0), h_dc=h.get('dc', 0),
        h_team=h.get('team25', 0),
        # ---- current-season Opta
        c_shots=float(c.get('shots', 0)), c_sot=float(c.get('sot', 0)), c_cc=float(c.get('cc', 0)),
        c_tob=float(c.get('tob', 0)), c_bcm=float(c.get('bcm', 0)), c_dc=float(c.get('dc', 0)),
        c_xgot=float(c.get('xgot', 0)), c_drib=float(c.get('drib', 0)),
        # ---- model
        xmins=round(xmins, 1), p_start=round(p_start, 3), p_play=round(p_play, 3), p60=round(p60, 3),
        mstart=round(mins_if_start, 1), psub=round(p_sub_given_nostart, 3),
        p60s=round(p60_if_start, 3), m0=round(m0, 3),
        h_apps=papps, c_apps=capps, prev_gw=prev_gw,
        xg90=round(xg90, 3), xa90=round(xa90, 3), ge90=round(ge90, 3), ae90=round(ae90, 3),
        sh90=round(sh90, 2), sot90=round(sot90, 2), cc90=round(cc90, 2), tob90=round(tob90, 2),
        dc90=round(dc90, 2), sv90=round(sv90, 2), bonus90=round(bonus90, 3), yc90=round(yc90, 3),
        dc_rate=round(float(dc_rate), 3), avail=round(avail, 2),
    ))

df = pd.DataFrame(rows)
print(f'players built: {len(df)}')

# ---------------------------------------------------------------- squad-level minutes constraint
# A team fields exactly 11 starters and ~3.6 substitutes per match, i.e. 990 minutes.
# Raw per-player estimates never sum to that, so fit them proportionally inside each squad.
def ipf(vals, target, cap):
    v = np.asarray(vals, dtype=float)
    for _ in range(60):
        s = v.sum()
        if s <= 1e-9:
            break
        v = np.clip(v * (target / s), 0, cap)
        if abs(v.sum() - target) < 1e-6:
            break
    return v

df['ps_eff'] = df.p_start * df.avail
df['sub_eff'] = (1 - df.p_start) * df.psub * df.avail
for tc, idx in df.groupby('team').groups.items():
    i = list(idx)
    df.loc[i, 'ps_eff'] = ipf(df.loc[i, 'ps_eff'].values, 11.0, 0.97)
    df.loc[i, 'sub_eff'] = ipf(df.loc[i, 'sub_eff'].values, 3.6, 0.90)
    xm = df.loc[i, 'ps_eff'].values * df.loc[i, 'mstart'].values + df.loc[i, 'sub_eff'].values * 21.0
    tot = xm.sum()
    if tot > 0:
        xm = np.minimum(xm * (990.0 / tot), 90.0)
    df.loc[i, 'xmins'] = np.round(xm, 1)

df['p_start'] = df.ps_eff.round(3)
df['p_play'] = (df.ps_eff + df.sub_eff).clip(0, 1).round(3)
df['p60'] = (df.ps_eff * df.p60s + df.sub_eff * 0.09).clip(0, 1).round(3)
df = df.drop(columns=['ps_eff', 'sub_eff'])
print(f'minutes check: total xMins {df.xmins.sum():.0f} (target {20 * 990}), '
      f'starters p_start>0.7: {(df.p_start > 0.7).sum()}')

# ---------------------------------------------------------------- expected points model
def gc_penalty(lam):
    """expected FPL deduction (-1 per 2 goals conceded) for a full match."""
    return -sum(_poisson_pmf(k, lam) * (k // 2) for k in range(0, 12))

GW_RANGE = list(range(NEXT_GW, 39))

xp_matrix, comp_next = {}, {}
for r in df.itertuples():
    tc = int(r.team)
    pos = int(r.pos)
    mf = r.xmins / 90.0
    avg_xg, avg_xgc = TEAM_AVG_XG[tc], TEAM_AVG_XGC[tc]
    per_gw = []
    for gw in GW_RANGE:
        fl = team_fix[tc].get(gw, [])
        tot = 0.0
        comps = None
        for f in fl:
            att_scale = f['xg'] / avg_xg
            xgc = f['xgc']
            g = r.ge90 * mf * att_scale
            a = r.ae90 * mf * att_scale
            p_att = g * GOAL_PTS[pos] + a * 3.0
            p_cs = r.p60 * math.exp(-xgc) * CS_PTS[pos]
            p_gc = gc_penalty(xgc) * mf if pos <= 2 else 0.0
            p_sv = (r.sv90 * mf * (xgc / max(avg_xgc, .01)) / 3.0) if pos == 1 else 0.0
            p_dc = r.dc_rate * r.p_start * r.avail * 2.0
            p_bon = r.bonus90 * mf * (0.9 + 0.2 * att_scale)
            p_app = r.p_play + r.p60
            p_card = -r.yc90 * mf
            s = p_att + p_cs + p_gc + p_sv + p_dc + p_bon + p_app + p_card
            tot += s
            if comps is None:
                comps = dict(app=p_app, att=p_att, g=g, a=a, cs=p_cs, gc=p_gc,
                             sv=p_sv, dc=p_dc, bon=p_bon, card=p_card, xgc=xgc, ascale=att_scale)
        per_gw.append(round(tot, 2))
        if gw == NEXT_GW:
            comp_next[r.id] = {k: round(v, 3) for k, v in (comps or {}).items()}
    xp_matrix[r.id] = per_gw

df['xp'] = df.id.map(lambda i: xp_matrix[i])
df['xp_next'] = df.xp.map(lambda v: v[0])
df['xp3'] = df.xp.map(lambda v: round(sum(v[:3]), 2))
df['xp6'] = df.xp.map(lambda v: round(sum(v[:6]), 2))
df['comp'] = df.id.map(lambda i: comp_next.get(i, {}))
df['val6'] = (df.xp6 / df.price).round(3)

# ---------------------------------------------------------------- team payload
team_payload = []
for _, t in teams.iterrows():
    c = int(t.code)
    o25, o26 = team_obs[c]['s25'], team_obs[c]['s26']
    team_payload.append(dict(
        code=c, id=int(t.id), name=str(t['name']), short=str(t.short_name),
        elo=int(round(elo.get(c, fallback_elo))), att=round(ATT[c], 3), dfn=round(DEF[c], 3),
        axg=round(TEAM_AVG_XG[c], 3), axgc=round(TEAM_AVG_XGC[c], 3),
        h25={k: (round(v, 2) if isinstance(v, float) else v) for k, v in o25.items()},
        h26={k: (round(v, 2) if isinstance(v, float) else v) for k, v in o26.items()},
        fix={str(g): team_fix[c].get(g, []) for g in range(NEXT_GW, 39)},
    ))

gw_payload = [dict(id=int(g.id), name=str(g.name), deadline=str(g.deadline_time),
                   finished=bool(g.finished), avg=int(g.average_entry_score or 0))
              for g in gwsum.itertuples()]

pl = df.drop(columns=['comp']).to_dict('records')
for p, r in zip(pl, df.itertuples()):
    p['comp'] = r.comp

payload = dict(
    meta=dict(
        generated=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        last_gw=int(LAST_GW), next_gw=int(NEXT_GW),
        next_deadline=str(gwsum[gwsum.id == NEXT_GW].deadline_time.iloc[0]) if NEXT_GW <= 38 else '',
        season=SEASON_LABEL,
        mu_home=round(mu_H, 3), mu_away=round(mu_A, 3),
        last_gw_total_points=int(pstats.event_points.sum()),
        xp_next_total=round(float(df.xp_next.sum()), 1),
        sources=['FPL official API (via FPL-Elo-Insights mirror)', 'Opta-style match stats (FPL-Elo-Insights)',
                 'ClubElo team ratings', 'vaastav/Fantasy-Premier-League fixture list + FDR'],
    ),
    gws=gw_payload, teams=team_payload, players=pl, gw_range=GW_RANGE,
)

def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if o is None or isinstance(o, (str, int)):
        return o
    return str(o)

payload = clean(payload)
with open(OUT, 'w') as fh:
    json.dump(payload, fh, separators=(',', ':'), allow_nan=False)
print('wrote', OUT, os.path.getsize(OUT) // 1024, 'KB')

# ---------------------------------------------------------------- sanity report
top = df.nlargest(15, 'xp_next')[['name', 'team', 'pos', 'price', 'xmins', 'xp_next', 'xp6', 'sel']]
top['team'] = top.team.map(code2short); top['pos'] = top.pos.map(POSNAME)
print('\nTop expected points next GW:\n', top.to_string(index=False))
tt = pd.DataFrame(team_payload)[['short', 'elo', 'att', 'dfn', 'axg', 'axgc']].sort_values('att', ascending=False)
print('\nTeam ratings:\n', tt.to_string(index=False))
