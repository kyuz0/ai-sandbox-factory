"""Factory's shipped UI adapters. External manifests never select Python code."""
from .aws import plugin as aws
from .ssh import plugin as ssh

ADAPTERS = {'ssh': ssh, 'aws': aws}
