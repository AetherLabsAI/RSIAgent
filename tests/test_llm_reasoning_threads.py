"""A response's reasoning must stay with its calling execution thread."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

from llm import client as C


def response(content, reasoning, finish_reason='stop'):
    return SimpleNamespace(
        provider='test-provider', usage=None,
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content, reasoning=reasoning, tool_calls=[]))])


def install_client(monkeypatch, create):
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(C, '_c', lambda: fake)


def test_concurrent_responses_keep_their_own_reasoning(monkeypatch):
    a_ready, b_ready, a_popped = Event(), Event(), Event()

    def create(**kwargs):
        branch = kwargs['messages'][-1]['content']
        return response('content-'+branch, 'reasoning-'+branch)

    install_client(monkeypatch, create)

    def branch_a():
        content = C.chat('test-model', 'system', 'a')
        a_ready.set()
        assert b_ready.wait(5)
        reasoning = C.pop_last_reasoning()
        a_popped.set()
        return content, reasoning, C.pop_last_reasoning()

    def branch_b():
        assert a_ready.wait(5)
        content = C.chat('test-model', 'system', 'b')
        b_ready.set()
        assert a_popped.wait(5)
        return content, C.pop_last_reasoning(), C.pop_last_reasoning()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(branch_a), pool.submit(branch_b)
        results = [a.result(), b.result()]
    assert results == [('content-a', 'reasoning-a', ''), ('content-b', 'reasoning-b', '')]


def test_reasoning_is_consumed_once_and_empty_response_clears_it(monkeypatch):
    replies = iter([response('one', 'first'), response('two', None)])
    install_client(monkeypatch, lambda **kwargs: next(replies))
    assert C.chat('test-model', 'system', 'one') == 'one'
    assert C.pop_last_reasoning() == 'first'
    assert C.pop_last_reasoning() == ''
    assert C.chat('test-model', 'system', 'two') == 'two'
    assert C.pop_last_reasoning() == ''


def test_truncation_retry_retains_only_final_response_reasoning(monkeypatch):
    replies = iter([response('', 'truncated-reasoning', 'length'), response('final', 'final-reasoning')])
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return next(replies)

    install_client(monkeypatch, create)
    assert C.chat('moonshotai/kimi-k3', 'system', 'user', max_tokens=1000) == 'final'
    assert C.pop_last_reasoning() == 'final-reasoning'
    assert C.pop_last_reasoning() == ''
    assert len(requests) == 2
    assert requests[0]['messages'] == requests[1]['messages']
    assert requests[0]['max_tokens'] == 1000
    assert requests[1]['max_tokens'] == 2000
    assert requests[1]['extra_body']['reasoning'] == {'effort': 'low'}
