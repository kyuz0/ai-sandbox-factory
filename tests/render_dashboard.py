"""Render dashboard timestamps and sorting at all supported review sizes."""
import asyncio,copy,os,sys,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from pi_sandbox_factory.sandbox_tui import Cockpit,DEFAULT_SETTINGS
from textual.widgets import Select,Input
from test_tui import FakeService
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
if '--mono' in sys.argv:os.environ['NO_COLOR']='1'
else:os.environ.pop('NO_COLOR',None)
os.environ['TEXTUAL_COLOR_SYSTEM']='truecolor'
async def main(size):
 with tempfile.TemporaryDirectory() as temp,patch('pi_sandbox_factory.sandbox_tui.load_defaults',side_effect=lambda:copy.deepcopy(DEFAULT_SETTINGS)),patch('pi_sandbox_factory.updates.available_update',return_value=None):
  records=[]
  for n,name in enumerate(['project-alpha','a-longer-project-name','legacy-project']):
   records.append(dict(name=name,profile='base',engine='podman',dev='running' if n==0 else 'exited',proxy='running',workspace='/home/example/projects/'+name,config={'network':'allowlist','local_model_ports':'8000'},activity={key:f'2026-11-{12+n}T12:30:00+00:00' if n<2 else None for key in ('created','edited','started')}))
  app=Cockpit(Path(temp),FakeService(records))
  async with app.run_test(size=size) as pilot:
   await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
   async def shot(name):
    await pilot.pause();(out/f'{name}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
   await shot('dashboard-dates')
   app.query_one('#sort-field',Select).focus();await pilot.press('enter');await shot('dashboard-sort');await pilot.press('escape')
   app.query_one('#search',Input).value='missing';await shot('dashboard-empty')
   app.query_one('#search',Input).value='';app.query_one('#home-error').update('Container engine unavailable; showing saved sandboxes.');await shot('dashboard-error')
for size in [(80,24),(120,40),(220,50)]:asyncio.run(main(size))
