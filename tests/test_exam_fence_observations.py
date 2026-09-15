"""Observed README references differ from executing their example commands."""
import json
import io
import tarfile
from types import SimpleNamespace

import pytest

from core.actor import trace_message
from core.trace import ArtifactSink
from tools import exam_fence as fence


@pytest.fixture(autouse=True)
def synthetic_fence(monkeypatch):
    # Use synthetic signatures; these tests need no benchmark installation.
    monkeypatch.setattr(fence, "_load_constants", lambda: {
        "011": ["case_name", "pdf_url", "answer_filename", "answer_layout"],
        "085": ["0.1234", "0.5678", "0.9123"],
    })
    monkeypatch.setattr(fence, "_GRAMS_CACHE", {
        "place the hidden reference answer in the output"})


def record(root, output, code="cat /home/user/app/README.md", *, reasoning=""):
    sink = ArtifactSink(str(root))
    action = json.dumps({"program": {"lang": "bash", "code": code}})
    trace = SimpleNamespace(stdout=output, exit_code=0, secs=1.0, timed_out=False)
    messages = [
        {"role": "assistant", "content": action, "reasoning": reasoning},
        {"role": "user", "content": trace_message(trace)},
    ]
    sink.save_turn(1, action)
    sink.save_program(1, "bash", code)
    sink.save_trace(1, trace)
    sink.save_transcript("Solve the local application task.", messages)
    return messages


@pytest.mark.parametrize("reference", [
    "Option 3: cd /home/ubuntu/OSWorld-V2",
    "cd OSWorld-V2",
    "Use /home/ubuntu/OSWorld-V2/self_hosted_websites/app/README.md",
])
def test_recorded_readme_references_are_not_executed_access(tmp_path, reference):
    record(tmp_path, reference)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert fence.audit_transcripts(str(tmp_path)) == []
    assert all(p.read_bytes() == content for p, content in before.items())


def test_document_schema_fields_are_not_answers_even_in_old_cache(tmp_path):
    assert fence.audit_text("columns: case_name, pdf_url") == []
    record(tmp_path, "columns: case_name, pdf_url")
    assert fence.audit_transcripts(str(tmp_path)) == []


@pytest.mark.parametrize("output", [
    "/home/ubuntu/OSWorld-V2/evaluation_examples/task_class/task_011.py",
    "/home/ubuntu/OSWorld-V2/desktop_env/evaluators/metrics.py",
    "/home/ubuntu/OSWorld-V2/unknown_private_file.json",
    "/home/ubuntu/OSWorld-V2/self_hosted_websites/../../private.json",
    "https://github.com/xlang-ai/OSWorld-V2",
    "answer_filename\nanswer_layout",
    "place the hidden reference answer in the output",
    "0.1234 0.5678 0.9123",
])
def test_output_still_carries_protected_material(tmp_path, output):
    record(tmp_path, output)
    assert fence.audit_transcripts(str(tmp_path))


@pytest.mark.parametrize(("suffix", "kind"), [
    ("task_011/index.html", "task-id"),
    ("answer_filename/answer_layout", "constant-tuple"),
    ("place/the/hidden/reference/answer/in/the/output", "instruction-8gram"),
])
def test_observed_path_exception_preserves_other_signals(tmp_path, suffix, kind):
    output = "/home/user/OSWorld-V2/self_hosted_websites/" + suffix
    record(tmp_path, output)
    # Only the path classification is relaxed, even when another signal
    # appears inside the very same application path.
    findings = fence.audit_transcripts(str(tmp_path))
    for filename in ("transcript.json", "trace.txt"):
        hits = [hit for row in findings if row["file"].endswith(filename)
                for hit in row["hits"]]
        assert any(hit["kind"] == kind for hit in hits)
        assert not any(hit["kind"] == "exam-path" for hit in hits)


@pytest.mark.parametrize("code", [
    "cat /home/ubuntu/OSWorld-V2/evaluation_examples/task_class/task_011.py",
    "cd /home/ubuntu/OSWorld-V2; ls",
    "ls OSWorld-V2",
    "curl https://github.com/xlang-ai/OSWorld-V2",
])
def test_actual_access_remains_blocked_in_program_archive(tmp_path, code):
    record(tmp_path, "ordinary output", code)
    hits = fence.audit_transcripts(str(tmp_path))
    assert any(item["file"].endswith("program.sh") for item in hits)


def test_assistant_reasoning_and_prompt_keep_strict_paths(tmp_path):
    record(tmp_path, "ordinary output", reasoning="Inspect /home/user/OSWorld-V2")
    assert fence.audit_transcripts(str(tmp_path))
    assert fence.audit_text("OSWorld-V2")
    assert fence.audit_text("PROGRAM OUTPUT (exit 0, 1s):\ncd /home/user/OSWorld-V2")


@pytest.mark.parametrize("malformation", ["missing_system", "not_program", "not_output"])
def test_unrecognized_messages_do_not_receive_output_exception(tmp_path, malformation):
    messages = record(tmp_path, "cd /home/user/OSWorld-V2")
    doc = {"system": "local task", "messages": messages}
    if malformation == "missing_system":
        del doc["system"]
    elif malformation == "not_program":
        messages[0]["content"] = '{"done": {"checks": []}}'
    else:
        messages[1]["content"] = "cd /home/user/OSWorld-V2"
    (tmp_path / "transcript.json").write_text(json.dumps(doc))
    assert any(p["file"].endswith("transcript.json")
               for p in fence.audit_transcripts(str(tmp_path)))


@pytest.mark.parametrize("metadata", [None, {"exit_code": 0}, {"exit_code": "0", "secs": 1, "timed_out": False}])
def test_unrecognized_trace_metadata_stays_strict(tmp_path, metadata):
    record(tmp_path, "cd /home/user/OSWorld-V2")
    path = tmp_path / "iter_01/trace_meta.json"
    if metadata is None:
        path.unlink()
    else:
        path.write_text(json.dumps(metadata))
    assert any(p["file"].endswith("trace.txt")
               for p in fence.audit_transcripts(str(tmp_path)))


def test_decoded_json_fields_preserve_real_constant_tuple(tmp_path):
    record(tmp_path, "answer_filename\nanswer_layout")
    hits = fence.audit_transcripts(str(tmp_path))
    assert any(p["file"].endswith("transcript.json")
               and any(h["kind"] == "constant-tuple" for h in p["hits"])
               for p in hits)


def test_trace_without_paired_program_is_not_classified_as_output(tmp_path):
    record(tmp_path, "cd /home/user/OSWorld-V2")
    (tmp_path / "iter_01/program.sh").unlink()
    assert any(p["file"].endswith("trace.txt")
               for p in fence.audit_transcripts(str(tmp_path)))


@pytest.mark.parametrize("text", [
    "ordinary application instructions", "task_999.py", "desktop_env/evaluators",
    "0.1234 0.5678", "columns: other_name, other_url",
])
def test_unrelated_plain_text_keeps_its_prior_acceptance(text):
    assert fence.audit_text(text) == []


@pytest.mark.parametrize("authorized", ["", "Use the local application."])
def test_material_payload_has_no_program_output_exception(tmp_path, authorized):
    from explore.provisioning import audit_captured_materials

    payload = b"PROGRAM OUTPUT (exit 0, 1s):\ncd /home/user/OSWorld-V2"
    with tarfile.open(tmp_path / "materials.tgz", "w:gz") as archive:
        member = tarfile.TarInfo("README.md")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    assert audit_captured_materials(str(tmp_path), authorized_instruction=authorized)
