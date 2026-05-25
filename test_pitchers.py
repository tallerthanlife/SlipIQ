from slipiq_lines import get_mlb_pitcher_props
from slipiq_pitcher_model import run_pitcher_model, get_recommendation

props = get_mlb_pitcher_props()
seen = set()

for p in props:
    if p['pitcher'] not in seen and p['direction'] == 'Over':
        seen.add(p['pitcher'])
        proj = run_pitcher_model(p['pitcher'], line=p['line'], verbose=False)
        if proj:
            rec = get_recommendation(proj, p['line'])
            print(f"{p['pitcher']}: proj={proj['projection']} line={p['line']} conf={proj['confidence']}% -> {rec[:40]}")
        else:
            print(f"{p['pitcher']}: NO PROJECTION")