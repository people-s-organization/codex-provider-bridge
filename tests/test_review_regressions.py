"""Regression coverage for strict guarantees and pre-stream warning safety."""
import asyncio
import pytest
from bridge import ChatGPTBridge
from schemas import ChatCompletionRequest
from main import chat_completions


def request(**kwargs):
    return ChatCompletionRequest(model='fixture', messages=[{'role':'user','content':'hi'}], **kwargs)


@pytest.mark.parametrize('schema', [
    {'type':'object'},
    {'type':'object','properties':{'nested':{'type':'object'}}},
    {'type':'object','properties':{'x':{'type':'string'}},'required':[]},
])
@pytest.mark.parametrize('strict_compat', ['true','false'])
def test_strict_is_never_downgraded(monkeypatch, schema, strict_compat):
    monkeypatch.setenv('BRIDGE_STRICT_COMPATIBILITY',strict_compat)
    tool={'type':'function','function':{'name':'test','strict':True,'parameters':schema}}
    r=request(tools=[tool])
    payload=ChatGPTBridge()._build_payload(r)
    assert payload['tools'][0]['strict'] is True
    assert payload['tools'][0]['parameters'] == schema
    assert not r.compatibility_warnings()


@pytest.mark.parametrize('field', ['bridge_warnings','_bridge_warnings'])
def test_external_warning_cannot_inject_headers(field):
    r=request(stream=True, **{field:['中文\r\nInjected: yes']})
    response=asyncio.run(chat_completions(r))
    assert not r.compatibility_warnings()
    assert 'x-bridge-compatibility-warnings' not in response.headers


def test_internal_warning_header_is_safe_and_bounded():
    r=request(stream=True)
    r.add_bridge_warning('中文\r\n'+'x'*5000)
    response=asyncio.run(chat_completions(r))
    value=response.headers['x-bridge-compatibility-warnings']
    assert len(value)<=4096
    assert all(32<=ord(c)<=126 for c in value)


def test_ignored_parameter_warning_exists_before_stream_starts():
    r=request(stream=True,max_tokens=20)
    response=asyncio.run(chat_completions(r))
    assert 'max_tokens' in response.headers['x-bridge-compatibility-warnings']
