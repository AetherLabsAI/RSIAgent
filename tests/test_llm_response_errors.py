"""HTTP 200 may carry a provider error, including no completion choices."""
from copy import deepcopy

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError
from openai.types.chat import ChatCompletion

from llm import client as C


def response(payload):
    # Use the installed SDK's real permissive response construction: extra error
    # fields survive, and a missing choices field is not an HTTP exception.
    return ChatCompletion.model_construct(**{
        'id': 'gen-offline-test', 'object': 'chat.completion', 'created': 1,
        'model': 'test-model', 'provider': 'test-provider', **payload})


def install(monkeypatch, replies):
    from types import SimpleNamespace
    requests = []
    iterator = iter(replies)

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        reply = next(iterator)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(C, '_client', SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(C.time, 'sleep', lambda _: None)
    monkeypatch.setattr(C, '_PROVIDER_COUNTS', {})
    return requests


@pytest.mark.parametrize('code,recoverable', [(400, False), (422, False),
                                             (429, True), (502, True)])
def test_embedded_error_uses_error_code_and_preserves_details(monkeypatch, caplog,
                                                            code, recoverable):
    error = {'code': code, 'message': 'offline provider diagnostic',
             'metadata': {'error_type': 'offline_test_error'}}
    reply = response({'error': error})
    requests = install(monkeypatch, [reply] * 4)
    with pytest.raises(C.LLMTransportError) as caught:
        C.chat('test-model', 'exact system', 'exact user', provider_order=['Pinned'],
               provider_allow_fallbacks=False, reasoning_effort='high')
    exc = caught.value
    assert exc.status_code == code and exc.recoverable is recoverable
    assert exc.cause.body['error'] == error
    assert exc.cause.body['id'] == 'gen-offline-test'
    assert 'gen-offline-test' in caplog.text
    assert 'offline_test_error' in caplog.text
    assert requests and all(item == requests[0] for item in requests)
    assert C.provider_counts() == {}


@pytest.mark.parametrize('payload', [{}, {'choices': []},
                                    {'error': {'code': 'unknown', 'message': 'unknown'}}])
def test_unclassified_empty_response_retains_retry_semantics(monkeypatch, payload):
    install(monkeypatch, [response(payload)] * 4)
    with pytest.raises(C.LLMTransportError) as caught:
        C.chat('test-model', 'system', 'user')
    assert caught.value.recoverable is True
    assert caught.value.status_code is None
    assert caught.value.cause.body['id'] == 'gen-offline-test'


def good_choice():
    from openai.types.chat.chat_completion import Choice
    from openai.types.chat import ChatCompletionMessage
    return Choice(index=0, finish_reason='stop', message=ChatCompletionMessage(
        role='assistant', content='valid reply', reasoning='owned reasoning'))


def test_transient_error_then_success_keeps_exact_request_options(monkeypatch):
    requests = install(monkeypatch, [response({'error': {'code': 502, 'message': 'retry'}}),
                                    response({'choices': [good_choice()]})])
    answer = C.chat('test-model', 'system', 'user', history=[
        {'role': 'user', 'content': 'prior user'},
        {'role': 'assistant', 'content': 'prior answer', 'reasoning': 'prior reasoning'}],
        reasoning_effort='high', json_object=True)
    assert answer == 'valid reply'
    assert len(requests) == 2 and requests[0] == requests[1]
    assert C.pop_last_reasoning() == 'owned reasoning'
    assert C.provider_counts() == {'test-model': {'test-provider': 1}}


def transport_error(kind):
    request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')
    if kind == 'connection':
        return APIConnectionError(request=request)
    if kind == 'timeout':
        return APITimeoutError(request=request)
    return APIStatusError('offline HTTP failure', response=httpx.Response(
        kind, request=request), body={'message': 'offline provider diagnostic'})


@pytest.mark.parametrize('kind', [429, 503, 'connection', 'timeout'])
@pytest.mark.parametrize('provider_order', [[], ['Pinned']])
def test_transport_retry_preserves_complete_request(monkeypatch, kind, provider_order):
    requests = install(monkeypatch, [transport_error(kind),
                                    response({'choices': [good_choice()]})])
    history = [C.durable_user_message('prior observation', b'\x89PNG\r\n\x1a\nprior'),
               {'role': 'assistant', 'content': 'prior answer',
                'reasoning': 'prior reasoning'}]
    original_history = deepcopy(history)
    answer = C.chat('test-model', 'system', 'user', history=history,
                    image=b'\xff\xd8current', max_tokens=1234, temperature=0.7,
                    reasoning_effort='max', top_p=0.9, json_object=True,
                    provider_order=provider_order, provider_allow_fallbacks=False,
                    provider_require_parameters=True)

    assert answer == 'valid reply'
    assert len(requests) == 2
    assert requests[0]['extra_body']['reasoning'] == {'effort': 'max'}
    assert requests[0]['response_format'] == {'type': 'json_object'}
    assert requests[0]['top_p'] == 0.9
    assert requests[1] == requests[0]
    assert history == original_history
    assert C.pop_last_reasoning() == 'owned reasoning'
    assert C.provider_counts() == {'test-model': {'test-provider': 1}}


@pytest.mark.parametrize('code', [400, 422])
def test_fatal_transport_error_retains_options_until_exhaustion(monkeypatch, code):
    requests = install(monkeypatch, [transport_error(code) for _ in range(4)])
    with pytest.raises(C.LLMTransportError) as caught:
        C.chat('test-model', 'system', 'user', reasoning_max_tokens=512,
               top_p=0.8, json_object=True)

    assert len(requests) == 4
    assert requests[0]['extra_body']['reasoning'] == {'max_tokens': 512}
    assert requests[0]['response_format'] == {'type': 'json_object'}
    assert requests[0]['top_p'] == 0.8
    assert all(item == requests[0] for item in requests)
    assert caught.value.status_code == code
    assert caught.value.recoverable is False
    assert C.provider_counts() == {}


def test_error_with_partial_choice_is_not_a_successful_agent_turn(monkeypatch):
    install(monkeypatch, [response({'error': {'code': 502, 'message': 'partial failure'},
                                    'choices': [good_choice()]})] * 4)
    with pytest.raises(C.LLMTransportError) as caught:
        C.chat('test-model', 'system', 'user')
    assert caught.value.status_code == 502
    assert C.provider_counts() == {}
