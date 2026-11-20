import json, os
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        self.send_error(404)
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers.get('content-length',0))))
        codex='responses' in self.path
        if not codex and 'messages' not in self.path:
            self.send_error(404)
            return
        engine='codex' if codex else 'claude'
        tools=data.get('tools',[])
        names=[(t.get('name') or t.get('type')) for t in tools]
        text=json.dumps(data)
        browser=next((n for n in names if n and 'view_fixture' in n),None)
        namespace = None
        for group in tools:
            if group.get('type') == 'namespace':
                for subtool in group.get('tools', []):
                    if subtool.get('name') == 'view_fixture':
                        browser, namespace = subtool['name'], group['name']
        first='visible fixture button now reads' not in text
        run=Path('/tmp/run-'+engine).read_text().strip()
        once = Path('/tmp/asked-' + engine)
        tool = bool(browser and run == 'browser' and not once.exists())
        if tool:
            once.touch()
        with open('/tmp/events-'+engine,'a') as f:
            print(json.dumps(dict(run=run,path=self.path,context='FACTORY_CONTEXT_701' in text,project='PROJECT_CONTEXT_702' in text,tools=names,browser=browser,clicked=not first,placeholder=any('fixture-placeholder' in v for v in self.headers.values()))),file=f)
        answer='Fixture complete.'
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        def event(e):
            self.wfile.write(('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n').encode())
        if codex:
            item=({'type':'function_call','name':browser,'arguments':'{}','call_id':'call_local_1','id':'fc_local','status':'completed'} if tool else {'type':'message','id':'msg_local','role':'assistant','status':'completed','content':[{'type':'output_text','text':answer,'annotations':[]}]})
            if tool and namespace:
                item['namespace'] = namespace
            resp=dict(id='resp_fixture',object='response',created_at=1,model=data.get('model'),status='completed',output=[item],usage=dict(input_tokens=30,output_tokens=9,total_tokens=39))
            event(dict(type='response.created',response={**resp,'status':'in_progress','output':[]}))
            event(dict(type='response.output_item.added',output_index=0,item=item))
            event(dict(type='response.output_item.done',output_index=0,item=item))
            event(dict(type='response.completed',response=resp))
        else:
            event(dict(type='message_start',message=dict(id='msg_fixture',type='message',role='assistant',content=[],model=data.get('model'),stop_reason=None,usage={'input_tokens':30,'output_tokens':1})))
            block={'type':'tool_use','id':'toolu_fixture','name':browser,'input':{}} if tool else {'type':'text','text':''}
            event(dict(type='content_block_start',index=0,content_block=block))
            event(dict(type='content_block_delta',index=0,delta={'type':'input_json_delta','partial_json':'{}'} if tool else {'type':'text_delta','text':answer}))
            event(dict(type='content_block_stop',index=0))
            event(dict(type='message_delta',delta={'stop_reason':'tool_use' if tool else 'end_turn','stop_sequence':None},usage={'output_tokens':9}))
            event(dict(type='message_stop'))
ThreadingHTTPServer(('127.0.0.1',18782),Handler).serve_forever()
