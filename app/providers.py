"""Official provider adapters. Endpoints are fixed; keys never enter agent history."""
import json
import httpx
from openai import OpenAI


class ProviderSession:
    def __init__(self, provider, model, key, prompt, history, tools):
        self.provider, self.model, self.prompt = provider, model, prompt
        self.messages, self.tools = list(history), tools
        if provider == 'anthropic':
            self.client = httpx.Client(base_url='https://api.anthropic.com', timeout=25,
                                      headers={'x-api-key': key, 'anthropic-version': '2023-06-01'})
        else:
            opts = {'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai/'} if provider == 'gemini' else {'base_url':'https://api.openai.com/v1'}
            self.client = OpenAI(api_key=key, timeout=25, max_retries=0, **opts)

    def close(self):
        self.client.close()

    def step(self, force_tool=False):
        if self.provider == 'openai':
            extra = {'tool_choice': {'type': 'function', 'name': self.tools[0]['name']}} if force_tool else {}
            response = self.client.responses.create(model=self.model, instructions=self.prompt, input=self.messages,
                tools=self.tools, parallel_tool_calls=False, store=False, include=['reasoning.encrypted_content'], max_output_tokens=2048, **extra)
            self.messages.extend(response.output)
            calls = [{'id': c.call_id, 'name': c.name, 'arguments': c.arguments} for c in response.output if c.type == 'function_call']
            return response.output_text, calls
        if self.provider == 'gemini':
            tools = [{'type': 'function', 'function': {k:t[k] for k in ('name', 'description', 'parameters')}} for t in self.tools]
            extra = {'tool_choice': {'type': 'function', 'function': {'name': self.tools[0]['name']}}} if force_tool else {}
            response = self.client.chat.completions.create(model=self.model, messages=[{'role':'system','content':self.prompt}]+self.messages, tools=tools, max_tokens=2048, **extra)
            message = response.choices[0].message
            # Preserve Gemini's extra_content / thought signature when present.
            wire = message.model_dump(exclude_none=True)
            wire.pop('parsed', None)
            self.messages.append(wire)
            return message.content or '', [{'id':c.id, 'name':c.function.name, 'arguments':c.function.arguments} for c in message.tool_calls or []]
        tools = [{'name':t['name'], 'description':t['description'], 'input_schema':t['parameters']} for t in self.tools]
        extra = {'tool_choice': {'type':'tool','name':self.tools[0]['name']}} if force_tool else {}
        response = self.client.post('/v1/messages', json={'model':self.model,'system':self.prompt,'messages':self.messages,'tools':tools,'max_tokens':2048, **extra})
        response.raise_for_status()
        data = response.json()
        blocks = data['content']
        self.messages.append({'role':'assistant','content':blocks})
        return '\n'.join(b['text'] for b in blocks if b['type']=='text'), [{'id':b['id'],'name':b['name'],'arguments':json.dumps(b['input'])} for b in blocks if b['type']=='tool_use']

    def results(self, results):
        if self.provider == 'anthropic':
            self.messages.append({'role':'user','content':[{'type':'tool_result','tool_use_id':cid,'content':json.dumps(value, ensure_ascii=False),'is_error':isinstance(value,dict) and 'error' in value} for cid,value in results]})
        elif self.provider == 'gemini':
            self.messages.extend({'role':'tool','tool_call_id':cid,'content':json.dumps(value, ensure_ascii=False)} for cid,value in results)
        else:
            self.messages.extend({'type':'function_call_output','call_id':cid,'output':json.dumps(value, ensure_ascii=False)} for cid,value in results)


def probe(provider, model, key):
    tool = {'type':'function','name':'connection_check','description':'Return the supplied nonce.',
            'parameters':{'type':'object','properties':{'nonce':{'type':'string'}},'required':['nonce'],'additionalProperties':False},'strict':True}
    session = ProviderSession(provider, model, key, 'Call connection_check with nonce daywork. After the result say OK.',
                              [{'role':'user','content':'Check tool calling.'}], [tool])
    try:
        _, calls = session.step(force_tool=True)
        if len(calls) != 1 or calls[0]['name'] != 'connection_check' or json.loads(calls[0]['arguments']) != {'nonce':'daywork'}:
            raise ValueError('Tool calling check failed')
        session.results([(calls[0]['id'], {'ok':True})])
        answer, more = session.step()
        if not answer or more:
            raise ValueError('Tool result round trip failed')
    finally:
        session.close()
