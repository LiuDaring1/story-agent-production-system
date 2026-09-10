"""Prepare one scoped, hash-bound independent review packet before any model call."""
import hashlib
import json
from pathlib import Path
from story_production_v2 import binding, current, write, validate_independent_approval, protect_outputs


def prepare_review(*, project_id, items, rules, output, previous=None, repairs=()):
    if not project_id or not items or not rules:
        raise ValueError('Project, review items and applicable rule sources are required')
    if len({i['scope'] for i in items}) != len(items):
        raise ValueError('Review scopes must be unique')
    protected = [r for r in rules] + [i['path'] for i in items]
    protected += [p for i in items for p in i.get('dependencies', [])]
    protect_outputs([output], protected + ([previous] if previous else []))
    rule_bindings = [binding(p) for p in rules]
    rows = []
    for item in items:
        row = {'scope': item['scope'], 'artifact': binding(item['path']),
               'dependencies': [binding(p) for p in item.get('dependencies', [])], 'rules': rule_bindings,
               'parameters': item.get('parameters', {})}
        row['fingerprint'] = hashlib.sha256(json.dumps({'project_id': project_id, **row}, sort_keys=True).encode()).hexdigest()
        definition_dir = Path(output).parent / 'review_scope_definitions'
        definition = definition_dir / (row['fingerprint'] + '.json')
        if definition.exists():
            if json.loads(definition.read_text()) != row:
                raise ValueError('Immutable review scope definition changed')
        else:
            write(definition, row)
        row['scope_definition'] = binding(definition)
        rows.append(row)
    repaired_scopes = set()
    repair_rows = []
    fields = ('scope', 'defect_code', 'requirement_source', 'requirement_scope', 'evidence', 'delivery_impact', 'retry_strategy', 'root_cause')
    for repair in repairs:
        missing = [k for k in fields if not str(repair.get(k) or '').strip()]
        if missing: raise ValueError('Incomplete repair checklist: ' + ', '.join(missing))
        row = next((r for r in rows if r['scope'] == repair['scope']), None)
        if row is None: raise ValueError('Repair scope is absent from review items')
        if str(Path(repair['requirement_source']).resolve()) not in {r['path'] for r in rule_bindings}:
            raise ValueError('Repair must cite an applicable bound rule source')
        repaired_scopes.add(repair['scope'])
        repair_rows.append({**repair, 'artifact_sha256': row['artifact']['sha256']})
    old_rows = {}
    if previous:
        prior = json.loads(Path(previous).read_text())
        if prior.get('project_id') == project_id:
            for row in prior.get('items', []):
                # Only explicitly independently approved scopes can be reused.
                approval = row.get('approval')
                if not approval: continue
                try:
                    definition = json.loads(current(row['scope_definition']).read_text())
                    if definition != {k:v for k,v in row.items() if k not in {'scope_definition', 'approval', 'review_action'}}:
                        continue
                    review = json.loads(current(approval['review']).read_text())
                    bundle = current(approval['bundle'])
                    validate_independent_approval(review, bundle, producer_context=approval['producer_context'])
                    from story_evidence import review_bundle_is_current
                    if not review_bundle_is_current(bundle): continue
                    members = json.loads(bundle.read_text())['artifacts']
                    if isinstance(members, dict): members = members.values()
                    hashes = {(str(Path(x['path']).resolve()), x['sha256']) for x in members}
                    if any((x['path'], x['sha256']) not in hashes for x in [row['artifact'], row['scope_definition'], *row['dependencies'], *row['rules']]): continue
                    old_rows[row['scope']] = row
                except (ValueError, KeyError, OSError):
                    continue
    for row in rows:
        old = old_rows.get(row['scope'])
        reusable = old and old.get('fingerprint') == row['fingerprint'] and row['scope'] not in repaired_scopes
        row['review_action'] = 'reuse_evidence' if reusable else 'review_current_artifact'
        if reusable: row['approval'] = old['approval']
    payload = {'schema_version': 'story-review-preparation/v1', 'project_id': project_id,
               'items': rows, 'repairs': repair_rows,
               'repair_groups': {code: [r['scope'] for r in repair_rows if r['defect_code'] == code] for code in sorted({r['defect_code'] for r in repair_rows})},
               'independent_review_required': True, 'generation_result_action_review_required': True,
               'reviewer_instruction': 'Open the actual listed media and bound rule sources. Producer summaries do not replace visual evidence.'}
    # Preparation reuse itself does not require the packet to claim QA approval.
    if Path(output).exists() and json.loads(Path(output).read_text()) == payload:
        return {**payload, 'preparation_reused': True}
    write(output, payload)
    return {**payload, 'preparation_reused': False}


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--request', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--run-file', type=Path)
    args = p.parse_args()
    protect_outputs([args.output], [args.request])
    request = json.loads(args.request.read_text())
    if args.run_file:
        from story_work_observation import operation_observation
        with operation_observation(args.run_file, 'prepare-review', 'review', artifacts=[args.output]) as fact:
            result = prepare_review(**request, output=args.output)
            if result['preparation_reused']: fact['execution_mode'] = 'resume_reuse'
    else:
        result = prepare_review(**request, output=args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
