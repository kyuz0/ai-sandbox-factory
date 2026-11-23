"""Run the shared command-line tool contract for the default Pi profile."""
import runpy

runpy.run_path('/opt/cli-tools/check.py', run_name='__main__')
