"""Publish a tested CalVer release, or resume that run's existing release."""
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess

PACKAGE = ('ai_sandbox_factory/', 'pyproject.toml', 'MANIFEST.in')


def prepare(root, run_id, output, *, now=None):
    root=Path(root)
    def git(*args,check=True):
        return subprocess.run(['git',*map(str,args)],cwd=root,check=check,text=True,capture_output=True)
    def revision(ref):
        return git('rev-parse',ref).stdout.strip()
    tested=revision('HEAD')
    git('config','user.name','github-actions[bot]')
    git('config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
    git('fetch','origin','main')
    previous=git('log','FETCH_HEAD','--format=%H','--grep',f'^Factory-Release-Run: {run_id}$').stdout.splitlines()
    if len(previous)>1:
        raise ValueError(f'Run {run_id} has multiple release commits; inspect them before retrying')
    if previous:
        release=previous[0]
        document=git('show',release+':pyproject.toml').stdout
        version=re.search(r'^version = "([0-9.]+)"$',document,re.M).group(1)
        tagged=git('ls-remote','origin','refs/tags/v'+version).stdout.split()
        if not tagged or tagged[0]!=release:
            raise ValueError(f'Release v{version} for run {run_id} has no matching remote tag')
        before=git('show',tested+':pyproject.toml').stdout
        normalize=lambda text:re.sub(r'^version = "[^"]+"$','version = "<release>"',text,flags=re.M)
        difference=git('diff','--quiet',tested,release,'--','ai_sandbox_factory/','MANIFEST.in',check=False)
        if difference.returncode or normalize(before)!=normalize(document):
            raise ValueError(f'Cannot resume v{version}: its package differs from this run’s tested source')
        git('checkout','--detach',release)
        with open(output,'a') as file:file.write('version=v'+version+'\n')
        print(f'Resuming retained-image/evidence publication for v{version}')
        return 'v'+version
    while True:
        latest=revision('FETCH_HEAD')
        git('merge-base','--is-ancestor',tested,latest)
        difference=git('diff','--quiet',tested,latest,'--',*PACKAGE,check=False)
        if difference.returncode==1:
            print('Main has newer package changes. Its own run will release them.')
            return None
        difference.check_returncode()
        git('checkout','--detach',latest)
        time=now or datetime.now(timezone.utc)
        version=f'{time.year}.{time.month}.{time.day}.{int(time.strftime("%H%M%S"))}'
        path=root/'pyproject.toml'
        path.write_text(re.sub(r'^version = "[^"]+"$',f'version = "{version}"',path.read_text(),flags=re.M))
        git('add','pyproject.toml')
        git('commit','-m',f'Release {version}\n\nFactory-Release-Run: {run_id}')
        pushed=git('push','--atomic','origin','HEAD:main',f'HEAD:refs/tags/v{version}',check=False)
        if pushed.returncode==0:
            with open(output,'a') as file:file.write('version=v'+version+'\n')
            return 'v'+version
        git('fetch','origin','main')
        if latest==revision('FETCH_HEAD'):
            raise ValueError(f'Cannot publish tested release v{version}: {pushed.stderr}')
        # Retry only an actual main-branch race. Source changes on the next
        # iteration defer to their own checked release; no failed push is hidden.


if __name__=='__main__':
    prepare(Path.cwd(),os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_OUTPUT'])
