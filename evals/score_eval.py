"""Compute preference-adherence counts from preserved independent blind judgments."""
import argparse
import json
from pathlib import Path


def score(directory):
    root = Path(directory)
    cases = {c['id']: c for c in json.loads((root / 'cases.json').read_text(encoding='utf-8'))}
    mapping = json.loads((root / 'blind-map.json').read_text(encoding='utf-8'))
    grades = json.loads((root / 'blind-grades.json').read_text(encoding='utf-8'))
    if len(grades) != len(cases) or {g['id'] for g in grades} != set(cases):
        raise ValueError('Missing or duplicate case grades.')
    rows = []
    for grade in grades:
        cid = grade['id']
        row = dict(id=cid, mode=cases[cid]['mode'], domain=cases[cid]['domain'])
        for label in ('A', 'B'):
            value = grade[label]
            if value['status'] not in ('met', 'violated', 'uncertain') or not value['evidence'].strip():
                raise ValueError('Invalid grade or missing evidence: ' + cid)
            row[mapping[cid][label]] = value
        rows.append(row)
    def summary(items):
        return {group: {status: sum(r[group]['status'] == status for r in items)
                        for status in ('met','violated','uncertain')} for group in ('baseline','treatment')}
    return dict(cases=len(rows), total=summary(rows),
                by_mode={mode: summary([r for r in rows if r['mode']==mode]) for mode in ('same_chat','new_chat')},
                by_domain={domain: summary([r for r in rows if r['domain']==domain]) for domain in ('writing','learning','office','coding')},
                regression_cases=[r['id'] for r in rows if r['baseline']['status']=='met' and r['treatment']['status']!='met'],
                rows=rows, interpretation='Small simulated preference-retention experiment; not real user correction counts or general accuracy.')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',default=str(Path(__file__).parent))
    args=parser.parse_args()
    result=score(args.directory)
    (Path(args.directory)/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=False,indent=2))
