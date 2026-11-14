import assert from 'node:assert/strict';
import test from 'node:test';
import { FlaggingError, flaggingRequest } from '../scripts/flagging-api.mjs';

const params = values => new URLSearchParams(values);

test('flagging API rejects malformed IDs before starting Python', async () => {
  for (const scenario of ['', '../topology', 'a b', 'x;rm -rf', '-flag']) {
    await assert.rejects(flaggingRequest('/api/flagging/analysis', params({ scenario })),
      problem => problem instanceof FlaggingError && problem.status === 400);
  }
  await assert.rejects(flaggingRequest('/api/flagging/entity', params({ scenario: 'F01-gpu-thermal-throttle', entity: '../../etc' })),
    problem => problem instanceof FlaggingError && problem.status === 400);
  await assert.rejects(flaggingRequest('/api/flagging/other', params({})),
    problem => problem instanceof FlaggingError && problem.status === 404);
});

test('flagging API replays a labelled fault through the Python flagger', { timeout: 120_000 }, async () => {
  const analysis = await flaggingRequest('/api/flagging/analysis', params({ scenario: 'F01-gpu-thermal-throttle' }));
  assert.equal(analysis.steps.length, 24);
  assert.equal(analysis.steps.at(-1).states['gpu-a02-06'], 'critical');
  assert.ok(analysis.verdict.match && analysis.verdict.rootMatch);
  const entity = await flaggingRequest('/api/flagging/entity', params({ scenario: 'F01-gpu-thermal-throttle', entity: 'gpu-a02-06' }));
  assert.deepEqual(entity.sources.map(source => source.id), ['gpu-12-02-05']);
  await assert.rejects(flaggingRequest('/api/flagging/analysis', params({ scenario: 'not-a-scenario' })),
    problem => problem instanceof FlaggingError && problem.status === 400);
});
