"""Conservative pre-generation consumer plan; no generator or new scheduler."""
import json
from pathlib import Path
from story_production_v2 import binding, current, write


def asset_generation_plan(director):
    assets = {a['asset_id']: a for a in director['assets']}
    consumers = {key: [] for key in assets}
    for shot in director['shots']:
        for key in shot.get('reference_asset_ids', []):
            if key in consumers: consumers[key].append('shot:' + shot['shot_id'])
    for asset in assets.values():
        key = asset.get('derived_from_asset_id')
        if key in consumers: consumers[key].append('derive:' + asset['asset_id'])
    for group in director.get('continuity_groups', []):
        master_id = group.get('environment_asset_id')
        if master_id in consumers: consumers[master_id].append('continuity:' + str(group.get('group_id', 'group')))
        for setup in group.get('camera_setups', []):
            key = setup.get('environment_view_asset_id')
            if key in consumers: consumers[key].append('camera:' + setup['setup_id'])
    aliases = {}
    for group in director.get('continuity_groups', []):
        setups = group.get('camera_setups', [])
        if group.get('asset_strategy') != 'single_view_reuse_master': continue
        if group.get('spatial_complexity') != 'simple' or len(setups) != 1:
            raise ValueError('Master-pixel reuse requires an explicitly simple single-view group; use full references')
        master = assets[group['environment_asset_id']]
        setup = setups[0]
        view = assets[setup['environment_view_asset_id']]
        fields = {'view_from_zone_id': 'camera_origin_zone_id', 'view_target_zone_id': 'look_target_zone_id',
                  'view_background_zone_ids': 'background_zone_ids'}
        # Exact camera facts must be supplied before generation, not guessed from filenames.
        contract = master.get('camera_contract', {})
        if master.get('contains_characters') or view.get('contains_characters'):
            raise ValueError('Environment alias requires empty scenes')
        if view.get('derived_from_asset_id') != master['asset_id']:
            raise ValueError('Environment alias requires the declared master')
        if not contract or any(contract.get(k) != setup.get(v) or view.get(k) != setup.get(v) for k, v in fields.items()):
            raise ValueError('Master camera differs; keep the separate camera view')
        if contract.get('camera_angle') != setup.get('camera_angle') or contract.get('shot_size') != setup.get('shot_size'):
            raise ValueError('Master crop/angle differs; keep the separate camera view')
        aliases[view['asset_id']] = master['asset_id']
    rows = []
    for key, asset in assets.items():
        uses = sorted(set(consumers[key]))
        # Declared continuity-only references remain necessary even with no direct runtime consumer.
        constraints = asset.get('continuity_constraints', [])
        operation = 'reuse_master_pixels' if key in aliases else ('generate' if uses or constraints else 'omit_unconsumed')
        rows.append({'asset_id': key, 'consumers': uses, 'continuity_constraints': constraints,
                     'operation': operation, 'source_asset_id': aliases.get(key)})
    return {'schema_version': 'story-asset-generation-plan/v1', 'story_id': director['story_id'],
            'assets': rows, 'unique_generations': sum(r['operation'] == 'generate' for r in rows),
            'separate_view_generations_avoided': len(aliases),
            'fallback': 'Separate master and camera-view generation for unproven or complex geometry'}


def validate_aliases(director):
    plan = asset_generation_plan(director)
    assets = {a['asset_id']: a for a in director['assets']}
    for row in plan['assets']:
        if row['operation'] == 'reuse_master_pixels':
            a, source = assets[row['asset_id']], assets[row['source_asset_id']]
            if binding(a['path'])['sha256'] != source['sha256'] or a['sha256'] != source['sha256']:
                raise ValueError('Declared environment reuse must bind unchanged master pixels')
    return plan


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--director-plan', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--run-file', type=Path)
    args = p.parse_args()
    from story_production_v2 import protect_outputs
    protect_outputs([args.output], [args.director_plan])
    result = asset_generation_plan(json.loads(args.director_plan.read_text()))
    result['director'] = binding(args.director_plan)
    if args.run_file:
        from story_work_observation import operation_observation
        with operation_observation(args.run_file, 'asset-plan', 'deterministic', artifacts=[args.output]):
            write(args.output, result)
    else:
        write(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
