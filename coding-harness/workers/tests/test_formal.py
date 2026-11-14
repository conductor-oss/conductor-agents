"""Formal specs (common/formal + common/conformance) on the real TLC model checker."""
from __future__ import annotations

import json
import os
import shutil

import pytest

from common import conformance, formal

pytestmark = pytest.mark.skipif(not shutil.which("java") or not os.path.isfile(formal.TLA_JAR),
                                reason="TLC needs java and the TLA+ tools jar")

SPEC = r'''---- MODULE Users ----
EXTENDS Naturals, FiniteSets
CONSTANTS Ids, Names
VARIABLES users, result
vars == <<users, result>>
Init == users = [i \in {} |-> ""] /\ result = "none"
Create(i, n) ==
  \/ /\ i \notin DOMAIN users
     /\ users' = [j \in DOMAIN users \cup {i} |-> IF j = i THEN n ELSE users[j]]
     /\ result' = "created"
  \/ /\ i \in DOMAIN users /\ UNCHANGED users /\ result' = "conflict"
Delete(i) ==
  \/ /\ i \in DOMAIN users /\ users' = [j \in DOMAIN users \ {i} |-> users[j]] /\ result' = "deleted"
  \/ /\ i \notin DOMAIN users /\ UNCHANGED users /\ result' = "notfound"
LeakyDelete(i) == /\ i \in DOMAIN users /\ UNCHANGED users /\ result' = "deleted"
Next == \/ \E i \in Ids, n \in Names : Create(i, n)
        \/ \E i \in Ids : Delete(i)
NextLeaky == \/ \E i \in Ids, n \in Names : Create(i, n)
             \/ \E i \in Ids : LeakyDelete(i)
DeletedIsGone == result = "deleted" => Cardinality(DOMAIN users) < Cardinality(Ids)
Vacuous == result = "deleted" => TRUE
====
'''

ADAPTER = r'''import readline from 'node:readline';
let users = {};
const say = o => process.stdout.write(JSON.stringify(o) + '\n');
readline.createInterface({ input: process.stdin }).on('line', line => {
  const m = JSON.parse(line);
  if (m.op === 'reset') { users = {}; return say({ ok: true }); }
  if (m.op === 'state') return say({ ok: true, state: { users } });
  const [id, name] = m.args;
  if (m.action === 'Create') { if (id in users) return say({ ok: true, result: 'conflict' }); users[id] = name; return say({ ok: true, result: 'created' }); }
  if (!(id in users)) return say({ ok: true, result: process.env.BUG ? 'deleted' : 'notfound' });
  delete users[id]; return say({ ok: true, result: 'deleted' });
});
'''


def _project(tmp_path, *, invariants=("DeletedIsGone",), mutants=(("DeletedIsGone", "NextLeaky"),), spec=SPEC):
    root = tmp_path / "proj"
    d = root / "design" / "spec"
    d.mkdir(parents=True)
    (d / "Users.tla").write_text(spec)
    (d / "Users.cfg").write_text('CONSTANTS\n  Ids = {"u1", "u2"}\n  Names = {"Ada", "Bob"}\nINIT Init\nNEXT Next\n'
                                 + "".join(f"INVARIANT {i}\n" for i in invariants))
    (d / "abstraction.json").write_text(json.dumps({"module": "Users", "config": "Users.cfg", "state": ["users"],
                                                    "result": "result", "adapter": "node adapter.mjs",
                                                    "mutants": [{"invariant": i, "next": n} for i, n in mutants]}))
    (root / "adapter.mjs").write_text(ADAPTER)
    return str(root)


def test_a_sound_spec_is_proven_and_every_transition_becomes_a_trace(tmp_path):
    root = _project(tmp_path)
    proof = formal.prove(root)
    assert proof["ok"] is True and proof["states"] == 27 and proof["transitions"] == proof["traces"] > 100
    assert all(m["caught"] for m in proof["mutants"])
    assert formal.prove(root)["cached"] is True  # an unchanged spec is not checked again


def test_a_violated_invariant_comes_back_with_tlcs_counterexample(tmp_path):
    root = _project(tmp_path, spec=SPEC.replace('/\\ result\' = "deleted"\n  \\/ /\\ i \\notin', '/\\ result\' = "deleted"\n  \\/ /\\ i \\notin'
                                                  ).replace("users' = [j \\in DOMAIN users \\ {i} |-> users[j]]", "UNCHANGED users"))
    proof = formal.prove(root)
    assert proof["ok"] is False and "DeletedIsGone" in proof["summary"] and "Error" in proof["feedback"]


def test_a_vacuous_invariant_is_rejected_because_its_mutant_is_not_caught(tmp_path):
    root = _project(tmp_path, invariants=("DeletedIsGone", "Vacuous"),
                    mutants=(("DeletedIsGone", "NextLeaky"), ("Vacuous", "NextLeaky")))
    proof = formal.prove(root)
    assert proof["ok"] is False and "not caught" in proof["summary"] and "Vacuous" in proof["feedback"]


def test_conformance_passes_a_correct_program_and_reports_a_wrong_one_minimally(tmp_path):
    root = _project(tmp_path)
    assert formal.prove(root)["ok"]
    good = conformance.run(root)
    assert good["ok"] and good["passed"] == good["traces"]
    bad = conformance.run(root, env={**os.environ, "BUG": "1"})
    assert not bad["ok"] and bad["failed"] > 0
    first = bad["mismatches"][0]
    assert first["path"] == ['Delete("u1")'] or first["path"] == ['Delete("u2")']  # the shortest explanation
    assert first["expected"] == "notfound" and first["actual"] == "deleted"


def test_set_valued_results_compare_unordered():
    """A spec set (a search result) equals the program's list in any order; the helper that orders
    them was once lost and every measurement with a set crashed (run dc9624ec)."""
    exp = {"$set": ["u2", "u1"]}
    assert conformance.normalize(exp, ["u1", "u2"]) == (["u1", "u2"], ["u1", "u2"])
    assert conformance.normalize(exp, ["u2", "u1"])[1] == ["u1", "u2"]
    nested = {"users": {"u1": {"emails": {"$set": ["b@x", "a@x"]}}}}
    e, a = conformance.normalize(nested, {"users": {"u1": {"emails": ["a@x", "b@x"]}}})
    assert e == a
    assert conformance.normalize(exp, ["u1"])[0] != conformance.normalize(exp, ["u1"])[1]
