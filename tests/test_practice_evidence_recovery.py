"""Evidence requests cannot be converted into verdicts, resets, or early learning."""
import dataclasses
import io
import json
import tarfile
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.settings import load
from explore import e15_loop as E, unified_evolution as U
from explore import practice_evidence_recovery as R
from explore.phase2_recovery import sha
from test_phase2_recovery import fixture, save, admit
from test_unified_evolution import _VM, _hooks


def stopped_practice(tmp_path):
    f = fixture(tmp_path)
    cfg = f.configs['curriculum']
    f.configs.update(practice_actor=cfg, practice_verifier=cfg, target_actor=cfg)
    manifest = json.loads((f.root/'manifest.json').read_text())
    for name in f.configs:
        manifest['configs'][name] = dict(manifest['configs']['curriculum'])
    save(f.root/'manifest.json', manifest)
    E._atomic_install_memory(str(f.root/'active_memory'), f.memory)
    journal = f.root/'memory_journal/ep001.tgz'
    journal.parent.mkdir()
    with tarfile.open(journal, 'w:gz') as out:
        for name, data in f.active.items():
            item = tarfile.TarInfo(name); item.size = len(data)
            out.addfile(item, io.BytesIO(data))
    state = json.loads((f.cycle/'state.json').read_text())
    state.update(status='infra', projects=2,
                 reason='practice Agentic Verifier ended without PASS/FAIL: request')
    save(f.cycle/'state.json', state)
    event={'event':'EVOLUTION_VERIFIER_PHASE_STARTED','payload':{
        'payload':{'project_index':2},'state':{'project_open':True,'memory_phase_open':False}}}
    with (f.root/'events.jsonl').open('a') as out:out.write(json.dumps(event)+'\n')
    ep = f.cycle/'episodes/ep002'
    ep.mkdir()
    project='DECISION: PROJECT\nUpdate /home/user/evolution_project/document.txt from supplied fixtures.'
    (ep/'project.md').write_text(project)
    publication=f.cycle/'curriculum/turn_002/segment_002/iter_01/program.py'
    publication.write_text('notes='+repr('exact latest notes')+'\nhandoff='+repr(project)+'\n')
    save(publication.parent/'trace_meta.json',{'exit_code':0})
    (publication.parent/'trace.txt').write_text('notes exists: True\n[exit 0]')
    history=[{'role':'user','content':'same actor context'},
             {'role':'assistant','content':'{"done":true,"checks":[]}'}]
    save(ep/'actor/attempt_001/segment_000/transcript.json',{'messages':history})
    (ep/'actor/attempt_001/segment_000/turn.txt').write_text('previous reply')
    verification={'verdict':'unverified','findings':'Need independently reproducible evidence.\nVERDICT: UNVERIFIED',
                  'transcript':[{'role':'user','content':'independent context'},
                                {'role':'assistant','content':'independent probe'}]}
    save(ep/'verifier/iter_02/verify2.json',verification)
    (ep/'verifier/turn.txt').write_text('previous verifier reply')
    (ep/'candidate/publication_001').mkdir(parents=True)
    (ep/'fixtures').mkdir()
    plan=json.loads(f.plan.read_text())
    plan.update(recovery_kind='unverified_practice',
                curriculum_publication_program=str(publication),
                prior_role_budget={'prior_actor_iters':1,'prior_actor_wall_secs':10,
                                   'prior_verifier_iters':1,'prior_verifier_wall_secs':5},
                input_sha256={str(f.root/'manifest.json'):sha(f.root/'manifest.json')})
    save(f.plan,plan)
    return f


