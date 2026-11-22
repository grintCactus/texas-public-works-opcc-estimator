"""Private, machine-local configuration.

The owner's working copy holds a private test set: bid files that a company
supplied, which never train or fit anything.  The pipeline needs a few facts
about those files (which one has a site-level layout, researched bid dates,
extra labels that mark an engineer's-estimate column).  Those facts are
configuration, not code, and they are confidential, so they live in

    data/local_config.json      (never committed: data/* is git-ignored)

or in the file named by the BIDEST_LOCAL_CONFIG environment variable.
Without that file every value is empty and the pipeline runs on public data
alone: nothing in this repository names a company file.

Keys (all optional):
  site_level_files          [file name, ...]  top-level files that print
                            site x bidder totals and no line items
                            (parse_bidtabs.SITE_LEVEL_FILES)
  date_overrides_csv        name of a file under data/ with researched bid
                            dates for files that print none
                            (build_master._apply_local_date_overrides)
  engineer_label_patterns   [regex, ...]  extra alternatives that mark an
                            engineer's-estimate column
                            (parse_bidtabs.ENGINEER_RE)
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("BIDEST_LOCAL_CONFIG") or os.path.join(
    ROOT, "data", "local_config.json")

_CFG = None


def load(path=None):
    """The configuration as a dict; {} when the file does not exist."""
    p = path or PATH
    try:
        with open(p, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        return {}
    if not isinstance(cfg, dict):
        raise ValueError("%s must hold a JSON object" % p)
    return cfg


def get(key, default=None):
    """One value, or `default` when the key (or the whole file) is absent."""
    global _CFG
    if _CFG is None:
        _CFG = load()
    v = _CFG.get(key)
    return default if v is None else v


def present():
    """True when a local configuration file was found."""
    get("")
    return bool(_CFG)
