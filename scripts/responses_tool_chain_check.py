#!/usr/bin/env python3
"""Live stored Responses tool loop, with a deterministic local addition function."""
import argparse
import json
from openai_surface_check import Checker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8790')
    parser.add_argument('--key', default='')
    args = parser.parse_args()
    client = Checker(args.endpoint, args.key)
    status, _, models = client.json_request('GET', '/v1/models')
    assert status == 200 and models['data'], 'No discovered model available'
    model = models['data'][0]['id']
    tool = {'type':'function', 'name':'add', 'description':'Add two integers.', 'strict':True,
            'parameters':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},
                          'required':['a','b'],'additionalProperties':False}}
    stored = []
    try:
        status, _, first = client.json_request('POST','/v1/responses', {
            'model':model, 'input':'Use add to compute 123 plus 456.', 'store':True,
            'tools':[tool], 'tool_choice':{'type':'function','name':'add'}})
        assert status == 200, (status, first)
        stored.append(first['id'])
        calls = [item for item in first['output'] if item['type']=='function_call']
        assert calls, first
        outputs=[]
        for call in calls:
            assert call['name']=='add'
            values=json.loads(call['arguments'])
            assert type(values['a']) is int and type(values['b']) is int
            result=values['a']+values['b']
            outputs.append({'type':'function_call_output','call_id':call['call_id'],'output':str(result)})
        status, _, second = client.json_request('POST','/v1/responses', {
            'model':model, 'previous_response_id':first['id'], 'input':outputs,
            'tools':[tool], 'tool_choice':'none', 'store':True})
        assert status == 200, (status, second)
        stored.append(second['id'])
        text=''.join(part.get('text','') for item in second['output'] for part in item.get('content',[]))
        assert '579' in text, text
        print(json.dumps({'success':True,'model':model,'executed_calls':len(outputs),
                          'previous_response_tool_result_accepted':True,'final_consumed_result':True}))
    finally:
        for response_id in stored:
            client.json_request('DELETE','/v1/responses/'+response_id)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