def test_stopped_practice_admission_retains_all_three_roles_and_trigger_memory(tmp_path):
    f=stopped_practice(tmp_path)
    before={str(p):sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    b=admit(f)
    assert b.projects==1 and b.completed_curriculum_turns==2
    assert b.notes=='exact latest notes'
    assert b.pending_practice['actor_history'][0]['content']=='same actor context'
    assert b.pending_practice['verifier_history'][0]['content']=='independent context'
    assert b.initial_evolution_memory==f.active
    assert E._read_memory_tree(str(f.root/'active_memory'))==f.memory
    assert before=={str(p):sha(p) for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('fault',['memory','journal','quarantine','partial_learning','verdict','budget','notes'])
def test_invalid_practice_checkpoint_is_rejected_without_writes(tmp_path,fault):
    f=stopped_practice(tmp_path);ep=f.cycle/'episodes/ep002'
    if fault=='memory':(f.cycle/'memory/lesson.md').write_bytes(b'partial')
    elif fault=='journal':(f.root/'memory_journal/ep001.tgz').write_bytes(b'bad archive')
    elif fault=='quarantine':save(ep/'boundary_audit.json',{'status':'quarantined'})
    elif fault=='partial_learning':(ep/'memory_distillation').mkdir()
    elif fault=='verdict':save(ep/'verifier/iter_02/verify2.json',{'verdict':'pass'})
    elif fault=='budget':
        plan=json.loads(f.plan.read_text());plan['prior_role_budget']['prior_actor_iters']=cfg_limit=f.configs['practice_actor'].max_iters;save(f.plan,plan)
    else:
        plan=json.loads(f.plan.read_text());Path(plan['curriculum_publication_program']).write_text('notes=untrusted_call()')
    before={str(p):sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises((E.E15InfrastructureError,tarfile.ReadError)):
        admit(f)
    assert before=={str(p):sha(p) for p in tmp_path.rglob('*') if p.is_file()}


def test_pending_practice_finishes_and_learns_before_next_curriculum_without_replay(tmp_path,monkeypatch):
    f=stopped_practice(tmp_path);boundary=admit(f);vm=_VM();prompts=[];calls=[]
    hooks=_hooks(vm,['DECISION: READY_FOR_TARGET\nReturn control.'],prompts)
    session=U.UnifiedCurriculumSession(history=boundary.history,notes=boundary.notes,turns=2)
    def practice(**kw):
        assert not prompts, 'Curriculum ran before resolving open practice'
        assert kw['project_index']==2 and kw['resume_state'] is boundary.pending_practice
        calls.append('practice2')
        return {'terminal_outcome':'FAIL','verifier_report':'VERDICT: FAIL\nEvidence establishes a violation.',
                'actor_handoff':'STATUS: SUBMIT','actor_history':kw['resume_state']['actor_history'],
                'before_memory':f.memory}
    def learning(**kw):
        assert calls==['practice2'] and kw['terminal_outcome']=='FAIL' and kw['experience_index']==2
        assert kw['actor_history']==boundary.pending_practice['actor_history']
        calls.append('learn2');return f.memory,'same Actor diagnosis',kw['actor_history']
    monkeypatch.setattr(U,'_run_practice_attempt',practice)
    monkeypatch.setattr(U,'_promote_learning',learning)
    old=sha(f.cycle/'episodes/ep001/outcome.json')
    result=U.evolve_until_ready(vm,str(f.cycle),f.target,boundary.trigger['verifier_report'],
        boundary.trigger['actor_learning_diagnosis'],boundary.initial_evolution_memory,
        load(None),load(None),load(None),load(None),hooks=hooks,session=session,
        trigger_authority='phase2_outcome_protocol',triggering_outcome='PASS',resume_boundary=boundary)
    assert result.status=='ready_for_retry',result.reason
    assert result.projects==2 and session.turns==3 and calls==['practice2','learn2']
    assert 'practice project 2: FAIL' in prompts[0] and 'exact latest notes' in prompts[0]
    assert sha(f.cycle/'episodes/ep001/outcome.json')==old


def evidence_setup(tmp_path,monkeypatch,reports):
    import core.verifier as verifier
    import config.settings as settings
    ep=tmp_path/'episodes/ep002';original=ep/'candidate/publication_001';original.mkdir(parents=True)
    (original/'artifact').write_bytes(b'original candidate');(ep/'actor_handoff.md').write_text('original handoff')
    cfg=dataclasses.replace(load(None),max_iters=100,wall_clock_secs=300)
    control=dataclasses.replace(cfg,agentic_verifier_config='verifier-role',
        verifier_unverified_evidence=True,wall_clock_secs=120)
    monkeypatch.setattr(settings,'load',lambda p:dataclasses.replace(cfg,wall_clock_secs=200))
    memory={'lesson.md':b'committed'};calls=[];sessions=[]
    monkeypatch.setattr(R.V,'_read_memory_tree',lambda p:dict(memory))
    for name in ('_fresh_vm','_replay','_push_canonical_memory','_require_owned_project_shape',
                 '_stage_original_practice_fixtures','_verify_candidate','_verify_original_practice_fixtures',
                 '_push_actor_execution_evidence','_verify_actor_execution_evidence'):
        monkeypatch.setattr(R.V,name,lambda *a,**kw:None)
    monkeypatch.setattr(R.V,'_capture_owned_tree',lambda hooks,vm,label,path,target:Path(path).mkdir(parents=True))
    monkeypatch.setattr(R.V,'_build_actor_execution_evidence',lambda *a:{'manifest_sha256':'frozen'})
    def actor(**kw):
        assert kw['history'][0]['content']=='original Actor private context'
        assert 'VERDICT: UNVERIFIED' in kw['prompt'] and kw['continuation'] and kw['bounded_transport']
        calls.append('actor');history=kw['history']+[{'role':'user','content':'requested evidence'},
                                                {'role':'assistant','content':'{"done":true,"checks":[]}'}]
        return SimpleNamespace(text='STATUS: SUBMIT\nCandidate preserved.'),history,SimpleNamespace(turns=1,iters=1)
    def verify(*args,**kw):
        assert kw['session'][0]['content']=='original Verifier independent context'
        assert all('Actor private context' not in str(m) for m in kw['session'])
        assert args[2].verifier_hide_actor_memory and 0 < kw['wall_budget'] <= 110
        assert kw['iters_budget'] <= 96
        calls.append('verify');sessions.append(kw['session']);kw['session'].inspections+=1
        v=next(reports);return v,('VERDICT: '+{'unverified':'UNVERIFIED','wrong':'FAIL','pass':'PASS'}[v])
    monkeypatch.setattr(R.V,'_run_handoff_phase',actor);monkeypatch.setattr(verifier,'verify_agentic',verify)
    saved={'actor_history':[{'role':'user','content':'original Actor private context'},{'role':'assistant','content':'old'}],
           'verifier_history':[{'role':'user','content':'original Verifier independent context'},{'role':'assistant','content':'old'}],
           'findings':'VERDICT: UNVERIFIED\nPlease expose evidence.', 'candidate_dir':str(original),
           'publication':1,'inspections':1,'prior_actor_iters':3,'prior_actor_wall_secs':20,
           'prior_verifier_iters':4,'prior_verifier_wall_secs':10}
    kwargs=dict(hooks=SimpleNamespace(),vm=object(),lineage=tmp_path,episode_dir=ep,project_index=2,
        project='Update the supplied document.',fixture_dir=ep/'fixtures',target='Update the supplied document.',
        actor_cfg=cfg,verifier_cfg=cfg,memory_dir=tmp_path/'memory',emit=lambda *a,**kw:None,
        agentic_verifier_cfg=control,resume_state=saved)
    return ep,kwargs,calls,sessions


def test_unverified_resumes_same_actor_and_verifier_then_returns_real_fail(tmp_path,monkeypatch):
    ep,kw,calls,sessions=evidence_setup(tmp_path,monkeypatch,iter(['unverified','wrong']))
    out=R.run_practice_with_evidence(**kw)
    assert out['terminal_outcome']=='FAIL' and calls==['actor','verify','actor','verify']
    assert sessions[0] is sessions[1]
    assert (ep/'candidate/publication_001/artifact').read_bytes()==b'original candidate'
    assert (ep/'actor_handoff.md').read_text()=='original handoff'
    assert not (ep/'outcome.json').exists() and not (ep/'memory_distillation').exists()
    assert (ep/'unverified_report_002.md').read_text()=='VERDICT: UNVERIFIED'


def test_evidence_budget_exhaustion_never_becomes_success(tmp_path,monkeypatch):
    ep,kw,calls,_=evidence_setup(tmp_path,monkeypatch,iter(['unverified']))
    kw['resume_state']['prior_actor_iters']=kw['actor_cfg'].max_iters-1
    with pytest.raises(E.E15InfrastructureError,match='remaining budget exhausted'):
        R.run_practice_with_evidence(**kw)
    assert calls==['actor','verify'] and not (ep/'verifier_report.md').exists()


def test_verifier_transport_absence_never_triggers_an_actor_retry(tmp_path,monkeypatch):
    import core.verifier as verifier
    ep,kw,calls,_=evidence_setup(tmp_path,monkeypatch,iter([]))
    monkeypatch.setattr(verifier,'verify_agentic',lambda *a,**kw:('unverified','VM connection failed'))
    with pytest.raises(E.E15InfrastructureError,match='infrastructure absence'):
        R.run_practice_with_evidence(**kw)
    assert calls==['actor'] and not (ep/'verifier_report.md').exists()


def stopped_first_practice(tmp_path):
    f = stopped_practice(tmp_path)
    shutil.rmtree(f.cycle / 'episodes/ep001')
    (f.cycle / 'episodes/ep002').rename(f.cycle / 'episodes/ep001')
    ep = f.cycle / 'episodes/ep001'
    (ep / 'verifier/iter_02').rename(ep / 'verifier/iter_01')
    shutil.rmtree(f.cycle / 'curriculum/turn_001')
    (f.cycle / 'curriculum/turn_002').rename(f.cycle / 'curriculum/turn_001')
    E._atomic_install_memory(str(f.root / 'active_memory'), f.active)
    E._atomic_install_memory(str(f.cycle / 'memory'), f.active)
    state = json.loads((f.cycle / 'state.json').read_text())
    state.update(projects=1, learning_experiences=0, memory_manifest=E._manifest(f.active),
                 memory_tree_sha256=R.V._memory_tree_sha256(f.active))
    save(f.cycle / 'state.json', state)
    (f.cycle / 'trigger/outcome.md').write_text('Same target Actor diagnosis and verifier report.')
    event = {'event': 'EVOLUTION_VERIFIER_PHASE_STARTED', 'payload': {
        'payload': {'project_index': 1}, 'state': {'project_open': True, 'memory_phase_open': False}}}
    (f.root / 'events.jsonl').write_text(json.dumps(event) + '\n')
    manifest = json.loads((f.root / 'manifest.json').read_text())
    manifest['protocol'] = {'curriculum_memory_access': 'none'}
    save(f.root / 'manifest.json', manifest)
    plan = json.loads(f.plan.read_text())
    plan.update(completed_projects=0, curriculum_memory_access='none',
                source_transcript=plan['source_transcript'].replace('turn_002', 'turn_001'),
                curriculum_publication_program=plan['curriculum_publication_program'].replace('turn_002', 'turn_001'),
                input_sha256={str(f.root / 'manifest.json'): sha(f.root / 'manifest.json')})
    save(f.plan, plan)
    return f


def test_first_practice_admission_preserves_target_memory_and_memory_off(tmp_path):
    f = stopped_first_practice(tmp_path)
    before = {str(p): sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    b = admit(f)
    assert b.projects == 0 and b.completed_curriculum_turns == 1 and b.project_records == []
    assert b.initial_evolution_memory == f.active
    assert b.pending_practice['actor_history'][0]['content'] == 'same actor context'
    assert b.pending_practice['verifier_history'][0]['content'] == 'independent context'
    b.validate_evolution(f.cycle, f.target, f.active, b.trigger['verifier_report'],
                         b.trigger['actor_learning_diagnosis'], 'phase2_outcome_protocol', 'PASS')
    assert before == {str(p): sha(p) for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('fault', ['access', 'count', 'early_learning', 'memory', 'notes_size'])
def test_first_practice_rejects_condition_drift_and_partial_learning(tmp_path, fault):
    f = stopped_first_practice(tmp_path)
    plan = json.loads(f.plan.read_text())
    if fault == 'access':
        plan['curriculum_memory_access'] = 'read_only'
    elif fault == 'count':
        plan['completed_projects'] = 1
    elif fault == 'early_learning':
        save(f.cycle / 'episodes/ep001/outcome.json', {'terminal_outcome': 'PASS'})
    elif fault == 'memory':
        (f.cycle / 'memory/lesson.md').write_text('uncommitted')
    else:
        publication = Path(plan['curriculum_publication_program'])
        publication.write_text(publication.read_text() + "open('/home/user/curriculum_notes.md', 'w').write(notes)\n")
        (publication.parent / 'trace.txt').write_text('curriculum_notes.md: 1 bytes\n[exit 0]')
    save(f.plan, plan)
    with pytest.raises(E.E15InfrastructureError):
        admit(f)


def test_literal_notes_byte_witness_is_accepted_without_executing_program(tmp_path):
    f = stopped_first_practice(tmp_path)
    plan = json.loads(f.plan.read_text())
    publication = Path(plan['curriculum_publication_program'])
    publication.write_text(publication.read_text() + "open('/home/user/curriculum_notes.md', 'w').write(notes)\n")
    (publication.parent / 'trace.txt').write_text('curriculum_notes.md: 18 bytes\n[exit 0]')
    assert admit(f).notes == 'exact latest notes'


def test_first_practice_resume_learns_once_before_memory_off_curriculum(tmp_path, monkeypatch):
    f = stopped_first_practice(tmp_path); boundary = admit(f); vm = _VM(); prompts = []; calls = []
    hooks = _hooks(vm, ['DECISION: READY_FOR_TARGET\nReturn control.'], prompts)
    session = U.UnifiedCurriculumSession(history=boundary.history, notes=boundary.notes, turns=1)
    def practice(**kw):
        assert not prompts and kw['project_index'] == 1 and kw['resume_state'] is boundary.pending_practice
        calls.append('practice1')
        return dict(terminal_outcome='FAIL', verifier_report='VERDICT: FAIL\nIndependent finding.',
                    actor_handoff='STATUS: SUBMIT', actor_history=kw['resume_state']['actor_history'],
                    before_memory=f.active)
    def learning(**kw):
        assert calls == ['practice1'] and kw['experience_index'] == 1 and kw['terminal_outcome'] == 'FAIL'
        calls.append('learn1'); return f.active, 'same Actor diagnosis', kw['actor_history']
    monkeypatch.setattr(U, '_run_practice_attempt', practice)
    monkeypatch.setattr(U, '_promote_learning', learning)
    result = U.evolve_until_ready(vm, str(f.cycle), f.target, boundary.trigger['verifier_report'],
        boundary.trigger['actor_learning_diagnosis'], boundary.initial_evolution_memory,
        load(None), load(None), load(None), load(None), hooks=hooks, session=session,
        trigger_authority='phase2_outcome_protocol', triggering_outcome='PASS',
        resume_boundary=boundary, curriculum_memory_access='none')
    assert result.status == 'ready_for_retry', result.reason
    assert result.projects == 1 and session.turns == 2 and calls == ['practice1', 'learn1']
    metadata = json.loads((f.cycle / 'curriculum/turn_002/memory_access.json').read_text())
    assert metadata['access'] == 'none' and metadata['attached_files'] == metadata['attached_bytes'] == 0
    assert 'practice project 1: FAIL' in prompts[0]
