import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const dir = path.dirname(fileURLToPath(import.meta.url));
const journey = require(path.join(dir, '..', 'dashboard', 'static', 'commercial-journey.js'));

const payload = {
  rings: {
    seguimiento: [{ deal_id: 'lead-1' }, { deal_id: 'lead-2' }],
    oportunidad: [{ deal_id: 'opp-1' }],
    propuesta: [
      { deal_id: 'prop-1', proposal_state: 'verified' },
      { deal_id: 'prop-2', proposal_state: null },
      { deal_id: 'prop-3', proposal_state: 'sent' },
    ],
  },
  centro: [{ deal_id: 'project-1', project_id: 'p1' }],
  won_sin_proyecto: [],
};

const model = journey.buildModel(payload);
assert.deepEqual(
  model.stages.map(stage => [stage.key, stage.count]),
  [['person', 2], ['opportunity', 1], ['proposal', 2], ['project', 1]],
);
assert.equal(model.proposalStageCount, 3);
assert.equal(model.sentCount, 1);
assert.equal(model.next.key, 'version-proposal');
assert.match(model.next.text, /1 propuesta/);
assert.equal(model.callEvidence, 'optional');

const wonGap = journey.buildModel({
  rings: { seguimiento: [], oportunidad: [], propuesta: [] },
  centro: [],
  won_sin_proyecto: [{ deal_id: 'won-1' }, { deal_id: 'won-2' }],
});
assert.equal(wonGap.next.key, 'register-project');
assert.match(wonGap.next.text, /2 deals ganados/);

const unsent = journey.buildModel({
  rings: { seguimiento: [], oportunidad: [], propuesta: [
    { deal_id: 'prop-1', proposal_state: 'verified' },
  ] },
  centro: [],
  won_sin_proyecto: [],
});
assert.equal(unsent.next.key, 'human-send');

const opportunity = journey.buildModel({
  rings: { seguimiento: [], oportunidad: [{ deal_id: 'opp-1' }], propuesta: [] },
  centro: [],
  won_sin_proyecto: [],
});
assert.equal(opportunity.next.key, 'ground-opportunity');

const empty = journey.buildModel({});
assert.deepEqual(empty.stages.map(stage => stage.count), [0, 0, 0, 0]);
assert.equal(empty.next.key, 'start-followup');

assert.throws(() => journey.buildModel(null), /radar payload/i);

console.log('commercial-journey: model contract PASS');
