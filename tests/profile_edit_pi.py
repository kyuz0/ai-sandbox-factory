"""Run inside the FrostPi fixture: real Pi RPC tools, with synthetic model decisions."""
import json
import queue
import subprocess
import threading


def main():
    process=subprocess.Popen(['pi','--mode','rpc','--provider','fixture','--model','fixture','--no-session'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd='/workspace')
    lines,errors,events=queue.Queue(),[],[]
    def reader():
        for line in process.stdout:lines.put(line)
        lines.put(None)
    threading.Thread(target=reader,daemon=True).start()
    threading.Thread(target=lambda:errors.extend(process.stderr.readlines()),daemon=True).start()
    try:
        process.stdin.write(json.dumps({'type':'prompt','message':'Factory profile editing fixture'})+'\n')
        process.stdin.flush()
        while True:
            try:line=lines.get(timeout=180)
            except queue.Empty:
                raise AssertionError(f'Pi stopped producing events; exit {process.poll()}, recent events {events[-10:]}, stderr {errors}') from None
            assert line is not None,(events,errors)
            try:event=json.loads(line)
            except json.JSONDecodeError:continue
            events.append(event)
            assert not (event.get('type')=='extension_ui_request' and event.get('method') in ('select','confirm','input','editor')),(event,errors)
            if event.get('type')=='agent_settled':break
        results=[event for event in events if event.get('type')=='tool_execution_end']
        assert [event['toolName'] for event in results]==['read','write','write','write','bash','bash','bash','bash','bash'],(events,errors)
        assert 'Static checks passed' in json.dumps(results[4]),results[4]
        assert not results[5].get('isError'),results[5]
        assert results[6].get('isError') and 'git repository' in json.dumps(results[6]),results[6]
        assert 'PROFILE_GIT_PUSHED' in json.dumps(results[7]),results[7]
        assert 'FACTORY PROFILE EDITING VERIFIED' in json.dumps(events),(events,errors)
        print('FACTORY PROFILE EDITING VERIFIED',flush=True)
    finally:
        process.terminate()
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':main()
