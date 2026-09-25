"""HTTP smoke checks against an isolated running server (URL argument)."""
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from test_projects import ProjectTests

base = sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:8095'

def request(path,body=None,raw=None,headers=None):
    headers=dict(headers or {})
    if body is not None:
        raw=json.dumps(body).encode()
        headers['Content-Type']='application/json'
    return urllib.request.urlopen(urllib.request.Request(base+path,data=raw,headers=headers))

def call(path,body=None,**kwargs):
    return json.load(request(path,body,**kwargs))

def fail(path,body):
    try: call(path,body)
    except urllib.error.HTTPError as e:
        assert e.code==400
    else: raise AssertionError('Expected rejection: '+path)

def wait(project_id,field):
    for _ in range(200):
        p=call('/api/projects/'+project_id)
        if not p['busy']:
            assert p[field]['state']=='done',p[field]
            return p
        time.sleep(.1)
    raise AssertionError('Job timeout')

fixtures=ProjectTests()
fixtures.setUp()
try:
    projects=[call('/api/projects',{'name':n}) for n in ['API continuous project','API isolated project']]
    first,second=[p['id'] for p in projects]
    route='/api/projects/'+first
    paths=[]
    fixture_dir=Path('/tmp/motionmeasure-project-fixtures')
    fixture_dir.mkdir(exist_ok=True)
    for f in fixtures.files:
        data=Path(f['path']).read_bytes()
        path=fixture_dir/f['name']
        path.write_bytes(data)
        paths.append(str(path))
        call(route+'/upload?name='+f['name'],raw=data)
    call('/api/projects/'+second+'/upload?name=source0.mp4',raw=Path(paths[0]).read_bytes())
    a,b=[call('/api/projects/'+p) for p in [first,second]]
    assert len(a['files'])==2 and len(b['files'])==1
    assert a['files'][0]['id']!=b['files'][0]['id']
    assert 'path' not in a['files'][0]
    settings={'step':1,'min-area':80,'min-duration':.3,'min-frames':2,'gap':.2}
    fail(route+'/analyze', {'width': 0})
    fail(route+'/analyze', {'samples-per-second': -1})
    call(route+'/analyze',settings)
    fail(route+'/order',{'ids':[f['id'] for f in a['files']]})
    p=wait(first,'analysisStatus')
    assert p['result']['segment_count']==2
    assert len(p['result']['segments'][0]['source_ids'])==2
    draft={'analysis_id':p['analysis']['id'],'clips':[{'segment_id':s['id'],'start':s['start'],'end':s['end']} for s in p['result']['segments']],'audio':True,'boxes':False}
    call(route+'/draft',draft)
    assert call(route)['draft']==draft
    fail(route+'/export',{**draft,'analysis_id':'invalid'})
    fail(route+'/draft',{**draft,'clips':[{'segment_id':'event-1','start':-1,'end':2}]})
    call(route+'/export',draft)
    p=wait(first,'exportStatus')
    exported=f"/media/{first}/exports/{p['exportJob']}/highlights.mp4"
    response=request(exported,headers={'Range':'bytes=0-99'})
    assert response.status==206 and len(response.read())==100
    edit=call(f"/media/{first}/exports/{p['exportJob']}/edit.json")
    assert edit['clips']==draft['clips'] and edit['audio'] and not edit['boxes']
    assert not call('/api/projects/'+second)['result']
    call(route+'/order',{'ids':[f['id'] for f in reversed(p['files'])]})
    assert call(route)['stale']
    fail(route+'/export',draft)
    call(route+'/analyze',{**settings, 'width':160, 'samples-per-second':10})
    p=wait(first,'analysisStatus')
    assert p['result']['out_size']==[160,90]
    assert p['result']['out_fps']==10
    assert p['result']['effective_min_area']==20
    assert p['draft']['clips']==[] and len(p['exports'])==1
    Path('/tmp/motionmeasure-project-browser.json').write_text(json.dumps({'paths':paths,'first':first,'second':second}))
    print('PASS: isolated projects, uploads, continuous seam events, persistent selection, MP4 export, stale-analysis validation, reanalysis and retained exports')
finally:
    fixtures.tearDown()
