"""Prepare the 3D rack explorer's saved replays at distinct recorded cursors."""
from datetime import timedelta
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'databricks' / 'backend'))
from replay_bridge import SavedReplay, _iso

source = SavedReplay()
gpu_keys = ('id', 'state', 'telemetry_is_observed', 'telemetry_ts', 'metrics', 'workload', 'cooling', 'limits')
device_ids = {gpu['gpu_id'] for gpu in source.inventory}


def prepare_replay(run, default_offset, event_offset):
    start, end = source.intervals[run]
    duration = int((end - start).total_seconds())
    offsets = sorted(set(range(0, duration, 30)) | {default_offset, event_offset, duration})
    frames = []
    for offset in offsets:
        cursor = _iso(start + timedelta(seconds=offset))
        snapshot = source.request('snapshot', [run, cursor])['data']
        findings = {}
        for incident in snapshot['incidents']:
            for finding in incident['findings']:
                if finding['entity'] not in device_ids or finding['detector'] == 'telemetry':
                    continue
                previous = findings.get(finding['entity'])
                if previous is None or finding['severity'] == 'critical':
                    findings[finding['entity']] = {'text': finding['text'], 'metric': finding['metric']}
        frames.append({
            'offset': offset,
            'time': cursor,
            'devices': {
                gpu['id']: {key: gpu[key] for key in gpu_keys}
                for rack in snapshot['racks']
                for gpu in rack['gpus']
            },
            'overview': snapshot['overview'],
            'gpuFindings': findings,
            'incidents': [{
                'severity': item['severity'],
                'root': item['rootEntity'],
                'gpus': item['affectedGpuIds'],
                'evidence': item['findings'][0]['text'] if item['findings'] else '',
            } for item in snapshot['incidents']],
        })
    return {'run': run, 'frames': frames, 'defaultOffset': default_offset, 'eventOffset': event_offset}


document = {
    'source': 'saved_databricks_replay',
    'synthetic': True,
    'exportedAt': source.exported_at,
    'inventory': sorted(source.inventory, key=lambda gpu: gpu['gpu_id']),
    'replays': {
        'cooling': prepare_replay('43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c', 600, 645),
        'single': prepare_replay('522f5945-1c4b-5bbd-afe4-807ae90cdd84', 420, 420),
    },
}
destination = root / 'public' / 'assets' / 'rack-replay.json'
destination.write_text(json.dumps(document, allow_nan=False, separators=(',', ':')) + '\n')
print(f'{len(document["replays"])} replays · {len(document["inventory"])} GPUs · {destination.stat().st_size} bytes')
