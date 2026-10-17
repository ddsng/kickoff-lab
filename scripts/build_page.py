#!/usr/bin/env python3
"""
Kickoff Lab — page builder.

Wraps site/template.html (a head-less fragment: <title>, styles, markup,
script — no <html>/<head>/<body> tags of its own, since it doubles as an
Artifact source) in a real HTML5 document, injects the built dataset in
place of the __DATA__ placeholder, and writes the publishable dist/index.html
that GitHub Pages serves.

If --alt-data is given (a second dataset built with build_data.py --no-prev),
its model outputs are embedded alongside the main ones so the page can switch
between "with last season" and "this season only" without reloading.

Usage:
    python scripts/build_page.py [--template site/template.html]
                                  [--data dist/fpl_data.json]
                                  [--alt-data dist/fpl_data_np.json]
                                  [--out dist/index.html]
"""
import argparse
import json
import os
import re
import shutil


# Model outputs that differ between the two builds and that the page reads.
PLAYER_MODEL_FIELDS = ['xmins', 'p_start', 'p_play', 'p60', 'mstart', 'psub', 'p60s',
                       'xg90', 'xa90', 'ge90', 'ae90', 'sh90', 'sot90', 'cc90', 'tob90',
                       'dc90', 'sv90', 'bonus90', 'yc90', 'dc_rate']
TEAM_MODEL_FIELDS = ['att', 'dfn', 'axg', 'axgc', 'fix']
META_MODEL_FIELDS = ['mu_home', 'mu_away']


def merge_alt(main_json: str, alt_json: str) -> str:
    """Attach the this-season-only model to the main dataset as `np` sub-objects."""
    d, a = json.loads(main_json), json.loads(alt_json)
    alt_p = {p['id']: p for p in a['players']}
    for p in d['players']:
        q = alt_p.get(p['id'])
        if q:
            p['np'] = {k: q[k] for k in PLAYER_MODEL_FIELDS if k in q}
    alt_t = {t['code']: t for t in a['teams']}
    for t in d['teams']:
        u = alt_t.get(t['code'])
        if u:
            t['np'] = {k: u[k] for k in TEAM_MODEL_FIELDS if k in u}
    d['meta']['np'] = {k: a['meta'][k] for k in META_MODEL_FIELDS if k in a['meta']}
    d['meta']['has_np'] = True
    return json.dumps(d, separators=(',', ':'), allow_nan=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--template', default='site/template.html')
    ap.add_argument('--data', default='dist/fpl_data.json')
    ap.add_argument('--out', default='dist/index.html')
    ap.add_argument('--alt-data', default=None,
                    help='dataset built with build_data.py --no-prev (optional)')
    args = ap.parse_args()

    tpl = open(args.template, encoding='utf-8').read()
    data = open(args.data, encoding='utf-8').read()
    if args.alt_data and os.path.exists(args.alt_data):
        data = merge_alt(data, open(args.alt_data, encoding='utf-8').read())
        print(f'embedded this-season-only model from {args.alt_data}')
    assert '</script' not in data.lower(), 'data would break out of the script tag'

    body = tpl.replace('__DATA__', data)

    # Pull the leading <title>...</title> out of the fragment so it lands in
    # <head> where browsers/crawlers expect it; everything else (styles,
    # markup, script) stays in <body> exactly as authored.
    m = re.match(r'\s*(<title>.*?</title>)\s*', body, flags=re.IGNORECASE | re.DOTALL)
    title_tag = m.group(1) if m else '<title>Kickoff Lab</title>'
    if m:
        body = body[m.end():]

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{title_tag}
<meta name="description" content="Fantasy Premier League squad planner and analytics — expected points, fixtures, and team ratings, refreshed automatically.">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
</head>
<body>
{body}
</body>
</html>
"""

    out_dir = os.path.dirname(args.out) or '.'
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as fh:
        fh.write(doc)
    print(f'wrote {args.out} ({len(doc) // 1024} KB)')

    # Static assets referenced by the page (currently just the favicon) live
    # next to the template and get copied alongside index.html as-is.
    tpl_dir = os.path.dirname(args.template) or '.'
    favicon_src = os.path.join(tpl_dir, 'favicon.svg')
    if os.path.exists(favicon_src):
        shutil.copy(favicon_src, os.path.join(out_dir, 'favicon.svg'))
        print(f'copied {favicon_src} -> {out_dir}/favicon.svg')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
