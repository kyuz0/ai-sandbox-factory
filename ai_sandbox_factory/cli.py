"""Application command and optional launcher notice."""
import os

PRODUCT_NAME = 'AI Sandbox Factory'
COMMAND = 'ai-sandbox-factory'


def main():
    from .sandbox import main as run
    run(startup_notice=os.environ.pop('AI_FACTORY_STARTUP_NOTICE', ''))
