"""Verify the configured provider with two real synthetic requests; consumes API credit."""
import argparse
import json
import os
import sys
from closeready.llm import ProviderError
from closeready.provider_check import check_provider
from closeready.provider_factory import provider_from_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', help='Override the configured model for this check only.')
    args = parser.parse_args()
    os.environ.setdefault('CLOSEREADY_LLM_ENV_FILE', '.env')
    if args.model:
        os.environ['LLM_MODEL'] = args.model
    try:
        result = check_provider(provider_from_environment())
    except (ProviderError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
