"""Run as the protected LOOM administrator after reviewing the concrete proposal."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loom.control import Control
from loom.governance import Governance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--control', required=True)
    parser.add_argument('--proposal', required=True)
    parser.add_argument('--version', required=True, type=int)
    parser.add_argument('--expected-epoch', required=True, type=int)
    args = parser.parse_args()
    root = Path(args.control)
    if not root.is_absolute() or not (root/'runtime.sqlite').is_file():
        raise RuntimeError('Existing protected control root required')
    result = Governance(Control(root)).apply_policy(args.proposal, args.version, args.expected_epoch)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
